"""Stage 3 — semantic evaluation of anonymized resumes with the Claude API.

- The JD + rubric live in the system prompt with a cache_control breakpoint,
  so N candidate calls reuse the cached prefix.
- Structured outputs (output_config.format json_schema) guarantee the
  response parses into our Evaluation shape.
- The SDK retries 429/5xx automatically; we add one outer retry for
  longer rate-limit waits.
"""

from __future__ import annotations

import json
import os
import time

import anthropic

from screener.models import Evaluation, JobDescription, Resume, compute_overall

DEFAULT_MODEL = "claude-sonnet-5"

_EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_match": {
            "type": "integer",
            "description": "0-100: how well the candidate's skills cover the required skills",
        },
        "experience_relevance": {
            "type": "integer",
            "description": "0-100: relevance and depth of work experience for this role",
        },
        "project_impact": {
            "type": "integer",
            "description": "0-100: concrete, measurable impact of projects described",
        },
        "justification": {
            "type": "string",
            "description": "3-6 sentences citing specific evidence from the resume against specific JD requirements. Refer to the candidate only as 'the candidate'.",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "JD requirements the resume shows no evidence for",
        },
        "interview_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exactly 4 tailored technical questions probing the candidate's claimed experience",
        },
    },
    "required": [
        "skill_match",
        "experience_relevance",
        "project_impact",
        "justification",
        "gaps",
        "interview_questions",
    ],
    "additionalProperties": False,
}

_SYSTEM_INSTRUCTIONS = """You are an expert technical recruiter performing a blind resume screen.

You will receive an ANONYMIZED resume (names, locations, universities, and contact details replaced with placeholders like [CANDIDATE], [LOCATION], [UNIVERSITY]). Evaluate ONLY the substance: skills, depth of experience, and project impact against the job description below.

Scoring rules:
- Judge evidence, not claims: a skill listed with a concrete project or outcome scores higher than a bare keyword.
- Ignore placeholders entirely; never speculate about the candidate's identity, background, or demographics.
- Prestige signals (employer or school reputation) must NOT affect scores - only what the candidate actually did.
- Be discriminating: reserve 85+ for candidates with strong evidence on nearly every requirement; use the full 0-100 range.
- Seniority Mismatch Rule: Check the required seniority (e.g., Senior, Lead) against the candidate's actual titles. If a junior or entry-level candidate applies for a senior architecture role, their Experience Relevance score must be capped at a maximum of 15/100, regardless of the keywords they list.
- Interview questions must probe specific claims in THIS resume, not generic trivia.

JOB DESCRIPTION:
"""


def build_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)  # reads ANTHROPIC_API_KEY / auth profile


def has_api_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def evaluate_resume(
    client: anthropic.Anthropic,
    jd: JobDescription,
    resume: Resume,
    model: str = DEFAULT_MODEL,
) -> Evaluation:
    system_blocks = [
        {
            "type": "text",
            "text": _SYSTEM_INSTRUCTIONS + jd.to_prompt_text(),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_blocks,
                output_config={"format": {"type": "json_schema", "schema": _EVALUATION_SCHEMA}},
                messages=[
                    {
                        "role": "user",
                        "content": "Evaluate this anonymized resume:\n\n" + resume.anonymized_text,
                    }
                ],
            )
            if response.stop_reason == "refusal":
                return Evaluation(0, 0, 0, 0, "", error="Model declined to evaluate this resume")
            data = json.loads(response.content[0].text)
            skill_match = int(data["skill_match"])
            experience_relevance = int(data["experience_relevance"])
            project_impact = int(data["project_impact"])
            return Evaluation(
                skill_match=skill_match,
                experience_relevance=experience_relevance,
                project_impact=project_impact,
                overall=compute_overall(skill_match, experience_relevance, project_impact),
                justification=data["justification"],
                gaps=list(data.get("gaps", [])),
                interview_questions=list(data.get("interview_questions", [])),
            )
        except anthropic.RateLimitError:
            if attempt == 0:
                time.sleep(30)  # SDK backoff exhausted; one longer wait
                continue
            return Evaluation(0, 0, 0, 0, "", error="Rate limited after retries")
        except anthropic.APIStatusError as exc:
            return Evaluation(0, 0, 0, 0, "", error=f"API error {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError:
            return Evaluation(0, 0, 0, 0, "", error="Network error reaching the Claude API")
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            return Evaluation(0, 0, 0, 0, "", error=f"Unexpected response shape: {exc}")
    return Evaluation(0, 0, 0, 0, "", error="Unreachable")

"""Free, local semantic evaluation backend using Ollama (no API key, no cost).

Requires a running Ollama server (https://ollama.com) with an instruction
model pulled, e.g.:  ollama pull llama3.2
Same output contract as screener.evaluate (returns an Evaluation), so the
CLI can switch backends with --backend ollama.
"""

from __future__ import annotations

import json
import os
import time

import requests

from screener.models import Evaluation, JobDescription, Resume, compute_overall

DEFAULT_MODEL = "llama3.2"
# Overridable via OLLAMA_BASE_URL — required when running in a container,
# where "localhost" resolves to the container itself, not the host machine
# actually running Ollama (see docker-compose.yml's host.docker.internal setup).
DEFAULT_HOST = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

_EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_match": {"type": "integer"},
        "experience_relevance": {"type": "integer"},
        "project_impact": {"type": "integer"},
        "justification": {"type": "string"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "interview_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "skill_match",
        "experience_relevance",
        "project_impact",
        "justification",
        "gaps",
        "interview_questions",
    ],
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

Respond with JSON only, matching this shape:
{"skill_match": 0-100, "experience_relevance": 0-100, "project_impact": 0-100,
 "justification": "3-6 sentences citing specific resume evidence against specific JD requirements",
 "gaps": ["JD requirements with no evidence in the resume"],
 "interview_questions": ["exactly 4 tailored technical questions probing claimed experience"]}

JOB DESCRIPTION:
"""


def has_ollama(host: str = DEFAULT_HOST, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{host}/api/tags", timeout=3)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        if attempt < retries:
            time.sleep(1)
    return False


def evaluate_resume(
    jd: JobDescription,
    resume: Resume,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
) -> Evaluation:
    system_prompt = _SYSTEM_INSTRUCTIONS + jd.to_prompt_text()
    try:
        resp = requests.post(
            f"{host}/api/chat",
            json={
                "model": model,
                "stream": False,
                "format": _EVALUATION_SCHEMA,
                "options": {"temperature": 0.0, "seed": 42},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": "Evaluate this anonymized resume:\n\n" + resume.anonymized_text,
                    },
                ],
            },
            timeout=300,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        data = json.loads(content)
        skill_match = int(data["skill_match"])
        experience_relevance = int(data["experience_relevance"])
        project_impact = int(data["project_impact"])
        return Evaluation(
            skill_match=skill_match,
            experience_relevance=experience_relevance,
            project_impact=project_impact,
            overall=compute_overall(skill_match, experience_relevance, project_impact),
            justification=str(data["justification"]),
            gaps=[str(g) for g in data.get("gaps", [])],
            interview_questions=[str(q) for q in data.get("interview_questions", [])],
        )
    except requests.ConnectionError:
        return Evaluation(
            0,
            0,
            0,
            0,
            "",
            error=(
                f"Could not reach Ollama at {host}. Start it with 'ollama serve' "
                f"(and 'ollama pull {model}' if the model isn't downloaded)."
            ),
        )
    except requests.Timeout:
        return Evaluation(0, 0, 0, 0, "", error="Ollama request timed out")
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return Evaluation(0, 0, 0, 0, "", error=f"Unexpected response shape: {exc}")
    except requests.HTTPError as exc:
        return Evaluation(0, 0, 0, 0, "", error=f"Ollama error: {exc}")

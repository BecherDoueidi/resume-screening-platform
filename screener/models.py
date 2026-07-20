"""Data models shared across the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class JobDescription:
    title: str
    summary: str = ""
    required_skills: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    min_years_experience: Optional[int] = None
    responsibilities: list[str] = field(default_factory=list)
    raw_text: str = ""  # populated when loaded from a plain .txt file

    def to_prompt_text(self) -> str:
        if self.raw_text:
            return self.raw_text
        lines = [f"Job Title: {self.title}", "", self.summary, ""]
        if self.required_skills:
            lines.append("Required skills:")
            lines += [f"- {s}" for s in self.required_skills]
            lines.append("")
        if self.nice_to_have:
            lines.append("Nice to have:")
            lines += [f"- {s}" for s in self.nice_to_have]
            lines.append("")
        if self.min_years_experience is not None:
            lines.append(f"Minimum years of relevant experience: {self.min_years_experience}")
        if self.responsibilities:
            lines.append("Responsibilities:")
            lines += [f"- {r}" for r in self.responsibilities]
        return "\n".join(lines)


@dataclass
class RedactionRecord:
    """One class of redaction applied to a resume (values are hashed, never stored)."""

    kind: str  # e.g. "person_name", "email", "location"
    replacement: str  # e.g. "[CANDIDATE]"
    count: int
    sample_hashes: list[str] = field(default_factory=list)  # sha256[:10] of originals


@dataclass
class Resume:
    source_path: str
    candidate_id: str  # anonymous label, e.g. "Candidate 03"
    raw_text: str = ""
    anonymized_text: str = ""
    redactions: list[RedactionRecord] = field(default_factory=list)
    parse_error: str = ""  # non-empty when the PDF had no extractable text


# Weights for computing `overall` from the sub-scores (see compute_overall).
# Skill match and experience relevance carry the most signal for role fit;
# project impact is weighted slightly lower since not every strong candidate
# has quantifiable project outcomes on their resume.
OVERALL_WEIGHTS = {"skill_match": 0.35, "experience_relevance": 0.35, "project_impact": 0.30}


def compute_overall(skill_match: int, experience_relevance: int, project_impact: int) -> int:
    """Deterministic weighted average — `overall` is never the model's own guess."""
    weighted = (
        skill_match * OVERALL_WEIGHTS["skill_match"]
        + experience_relevance * OVERALL_WEIGHTS["experience_relevance"]
        + project_impact * OVERALL_WEIGHTS["project_impact"]
    )
    return round(weighted)


@dataclass
class Evaluation:
    skill_match: int
    experience_relevance: int
    project_impact: int
    overall: int
    justification: str
    gaps: list[str] = field(default_factory=list)
    interview_questions: list[str] = field(default_factory=list)
    error: str = ""  # non-empty when the API call failed


@dataclass
class CandidateResult:
    resume: Resume
    evaluation: Optional[Evaluation] = None

    def to_dict(self) -> dict:
        d = {
            "candidate_id": self.resume.candidate_id,
            "source_file": self.resume.source_path,
            "parse_error": self.resume.parse_error,
            "redactions": [asdict(r) for r in self.resume.redactions],
            "evaluation": asdict(self.evaluation) if self.evaluation else None,
        }
        return d

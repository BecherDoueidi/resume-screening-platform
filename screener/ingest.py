"""Stage 1 — load the job description and extract text from resume PDFs."""

from __future__ import annotations

import json
from pathlib import Path

import pdfplumber

from screener.models import JobDescription, Resume


def load_job_description(path: str | Path) -> JobDescription:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return JobDescription(
            title=data.get("title", "Untitled role"),
            summary=data.get("summary", ""),
            required_skills=data.get("required_skills", []),
            nice_to_have=data.get("nice_to_have", []),
            min_years_experience=data.get("min_years_experience"),
            responsibilities=data.get("responsibilities", []),
        )
    # Plain text JD: pass through verbatim.
    text = path.read_text(encoding="utf-8")
    first_line = text.strip().splitlines()[0] if text.strip() else "Untitled role"
    return JobDescription(title=first_line[:120], raw_text=text)


def extract_pdf_text(path: Path) -> str:
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def load_resumes(directory: str | Path, limit: int | None = None) -> list[Resume]:
    directory = Path(directory)
    pdfs = sorted(directory.rglob("*.pdf"))
    if limit is not None:
        pdfs = pdfs[:limit]
    resumes: list[Resume] = []
    for i, pdf_path in enumerate(pdfs, start=1):
        resume = Resume(source_path=str(pdf_path), candidate_id=f"Candidate {i:02d}")
        try:
            resume.raw_text = extract_pdf_text(pdf_path)
            if not resume.raw_text:
                resume.parse_error = "No extractable text (likely an image-only scan)"
        except Exception as exc:  # corrupt or password-protected PDFs
            resume.parse_error = f"Failed to parse PDF: {exc}"
        resumes.append(resume)
    return resumes

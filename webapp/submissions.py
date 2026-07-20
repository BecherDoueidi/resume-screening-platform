"""Shared "accept a resume upload and queue it" logic.

Used by both the public HTML flow (webapp/app.py's /apply) and the JSON API
(webapp/api.py's POST /api/resumes) — one code path, so both stay consistent
and a fix/change only has to happen once.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from werkzeug.datastructures import FileStorage

from webapp import store
from webapp.db import new_session
from webapp.jobs import DEFAULT_RETRY, get_queue
from webapp.tasks import process_resume

logger = logging.getLogger(__name__)


class SubmissionError(ValueError):
    """Raised on bad input (missing/non-PDF file, empty name) — callers turn
    this into whatever error response shape fits their route (HTML vs JSON)."""


def submit_resume(
    *,
    file: FileStorage | None,
    applicant_name: str,
    job_position_id: int,
    upload_dir: Path,
) -> dict:
    """Validates, saves the file, persists Applicant+Resume+ProcessingJob rows,
    and enqueues the background evaluation job. Returns identifiers the caller
    needs to build its response (HTML redirect or JSON body)."""
    if not file or file.filename == "":
        logger.warning("apply_rejected", extra={"reason": "missing_file"})
        raise SubmissionError("Please choose a PDF file.")
    if not file.filename.lower().endswith(".pdf"):
        logger.warning("apply_rejected", extra={"reason": "non_pdf_extension", "uploaded_filename": file.filename})
        raise SubmissionError("Only PDF resumes are accepted.")

    name = (applicant_name or "").strip()[:200] or "Applicant"
    saved_path = upload_dir / f"{uuid.uuid4().hex}.pdf"
    file.save(saved_path)

    # Nothing is parsed/evaluated here — just persist the upload and hand it
    # to the queue. The worker (webapp/tasks.py) does the actual pipeline run.
    with new_session() as db:
        db_resume, db_job = store.create_submission(
            db,
            job_position_id=job_position_id,
            applicant_name=name,
            original_filename=file.filename,
            storage_path=str(saved_path),
        )
        public_id, resume_id, job_id = db_resume.public_id, db_resume.id, db_job.id

    try:
        rq_job = get_queue().enqueue(process_resume, resume_id, job_id, retry=DEFAULT_RETRY, job_timeout="10m")
    except Exception as exc:
        # The Resume/ProcessingJob rows above are already committed — if we
        # don't mark them failed here, they're stuck at "pending" forever
        # with no evaluation and no visible error (e.g. Redis unreachable).
        logger.error(
            "enqueue_failed", extra={"public_id": public_id, "resume_id": resume_id, "job_id": job_id}, exc_info=True
        )
        with new_session() as db:
            store.fail_resume(db, job_id, error=str(exc), retryable=False)
        raise SubmissionError("Could not queue your resume for processing. Please try again shortly.") from exc

    with new_session() as db:
        store.attach_rq_job_id(db, job_id, rq_job.id)

    logger.info(
        "resume_submitted",
        extra={"public_id": public_id, "resume_id": resume_id, "job_id": job_id, "rq_job_id": rq_job.id},
    )

    return {
        "public_id": public_id,
        "resume_id": resume_id,
        "job_id": job_id,
        "rq_job_id": rq_job.id,
        "applicant_name": name,
    }

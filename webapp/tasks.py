"""Background worker task: the actual resume-processing pipeline.

Runs inside an `rq worker` process (see webapp/worker.py), never inside the
Flask request. Everything the old synchronous apply() route did — parse,
anonymize, evaluate, render the card — happens here instead, against its own
DB session (sessions can't cross process boundaries).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rq import get_current_job  # noqa: E402

from screener import anonymize, evaluate, evaluate_ollama, ingest, report  # noqa: E402
from screener.models import CandidateResult, JobDescription  # noqa: E402
from screener.models import Resume as PipelineResume  # noqa: E402
from webapp import store  # noqa: E402
from webapp.db import new_session  # noqa: E402
from webapp.logging_config import bind_processing_id, configure_logging  # noqa: E402
from webapp.models_db import Resume as ResumeRow  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

_JD_CACHE: JobDescription | None = None


def _load_jd() -> JobDescription:
    global _JD_CACHE
    if _JD_CACHE is None:
        _JD_CACHE = ingest.load_job_description(ROOT / "data" / "job_description.json")
    return _JD_CACHE


def _pick_backend() -> tuple[str, str]:
    if evaluate.has_api_credentials():
        return "claude", "Claude API key detected"
    if evaluate_ollama.has_ollama():
        return "ollama", "free local Ollama model"
    return "none", "no evaluation engine available"


def _retries_left() -> int | None:
    job = get_current_job()
    return job.retries_left if job else None


def process_resume(resume_db_id: int, job_db_id: int) -> None:
    """Entry point enqueued by webapp/app.py. Retries (see webapp/jobs.py's
    DEFAULT_RETRY) re-invoke this same function from the top — it must be
    safe to re-run, which it is: it always re-derives everything from the
    stored PDF rather than assuming partial progress survived."""
    bind_processing_id(f"job-{job_db_id}")
    pipeline_started = time.perf_counter()

    with new_session() as db:
        store.start_job(db, job_db_id)
        db_resume = db.get(ResumeRow, resume_db_id)
        storage_path = db_resume.storage_path
        applicant_name = db_resume.applicant.full_name

    logger.info("processing_started", extra={"resume_id": resume_db_id, "job_id": job_db_id})

    try:
        pipeline_resume = PipelineResume(source_path=storage_path, candidate_id=applicant_name)

        with new_session() as db:
            store.update_progress(db, job_db_id, progress=15, message="Reading PDF")
        try:
            pipeline_resume.raw_text = ingest.extract_pdf_text(Path(storage_path))
            if not pipeline_resume.raw_text:
                pipeline_resume.parse_error = "No extractable text (likely an image-only scan)"
        except Exception as exc:  # corrupt PDF — not retryable, retrying won't fix the file
            pipeline_resume.parse_error = f"Failed to parse PDF: {exc}"

        if pipeline_resume.parse_error:
            logger.warning(
                "parsing_failed",
                extra={"resume_id": resume_db_id, "job_id": job_db_id, "error": pipeline_resume.parse_error},
            )
            with new_session() as db:
                store.fail_resume(db, job_db_id, error=pipeline_resume.parse_error, retryable=False)
            return

        with new_session() as db:
            store.update_progress(db, job_db_id, progress=35, message="Anonymizing")
        anonymize.anonymize_resume(pipeline_resume)
        with new_session() as db:
            store.save_anonymized_text(db, resume_db_id, pipeline_resume.anonymized_text)
            if pipeline_resume.redactions:
                store.save_redactions(db, resume_db_id, pipeline_resume.redactions)

        backend, _reason = _pick_backend()
        result = CandidateResult(resume=pipeline_resume)

        with new_session() as db:
            store.update_progress(db, job_db_id, progress=60, message=f"Evaluating via {backend}")

        jd = _load_jd()
        ai_started = time.perf_counter()
        if backend == "claude":
            client = evaluate.build_client()
            result.evaluation = evaluate.evaluate_resume(client, jd, pipeline_resume)
        elif backend == "ollama":
            result.evaluation = evaluate_ollama.evaluate_resume(jd, pipeline_resume)
        ai_duration_ms = round((time.perf_counter() - ai_started) * 1000, 1)

        if result.evaluation and result.evaluation.error:
            logger.error(
                "evaluation_failed",
                extra={
                    "resume_id": resume_db_id,
                    "job_id": job_db_id,
                    "backend": backend,
                    "ai_duration_ms": ai_duration_ms,
                    "error": result.evaluation.error,
                },
            )
        else:
            logger.info(
                "ai_response_time",
                extra={
                    "resume_id": resume_db_id,
                    "job_id": job_db_id,
                    "backend": backend,
                    "ai_duration_ms": ai_duration_ms,
                },
            )

        card_doc = None
        if result.evaluation and not result.evaluation.error:
            card_html = report.candidate_card_html(1, result)
            card_doc = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<style>{report.CSS}"
                "body{padding:0;background:transparent;}</style></head>"
                f"<body>{card_html}</body></html>"
            )

        with new_session() as db:
            store.update_progress(db, job_db_id, progress=90, message="Saving results")
            store.save_evaluation(
                db, resume_db_id, job_db_id, backend=backend, evaluation=result.evaluation, card_html=card_doc
            )

        total_duration_ms = round((time.perf_counter() - pipeline_started) * 1000, 1)
        logger.info(
            "processing_completed",
            extra={
                "resume_id": resume_db_id,
                "job_id": job_db_id,
                "backend": backend,
                "duration_ms": total_duration_ms,
                "success": bool(result.evaluation and not result.evaluation.error),
            },
        )

    except Exception as exc:  # unexpected failure (DB down, code bug, uncaught network error, ...)
        retries_left = _retries_left()
        retryable = bool(retries_left)
        total_duration_ms = round((time.perf_counter() - pipeline_started) * 1000, 1)
        logger.error(
            "processing_failed",
            extra={
                "resume_id": resume_db_id,
                "job_id": job_db_id,
                "duration_ms": total_duration_ms,
                "retryable": retryable,
                "error": str(exc),
            },
            exc_info=True,
        )
        with new_session() as db:
            store.fail_resume(db, job_db_id, error=str(exc), retryable=retryable)
        raise  # let RQ's Retry policy (or final-failure bookkeeping) take over

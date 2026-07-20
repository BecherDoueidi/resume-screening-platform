"""Repository layer over the Postgres-backed schema (webapp/models_db.py).

Replaces the old JSON-file store. Callers pass a SQLAlchemy Session (see
webapp/db.py) instead of a filesystem root — nothing here touches disk.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from screener.models import Evaluation as EvaluationResult
from screener.models import JobDescription, RedactionRecord
from webapp.models_db import (
    Applicant,
    AuditLog,
    Evaluation,
    JobPosition,
    ProcessingJob,
    Resume,
    User,
)


def get_or_create_job_position(session: Session, jd: JobDescription) -> JobPosition:
    """Idempotent by title — the webapp currently loads one JD at startup."""
    existing = session.scalar(select(JobPosition).where(JobPosition.title == jd.title))
    if existing:
        return existing
    job = JobPosition(
        title=jd.title,
        summary=jd.summary,
        required_skills=list(jd.required_skills),
        nice_to_have=list(jd.nice_to_have),
        min_years_experience=jd.min_years_experience,
        responsibilities=list(jd.responsibilities),
        raw_text=jd.raw_text,
    )
    session.add(job)
    session.commit()
    return job


def create_submission(
    session: Session,
    *,
    job_position_id: int,
    applicant_name: str,
    original_filename: str,
    storage_path: str,
) -> tuple[Resume, ProcessingJob]:
    """Creates the Applicant + Resume + a pending ProcessingJob for one upload.

    Nothing is parsed, anonymized, or evaluated here — that all happens in the
    worker (webapp/tasks.py) once this job is picked up off the queue. This
    function only needs to be fast enough to return an immediate response.
    """
    applicant = Applicant(full_name=applicant_name)
    session.add(applicant)
    session.flush()  # populate applicant.id without a full commit

    resume = Resume(
        applicant_id=applicant.id,
        job_position_id=job_position_id,
        original_filename=original_filename,
        storage_path=storage_path,
        status="pending",
    )
    session.add(resume)
    session.flush()

    job = ProcessingJob(resume_id=resume.id, status="pending", progress=0, progress_message="Queued")
    session.add(job)
    session.commit()
    return resume, job


def attach_rq_job_id(session: Session, job_id: int, rq_job_id: str) -> None:
    job = session.get(ProcessingJob, job_id)
    job.rq_job_id = rq_job_id
    session.commit()


def start_job(session: Session, job_id: int) -> None:
    job = session.get(ProcessingJob, job_id)
    job.status = "processing"
    job.attempts += 1
    job.started_at = datetime.now(timezone.utc)
    job.progress = 5
    job.progress_message = "Starting"
    resume = session.get(Resume, job.resume_id)
    resume.status = "processing"
    session.commit()


def update_progress(session: Session, job_id: int, *, progress: int, message: str) -> None:
    job = session.get(ProcessingJob, job_id)
    job.progress = progress
    job.progress_message = message
    session.commit()


def save_anonymized_text(session: Session, resume_id: int, anonymized_text: str) -> None:
    resume = session.get(Resume, resume_id)
    resume.anonymized_text = anonymized_text
    session.commit()


def fail_resume(session: Session, job_id: int, *, error: str, retryable: bool) -> None:
    """Called on any processing failure (parse error, model error, unexpected exception).

    `retryable=True` leaves status as "pending" so a re-enqueue can pick it up
    again; the caller decides retryable-ness from RQ's own retries-left count.
    A definitive (non-retryable) failure marks both the job and the resume
    failed so the UI stops polling and shows the error.
    """
    job = session.get(ProcessingJob, job_id)
    job.error = error
    if retryable:
        job.status = "pending"
        job.progress_message = f"Retrying after error: {error}"
    else:
        job.status = "failed"
        job.progress = 100
        job.progress_message = "Failed"
        job.finished_at = datetime.now(timezone.utc)
        resume = session.get(Resume, job.resume_id)
        resume.status = "failed"
        resume.parse_error = resume.parse_error or error
    session.commit()


def save_redactions(session: Session, resume_id: int, redactions: list[RedactionRecord]) -> None:
    for r in redactions:
        session.add(
            AuditLog(
                resume_id=resume_id,
                actor="system",
                action="anonymize",
                details=asdict(r),
            )
        )
    session.commit()


def save_evaluation(
    session: Session,
    resume_id: int,
    job_id: int,
    *,
    backend: str,
    evaluation: EvaluationResult | None,
    card_html: str | None,
) -> None:
    ev = Evaluation(
        resume_id=resume_id,
        backend=backend,
        skill_match=evaluation.skill_match if evaluation and not evaluation.error else None,
        experience_relevance=evaluation.experience_relevance if evaluation and not evaluation.error else None,
        project_impact=evaluation.project_impact if evaluation and not evaluation.error else None,
        overall=evaluation.overall if (evaluation and not evaluation.error) else None,
        justification=evaluation.justification if evaluation else "",
        gaps=list(evaluation.gaps) if evaluation else [],
        interview_questions=list(evaluation.interview_questions) if evaluation else [],
        error=evaluation.error if evaluation else "",
        card_html=card_html or "",
    )
    session.add(ev)

    ok = bool(evaluation and not evaluation.error)
    resume = session.get(Resume, resume_id)
    resume.status = "evaluated" if ok else "failed"

    job = session.get(ProcessingJob, job_id)
    job.status = "completed" if ok else "failed"
    job.backend = backend
    job.error = evaluation.error if evaluation else ""
    job.progress = 100
    job.progress_message = "Completed" if ok else "Failed"
    job.finished_at = datetime.now(timezone.utc)

    session.commit()


def log_action(session: Session, *, actor: str, action: str, details: dict | None = None) -> None:
    session.add(AuditLog(actor=actor, action=action, details=details or {}))
    session.commit()


def _latest_job(r: Resume) -> ProcessingJob | None:
    return max(r.processing_jobs, key=lambda j: j.id) if r.processing_jobs else None


def load_applications_view(session: Session) -> list[SimpleNamespace]:
    """Shaped to match exactly what admin_dashboard.html already expects, plus
    job_status/progress/progress_message for the new status badge."""
    resumes = session.scalars(select(Resume).order_by(Resume.created_at.desc())).all()
    out = []
    for r in resumes:
        ev = r.evaluation
        job = _latest_job(r)
        out.append(
            SimpleNamespace(
                id=r.public_id,
                submitted_at=r.created_at.isoformat(timespec="seconds"),
                name=r.applicant.full_name,
                filename=r.original_filename,
                backend=ev.backend if ev else "none",
                parse_error=r.parse_error,
                overall=ev.overall if ev else None,
                eval_error=ev.error if ev else None,
                card_doc=ev.card_html if ev else None,
                job_status=job.status if job else "pending",
                job_progress=job.progress if job else 0,
                job_message=job.progress_message if job else "Queued",
            )
        )
    return out


def get_status_view(session: Session, public_id: str) -> SimpleNamespace | None:
    """Backs the polling endpoint (GET /status/<public_id>)."""
    resume = session.scalar(select(Resume).where(Resume.public_id == public_id))
    if not resume:
        return None
    job = _latest_job(resume)
    ev = resume.evaluation
    return SimpleNamespace(
        status=job.status if job else "pending",
        progress=job.progress if job else 0,
        message=job.progress_message if job else "Queued",
        error=job.error if job else "",
        parse_error=resume.parse_error,
        overall=ev.overall if ev else None,
        eval_error=ev.error if ev else None,
        card_doc=ev.card_html if ev else None,
    )


def clear_applications(session: Session) -> None:
    for resume in session.scalars(select(Resume)).all():
        session.delete(resume)
    session.commit()


def delete_application(session: Session, public_id: str) -> None:
    resume = session.scalar(select(Resume).where(Resume.public_id == public_id))
    if resume:
        session.delete(resume)
        session.commit()


# --- Recruiter dashboard: overview, candidate list, job management ---------

RECRUITER_STATUSES = ("new", "shortlisted", "interview", "rejected")
SORT_OPTIONS = {
    "date_desc": ("created_at", True),
    "date_asc": ("created_at", False),
    "score_desc": ("overall", True),
    "score_asc": ("overall", False),
    "name_asc": ("name", False),
    "name_desc": ("name", True),
}


def get_overview_stats(session: Session) -> SimpleNamespace:
    total_applicants = session.scalar(select(func.count(Applicant.id))) or 0
    active_jobs = session.scalar(select(func.count(JobPosition.id)).where(JobPosition.status == "active")) or 0
    completed_evaluations = session.scalar(select(func.count(Evaluation.id)).where(Evaluation.error == "")) or 0
    processing_queue = (
        session.scalar(select(func.count(ProcessingJob.id)).where(ProcessingJob.status.in_(["pending", "processing"])))
        or 0
    )
    return SimpleNamespace(
        total_applicants=total_applicants,
        active_jobs=active_jobs,
        completed_evaluations=completed_evaluations,
        processing_queue=processing_queue,
    )


def _candidate_view(r: Resume) -> SimpleNamespace:
    ev = r.evaluation
    job = _latest_job(r)
    redaction_count = sum(1 for a in r.audit_logs if a.action == "anonymize")
    return SimpleNamespace(
        id=r.public_id,
        submitted_at=r.created_at.isoformat(timespec="seconds"),
        name=r.applicant.full_name,
        filename=r.original_filename,
        job_title=r.job_position.title,
        backend=ev.backend if ev else "none",
        parse_error=r.parse_error,
        overall=ev.overall if ev else None,
        skill_match=ev.skill_match if ev else None,
        experience_relevance=ev.experience_relevance if ev else None,
        project_impact=ev.project_impact if ev else None,
        justification=ev.justification if ev else "",
        gaps=ev.gaps if ev else [],
        interview_questions=ev.interview_questions if ev else [],
        eval_error=ev.error if ev else None,
        card_doc=ev.card_html if ev else None,
        job_status=job.status if job else "pending",
        job_progress=job.progress if job else 0,
        job_message=job.progress_message if job else "Queued",
        recruiter_status=r.recruiter_status,
        redaction_count=redaction_count,
        has_anonymized_text=bool(r.anonymized_text),
    )


def list_candidates(
    session: Session,
    *,
    search: str = "",
    job_position_id: int | None = None,
    recruiter_status: str | None = None,
    eval_status: str | None = None,
    sort: str = "date_desc",
) -> list[SimpleNamespace]:
    """Search + filter + sort over resumes, for the recruiter dashboard and export."""
    query = select(Resume).join(Applicant).join(JobPosition)

    if search:
        like = f"%{search.lower()}%"
        query = query.where(
            or_(func.lower(Applicant.full_name).like(like), func.lower(Resume.original_filename).like(like))
        )
    if job_position_id:
        query = query.where(Resume.job_position_id == job_position_id)
    if recruiter_status:
        query = query.where(Resume.recruiter_status == recruiter_status)
    if eval_status:
        query = query.where(Resume.status == eval_status)

    resumes = session.scalars(query).all()
    views = [_candidate_view(r) for r in resumes]

    sort_field, reverse = SORT_OPTIONS.get(sort, SORT_OPTIONS["date_desc"])
    if sort_field == "overall":
        views.sort(key=lambda v: (v.overall is None, v.overall or 0), reverse=reverse)
    elif sort_field == "name":
        views.sort(key=lambda v: v.name.lower(), reverse=reverse)
    else:  # created_at — submitted_at is already an ISO string, sorts correctly as text
        views.sort(key=lambda v: v.submitted_at, reverse=reverse)
    return views


def get_candidate_detail(session: Session, public_id: str) -> SimpleNamespace | None:
    resume = session.scalar(select(Resume).where(Resume.public_id == public_id))
    if not resume:
        return None
    view = _candidate_view(resume)
    view.anonymized_text = resume.anonymized_text
    view.storage_path = resume.storage_path
    view.redactions = [
        {"kind": a.details.get("kind"), "replacement": a.details.get("replacement"), "count": a.details.get("count")}
        for a in resume.audit_logs
        if a.action == "anonymize"
    ]
    return view


def set_recruiter_status(session: Session, public_id: str, status: str) -> bool:
    if status not in RECRUITER_STATUSES:
        raise ValueError(f"invalid recruiter status: {status!r}")
    resume = session.scalar(select(Resume).where(Resume.public_id == public_id))
    if not resume:
        return False
    resume.recruiter_status = status
    session.commit()
    return True


def list_job_positions(session: Session) -> list[JobPosition]:
    return list(session.scalars(select(JobPosition).order_by(JobPosition.created_at.desc())).all())


def get_job_position(session: Session, job_id: int) -> JobPosition | None:
    return session.get(JobPosition, job_id)


def create_job_position(
    session: Session,
    *,
    title: str,
    summary: str = "",
    required_skills: list[str] | None = None,
    min_years_experience: int | None = None,
    status: str = "active",
) -> JobPosition:
    job = JobPosition(
        title=title,
        summary=summary,
        required_skills=required_skills or [],
        min_years_experience=min_years_experience,
        status=status,
    )
    session.add(job)
    session.commit()
    return job


def update_job_position(
    session: Session,
    job_id: int,
    *,
    title: str | None = None,
    summary: str | None = None,
    required_skills: list[str] | None = None,
    min_years_experience: int | None = None,
    status: str | None = None,
) -> JobPosition | None:
    job = session.get(JobPosition, job_id)
    if not job:
        return None
    if title is not None:
        job.title = title
    if summary is not None:
        job.summary = summary
    if required_skills is not None:
        job.required_skills = required_skills
    if min_years_experience is not None:
        job.min_years_experience = min_years_experience
    if status is not None:
        job.status = status
    session.commit()
    return job


# --- User management (Admin: manage_users permission) ----------------------


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.created_at.desc())).all())


def set_user_active(session: Session, user_id: int, *, active: bool) -> bool:
    user = session.get(User, user_id)
    if not user:
        return False
    user.is_active = active
    session.commit()
    return True


# --- REST API support (webapp/api.py) ---------------------------------------


def paginate(items: list, *, page: int, per_page: int) -> tuple[list, dict]:
    """Generic in-memory pagination — the candidate/evaluation lists here are
    demo-scale (hundreds, not millions of rows), so slicing an already-fetched
    list is simpler and clear enough; a real high-volume deployment would push
    LIMIT/OFFSET into the SQL query instead."""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = items[start : start + per_page]
    return page_items, {"page": page, "per_page": per_page, "total": total, "total_pages": total_pages}


def list_evaluations(
    session: Session,
    *,
    job_position_id: int | None = None,
    backend: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
) -> list[SimpleNamespace]:
    """Unlike list_candidates (every submission, evaluated or not), this only
    returns resumes that actually have an Evaluation row — for the
    evaluations-focused GET /api/evaluations."""
    query = select(Resume).join(Applicant).join(JobPosition).join(Evaluation)

    if job_position_id:
        query = query.where(Resume.job_position_id == job_position_id)
    if backend:
        query = query.where(Evaluation.backend == backend)
    if min_score is not None:
        query = query.where(Evaluation.overall >= min_score)
    if max_score is not None:
        query = query.where(Evaluation.overall <= max_score)

    resumes = session.scalars(query.order_by(Resume.created_at.desc())).all()
    return [_candidate_view(r) for r in resumes]

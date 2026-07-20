"""SQLAlchemy ORM models — the relational schema backing the webapp.

Replaces the old JSON-file store (webapp/applications.json). Every table
carries an integer primary key, timestamps, and explicit foreign keys; status
fields are plain strings rather than DB-level enums so Postgres and the
sqlite dev fallback both work without a migration to add enum values later.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from webapp.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class User(Base, TimestampMixin, UserMixin):
    """Login-capable account. Password is never stored in plaintext — only a
    salted hash (webapp/auth.py, werkzeug's scrypt-based hasher). `role`
    drives the permission checks in webapp/auth.py's PERMISSIONS map.

    UserMixin (flask-login) supplies is_authenticated/is_anonymous/get_id();
    our own `is_active` mapped column below overrides its default property.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")  # admin | recruiter | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    recruiter: Mapped["Recruiter | None"] = relationship(back_populates="user", uselist=False)


class Recruiter(Base, TimestampMixin):
    __tablename__ = "recruiters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    user: Mapped["User"] = relationship(back_populates="recruiter")
    job_positions: Mapped[list["JobPosition"]] = relationship(back_populates="created_by")


class JobPosition(Base, TimestampMixin):
    __tablename__ = "job_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("recruiters.id", ondelete="SET NULL"))
    # created_by_id (-> recruiters.id) predates real user accounts and is unused;
    # kept for backward compatibility. New code attributes jobs directly to the
    # authenticated User instead — no Recruiter profile row required.
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    nice_to_have: Mapped[list] = mapped_column(JSON, default=list)
    min_years_experience: Mapped[int | None] = mapped_column(Integer)
    responsibilities: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active | closed

    created_by: Mapped["Recruiter | None"] = relationship(back_populates="job_positions")
    created_by_user: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    resumes: Mapped[list["Resume"]] = relationship(back_populates="job_position")


class Applicant(Base, TimestampMixin):
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))

    resumes: Mapped[list["Resume"]] = relationship(back_populates="applicant")


class Resume(Base, TimestampMixin):
    """Metadata for one uploaded resume file.

    Raw (pre-redaction) text is deliberately never persisted anywhere in this
    schema — only the anonymized text is, consistent with the pipeline's
    hash-only audit trail (AuditLog never stores an original value either).
    """

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=_uuid_hex)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id", ondelete="CASCADE"), nullable=False)
    job_position_id: Mapped[int] = mapped_column(ForeignKey("job_positions.id", ondelete="CASCADE"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    anonymized_text: Mapped[str] = mapped_column(Text, default="")
    parse_error: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    # uploaded | processing | evaluated | failed
    recruiter_status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    # new | shortlisted | interview | rejected

    applicant: Mapped["Applicant"] = relationship(back_populates="resumes")
    job_position: Mapped["JobPosition"] = relationship(back_populates="resumes")
    evaluation: Mapped["Evaluation | None"] = relationship(
        back_populates="resume", uselist=False, cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="resume", cascade="all, delete-orphan")
    processing_jobs: Mapped[list["ProcessingJob"]] = relationship(back_populates="resume", cascade="all, delete-orphan")


class Evaluation(Base, TimestampMixin):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), unique=True, nullable=False)
    backend: Mapped[str] = mapped_column(String(20), nullable=False)  # claude | ollama | none
    skill_match: Mapped[int | None] = mapped_column(Integer)
    experience_relevance: Mapped[int | None] = mapped_column(Integer)
    project_impact: Mapped[int | None] = mapped_column(Integer)
    overall: Mapped[int | None] = mapped_column(Integer)
    justification: Mapped[str] = mapped_column(Text, default="")
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    interview_questions: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    card_html: Mapped[str] = mapped_column(Text, default="")  # rendered candidate-card doc for instant display

    resume: Mapped["Resume"] = relationship(back_populates="evaluation")


class AuditLog(Base):
    """Generic audit trail: bias-mitigation redaction summaries and admin actions.

    `details` holds a JSON payload whose shape depends on `action`:
      - action="anonymize": {"kind", "replacement", "count", "sample_hashes"} per redaction
      - action="delete_application" / "delete_all" / "login": free-form context
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    resume: Mapped["Resume | None"] = relationship(back_populates="audit_logs")


class ProcessingJob(Base, TimestampMixin):
    """Tracks one background (RQ) evaluation run for a resume.

    `status` is the source of truth the UI polls. `progress`/`progress_message`
    give coarse-grained feedback while the worker is running. `attempts` /
    `max_attempts` back the retry mechanism: the worker increments `attempts`
    on every run and only marks the job definitively `failed` once RQ's own
    retry budget (mirrored here) is exhausted.
    """

    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | processing | completed | failed
    backend: Mapped[str | None] = mapped_column(String(20))
    rq_job_id: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    progress_message: Mapped[str] = mapped_column(String(200), default="Queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")

    resume: Mapped["Resume"] = relationship(back_populates="processing_jobs")


__all__ = [
    "User",
    "Recruiter",
    "JobPosition",
    "Applicant",
    "Resume",
    "Evaluation",
    "AuditLog",
    "ProcessingJob",
]

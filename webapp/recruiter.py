"""Recruiter dashboard: overview, candidate search/filter/sort, ranking
actions (shortlist/reject/interview), job-position management, user
management, CSV/JSON export.

Every route is gated by webapp.auth.require_permission, not a blanket
"logged in" check — Viewer gets read-only routes only; Recruiter adds
candidate/job management; Admin adds bulk delete and user management. See
webapp/auth.py's ROLE_PERMISSIONS for the exact map.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for

from webapp import store
from webapp.auth import (
    DELETE_ALL,
    MANAGE_CANDIDATES,
    MANAGE_JOBS,
    MANAGE_USERS,
    VIEW,
    ROLES,
    ValidationError,
    create_user,
    current_user,
    require_permission,
)
from webapp.db import new_session
from webapp.store import RECRUITER_STATUSES
from webapp.submissions import SubmissionError, enqueue_processing

logger = logging.getLogger(__name__)

recruiter_bp = Blueprint("recruiter", __name__, url_prefix="/admin")

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"


@recruiter_bp.route("")
def dashboard_root():
    return redirect(url_for("recruiter.dashboard"))


@recruiter_bp.route("/dashboard")
@require_permission(VIEW)
def dashboard():
    with new_session() as db:
        stats = store.get_overview_stats(db)
        jobs = store.list_job_positions(db)
        candidates = store.list_candidates(
            db,
            search=request.args.get("q", ""),
            job_position_id=request.args.get("job", type=int),
            recruiter_status=request.args.get("status") or None,
            eval_status=request.args.get("eval_status") or None,
            sort=request.args.get("sort", "date_desc"),
        )
    return render_template(
        "recruiter/dashboard.html",
        stats=stats,
        jobs=jobs,
        candidates=candidates,
        recruiter_statuses=RECRUITER_STATUSES,
        filters={
            "q": request.args.get("q", ""),
            "job": request.args.get("job", type=int),
            "status": request.args.get("status", ""),
            "eval_status": request.args.get("eval_status", ""),
            "sort": request.args.get("sort", "date_desc"),
        },
    )


@recruiter_bp.route("/candidates/<public_id>")
@require_permission(VIEW)
def candidate_detail(public_id):
    with new_session() as db:
        candidate = store.get_candidate_detail(db, public_id)
    if candidate is None:
        return "Not found", 404
    return render_template("recruiter/candidate_detail.html", c=candidate, recruiter_statuses=RECRUITER_STATUSES)


@recruiter_bp.route("/candidates/<public_id>/resume")
@require_permission(VIEW)
def candidate_resume_file(public_id):
    """Serves the original uploaded PDF (the actual, non-anonymized file) for recruiter review."""
    from flask import send_file

    with new_session() as db:
        candidate = store.get_candidate_detail(db, public_id)
    if candidate is None or not candidate.storage_path:
        return "Not found", 404
    path = Path(candidate.storage_path)
    if not path.exists():
        return "File no longer on disk", 404
    return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=candidate.filename)


@recruiter_bp.route("/candidates/<public_id>/status", methods=["POST"])
@require_permission(MANAGE_CANDIDATES)
def set_candidate_status(public_id):
    new_status = request.form.get("status") or (request.get_json(silent=True) or {}).get("status")
    if new_status not in RECRUITER_STATUSES:
        return jsonify({"error": "invalid status"}), 400
    with new_session() as db:
        ok = store.set_recruiter_status(db, public_id, new_status)
        if ok:
            store.log_action(
                db,
                actor=current_user.username,
                action="set_recruiter_status",
                details={"public_id": public_id, "status": new_status},
            )
    if not ok:
        return jsonify({"error": "not found"}), 404
    if request.is_json:
        return jsonify({"ok": True, "status": new_status})
    return redirect(request.referrer or url_for("recruiter.dashboard"))


@recruiter_bp.route("/candidates/<public_id>/retry", methods=["POST"])
@require_permission(MANAGE_CANDIDATES)
def retry_candidate_evaluation(public_id):
    """Re-runs the pipeline for a resume stuck in "failed" (e.g. an invalid
    API key, or Ollama being unreachable at submission time) — without this,
    the only way to get a fresh evaluation was deleting and resubmitting the
    whole application."""
    with new_session() as db:
        ids = store.retry_evaluation(db, public_id)
    if ids is None:
        message = "Only a failed evaluation can be retried."
        if request.is_json:
            return jsonify({"error": message}), 400
        return redirect(request.referrer or url_for("recruiter.candidate_detail", public_id=public_id))

    resume_id, job_id = ids
    try:
        enqueue_processing(resume_id=resume_id, job_id=job_id, public_id=public_id)
    except SubmissionError as exc:
        if request.is_json:
            return jsonify({"error": str(exc)}), 400
        return redirect(request.referrer or url_for("recruiter.candidate_detail", public_id=public_id))

    with new_session() as db:
        store.log_action(db, actor=current_user.username, action="retry_evaluation", details={"public_id": public_id})
    logger.info("retry_candidate_evaluation", extra={"actor": current_user.username, "public_id": public_id})

    if request.is_json:
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("recruiter.candidate_detail", public_id=public_id))


@recruiter_bp.route("/delete-all", methods=["POST"])
@require_permission(DELETE_ALL)
def delete_all():
    with new_session() as db:
        store.clear_applications(db)
        store.log_action(db, actor=current_user.username, action="delete_all")
    for upload in UPLOAD_DIR.glob("*.pdf"):
        upload.unlink(missing_ok=True)
    logger.warning("delete_all_candidates", extra={"actor": current_user.username})
    return redirect(url_for("recruiter.dashboard"))


@recruiter_bp.route("/delete/<public_id>", methods=["POST"])
@require_permission(MANAGE_CANDIDATES)
def delete_one(public_id):
    with new_session() as db:
        store.delete_application(db, public_id)
        store.log_action(db, actor=current_user.username, action="delete_application", details={"public_id": public_id})
    logger.info("delete_candidate", extra={"actor": current_user.username, "public_id": public_id})
    return redirect(url_for("recruiter.dashboard"))


# --- Job management ----------------------------------------------------


@recruiter_bp.route("/jobs")
@require_permission(VIEW)
def jobs():
    with new_session() as db:
        job_list = store.list_job_positions(db)
    return render_template("recruiter/jobs.html", jobs=job_list)


@recruiter_bp.route("/jobs/create", methods=["POST"])
@require_permission(MANAGE_JOBS)
def create_job():
    title = request.form.get("title", "").strip()
    if not title:
        return "Title is required", 400
    min_years = request.form.get("min_years_experience", type=int)
    if min_years is not None and min_years < 0:
        return "Minimum years of experience cannot be negative", 400
    skills = [s.strip() for s in request.form.get("required_skills", "").split(",") if s.strip()]
    status = request.form.get("status", "active")
    if status not in ("active", "closed"):
        return "Invalid status", 400
    with new_session() as db:
        store.create_job_position(
            db,
            title=title,
            summary=request.form.get("summary", "").strip(),
            required_skills=skills,
            min_years_experience=min_years,
            status=status,
        )
    return redirect(url_for("recruiter.jobs"))


@recruiter_bp.route("/jobs/<int:job_id>/update", methods=["POST"])
@require_permission(MANAGE_JOBS)
def update_job(job_id):
    title = request.form.get("title")
    if title is not None and not title.strip():
        return "Title cannot be empty", 400
    min_years = request.form.get("min_years_experience", type=int)
    if min_years is not None and min_years < 0:
        return "Minimum years of experience cannot be negative", 400
    status = request.form.get("status") or None
    if status is not None and status not in ("active", "closed"):
        return "Invalid status", 400
    skills_raw = request.form.get("required_skills")
    skills = [s.strip() for s in skills_raw.split(",") if s.strip()] if skills_raw is not None else None
    with new_session() as db:
        store.update_job_position(
            db,
            job_id,
            title=title,
            summary=request.form.get("summary"),
            required_skills=skills,
            min_years_experience=min_years,
            status=status,
        )
    return redirect(url_for("recruiter.jobs"))


# --- User management (admin only) -----------------------------------------


@recruiter_bp.route("/users")
@require_permission(MANAGE_USERS)
def users():
    with new_session() as db:
        user_list = store.list_users(db)
    return render_template("recruiter/users.html", users=user_list, roles=ROLES)


@recruiter_bp.route("/users/create", methods=["POST"])
@require_permission(MANAGE_USERS)
def create_user_route():
    error = None
    try:
        with new_session() as db:
            new = create_user(
                db,
                username=request.form.get("username", ""),
                password=request.form.get("password", ""),
                full_name=request.form.get("full_name", ""),
                role=request.form.get("role", ""),
            )
            store.log_action(
                db,
                actor=current_user.username,
                action="create_user",
                details={"username": new.username, "role": new.role},
            )
            logger.info(
                "user_created", extra={"actor": current_user.username, "username": new.username, "role": new.role}
            )
    except ValidationError as exc:
        logger.warning("user_creation_failed", extra={"actor": current_user.username, "error": str(exc)})
        error = str(exc)
    if error:
        with new_session() as db:
            user_list = store.list_users(db)
        return render_template("recruiter/users.html", users=user_list, roles=ROLES, error=error), 400
    return redirect(url_for("recruiter.users"))


@recruiter_bp.route("/users/<int:user_id>/deactivate", methods=["POST"])
@require_permission(MANAGE_USERS)
def deactivate_user(user_id):
    with new_session() as db:
        store.set_user_active(db, user_id, active=False)
        store.log_action(db, actor=current_user.username, action="deactivate_user", details={"user_id": user_id})
    logger.warning("user_deactivated", extra={"actor": current_user.username, "user_id": user_id})
    return redirect(url_for("recruiter.users"))


@recruiter_bp.route("/users/<int:user_id>/activate", methods=["POST"])
@require_permission(MANAGE_USERS)
def activate_user(user_id):
    with new_session() as db:
        store.set_user_active(db, user_id, active=True)
        store.log_action(db, actor=current_user.username, action="activate_user", details={"user_id": user_id})
    logger.info("user_activated", extra={"actor": current_user.username, "user_id": user_id})
    return redirect(url_for("recruiter.users"))


@recruiter_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@require_permission(MANAGE_USERS)
def delete_user_route(user_id):
    if user_id == current_user.id:
        # Deleting your own account mid-session would immediately invalidate
        # the login that just authorized the request — deactivate instead.
        return redirect(url_for("recruiter.users"))
    with new_session() as db:
        ok = store.delete_user(db, user_id)
        if ok:
            store.log_action(db, actor=current_user.username, action="delete_user", details={"user_id": user_id})
    if ok:
        logger.warning("user_deleted", extra={"actor": current_user.username, "user_id": user_id})
    return redirect(url_for("recruiter.users"))


# --- Export --------------------------------------------------------------

_EXPORT_FIELDS = [
    "name",
    "filename",
    "job_title",
    "submitted_at",
    "recruiter_status",
    "backend",
    "overall",
    "skill_match",
    "experience_relevance",
    "project_impact",
    "job_status",
    "redaction_count",
]


def _export_rows():
    with new_session() as db:
        candidates = store.list_candidates(
            db,
            search=request.args.get("q", ""),
            job_position_id=request.args.get("job", type=int),
            recruiter_status=request.args.get("status") or None,
            eval_status=request.args.get("eval_status") or None,
            sort=request.args.get("sort", "date_desc"),
        )
    return [{field: getattr(c, field) for field in _EXPORT_FIELDS} for c in candidates]


@recruiter_bp.route("/export.csv")
@require_permission(VIEW)
def export_csv():
    rows = _export_rows()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=candidates.csv"},
    )


@recruiter_bp.route("/export.json")
@require_permission(VIEW)
def export_json():
    rows = _export_rows()
    return Response(
        json.dumps(rows, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=candidates.json"},
    )

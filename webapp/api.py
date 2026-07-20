"""JSON REST API — /api/*.

Auth: reuses the existing session-based login (Flask-Login) and role
permissions (webapp/auth.py), rather than a separate API-key/token scheme —
the app already has one full auth system with real accounts and RBAC; adding
a second, parallel auth mechanism just for this blueprint would be needless
duplicated security surface for a project this size. The practical
consequence: an API client must first POST to /admin/login (same as a
browser) to obtain a session cookie, then send it on subsequent /api/ calls.
A pure machine-to-machine deployment would more typically want a bearer
token instead — noted as a deliberate scope decision, not an oversight.

Every route here uses require_api_permission (not require_permission): a
401/403 JSON body, never an HTML redirect to the login page, which is what
webapp.auth.require_permission does for the browser-facing dashboard.

CSRF: exempted for this blueprint in webapp/app.py (CSRFProtect's form-token
model doesn't fit non-browser JSON/multipart clients); session-cookie auth
without CSRF tokens is acceptable here because every mutating endpoint reads
JSON or requires an explicit Content-Type multipart body — not something a
plain cross-site HTML form can forge.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from webapp import store
from webapp.auth import MANAGE_CANDIDATES, MANAGE_JOBS, VIEW, require_api_permission
from webapp.db import new_session
from webapp.submissions import SubmissionError, submit_resume

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")

_MAX_PER_PAGE = 100
_UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"


class ApiValidationError(Exception):
    """Raised on bad request input; caught below and turned into a 400 with
    field-level detail, so every endpoint reports validation failures the
    same shape instead of each view function inventing its own."""

    def __init__(self, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.fields = fields or {}


@api_bp.errorhandler(ApiValidationError)
def _handle_validation_error(exc: ApiValidationError):
    return jsonify(error={"code": "validation_error", "message": exc.message, "fields": exc.fields}), 400


@api_bp.errorhandler(HTTPException)
def _handle_http_exception(exc: HTTPException):
    return jsonify(error={"code": (exc.name or "error").lower().replace(" ", "_"), "message": exc.description}), (
        exc.code or 500
    )


@api_bp.errorhandler(Exception)
def _handle_unexpected_error(exc: Exception):
    logger.error("api_unhandled_exception", extra={"path": request.path}, exc_info=True)
    return jsonify(error={"code": "internal_error", "message": "An unexpected error occurred."}), 500


# --- Shared helpers ----------------------------------------------------


def _pagination_params() -> tuple[int, int]:
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    if page is None or page < 1:
        raise ApiValidationError("Invalid pagination parameters.", {"page": "must be a positive integer"})
    if per_page is None or per_page < 1:
        raise ApiValidationError("Invalid pagination parameters.", {"per_page": "must be a positive integer"})
    return page, min(per_page, _MAX_PER_PAGE)


def _paginated_response(items: list, to_dict, page: int, per_page: int) -> dict:
    page_items, pagination = store.paginate(items, page=page, per_page=per_page)
    return {"data": [to_dict(item) for item in page_items], "pagination": pagination}


def _candidate_to_dict(c) -> dict:
    return vars(c)


def _job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "summary": job.summary,
        "required_skills": job.required_skills,
        "nice_to_have": job.nice_to_have,
        "min_years_experience": job.min_years_experience,
        "status": job.status,
        "created_at": job.created_at.isoformat(timespec="seconds"),
    }


# --- Jobs ----------------------------------------------------------------


@api_bp.route("/jobs", methods=["GET"])
@require_api_permission(VIEW)
def list_jobs():
    page, per_page = _pagination_params()
    status_filter = request.args.get("status")
    if status_filter is not None and status_filter not in ("active", "closed"):
        raise ApiValidationError("Invalid status filter.", {"status": "must be 'active' or 'closed'"})

    with new_session() as db:
        jobs = store.list_job_positions(db)
        if status_filter:
            jobs = [j for j in jobs if j.status == status_filter]
        return jsonify(_paginated_response(jobs, _job_to_dict, page, per_page))


@api_bp.route("/jobs/<int:job_id>", methods=["GET"])
@require_api_permission(VIEW)
def get_job(job_id: int):
    with new_session() as db:
        job = store.get_job_position(db, job_id)
        if not job:
            return jsonify(error={"code": "not_found", "message": f"No job position with id {job_id}."}), 404
        return jsonify(_job_to_dict(job))


@api_bp.route("/jobs", methods=["POST"])
@require_api_permission(MANAGE_JOBS)
def create_job():
    body = request.get_json(silent=True)
    if body is None:
        raise ApiValidationError("Request body must be JSON.")

    fields: dict[str, str] = {}
    title = (body.get("title") or "").strip()
    if not title:
        fields["title"] = "required"

    required_skills = body.get("required_skills", [])
    if required_skills is not None and not isinstance(required_skills, list):
        fields["required_skills"] = "must be a list of strings"

    min_years = body.get("min_years_experience")
    if min_years is not None:
        if not isinstance(min_years, int) or isinstance(min_years, bool) or min_years < 0:
            fields["min_years_experience"] = "must be a non-negative integer"

    status = body.get("status", "active")
    if status not in ("active", "closed"):
        fields["status"] = "must be 'active' or 'closed'"

    if fields:
        raise ApiValidationError("Invalid job position payload.", fields)

    with new_session() as db:
        job = store.create_job_position(
            db,
            title=title,
            summary=(body.get("summary") or "").strip(),
            required_skills=[str(s) for s in (required_skills or [])],
            min_years_experience=min_years,
            status=status,
        )
        return jsonify(_job_to_dict(job)), 201


# --- Resumes ---------------------------------------------------------------


@api_bp.route("/resumes", methods=["POST"])
@require_api_permission(MANAGE_CANDIDATES)
def create_resume():
    job_id = request.form.get("job_id", type=int)
    if job_id is None:
        raise ApiValidationError("job_id is required.", {"job_id": "required, must be an integer"})

    with new_session() as db:
        job = store.get_job_position(db, job_id)
        if not job:
            return jsonify(error={"code": "not_found", "message": f"No job position with id {job_id}."}), 404

    try:
        result = submit_resume(
            file=request.files.get("resume"),
            applicant_name=request.form.get("name", ""),
            job_position_id=job_id,
            upload_dir=_UPLOAD_DIR,
        )
    except SubmissionError as exc:
        raise ApiValidationError(str(exc), {"resume": str(exc)})

    return (
        jsonify(
            {
                "public_id": result["public_id"],
                "applicant_name": result["applicant_name"],
                "job_id": job_id,
                "status": "pending",
                "status_url": f"/api/resumes/{result['public_id']}",
            }
        ),
        202,
    )


@api_bp.route("/resumes/<public_id>", methods=["GET"])
@require_api_permission(VIEW)
def get_resume(public_id: str):
    with new_session() as db:
        candidate = store.get_candidate_detail(db, public_id)
        if candidate is None:
            return jsonify(error={"code": "not_found", "message": "No resume with that id."}), 404
        data = vars(candidate)
        data.pop("card_doc", None)  # HTML fragment — not useful in a JSON API response
        return jsonify(data)


# --- Applicants --------------------------------------------------------


@api_bp.route("/applicants", methods=["GET"])
@require_api_permission(VIEW)
def list_applicants():
    page, per_page = _pagination_params()
    job_id = request.args.get("job_id", type=int)
    recruiter_status = request.args.get("status")
    eval_status = request.args.get("eval_status")
    search = request.args.get("q", "")

    if recruiter_status is not None and recruiter_status not in store.RECRUITER_STATUSES:
        raise ApiValidationError(
            "Invalid status filter.", {"status": f"must be one of {', '.join(store.RECRUITER_STATUSES)}"}
        )

    with new_session() as db:
        candidates = store.list_candidates(
            db,
            search=search,
            job_position_id=job_id,
            recruiter_status=recruiter_status,
            eval_status=eval_status,
            sort=request.args.get("sort", "date_desc"),
        )
        return jsonify(_paginated_response(candidates, _candidate_to_dict, page, per_page))


# --- Evaluations -------------------------------------------------------


@api_bp.route("/evaluations", methods=["GET"])
@require_api_permission(VIEW)
def list_evaluations():
    page, per_page = _pagination_params()
    job_id = request.args.get("job_id", type=int)
    backend = request.args.get("backend")
    min_score = request.args.get("min_score", type=int)
    max_score = request.args.get("max_score", type=int)

    for label, value in (("min_score", min_score), ("max_score", max_score)):
        if value is not None and not (0 <= value <= 100):
            raise ApiValidationError("Invalid score filter.", {label: "must be between 0 and 100"})

    with new_session() as db:
        evaluations = store.list_evaluations(
            db, job_position_id=job_id, backend=backend, min_score=min_score, max_score=max_score
        )
        return jsonify(_paginated_response(evaluations, _candidate_to_dict, page, per_page))


# --- Dashboard -----------------------------------------------------------


@api_bp.route("/dashboard", methods=["GET"])
@require_api_permission(VIEW)
def dashboard():
    with new_session() as db:
        stats = store.get_overview_stats(db)
        return jsonify(vars(stats))

"""Candidate-facing demo portal: upload a resume PDF, get screened in the background.

Run:  python webapp/app.py        (and, separately: python webapp/worker.py)
Then open http://localhost:5000

Uploading a resume returns immediately — the actual parse/anonymize/evaluate
pipeline runs in a background worker (webapp/tasks.py) via an RQ queue backed
by Redis. The result page polls GET /status/<id> until the job completes.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # allow `from screener import ...` without install

from screener import ingest  # noqa: E402
from webapp import store  # noqa: E402
from webapp.api import api_bp  # noqa: E402
from webapp.auth import authenticate, has_permission, login_manager  # noqa: E402
from webapp.db import init_db, new_session  # noqa: E402
from webapp.logging_config import bind_request_id, configure_logging  # noqa: E402
from webapp.recruiter import recruiter_bp  # noqa: E402
from webapp.submissions import SubmissionError, submit_resume  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

load_dotenv(ROOT / ".env")

UPLOAD_DIR = ROOT / "webapp" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

JD = ingest.load_job_description(ROOT / "data" / "job_description.json")

init_db()  # dev/CI convenience; production should provision the schema via Alembic instead
with new_session() as _session:
    # Seeds one active job position on a fresh database so the apply page
    # isn't empty on first run — recruiters create/close postings from the
    # dashboard afterwards (webapp/recruiter.py), and the apply page always
    # reflects whatever's active in the database, not this fixture.
    store.get_or_create_job_position(_session, JD)

_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError(
        'FLASK_SECRET_KEY is not set. Generate one with `python -c "import secrets; '
        'print(secrets.token_hex(32))"` and put it in .env — an app-generated random '
        "key would invalidate every session on restart and differ across worker "
        "processes in production, silently breaking logins."
    )

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB
app.secret_key = _SECRET_KEY

login_manager.init_app(app)
csrf = CSRFProtect(app)
app.register_blueprint(recruiter_bp)
app.register_blueprint(api_bp)
csrf.exempt(api_bp)  # JSON/multipart API clients, not browser forms — see webapp/api.py's module docstring


@app.errorhandler(HTTPException)
def _api_http_exception(exc: HTTPException):
    """Werkzeug raises routing errors (404 on an unmatched path, 405 on a
    wrong method) before Flask can attribute them to a blueprint, so
    api_bp's own errorhandler never sees them — this app-level handler
    catches the /api/* case and still returns JSON instead of Flask's
    default HTML error page."""
    if request.path.startswith("/api/"):
        return (
            jsonify(error={"code": (exc.name or "error").lower().replace(" ", "_"), "message": exc.description}),
            exc.code or 500,
        )
    return exc


@app.context_processor
def inject_auth_helpers():
    """Makes current_user + has_permission available in every template
    without threading them through each render_template call."""
    return dict(current_user=current_user, has_permission=has_permission)


@app.before_request
def _start_request_log():
    g.request_id = bind_request_id(request.headers.get("X-Request-ID"))
    g.request_start = time.perf_counter()
    logger.info("request_started", extra={"method": request.method, "path": request.path})


@app.after_request
def _finish_request_log(response):
    response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
    duration_ms = round((time.perf_counter() - getattr(g, "request_start", time.perf_counter())) * 1000, 1)
    log_fn = (
        logger.error
        if response.status_code >= 500
        else (logger.warning if response.status_code >= 400 else logger.info)
    )
    log_fn(
        "request_finished",
        extra={
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


def _open_jobs_for_template() -> list[dict]:
    with new_session() as db:
        jobs = store.list_active_job_positions(db)
        return [{"id": j.id, "title": j.title, "summary": j.summary, "skills": j.required_skills[:5]} for j in jobs]


@app.route("/")
def index():
    return render_template("index.html", jobs=_open_jobs_for_template(), selected_job_id=None)


@app.route("/apply", methods=["POST"])
def apply():
    job_id = request.form.get("job_id", type=int)
    with new_session() as db:
        job = store.get_job_position(db, job_id) if job_id is not None else None
        job_title = job.title if job else None
        job_active = bool(job and job.status == "active")

    if not job_active:
        return (
            render_template(
                "index.html",
                jobs=_open_jobs_for_template(),
                selected_job_id=job_id,
                error="Please select an open position to apply for.",
            ),
            400,
        )

    try:
        result = submit_resume(
            file=request.files.get("resume"),
            applicant_name=request.form.get("name", ""),
            job_position_id=job_id,
            upload_dir=UPLOAD_DIR,
        )
    except SubmissionError as exc:
        return (
            render_template("index.html", jobs=_open_jobs_for_template(), selected_job_id=job_id, error=str(exc)),
            400,
        )

    return render_template(
        "result.html",
        job_title=job_title,
        applicant_name=result["applicant_name"],
        public_id=result["public_id"],
    )


@app.route("/status/<public_id>")
def status(public_id):
    with new_session() as db:
        view = store.get_status_view(db, public_id)
    if view is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(vars(view))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        with new_session() as db:
            user = authenticate(db, username, password)
            if user:
                login_user(user)
                store.log_action(db, actor=user.username, action="login")
                logger.info("login_succeeded", extra={"username": user.username, "role": user.role})
                dest = request.args.get("next") or url_for("recruiter.dashboard")
                return redirect(dest)
        logger.warning("login_failed", extra={"username": username})
        error = "Incorrect username or password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    if current_user.is_authenticated:
        with new_session() as db:
            store.log_action(db, actor=current_user.username, action="logout")
    logout_user()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))

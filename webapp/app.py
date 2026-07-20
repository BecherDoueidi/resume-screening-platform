"""Candidate-facing demo portal: upload a resume PDF, get screened in the background.

Run:  python webapp/app.py        (and, separately: python webapp/worker.py)
Then open http://localhost:5000

Uploading a resume returns immediately — the actual parse/anonymize/evaluate
pipeline runs in a background worker (webapp/tasks.py) via an RQ queue backed
by Redis. The result page polls GET /status/<id> until the job completes.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from flask_wtf.csrf import CSRFProtect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # allow `from screener import ...` without install

from screener import ingest  # noqa: E402
from webapp import store  # noqa: E402
from webapp.auth import authenticate, has_permission, login_manager  # noqa: E402
from webapp.db import init_db, new_session  # noqa: E402
from webapp.jobs import DEFAULT_RETRY, get_queue  # noqa: E402
from webapp.recruiter import recruiter_bp  # noqa: E402
from webapp.tasks import process_resume  # noqa: E402

load_dotenv(ROOT / ".env")

UPLOAD_DIR = ROOT / "webapp" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

JD = ingest.load_job_description(ROOT / "data" / "job_description.json")

init_db()  # dev/CI convenience; production should provision the schema via Alembic instead
with new_session() as _session:
    _JOB_POSITION_ID = store.get_or_create_job_position(_session, JD).id

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
CSRFProtect(app)
app.register_blueprint(recruiter_bp)


@app.context_processor
def inject_auth_helpers():
    """Makes current_user + has_permission available in every template
    without threading them through each render_template call."""
    return dict(current_user=current_user, has_permission=has_permission)


@app.route("/")
def index():
    return render_template("index.html", job=JD)


@app.route("/apply", methods=["POST"])
def apply():
    file = request.files.get("resume")
    if not file or file.filename == "":
        return render_template("index.html", job=JD, error="Please choose a PDF file."), 400
    if not file.filename.lower().endswith(".pdf"):
        return render_template("index.html", job=JD, error="Only PDF resumes are accepted."), 400

    applicant_name = (request.form.get("name", "").strip() or "Applicant")[:200]
    saved_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    file.save(saved_path)

    # Nothing is parsed/evaluated here — just persist the upload and hand it
    # to the queue. The worker (webapp/tasks.py) does the actual pipeline run.
    with new_session() as db:
        db_resume, db_job = store.create_submission(
            db,
            job_position_id=_JOB_POSITION_ID,
            applicant_name=applicant_name,
            original_filename=file.filename,
            storage_path=str(saved_path),
        )
        public_id, resume_id, job_id = db_resume.public_id, db_resume.id, db_job.id

    rq_job = get_queue().enqueue(process_resume, resume_id, job_id, retry=DEFAULT_RETRY, job_timeout="10m")
    with new_session() as db:
        store.attach_rq_job_id(db, job_id, rq_job.id)

    return render_template(
        "result.html",
        job=JD,
        applicant_name=applicant_name,
        public_id=public_id,
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
                dest = request.args.get("next") or url_for("recruiter.dashboard")
                return redirect(dest)
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

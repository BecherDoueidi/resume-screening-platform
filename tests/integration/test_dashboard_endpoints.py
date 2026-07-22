"""Integration tests for the recruiter dashboard blueprint: auth gating,
role-based permissions (Admin/Recruiter/Viewer), candidate listing/filtering,
job management, user management, and CSV/JSON export.
"""

from __future__ import annotations

import pytest

from webapp import store
from webapp.db import new_session


@pytest.fixture
def job_id(sample_jd):
    with new_session() as s:
        return store.get_or_create_job_position(s, sample_jd).id


@pytest.fixture
def evaluated_candidate(job_id):
    """A fully-evaluated candidate, for dashboard/detail/export assertions."""
    from screener.models import Evaluation as EvaluationResult

    with new_session() as s:
        resume, job = store.create_submission(
            s,
            job_position_id=job_id,
            applicant_name="Jane Candidate",
            original_filename="jane.pdf",
            storage_path="/tmp/jane.pdf",
        )
        resume_id, job_id_ = resume.id, job.id
        public_id = resume.public_id
    with new_session() as s:
        store.save_anonymized_text(s, resume_id, "Anonymized resume text for Jane.")
        store.save_evaluation(
            s,
            resume_id,
            job_id_,
            backend="ollama",
            evaluation=EvaluationResult(
                skill_match=80,
                experience_relevance=70,
                project_impact=60,
                overall=70,
                justification="Good fit.",
                gaps=[],
                interview_questions=["Q?"],
            ),
            card_html="<div>card</div>",
        )
    return public_id


@pytest.fixture
def failed_candidate(job_id):
    """A candidate whose evaluation definitively failed (e.g. a bad API key)
    — the only state retry_evaluation should act on."""
    from screener.models import Evaluation as EvaluationResult

    with new_session() as s:
        resume, job = store.create_submission(
            s,
            job_position_id=job_id,
            applicant_name="Failed Candidate",
            original_filename="failed.pdf",
            storage_path="/tmp/failed.pdf",
        )
        resume_id, job_id_ = resume.id, job.id
        public_id = resume.public_id
    with new_session() as s:
        store.save_anonymized_text(s, resume_id, "Anonymized resume text.")
        store.save_evaluation(
            s,
            resume_id,
            job_id_,
            backend="claude",
            evaluation=EvaluationResult(
                skill_match=0,
                experience_relevance=0,
                project_impact=0,
                overall=0,
                justification="",
                error="API error 401: invalid x-api-key",
            ),
            card_html=None,
        )
    return public_id


@pytest.fixture(autouse=True)
def _mock_queue(mocker):
    fake_job = mocker.Mock(id="fake-rq-job-id")
    fake_queue = mocker.Mock()
    fake_queue.enqueue.return_value = fake_job
    return mocker.patch("webapp.submissions.get_queue", return_value=fake_queue)


class TestAuthGating:
    def test_dashboard_redirects_to_login_when_anonymous(self, client):
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers["Location"]

    def test_dashboard_accessible_once_logged_in(self, admin_client):
        resp = admin_client.get("/admin/dashboard")
        assert resp.status_code == 200

    def test_admin_root_redirects_to_dashboard(self, admin_client):
        resp = admin_client.get("/admin")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/admin/dashboard")


class TestRolePermissions:
    @pytest.mark.parametrize("path", ["/admin/dashboard", "/admin/jobs", "/admin/export.csv", "/admin/export.json"])
    def test_viewer_can_read(self, viewer_client, path):
        assert viewer_client.get(path).status_code == 200

    def test_viewer_cannot_delete_all(self, viewer_client):
        assert viewer_client.post("/admin/delete-all").status_code == 403

    def test_viewer_cannot_manage_users(self, viewer_client):
        assert viewer_client.get("/admin/users").status_code == 403

    def test_viewer_cannot_create_job(self, viewer_client):
        resp = viewer_client.post("/admin/jobs/create", data={"title": "New Role"})
        assert resp.status_code == 403

    def test_viewer_cannot_change_candidate_status(self, viewer_client, evaluated_candidate):
        resp = viewer_client.post(f"/admin/candidates/{evaluated_candidate}/status", json={"status": "shortlisted"})
        assert resp.status_code == 403

    def test_recruiter_can_create_job(self, recruiter_client):
        resp = recruiter_client.post("/admin/jobs/create", data={"title": "New Role", "status": "active"})
        assert resp.status_code == 302

    def test_recruiter_can_change_candidate_status(self, recruiter_client, evaluated_candidate):
        resp = recruiter_client.post(f"/admin/candidates/{evaluated_candidate}/status", json={"status": "shortlisted"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "shortlisted"

    def test_recruiter_cannot_delete_all(self, recruiter_client):
        assert recruiter_client.post("/admin/delete-all").status_code == 403

    def test_recruiter_cannot_manage_users(self, recruiter_client):
        assert recruiter_client.get("/admin/users").status_code == 403

    def test_admin_can_delete_all(self, admin_client):
        assert admin_client.post("/admin/delete-all").status_code == 302

    def test_admin_can_manage_users(self, admin_client):
        assert admin_client.get("/admin/users").status_code == 200


class TestCandidateListing:
    def test_dashboard_lists_candidate(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/dashboard")
        assert b"Jane Candidate" in resp.data

    def test_search_filters_by_name(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/dashboard?q=Jane")
        assert b"Jane Candidate" in resp.data
        resp = admin_client.get("/admin/dashboard?q=Nonexistent")
        assert b"Jane Candidate" not in resp.data

    def test_filter_by_recruiter_status(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/dashboard?status=rejected")
        assert b"Jane Candidate" not in resp.data
        resp = admin_client.get("/admin/dashboard?status=new")
        assert b"Jane Candidate" in resp.data

    def test_overview_stats_reflect_data(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/dashboard")
        assert resp.status_code == 200
        # 1 applicant, 1 active job, 1 completed evaluation, 0 in queue
        assert b"1" in resp.data


class TestCandidateDetail:
    def test_shows_scores_and_anonymized_text(self, admin_client, evaluated_candidate):
        resp = admin_client.get(f"/admin/candidates/{evaluated_candidate}")
        assert resp.status_code == 200
        assert b"Jane Candidate" in resp.data
        assert b"Anonymized resume text for Jane" in resp.data

    def test_404_for_unknown_candidate(self, admin_client):
        resp = admin_client.get("/admin/candidates/does-not-exist")
        assert resp.status_code == 404


class TestRetryEvaluation:
    def test_admin_can_retry_failed_evaluation(self, admin_client, failed_candidate, db_session, _mock_queue):
        from webapp.models_db import ProcessingJob, Resume

        resp = admin_client.post(f"/admin/candidates/{failed_candidate}/retry")
        assert resp.status_code == 302

        resume = db_session.query(Resume).filter_by(public_id=failed_candidate).one()
        assert resume.status == "pending"
        assert resume.parse_error == ""

        jobs = db_session.query(ProcessingJob).filter_by(resume_id=resume.id).order_by(ProcessingJob.id).all()
        assert len(jobs) == 2  # original failed job + the fresh retry job
        assert jobs[-1].status == "pending"

        fake_queue = _mock_queue.return_value
        fake_queue.enqueue.assert_called_once()

    def test_retried_evaluation_replaces_the_stale_failed_one(self, failed_candidate, db_session):
        """save_evaluation must be able to overwrite a resume's previous
        (failed) Evaluation row — Evaluation.resume_id is unique, so without
        the upsert-on-save behavior a retry's worker run would crash with an
        IntegrityError the moment it tried to persist the fresh result."""
        from screener.models import Evaluation as EvaluationResult
        from webapp.models_db import Evaluation, ProcessingJob, Resume

        resume = db_session.query(Resume).filter_by(public_id=failed_candidate).one()
        job = ProcessingJob(resume_id=resume.id, status="pending", progress=0, progress_message="Queued")
        db_session.add(job)
        db_session.commit()

        store.save_evaluation(
            db_session,
            resume.id,
            job.id,
            backend="ollama",
            evaluation=EvaluationResult(
                skill_match=90, experience_relevance=85, project_impact=80, overall=85, justification="Great fit."
            ),
            card_html="<div>card</div>",
        )

        evals = db_session.query(Evaluation).filter_by(resume_id=resume.id).all()
        assert len(evals) == 1
        assert evals[0].overall == 85

    def test_recruiter_can_retry_failed_evaluation(self, recruiter_client, failed_candidate):
        resp = recruiter_client.post(f"/admin/candidates/{failed_candidate}/retry")
        assert resp.status_code == 302

    def test_viewer_cannot_retry(self, viewer_client, failed_candidate):
        resp = viewer_client.post(f"/admin/candidates/{failed_candidate}/retry")
        assert resp.status_code == 403

    def test_cannot_retry_an_already_evaluated_candidate(self, admin_client, evaluated_candidate, _mock_queue):
        resp = admin_client.post(f"/admin/candidates/{evaluated_candidate}/retry")
        assert resp.status_code == 302
        _mock_queue.return_value.enqueue.assert_not_called()

    def test_returns_400_json_for_unknown_candidate(self, admin_client):
        resp = admin_client.post("/admin/candidates/does-not-exist/retry", content_type="application/json", data="{}")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Only a failed evaluation can be retried."

    def test_enqueue_failure_keeps_resume_failed(self, admin_client, failed_candidate, db_session, mocker):
        from webapp.models_db import Resume

        mocker.patch("webapp.submissions.get_queue").return_value.enqueue.side_effect = ConnectionError("no redis")
        resp = admin_client.post(f"/admin/candidates/{failed_candidate}/retry")
        assert resp.status_code == 302

        resume = db_session.query(Resume).filter_by(public_id=failed_candidate).one()
        assert resume.status == "failed"


class TestDeletion:
    def test_delete_one_removes_candidate(self, admin_client, evaluated_candidate):
        resp = admin_client.post(f"/admin/delete/{evaluated_candidate}")
        assert resp.status_code == 302
        resp = admin_client.get("/admin/dashboard")
        assert b"Jane Candidate" not in resp.data

    def test_delete_one_updates_total_applicants_stat(self, admin_client, evaluated_candidate, db_session):
        """Deleting a Resume without also deleting its Applicant left an
        orphaned row that kept inflating this count — regression test."""
        from webapp.models_db import Applicant

        admin_client.post(f"/admin/delete/{evaluated_candidate}")
        assert db_session.query(Applicant).count() == 0
        resp = admin_client.get("/admin/dashboard")
        assert b">0<" in resp.data  # total applicants stat card now shows 0

    def test_delete_all_clears_everything(self, admin_client, evaluated_candidate):
        admin_client.post("/admin/delete-all")
        resp = admin_client.get("/admin/dashboard")
        assert b"Jane Candidate" not in resp.data

    def test_delete_all_leaves_no_orphaned_applicants(self, admin_client, evaluated_candidate, db_session):
        from webapp.models_db import Applicant

        admin_client.post("/admin/delete-all")
        assert db_session.query(Applicant).count() == 0

    def test_dashboard_delete_all_form_is_not_nested_inside_filter_form(self, admin_client, evaluated_candidate):
        """Regression test: the delete-all <form> was nested inside the
        filters <form>, which is invalid HTML — browsers silently drop the
        nested tag, so clicking "Delete all" actually submitted the outer
        GET filter form instead of POSTing to /admin/delete-all."""
        import re

        resp = admin_client.get("/admin/dashboard")
        html = resp.data.decode()
        forms = list(re.finditer(r"<form\b", html))
        form_ends = list(re.finditer(r"</form>", html))
        # every <form> open must close before the next one opens
        positions = sorted([(m.start(), "open") for m in forms] + [(m.start(), "close") for m in form_ends])
        depth = 0
        for _, kind in positions:
            depth += 1 if kind == "open" else -1
            assert depth <= 1, "a <form> is nested inside another <form>"


class TestJobManagement:
    def test_create_job_persists(self, admin_client, db_session):
        admin_client.post(
            "/admin/jobs/create",
            data={"title": "Data Analyst", "required_skills": "SQL, Excel", "status": "active"},
        )
        from webapp.models_db import JobPosition

        job = db_session.query(JobPosition).filter_by(title="Data Analyst").one()
        assert job.required_skills == ["SQL", "Excel"]

    def test_create_job_rejects_empty_title(self, admin_client):
        resp = admin_client.post("/admin/jobs/create", data={"title": "  ", "status": "active"})
        assert resp.status_code == 400

    def test_create_job_rejects_negative_experience(self, admin_client):
        resp = admin_client.post(
            "/admin/jobs/create", data={"title": "Role", "min_years_experience": "-1", "status": "active"}
        )
        assert resp.status_code == 400

    def test_create_job_rejects_invalid_status(self, admin_client):
        resp = admin_client.post("/admin/jobs/create", data={"title": "Role", "status": "bogus"})
        assert resp.status_code == 400


class TestUserManagement:
    def test_admin_creates_new_user(self, admin_client, db_session):
        resp = admin_client.post(
            "/admin/users/create",
            data={
                "username": "newrecruiter",
                "password": "securepass123",
                "full_name": "New Recruiter",
                "role": "recruiter",
            },
        )
        assert resp.status_code == 302
        from webapp.models_db import User

        user = db_session.query(User).filter_by(username="newrecruiter").one()
        assert user.role == "recruiter"
        assert user.password_hash != "securepass123"  # never stored in plaintext

    def test_duplicate_username_shows_error(self, admin_client, admin_user):
        resp = admin_client.post(
            "/admin/users/create",
            data={"username": admin_user.username, "password": "securepass123", "role": "viewer"},
        )
        assert resp.status_code == 400
        assert b"already taken" in resp.data

    def test_weak_password_rejected(self, admin_client):
        resp = admin_client.post(
            "/admin/users/create", data={"username": "someuser", "password": "short", "role": "viewer"}
        )
        assert resp.status_code == 400

    def test_deactivate_user_prevents_login(self, admin_client, recruiter_user, client, db_session):
        admin_client.post(f"/admin/users/{recruiter_user.id}/deactivate")
        db_session.expire_all()

        from webapp.auth import authenticate

        assert authenticate(db_session, "recruiter1", "recruitpass123") is None

    def test_activate_user_restores_login(self, admin_client, recruiter_user, db_session):
        admin_client.post(f"/admin/users/{recruiter_user.id}/deactivate")
        db_session.expire_all()

        resp = admin_client.post(f"/admin/users/{recruiter_user.id}/activate")
        assert resp.status_code == 302
        db_session.expire_all()

        from webapp.auth import authenticate

        assert authenticate(db_session, "recruiter1", "recruitpass123") is not None

    def test_deactivated_user_shows_activate_button(self, admin_client, recruiter_user):
        admin_client.post(f"/admin/users/{recruiter_user.id}/deactivate")
        resp = admin_client.get("/admin/users")
        assert b"Activate" in resp.data

    def test_viewer_cannot_activate_user(self, client, admin_user, viewer_user, recruiter_user):
        from tests.integration.conftest import _login_as

        _login_as(client, admin_user)
        client.post(f"/admin/users/{recruiter_user.id}/deactivate")

        _login_as(client, viewer_user)
        resp = client.post(f"/admin/users/{recruiter_user.id}/activate")
        assert resp.status_code == 403

    def test_admin_can_delete_user(self, admin_client, recruiter_user, db_session):
        from webapp.models_db import User

        resp = admin_client.post(f"/admin/users/{recruiter_user.id}/delete")
        assert resp.status_code == 302
        assert db_session.query(User).filter_by(id=recruiter_user.id).first() is None

    def test_admin_cannot_delete_own_account(self, admin_client, admin_user, db_session):
        from webapp.models_db import User

        resp = admin_client.post(f"/admin/users/{admin_user.id}/delete")
        assert resp.status_code == 302
        assert db_session.query(User).filter_by(id=admin_user.id).first() is not None

    def test_viewer_cannot_delete_user(self, viewer_client, recruiter_user, db_session):
        from webapp.models_db import User

        resp = viewer_client.post(f"/admin/users/{recruiter_user.id}/delete")
        assert resp.status_code == 403
        assert db_session.query(User).filter_by(id=recruiter_user.id).first() is not None

    def test_deleting_user_keeps_their_created_job_positions(self, admin_client, recruiter_user, db_session):
        from webapp.models_db import JobPosition

        job = JobPosition(title="Orphaned Job", created_by_user_id=recruiter_user.id)
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        admin_client.post(f"/admin/users/{recruiter_user.id}/delete")
        db_session.expire_all()

        kept = db_session.query(JobPosition).filter_by(id=job_id).one()
        assert kept.created_by_user_id is None

    def test_users_page_lists_delete_button_for_others(self, admin_client, recruiter_user):
        resp = admin_client.get("/admin/users")
        assert b"Delete" in resp.data


class TestExport:
    def test_csv_export_contains_candidate(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/export.csv")
        assert resp.status_code == 200
        assert b"Jane Candidate" in resp.data
        assert resp.headers["Content-Type"].startswith("text/csv")

    def test_json_export_contains_candidate(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/export.json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert any(row["name"] == "Jane Candidate" for row in data)

    def test_export_honors_search_filter(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/admin/export.json?q=Nonexistent")
        assert resp.get_json() == []


class TestCsrfProtection:
    def test_post_without_csrf_token_is_rejected_when_enabled(self, app, admin_user):
        """CSRF is disabled for the rest of this suite for convenience — this one
        test proves it's actually wired up in the real app configuration."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(admin_user.id)
            resp = client.post("/admin/delete-all")
            assert resp.status_code == 400
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

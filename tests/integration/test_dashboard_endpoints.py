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

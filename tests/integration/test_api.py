"""Integration tests for the JSON REST API (webapp/api.py): auth (401/403
via require_api_permission, not an HTML redirect), validation, pagination,
filtering, and the consistent JSON error envelope.
"""

from __future__ import annotations

import io

import pytest

from screener.models import Evaluation as EvaluationResult
from webapp import store
from webapp.db import new_session


@pytest.fixture
def job_id(sample_jd):
    with new_session() as s:
        return store.get_or_create_job_position(s, sample_jd).id


@pytest.fixture
def evaluated_candidate(job_id):
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


@pytest.fixture(autouse=True)
def _mock_queue(mocker):
    """POST /api/resumes enqueues just like /apply — never touch real Redis in tests."""
    fake_job = mocker.Mock(id="fake-rq-job-id")
    fake_queue = mocker.Mock()
    fake_queue.enqueue.return_value = fake_job
    return mocker.patch("webapp.submissions.get_queue", return_value=fake_queue)


class TestAuth:
    def test_anonymous_gets_401_json_not_redirect(self, client):
        resp = client.get("/api/dashboard")
        assert resp.status_code == 401
        assert resp.get_json()["error"]["code"] == "unauthorized"

    def test_viewer_can_read(self, viewer_client, job_id):
        assert viewer_client.get("/api/dashboard").status_code == 200
        assert viewer_client.get("/api/jobs").status_code == 200
        assert viewer_client.get("/api/applicants").status_code == 200
        assert viewer_client.get("/api/evaluations").status_code == 200

    def test_viewer_cannot_create_job_gets_403_json(self, viewer_client):
        resp = viewer_client.post("/api/jobs", json={"title": "New Role"})
        assert resp.status_code == 403
        assert resp.get_json()["error"]["code"] == "forbidden"

    def test_recruiter_can_create_job(self, recruiter_client):
        resp = recruiter_client.post("/api/jobs", json={"title": "Recruiter-created Role"})
        assert resp.status_code == 201

    def test_csrf_exempt_unlike_html_routes(self, app, admin_user):
        """The whole point of exempting the API blueprint: a JSON POST works
        without a CSRF token, even with CSRF protection enabled app-wide."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(admin_user.id)
            resp = client.post("/api/jobs", json={"title": "No CSRF Token Needed"})
            assert resp.status_code == 201
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestJobsEndpoint:
    def test_create_job_returns_201_with_body(self, admin_client):
        resp = admin_client.post(
            "/api/jobs",
            json={"title": "Data Analyst", "required_skills": ["SQL", "Excel"], "min_years_experience": 2},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["title"] == "Data Analyst"
        assert body["required_skills"] == ["SQL", "Excel"]
        assert "id" in body

    def test_create_job_missing_title_gives_validation_error(self, admin_client):
        resp = admin_client.post("/api/jobs", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"]["code"] == "validation_error"
        assert "title" in body["error"]["fields"]

    def test_create_job_rejects_non_json_body(self, admin_client):
        resp = admin_client.post("/api/jobs", data="not json")
        assert resp.status_code == 400

    def test_create_job_rejects_negative_experience(self, admin_client):
        resp = admin_client.post("/api/jobs", json={"title": "Role", "min_years_experience": -1})
        assert resp.status_code == 400
        assert "min_years_experience" in resp.get_json()["error"]["fields"]

    def test_create_job_rejects_invalid_status(self, admin_client):
        resp = admin_client.post("/api/jobs", json={"title": "Role", "status": "bogus"})
        assert resp.status_code == 400

    def test_get_job_by_id(self, admin_client, job_id):
        resp = admin_client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["id"] == job_id

    def test_get_job_404_for_unknown_id(self, admin_client):
        resp = admin_client.get("/api/jobs/999999")
        assert resp.status_code == 404
        assert resp.get_json()["error"]["code"] == "not_found"

    def test_list_jobs_filters_by_status(self, admin_client, job_id):
        admin_client.post("/api/jobs", json={"title": "Closed Role", "status": "closed"})
        resp = admin_client.get("/api/jobs?status=closed")
        titles = [j["title"] for j in resp.get_json()["data"]]
        assert "Closed Role" in titles
        assert not any(t != "Closed Role" for t in titles)  # only closed jobs returned

    def test_list_jobs_rejects_invalid_status_filter(self, admin_client):
        resp = admin_client.get("/api/jobs?status=bogus")
        assert resp.status_code == 400


class TestPagination:
    def test_pagination_metadata_present(self, admin_client, job_id):
        for i in range(5):
            admin_client.post("/api/jobs", json={"title": f"Role {i}"})
        resp = admin_client.get("/api/jobs?page=1&per_page=2")
        body = resp.get_json()
        assert len(body["data"]) == 2
        assert body["pagination"]["page"] == 1
        assert body["pagination"]["per_page"] == 2
        assert body["pagination"]["total"] >= 6  # 5 created + the seeded job

    def test_per_page_capped_at_max(self, admin_client):
        resp = admin_client.get("/api/jobs?per_page=99999")
        assert resp.get_json()["pagination"]["per_page"] == 100

    def test_invalid_page_param_rejected(self, admin_client):
        resp = admin_client.get("/api/jobs?page=0")
        assert resp.status_code == 400

    def test_out_of_range_page_clamped_not_error(self, admin_client, job_id):
        resp = admin_client.get("/api/jobs?page=999")
        assert resp.status_code == 200


class TestResumesEndpoint:
    def test_create_resume_returns_202_with_status_url(self, admin_client, job_id):
        resp = admin_client.post(
            "/api/resumes",
            data={"name": "Jane Doe", "job_id": str(job_id), "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["status"] == "pending"
        assert body["status_url"] == f"/api/resumes/{body['public_id']}"

    def test_create_resume_requires_job_id(self, admin_client):
        resp = admin_client.post(
            "/api/resumes",
            data={"name": "Jane Doe", "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "job_id" in resp.get_json()["error"]["fields"]

    def test_create_resume_404_for_unknown_job(self, admin_client):
        resp = admin_client.post(
            "/api/resumes",
            data={"name": "Jane Doe", "job_id": "999999", "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404

    def test_create_resume_rejects_non_pdf(self, admin_client, job_id):
        resp = admin_client.post(
            "/api/resumes",
            data={"name": "Jane Doe", "job_id": str(job_id), "resume": (io.BytesIO(b"hello"), "resume.docx")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_viewer_cannot_create_resume(self, viewer_client, job_id):
        resp = viewer_client.post(
            "/api/resumes",
            data={"name": "Jane Doe", "job_id": str(job_id), "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 403

    def test_get_resume_detail(self, admin_client, evaluated_candidate):
        resp = admin_client.get(f"/api/resumes/{evaluated_candidate}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["name"] == "Jane Candidate"
        assert body["overall"] == 70
        assert "card_doc" not in body  # HTML fragment stripped from the API response

    def test_get_resume_404_for_unknown_id(self, admin_client):
        resp = admin_client.get("/api/resumes/does-not-exist")
        assert resp.status_code == 404


class TestApplicantsEndpoint:
    def test_search_filters_by_name(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/api/applicants?q=Jane")
        assert any(c["name"] == "Jane Candidate" for c in resp.get_json()["data"])
        resp = admin_client.get("/api/applicants?q=Nonexistent")
        assert resp.get_json()["data"] == []

    def test_filter_by_recruiter_status(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/api/applicants?status=rejected")
        assert resp.get_json()["data"] == []
        resp = admin_client.get("/api/applicants?status=new")
        assert len(resp.get_json()["data"]) == 1

    def test_invalid_status_filter_rejected(self, admin_client):
        resp = admin_client.get("/api/applicants?status=bogus")
        assert resp.status_code == 400


class TestEvaluationsEndpoint:
    def test_only_evaluated_resumes_returned(self, admin_client, job_id, evaluated_candidate):
        with new_session() as s:
            store.create_submission(
                s,
                job_position_id=job_id,
                applicant_name="Not Yet Evaluated",
                original_filename="x.pdf",
                storage_path="/tmp/x.pdf",
            )
        resp = admin_client.get("/api/evaluations")
        names = [c["name"] for c in resp.get_json()["data"]]
        assert "Jane Candidate" in names
        assert "Not Yet Evaluated" not in names

    def test_filter_by_min_score(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/api/evaluations?min_score=80")
        assert resp.get_json()["data"] == []
        resp = admin_client.get("/api/evaluations?min_score=50")
        assert len(resp.get_json()["data"]) == 1

    def test_filter_by_backend(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/api/evaluations?backend=claude")
        assert resp.get_json()["data"] == []
        resp = admin_client.get("/api/evaluations?backend=ollama")
        assert len(resp.get_json()["data"]) == 1

    def test_invalid_score_filter_rejected(self, admin_client):
        resp = admin_client.get("/api/evaluations?min_score=999")
        assert resp.status_code == 400


class TestDashboardEndpoint:
    def test_returns_overview_stats(self, admin_client, evaluated_candidate):
        resp = admin_client.get("/api/dashboard")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_applicants"] >= 1
        assert body["completed_evaluations"] >= 1


class TestErrorEnvelope:
    def test_404_on_unknown_route_returns_json(self, admin_client):
        resp = admin_client.get("/api/does-not-exist")
        assert resp.status_code == 404
        assert resp.get_json()["error"]["code"] == "not_found"

    def test_405_on_wrong_method_returns_json(self, admin_client):
        resp = admin_client.delete("/api/dashboard")
        assert resp.status_code == 405
        assert "error" in resp.get_json()

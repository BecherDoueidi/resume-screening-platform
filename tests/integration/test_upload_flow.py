"""Integration tests for the upload flow: POST /apply -> immediate response,
Resume + ProcessingJob rows created, job handed to the queue (mocked — no
real Redis needed). Background processing itself is covered separately in
test_processing_flow.py.
"""

from __future__ import annotations

import io

import pytest

from webapp import store
from webapp.db import new_session
from webapp.models_db import ProcessingJob, Resume


@pytest.fixture(autouse=True)
def _mock_queue(mocker):
    """Every test in this module gets a fake queue — /apply must never touch real Redis."""
    fake_job = mocker.Mock(id="fake-rq-job-id")
    fake_queue = mocker.Mock()
    fake_queue.enqueue.return_value = fake_job
    return mocker.patch("webapp.submissions.get_queue", return_value=fake_queue)


@pytest.fixture
def job_id(sample_jd):
    with new_session() as s:
        return store.get_or_create_job_position(s, sample_jd).id


def _upload(client, job_id, name="Jane Doe", filename="resume.pdf", content=b"%PDF-1.4 fake pdf content"):
    return client.post(
        "/apply",
        data={"name": name, "job_id": job_id, "resume": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


class TestApplyRoute:
    def test_returns_200_immediately(self, client, job_id):
        resp = _upload(client, job_id)
        assert resp.status_code == 200

    def test_response_contains_public_id_for_polling(self, client, job_id):
        resp = _upload(client, job_id)
        assert b"data-public-id=" in resp.data

    def test_creates_applicant_resume_and_pending_job(self, client, job_id, db_session):
        _upload(client, job_id, name="Jane Doe")

        resumes = db_session.query(Resume).all()
        assert len(resumes) == 1
        assert resumes[0].applicant.full_name == "Jane Doe"
        assert resumes[0].status == "pending"

        jobs = db_session.query(ProcessingJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "pending"
        assert jobs[0].progress == 0

    def test_enqueues_with_retry_and_stores_rq_job_id(self, client, job_id, db_session, _mock_queue):
        _upload(client, job_id)

        fake_queue = _mock_queue.return_value
        fake_queue.enqueue.assert_called_once()
        _, kwargs = fake_queue.enqueue.call_args
        assert kwargs["retry"] is not None

        job = db_session.query(ProcessingJob).one()
        assert job.rq_job_id == "fake-rq-job-id"

    def test_enqueue_failure_marks_job_failed_instead_of_stuck_pending(self, client, job_id, db_session, _mock_queue):
        """If Redis is unreachable, the Resume/ProcessingJob rows are already
        committed by the time enqueue() runs — without this handling they'd
        be stuck at "pending" forever with no visible error."""
        fake_queue = _mock_queue.return_value
        fake_queue.enqueue.side_effect = ConnectionError("Redis is unreachable")

        resp = _upload(client, job_id)

        assert resp.status_code == 400
        assert b"Could not queue your resume" in resp.data

        job = db_session.query(ProcessingJob).one()
        assert job.status == "failed"
        assert job.error == "Redis is unreachable"

        resume = db_session.query(Resume).one()
        assert resume.status == "failed"
        assert "Redis is unreachable" in resume.parse_error

    def test_rejects_missing_file(self, client, job_id):
        resp = client.post("/apply", data={"name": "Jane Doe", "job_id": job_id}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert b"choose a PDF" in resp.data

    def test_rejects_non_pdf_extension(self, client, job_id):
        resp = client.post(
            "/apply",
            data={"name": "Jane Doe", "job_id": job_id, "resume": (io.BytesIO(b"hello"), "resume.docx")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"Only PDF" in resp.data

    def test_rejects_missing_job_id(self, client):
        resp = client.post(
            "/apply",
            data={"name": "Jane Doe", "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"select an open position" in resp.data

    def test_rejects_unknown_job_id(self, client):
        resp = client.post(
            "/apply",
            data={"name": "Jane Doe", "job_id": 999999, "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"select an open position" in resp.data

    def test_rejects_closed_job_id(self, client, db_session):
        with new_session() as s:
            job = store.create_job_position(s, title="Closed Role", status="closed")
            closed_id = job.id
        resp = client.post(
            "/apply",
            data={"name": "Jane Doe", "job_id": closed_id, "resume": (io.BytesIO(b"%PDF-1.4 fake"), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"select an open position" in resp.data

    def test_blank_name_falls_back_to_applicant(self, client, job_id, db_session):
        _upload(client, job_id, name="   ")
        resume = db_session.query(Resume).one()
        assert resume.applicant.full_name == "Applicant"

    def test_overly_long_name_is_truncated(self, client, job_id, db_session):
        _upload(client, job_id, name="A" * 500)
        resume = db_session.query(Resume).one()
        assert len(resume.applicant.full_name) == 200

    def test_original_filename_preserved(self, client, job_id, db_session):
        _upload(client, job_id, filename="my_resume_final_v2.pdf")
        resume = db_session.query(Resume).one()
        assert resume.original_filename == "my_resume_final_v2.pdf"


class TestIndexRoute:
    def test_lists_active_job_as_an_option(self, client, job_id):
        resp = client.get("/")
        assert resp.status_code == 200
        assert f'value="{job_id}"'.encode() in resp.data

    def test_hides_closed_job_from_the_picker(self, client, db_session):
        with new_session() as s:
            job = store.create_job_position(s, title="Closed Role Hidden", status="closed")
            closed_id = job.id
        resp = client.get("/")
        assert f'value="{closed_id}"'.encode() not in resp.data

    def test_shows_no_openings_message_when_nothing_active(self, client, db_session):
        with new_session() as s:
            for j in store.list_job_positions(s):
                j.status = "closed"
            s.commit()
        resp = client.get("/")
        assert b"No open positions" in resp.data


class TestStatusEndpoint:
    def test_returns_pending_status_for_new_submission(self, client, job_id):
        upload_resp = _upload(client, job_id)
        public_id = upload_resp.data.decode().split('data-public-id="')[1].split('"')[0]

        resp = client.get(f"/status/{public_id}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "pending"
        assert data["progress"] == 0

    def test_returns_404_for_unknown_id(self, client):
        resp = client.get("/status/does-not-exist")
        assert resp.status_code == 404

    def test_completed_status_does_not_expose_the_evaluation(self, client, job_id, db_session):
        """The candidate-facing polling endpoint must never leak the AI's
        score, justification, or interview questions — only that the
        submission was received and its pipeline status."""
        from screener.models import Evaluation as EvaluationResult

        upload_resp = _upload(client, job_id)
        public_id = upload_resp.data.decode().split('data-public-id="')[1].split('"')[0]
        resume = db_session.query(Resume).one()
        job = db_session.query(ProcessingJob).one()

        store.save_evaluation(
            db_session,
            resume.id,
            job.id,
            backend="ollama",
            evaluation=EvaluationResult(
                skill_match=90,
                experience_relevance=80,
                project_impact=70,
                overall=80,
                justification="Secret justification text.",
                gaps=[],
                interview_questions=["Secret question?"],
            ),
            card_html="<div>Secret score card</div>",
        )

        resp = client.get(f"/status/{public_id}")
        data = resp.get_json()
        assert data["status"] in ("pending", "processing", "completed")
        assert "overall" not in data
        assert "card_doc" not in data
        assert "skill_match" not in data
        assert "justification" not in data

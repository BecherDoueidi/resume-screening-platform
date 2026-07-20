"""Integration tests for the upload flow: POST /apply -> immediate response,
Resume + ProcessingJob rows created, job handed to the queue (mocked — no
real Redis needed). Background processing itself is covered separately in
test_processing_flow.py.
"""

from __future__ import annotations

import io

import pytest

from webapp.models_db import ProcessingJob, Resume


@pytest.fixture(autouse=True)
def _mock_queue(mocker):
    """Every test in this module gets a fake queue — /apply must never touch real Redis."""
    fake_job = mocker.Mock(id="fake-rq-job-id")
    fake_queue = mocker.Mock()
    fake_queue.enqueue.return_value = fake_job
    return mocker.patch("webapp.submissions.get_queue", return_value=fake_queue)


def _upload(client, name="Jane Doe", filename="resume.pdf", content=b"%PDF-1.4 fake pdf content"):
    return client.post(
        "/apply",
        data={"name": name, "resume": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


class TestApplyRoute:
    def test_returns_200_immediately(self, client):
        resp = _upload(client)
        assert resp.status_code == 200

    def test_response_contains_public_id_for_polling(self, client):
        resp = _upload(client)
        assert b"data-public-id=" in resp.data

    def test_creates_applicant_resume_and_pending_job(self, client, db_session):
        _upload(client, name="Jane Doe")

        resumes = db_session.query(Resume).all()
        assert len(resumes) == 1
        assert resumes[0].applicant.full_name == "Jane Doe"
        assert resumes[0].status == "pending"

        jobs = db_session.query(ProcessingJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "pending"
        assert jobs[0].progress == 0

    def test_enqueues_with_retry_and_stores_rq_job_id(self, client, db_session, _mock_queue):
        _upload(client)

        fake_queue = _mock_queue.return_value
        fake_queue.enqueue.assert_called_once()
        _, kwargs = fake_queue.enqueue.call_args
        assert kwargs["retry"] is not None

        job = db_session.query(ProcessingJob).one()
        assert job.rq_job_id == "fake-rq-job-id"

    def test_rejects_missing_file(self, client):
        resp = client.post("/apply", data={"name": "Jane Doe"}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert b"choose a PDF" in resp.data

    def test_rejects_non_pdf_extension(self, client):
        resp = client.post(
            "/apply",
            data={"name": "Jane Doe", "resume": (io.BytesIO(b"hello"), "resume.docx")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"Only PDF" in resp.data

    def test_blank_name_falls_back_to_applicant(self, client, db_session):
        _upload(client, name="   ")
        resume = db_session.query(Resume).one()
        assert resume.applicant.full_name == "Applicant"

    def test_overly_long_name_is_truncated(self, client, db_session):
        _upload(client, name="A" * 500)
        resume = db_session.query(Resume).one()
        assert len(resume.applicant.full_name) == 200

    def test_original_filename_preserved(self, client, db_session):
        _upload(client, filename="my_resume_final_v2.pdf")
        resume = db_session.query(Resume).one()
        assert resume.original_filename == "my_resume_final_v2.pdf"


class TestStatusEndpoint:
    def test_returns_pending_status_for_new_submission(self, client):
        upload_resp = _upload(client)
        public_id = upload_resp.data.decode().split('data-public-id="')[1].split('"')[0]

        resp = client.get(f"/status/{public_id}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "pending"
        assert data["progress"] == 0

    def test_returns_404_for_unknown_id(self, client):
        resp = client.get("/status/does-not-exist")
        assert resp.status_code == 404

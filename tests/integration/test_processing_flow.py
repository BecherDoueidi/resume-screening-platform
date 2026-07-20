"""Integration tests for webapp.tasks.process_resume — the background worker
pipeline (parse -> anonymize -> evaluate -> save), including the retry /
error-recovery logic. AI calls are mocked; PDFs are real (via make_pdf)."""

from __future__ import annotations

from unittest import mock

import pytest

from screener.models import Evaluation as EvaluationResult
from webapp import store, tasks
from webapp.db import new_session
from webapp.models_db import AuditLog, Evaluation, ProcessingJob, Resume


@pytest.fixture
def job_position_id(sample_jd):
    # Deliberately its own session, not the test's `db_session` — otherwise the
    # created row lands in db_session's identity map and later `db_session.get()`
    # calls in the test return the stale pre-processing object instead of
    # re-querying the row this test's assertions actually care about.
    with new_session() as s:
        return store.get_or_create_job_position(s, sample_jd).id


@pytest.fixture
def submitted_resume(job_position_id, make_pdf):
    pdf_path = make_pdf(["John Smith", "john.smith@example.com", "10 years Python experience."])
    with new_session() as s:
        resume, job = store.create_submission(
            s,
            job_position_id=job_position_id,
            applicant_name="John Smith",
            original_filename="resume.pdf",
            storage_path=str(pdf_path),
        )
        return resume.id, job.id


def _fake_evaluation(**overrides):
    defaults = dict(
        skill_match=80,
        experience_relevance=70,
        project_impact=60,
        overall=70,
        justification="Strong backend experience.",
        gaps=[],
        interview_questions=["Q1?"],
        error="",
    )
    defaults.update(overrides)
    return EvaluationResult(**defaults)


class TestSuccessPath:
    def test_marks_resume_and_job_completed(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=False)
        mocker.patch("webapp.tasks.evaluate_ollama.has_ollama", return_value=True)
        mocker.patch("webapp.tasks.evaluate_ollama.evaluate_resume", return_value=_fake_evaluation())

        tasks.process_resume(resume_id, job_id)

        resume = db_session.get(Resume, resume_id)
        job = db_session.get(ProcessingJob, job_id)
        assert resume.status == "evaluated"
        assert job.status == "completed"
        assert job.progress == 100
        assert job.attempts == 1

    def test_saves_evaluation_scores(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=False)
        mocker.patch("webapp.tasks.evaluate_ollama.has_ollama", return_value=True)
        mocker.patch(
            "webapp.tasks.evaluate_ollama.evaluate_resume",
            return_value=_fake_evaluation(skill_match=85, experience_relevance=75, project_impact=65),
        )

        tasks.process_resume(resume_id, job_id)

        evaluation = db_session.query(Evaluation).filter_by(resume_id=resume_id).one()
        assert evaluation.skill_match == 85
        assert evaluation.experience_relevance == 75
        assert evaluation.project_impact == 65
        assert evaluation.backend == "ollama"

    def test_saves_anonymized_text_and_redactions(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=False)
        mocker.patch("webapp.tasks.evaluate_ollama.has_ollama", return_value=True)
        mocker.patch("webapp.tasks.evaluate_ollama.evaluate_resume", return_value=_fake_evaluation())

        tasks.process_resume(resume_id, job_id)

        resume = db_session.get(Resume, resume_id)
        assert resume.anonymized_text
        assert "John Smith" not in resume.anonymized_text
        audit_rows = db_session.query(AuditLog).filter_by(resume_id=resume_id, action="anonymize").all()
        assert len(audit_rows) > 0

    def test_prefers_claude_backend_when_credentials_available(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=True)
        mocker.patch("webapp.tasks.evaluate.build_client", return_value=mocker.Mock())
        claude_eval = mocker.patch("webapp.tasks.evaluate.evaluate_resume", return_value=_fake_evaluation())
        ollama_eval = mocker.patch("webapp.tasks.evaluate_ollama.evaluate_resume")

        tasks.process_resume(resume_id, job_id)

        claude_eval.assert_called_once()
        ollama_eval.assert_not_called()
        job = db_session.get(ProcessingJob, job_id)
        assert job.backend == "claude"

    def test_progress_reaches_100_percent(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=False)
        mocker.patch("webapp.tasks.evaluate_ollama.has_ollama", return_value=True)
        mocker.patch("webapp.tasks.evaluate_ollama.evaluate_resume", return_value=_fake_evaluation())

        tasks.process_resume(resume_id, job_id)

        job = db_session.get(ProcessingJob, job_id)
        assert job.progress == 100
        assert job.progress_message == "Completed"


class TestNoBackendAvailable:
    def test_marks_failed_when_no_engine_available(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.evaluate.has_api_credentials", return_value=False)
        mocker.patch("webapp.tasks.evaluate_ollama.has_ollama", return_value=False)

        tasks.process_resume(resume_id, job_id)

        resume = db_session.get(Resume, resume_id)
        job = db_session.get(ProcessingJob, job_id)
        assert resume.status == "failed"
        assert job.status == "failed"


class TestParseErrorHandling:
    def test_corrupt_pdf_fails_immediately_without_retry(self, mocker, db_session, job_position_id, tmp_path):
        bad_pdf = tmp_path / "corrupt.pdf"
        bad_pdf.write_text("this is not a real pdf")
        resume, job = store.create_submission(
            db_session,
            job_position_id=job_position_id,
            applicant_name="Bad Candidate",
            original_filename="corrupt.pdf",
            storage_path=str(bad_pdf),
        )
        evaluate_spy = mocker.patch("webapp.tasks.evaluate_ollama.evaluate_resume")

        tasks.process_resume(resume.id, job.id)

        db_session.refresh(resume)
        db_session.refresh(job)
        assert resume.status == "failed"
        assert job.status == "failed"
        assert job.attempts == 1  # a parse error never even reaches the retry logic
        evaluate_spy.assert_not_called()


class TestRetryLogic:
    def test_transient_failure_with_retries_left_stays_pending(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        fake_job = mock.Mock(retries_left=2)
        mocker.patch("webapp.tasks.get_current_job", return_value=fake_job)
        mocker.patch("webapp.tasks.anonymize.anonymize_resume", side_effect=RuntimeError("simulated DB hiccup"))

        with pytest.raises(RuntimeError):
            tasks.process_resume(resume_id, job_id)

        job = db_session.get(ProcessingJob, job_id)
        assert job.status == "pending"  # left retryable
        assert "simulated DB hiccup" in job.error
        assert job.attempts == 1

    def test_exhausted_retries_marks_definitively_failed(self, mocker, db_session, submitted_resume):
        resume_id, job_id = submitted_resume
        fake_job = mock.Mock(retries_left=0)
        mocker.patch("webapp.tasks.get_current_job", return_value=fake_job)
        mocker.patch("webapp.tasks.anonymize.anonymize_resume", side_effect=RuntimeError("final failure"))

        with pytest.raises(RuntimeError):
            tasks.process_resume(resume_id, job_id)

        resume = db_session.get(Resume, resume_id)
        job = db_session.get(ProcessingJob, job_id)
        assert job.status == "failed"
        assert resume.status == "failed"

    def test_exception_always_reraised_for_rq_to_see(self, mocker, db_session, submitted_resume):
        """RQ's own Retry bookkeeping needs the exception to propagate regardless
        of whether our own retryable/failed distinction says it's retryable."""
        resume_id, job_id = submitted_resume
        mocker.patch("webapp.tasks.get_current_job", return_value=None)
        mocker.patch("webapp.tasks.anonymize.anonymize_resume", side_effect=ValueError("boom"))

        with pytest.raises(ValueError, match="boom"):
            tasks.process_resume(resume_id, job_id)

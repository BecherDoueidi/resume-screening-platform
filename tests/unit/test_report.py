"""Unit tests for screener.report — HTML candidate cards and the batch report writer."""

from __future__ import annotations

import json

from screener.models import CandidateResult, Evaluation, JobDescription, RedactionRecord, Resume
from screener.report import candidate_card_html, write_report


def _evaluated_result(overall=80, error=""):
    resume = Resume(source_path="/tmp/jane.pdf", candidate_id="Jane Doe")
    evaluation = Evaluation(
        skill_match=85,
        experience_relevance=75,
        project_impact=70,
        overall=overall,
        justification="Strong evidence of relevant experience.",
        gaps=["No Kubernetes experience"],
        interview_questions=["Describe a scaling challenge you solved."],
        error=error,
    )
    return CandidateResult(resume=resume, evaluation=None if error else evaluation)


class TestCandidateCardHtml:
    def test_renders_score_and_justification(self):
        html_out = candidate_card_html(1, _evaluated_result(overall=80))
        assert "#1" in html_out
        assert "Jane Doe" in html_out
        assert "80" in html_out
        assert "Strong evidence" in html_out

    def test_renders_gaps_and_interview_questions(self):
        html_out = candidate_card_html(1, _evaluated_result())
        assert "No Kubernetes experience" in html_out
        assert "Describe a scaling challenge" in html_out

    def test_renders_error_message_when_evaluation_failed(self):
        resume = Resume(source_path="/tmp/x.pdf", candidate_id="X")
        result = CandidateResult(resume=resume, evaluation=Evaluation(0, 0, 0, 0, "", error="Rate limited"))
        html_out = candidate_card_html(1, result)
        assert "Rate limited" in html_out
        assert "err" in html_out

    def test_renders_parse_error_when_no_evaluation_at_all(self):
        resume = Resume(source_path="/tmp/x.pdf", candidate_id="X", parse_error="Failed to parse PDF: corrupt")
        result = CandidateResult(resume=resume, evaluation=None)
        html_out = candidate_card_html(1, result)
        assert "Failed to parse PDF" in html_out

    def test_escapes_html_in_candidate_name(self):
        resume = Resume(source_path="/tmp/x.pdf", candidate_id="<script>alert(1)</script>")
        result = CandidateResult(resume=resume, evaluation=None)
        html_out = candidate_card_html(1, result)
        assert "<script>alert(1)</script>" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_renders_redaction_audit_when_present(self):
        result = _evaluated_result()
        result.resume.redactions = [RedactionRecord(kind="person_name", replacement="[CANDIDATE]", count=2)]
        html_out = candidate_card_html(1, result)
        assert "Bias-mitigation audit: 2 redactions" in html_out
        assert "person name" in html_out


class TestWriteReport:
    def test_writes_json_and_html_files(self, tmp_path):
        results = [_evaluated_result(overall=90), _evaluated_result(overall=60)]
        jd = JobDescription(title="Backend Engineer")

        json_path, html_path = write_report(jd, results, tmp_path)

        assert json_path.exists()
        assert html_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["candidates_evaluated"] == 2
        assert data["job_title"] == "Backend Engineer"

    def test_ranks_by_overall_score_descending(self, tmp_path):
        results = [_evaluated_result(overall=50), _evaluated_result(overall=95)]
        jd = JobDescription(title="Role")

        json_path, _ = write_report(jd, results, tmp_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        scores = [r["evaluation"]["overall"] for r in data["ranking"]]
        assert scores == sorted(scores, reverse=True)

    def test_skipped_candidates_excluded_from_ranking(self, tmp_path):
        ok = _evaluated_result(overall=80)
        failed = _evaluated_result(error="Rate limited")
        jd = JobDescription(title="Role")

        json_path, _ = write_report(jd, [ok, failed], tmp_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["candidates_evaluated"] == 1
        assert data["candidates_skipped"] == 1

    def test_top_n_limits_shown_candidates_in_html(self, tmp_path):
        results = [_evaluated_result(overall=90 - i) for i in range(5)]
        jd = JobDescription(title="Role")

        _, html_path = write_report(jd, results, tmp_path, top=2)

        html_text = html_path.read_text(encoding="utf-8")
        assert html_text.count('class="rank"') == 2

"""Unit tests for screener.evaluate_ollama — the free local Ollama backend.

All HTTP calls are mocked; no Ollama server needs to be running.
"""

from __future__ import annotations

import json

import requests

from screener import evaluate_ollama
from screener.models import Resume


def _fake_ok_response(content=None):
    payload = content or json.dumps(
        {
            "skill_match": 65,
            "experience_relevance": 55,
            "project_impact": 45,
            "justification": "Reasonable backend experience for the role.",
            "gaps": ["No cloud experience"],
            "interview_questions": ["Tell me about a production incident."],
        }
    )
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({"message": {"content": payload}}).encode("utf-8")
    return resp


class TestHasOllama:
    def test_true_when_reachable(self, mocker):
        mocker.patch("screener.evaluate_ollama.requests.get", return_value=mocker.Mock(status_code=200))
        assert evaluate_ollama.has_ollama() is True

    def test_retries_then_succeeds(self, mocker):
        mocker.patch("screener.evaluate_ollama.time.sleep")
        mocker.patch(
            "screener.evaluate_ollama.requests.get",
            side_effect=[requests.ConnectionError(), mocker.Mock(status_code=200)],
        )
        assert evaluate_ollama.has_ollama(retries=2) is True

    def test_false_after_exhausting_retries(self, mocker):
        mocker.patch("screener.evaluate_ollama.time.sleep")
        mocker.patch("screener.evaluate_ollama.requests.get", side_effect=requests.ConnectionError())
        assert evaluate_ollama.has_ollama(retries=2) is False


class TestEvaluateResume:
    def test_successful_evaluation(self, mocker, sample_jd):
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="Backend engineer.")
        mocker.patch("screener.evaluate_ollama.requests.post", return_value=_fake_ok_response())

        result = evaluate_ollama.evaluate_resume(sample_jd, resume)

        assert result.error == ""
        assert result.skill_match == 65
        assert result.overall == evaluate_ollama.compute_overall(65, 55, 45)
        assert result.gaps == ["No cloud experience"]

    def test_connection_error_returns_actionable_message(self, mocker, sample_jd):
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="x")
        mocker.patch("screener.evaluate_ollama.requests.post", side_effect=requests.ConnectionError())

        result = evaluate_ollama.evaluate_resume(sample_jd, resume)

        assert "Could not reach Ollama" in result.error
        assert result.overall == 0

    def test_timeout_returns_error(self, mocker, sample_jd):
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="x")
        mocker.patch("screener.evaluate_ollama.requests.post", side_effect=requests.Timeout())

        result = evaluate_ollama.evaluate_resume(sample_jd, resume)

        assert "timed out" in result.error

    def test_malformed_json_returns_error(self, mocker, sample_jd):
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="x")
        mocker.patch(
            "screener.evaluate_ollama.requests.post",
            return_value=_fake_ok_response(content="not json{"),
        )

        result = evaluate_ollama.evaluate_resume(sample_jd, resume)

        assert "Unexpected response shape" in result.error

    def test_http_error_returns_error(self, mocker, sample_jd):
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="x")
        bad_resp = requests.Response()
        bad_resp.status_code = 500
        mocker.patch("screener.evaluate_ollama.requests.post", return_value=bad_resp)

        result = evaluate_ollama.evaluate_resume(sample_jd, resume)

        assert "Ollama error" in result.error

    def test_uses_deterministic_sampling_options(self, mocker, sample_jd):
        """Regression test: temperature=0 + fixed seed so identical resumes get
        identical scores (this was a real bug — see conversation history)."""
        resume = Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="x")
        mock_post = mocker.patch("screener.evaluate_ollama.requests.post", return_value=_fake_ok_response())

        evaluate_ollama.evaluate_resume(sample_jd, resume)

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["options"]["temperature"] == 0.0
        assert kwargs["json"]["options"]["seed"] == 42

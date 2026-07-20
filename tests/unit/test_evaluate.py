"""Unit tests for screener.evaluate — the Claude evaluation backend.

All Anthropic API calls are mocked; no network access, no API key needed.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from screener import evaluate
from screener.models import Resume

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_response(stop_reason="end_turn", text=None):
    payload = text or json.dumps(
        {
            "skill_match": 80,
            "experience_relevance": 70,
            "project_impact": 60,
            "justification": "Strong evidence of relevant backend experience.",
            "gaps": ["No Kubernetes experience mentioned"],
            "interview_questions": ["Describe a time you optimized a slow query."],
        }
    )
    return type("FakeResponse", (), {"stop_reason": stop_reason, "content": [type("Block", (), {"text": payload})()]})()


@pytest.fixture
def resume():
    return Resume(source_path="x.pdf", candidate_id="c1", anonymized_text="Backend engineer, [LOCATION].")


class TestHasApiCredentials:
    def test_true_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert evaluate.has_api_credentials() is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert evaluate.has_api_credentials() is False


class TestEvaluateResume:
    def test_successful_evaluation(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.return_value = _fake_response()

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert result.error == ""
        assert result.skill_match == 80
        assert result.experience_relevance == 70
        assert result.project_impact == 60
        # overall is computed deterministically, never taken from the model's response.
        assert result.overall == evaluate.compute_overall(80, 70, 60)
        assert "backend experience" in result.justification
        assert result.gaps == ["No Kubernetes experience mentioned"]

    def test_model_refusal_returns_error(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.return_value = _fake_response(stop_reason="refusal")

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert result.error == "Model declined to evaluate this resume"
        assert result.overall == 0

    def test_rate_limit_retries_then_succeeds(self, mocker, sample_jd, resume):
        mocker.patch("screener.evaluate.time.sleep")  # skip the real 30s backoff
        client = mocker.Mock()
        client.messages.create.side_effect = [
            anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=_REQUEST), body=None),
            _fake_response(),
        ]

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert result.error == ""
        assert result.skill_match == 80
        assert client.messages.create.call_count == 2

    def test_rate_limit_exhausted_returns_error(self, mocker, sample_jd, resume):
        mocker.patch("screener.evaluate.time.sleep")
        client = mocker.Mock()
        client.messages.create.side_effect = anthropic.RateLimitError(
            "rate limited", response=httpx.Response(429, request=_REQUEST), body=None
        )

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert result.error == "Rate limited after retries"

    def test_api_status_error_returns_error(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.side_effect = anthropic.APIStatusError(
            "server error", response=httpx.Response(500, request=_REQUEST), body=None
        )

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert "API error 500" in result.error

    def test_connection_error_returns_error(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.side_effect = anthropic.APIConnectionError(request=_REQUEST)

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert "Network error" in result.error

    def test_malformed_json_returns_error(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.return_value = _fake_response(text="not valid json{")

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert "Unexpected response shape" in result.error

    def test_missing_required_field_returns_error(self, mocker, sample_jd, resume):
        client = mocker.Mock()
        client.messages.create.return_value = _fake_response(text=json.dumps({"skill_match": 50}))

        result = evaluate.evaluate_resume(client, sample_jd, resume)

        assert "Unexpected response shape" in result.error

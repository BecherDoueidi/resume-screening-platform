"""Unit tests for screener.models.compute_overall — the deterministic scoring formula.

`overall` is never the model's own guess (see evaluate.py/evaluate_ollama.py) —
it's always this weighted average: 35% skill match, 35% experience relevance,
30% project impact.
"""

from __future__ import annotations

from screener.models import OVERALL_WEIGHTS, compute_overall


class TestComputeOverall:
    def test_weights_sum_to_one(self):
        assert sum(OVERALL_WEIGHTS.values()) == 1.0

    def test_all_zero(self):
        assert compute_overall(0, 0, 0) == 0

    def test_all_max(self):
        assert compute_overall(100, 100, 100) == 100

    def test_known_weighted_average(self):
        # 0.35*70 + 0.35*80 + 0.30*60 = 24.5 + 28 + 18 = 70.5 -> banker's rounding to 70
        assert compute_overall(70, 80, 60) == 70

    def test_skill_and_experience_weighted_equally(self):
        # Swapping skill_match and experience_relevance should give the same result
        # since both carry the same 0.35 weight.
        assert compute_overall(90, 40, 50) == compute_overall(40, 90, 50)

    def test_project_impact_weighted_less(self):
        # A point of project_impact should move the result less than a point of
        # skill_match, since 0.30 < 0.35.
        base = compute_overall(50, 50, 50)
        bumped_skill = compute_overall(60, 50, 50)
        bumped_impact = compute_overall(50, 50, 60)
        assert (bumped_skill - base) > (bumped_impact - base)

    def test_result_is_always_int(self):
        result = compute_overall(33, 67, 51)
        assert isinstance(result, int)

    def test_seniority_mismatch_cap_produces_low_overall(self):
        # Enforced by the prompt (Seniority Mismatch Rule caps experience_relevance
        # at 15), not by this function — but confirms a capped experience score
        # correctly drags the overall down even with strong skills.
        result = compute_overall(90, 15, 40)
        assert result < 55

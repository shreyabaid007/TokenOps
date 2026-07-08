"""Tests for the eval pipeline — deterministic parts only (no LLM calls)."""

from pathlib import Path

import pytest

from eval import experiments
from eval.evaluators import routing_correctness

GOLDEN_PATH = Path(__file__).parent.parent / "eval" / "golden_dataset.json"


# ---- routing_correctness ----

def test_exact_match_scores_one():
    assert routing_correctness("p", "low", "low") == 1.0
    assert routing_correctness("p", "mid", "mid") == 1.0
    assert routing_correctness("p", "high", "high") == 1.0


def test_adjacent_tier_scores_half():
    assert routing_correctness("p", "low", "mid") == 0.5
    assert routing_correctness("p", "mid", "high") == 0.5
    assert routing_correctness("p", "high", "mid") == 0.5


def test_opposite_tier_scores_zero():
    assert routing_correctness("p", "low", "high") == 0.0
    assert routing_correctness("p", "high", "low") == 0.0


# ---- golden dataset ----

def test_golden_dataset_loads_and_is_valid():
    data = experiments.load_dataset(GOLDEN_PATH)
    entries = data["entries"]
    assert len(entries) == 50
    for entry in entries:
        assert entry["expected_tier"] in ("low", "mid", "high")
        assert entry["prompt"]
        assert entry["tags"]


def test_golden_dataset_covers_all_tiers_and_endpoints():
    data = experiments.load_dataset(GOLDEN_PATH)
    tiers = {e["expected_tier"] for e in data["entries"]}
    tags = {t for e in data["entries"] for t in e["tags"]}
    assert tiers == {"low", "mid", "high"}
    assert {"sql-analyst", "code-reviewer", "log-explainer", "doc-writer"} <= tags


def test_load_dataset_rejects_empty(tmp_path):
    bad = tmp_path / "empty.json"
    bad.write_text('{"entries": []}')
    with pytest.raises(ValueError):
        experiments.load_dataset(bad)


# ---- routing experiment ----

def _mini_dataset() -> dict:
    return {
        "version": "test",
        "entries": [
            {"id": 1, "prompt": "short question", "expected_tier": "low", "tags": ["t"]},
            {"id": 2, "prompt": " ".join(["word"] * 30), "expected_tier": "mid", "tags": ["t"]},
            {"id": 3, "prompt": " ".join(["word"] * 120), "expected_tier": "high", "tags": ["t"]},
        ],
    }


def test_routing_experiment_smart_policy_perfect_on_calibrated_set():
    exp = experiments.run_routing_experiment(_mini_dataset(), "smart-routing")
    assert exp.total_requests == 3
    assert exp.avg_quality == 1.0
    assert exp.total_cost_usd > 0


def test_routing_experiment_all_opus_costs_most():
    ds = _mini_dataset()
    opus = experiments.run_routing_experiment(ds, "all-opus")
    haiku = experiments.run_routing_experiment(ds, "all-haiku")
    assert opus.total_cost_usd > haiku.total_cost_usd


def test_routing_experiment_unknown_policy_raises():
    with pytest.raises(ValueError, match="unknown policy"):
        experiments.run_routing_experiment(_mini_dataset(), "nonexistent")


# ---- frontier ----

def test_frontier_marks_pareto_points():
    frontier = experiments.run_cost_quality_frontier(
        _mini_dataset(), ["smart-routing", "all-haiku", "all-opus"]
    )
    by_policy = {p.policy: p for p in frontier.points}

    # smart-routing achieves quality 1.0 on the calibrated set; all-opus is
    # more expensive with lower quality, so it must be dominated.
    assert by_policy["smart-routing"].pareto_optimal is True
    assert by_policy["all-opus"].pareto_optimal is False

    # Points come back sorted by cost ascending.
    costs = [p.avg_cost_usd for p in frontier.points]
    assert costs == sorted(costs)

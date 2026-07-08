"""Tests for agent.graph v2 — approval gate, checkpointer, state management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.graph import (
    CACHE_THRESHOLD_RANGE,
    HIGH_MIN_TOKENS_RANGE,
    LOW_MAX_TOKENS_RANGE,
    MAX_QUALITY_DROP_PCT,
    OptimizerState,
    _validate_cache_proposal,
    _validate_route_proposal,
    approval_gate_node,
    observe_node,
    validate_node,
)


def _dummy_state(**overrides) -> OptimizerState:
    base = OptimizerState(
        run_id="test-run-1",
        window_hours=24,
        stats={},
        proposals=[],
        validated_proposals=[],
        reasoning="test",
    )
    base.update(overrides)
    return base


# ---- validate_cache_proposal ----

def test_cache_threshold_within_bounds():
    proposal = {"tool": "cache_tune", "action": "lower", "proposed_threshold": 0.90}
    result = _validate_cache_proposal(proposal, [])
    assert result["valid"] is True


def test_cache_threshold_below_lower_bound():
    proposal = {"tool": "cache_tune", "action": "lower", "proposed_threshold": 0.80}
    result = _validate_cache_proposal(proposal, [])
    assert result["valid"] is False
    assert "outside" in result["reason"]


def test_cache_threshold_above_upper_bound():
    proposal = {"tool": "cache_tune", "action": "raise", "proposed_threshold": 0.99}
    result = _validate_cache_proposal(proposal, [])
    assert result["valid"] is False


def test_cache_lower_rejected_on_quality_drop():
    """Lowering threshold rejected when cached quality is significantly worse."""
    backtest = [
        {"cached": True, "quality_score": 0.5, "tier": "low", "prompt_snip": "test"},
        {"cached": False, "quality_score": 0.9, "tier": "low", "prompt_snip": "test"},
    ]
    proposal = {"tool": "cache_tune", "action": "lower", "proposed_threshold": 0.88}
    result = _validate_cache_proposal(proposal, backtest)
    assert result["valid"] is False


# ---- validate_route_proposal ----

def test_route_no_change_accepted():
    proposal = {
        "tool": "route_optimize",
        "action": "no_change",
        "proposed_low_max": 300,
        "proposed_high_min": 800,
    }
    result = _validate_route_proposal(proposal, [])
    assert result["valid"] is True


def test_route_low_max_out_of_range():
    proposal = {
        "tool": "route_optimize",
        "action": "widen_low",
        "proposed_low_max": 100,
        "proposed_high_min": 800,
    }
    result = _validate_route_proposal(proposal, [])
    assert result["valid"] is False


def test_route_high_min_out_of_range():
    proposal = {
        "tool": "route_optimize",
        "action": "widen_low",
        "proposed_low_max": 300,
        "proposed_high_min": 1500,
    }
    result = _validate_route_proposal(proposal, [])
    assert result["valid"] is False


def test_route_low_max_exceeds_high_min():
    proposal = {
        "tool": "route_optimize",
        "action": "widen_low",
        "proposed_low_max": 900,
        "proposed_high_min": 800,
    }
    result = _validate_route_proposal(proposal, [])
    assert result["valid"] is False


def test_route_backtest_quality_pass():
    """Proposal accepted when projected quality drop is within bounds."""
    backtest = [
        {"quality_score": 0.9, "tier": "low", "prompt_snip": "short prompt", "cached": False},
        {"quality_score": 0.85, "tier": "mid", "prompt_snip": "a slightly longer prompt here", "cached": False},
        {"quality_score": 0.88, "tier": "high", "prompt_snip": "a very long and complex prompt that has many words", "cached": False},
    ]
    proposal = {
        "tool": "route_optimize",
        "action": "widen_low",
        "proposed_low_max": 305,
        "proposed_high_min": 800,
    }
    result = _validate_route_proposal(proposal, backtest)
    assert result["valid"] is True


# ---- validate_node ----

def test_validate_node_empty_proposals():
    state = _dummy_state(proposals=[])
    result = validate_node(state)
    assert result["validated_proposals"] == []


def test_validate_node_unknown_tool():
    state = _dummy_state(proposals=[{"tool": "unknown", "action": "do_stuff"}])
    with patch("agent.graph._fetch_backtest_rows_async", new_callable=AsyncMock, return_value=[]):
        with patch("agent.graph.asyncio.run", side_effect=lambda coro: []):
            result = validate_node(state)
    assert len(result.get("validated_proposals", [])) == 0


# ---- approval_gate_node ----

def test_approval_gate_auto_skips_no_rule_changes():
    """When no rule changes passed validation, approval is auto-granted."""
    state = _dummy_state(
        validated_proposals=[
            {"tool": "quality_sample", "action": "scored", "sampled": 20},
        ],
    )
    result = approval_gate_node(state)
    assert result["approval"]["approved"] is True
    assert result["approval"]["reviewer"] == "auto"


def test_approval_gate_skips_no_change_actions():
    """no_change actions don't trigger the gate."""
    state = _dummy_state(
        validated_proposals=[
            {"tool": "cache_tune", "action": "no_change", "proposed_threshold": 0.92},
        ],
    )
    result = approval_gate_node(state)
    assert result["approval"]["approved"] is True


# ---- safety bounds are correct ----

def test_safety_bounds_constants():
    assert CACHE_THRESHOLD_RANGE == (0.85, 0.97)
    assert LOW_MAX_TOKENS_RANGE == (200, 500)
    assert HIGH_MIN_TOKENS_RANGE == (600, 1200)
    assert MAX_QUALITY_DROP_PCT == 0.05

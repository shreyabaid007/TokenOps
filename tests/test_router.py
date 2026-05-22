"""Tests for proxy.router — pure model-selection and cost arithmetic.

No mocks needed; every function is pure.
"""

import pytest

from proxy import router


def test_select_model_maps_tier_to_model() -> None:
    assert router.select_model("low") == "anthropic/claude-haiku-4-5"
    assert router.select_model("mid") == "anthropic/claude-sonnet-4-5"
    assert router.select_model("high") == "anthropic/claude-opus-4-5"


def test_select_model_raises_on_unknown_tier() -> None:
    with pytest.raises(KeyError):
        router.select_model("ultra")


def test_compute_cost_uses_correct_rates_for_each_model() -> None:
    # Haiku: $0.00025/1k in + $0.00125/1k out → 1k+1k = $0.0015
    assert router.compute_cost("anthropic/claude-haiku-4-5", 1000, 1000) == pytest.approx(0.0015)
    # Sonnet: $0.003 + $0.015 → 1k+1k = $0.018
    assert router.compute_cost("anthropic/claude-sonnet-4-5", 1000, 1000) == pytest.approx(0.018)
    # Opus: $0.015 + $0.075 → 1k+1k = $0.090
    assert router.compute_cost("anthropic/claude-opus-4-5", 1000, 1000) == pytest.approx(0.090)


def test_compute_cost_zero_tokens_is_zero() -> None:
    assert router.compute_cost("anthropic/claude-haiku-4-5", 0, 0) == 0.0


def test_compute_cost_scales_linearly_with_tokens() -> None:
    base = router.compute_cost("anthropic/claude-haiku-4-5", 1000, 1000)
    assert router.compute_cost("anthropic/claude-haiku-4-5", 2000, 2000) == pytest.approx(base * 2)


def test_compute_cost_separates_input_and_output_rates() -> None:
    """Output tokens cost 5x input tokens for Haiku — the test catches any
    accidental swap of the 'in'/'out' keys in COST_PER_1K_TOKENS."""
    in_only = router.compute_cost("anthropic/claude-haiku-4-5", 1000, 0)
    out_only = router.compute_cost("anthropic/claude-haiku-4-5", 0, 1000)
    assert out_only == pytest.approx(in_only * 5)


def test_counterfactual_cost_always_uses_opus_rates() -> None:
    """Savings calculations are nonsense if counterfactual_cost ever returns
    something other than the Opus price."""
    direct_opus = router.compute_cost("anthropic/claude-opus-4-5", 1234, 5678)
    assert router.counterfactual_cost(1234, 5678) == direct_opus


def test_counterfactual_cost_zero_tokens_is_zero() -> None:
    assert router.counterfactual_cost(0, 0) == 0.0


def test_model_map_and_cost_table_in_sync() -> None:
    """Every tier in MODEL_MAP must have pricing in COST_PER_1K_TOKENS.
    Catches a future tier addition that forgets the cost table."""
    for tier, model in router.MODEL_MAP.items():
        assert model in router.COST_PER_1K_TOKENS, (
            f"tier {tier!r} maps to {model!r} but COST_PER_1K_TOKENS has no entry"
        )

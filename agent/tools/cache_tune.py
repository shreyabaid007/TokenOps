"""Cache threshold tuner — proposes adjustments based on hit rate.

Pure calculation, no I/O. Bounded step sizes; the hard CACHE_THRESHOLD_RANGE
limit is enforced by agent.graph.validate_node, not here.
"""

_HIT_RATE_FLOOR = 0.30
_HIT_RATE_CEILING = 0.70
_LOWER_STEP = 0.02
_RAISE_STEP = 0.01


def propose(hit_rate: float, current_threshold: float) -> dict[str, object]:
    """Decide whether to lower, raise, or hold the cosine threshold.

    Logic per structure.md:
      hit_rate < 0.30 → lower by 0.02 (cache too strict, missing valid matches)
      hit_rate > 0.70 → raise by 0.01 (cache may be too loose, risking quality)
      else            → no_change
    """
    if hit_rate < _HIT_RATE_FLOOR:
        return {
            "tool": "cache_tune",
            "action": "lower",
            "current_threshold": current_threshold,
            "proposed_threshold": round(current_threshold - _LOWER_STEP, 4),
            "trigger": f"hit_rate {hit_rate:.2f} below floor {_HIT_RATE_FLOOR:.2f}",
        }
    if hit_rate > _HIT_RATE_CEILING:
        return {
            "tool": "cache_tune",
            "action": "raise",
            "current_threshold": current_threshold,
            "proposed_threshold": round(current_threshold + _RAISE_STEP, 4),
            "trigger": f"hit_rate {hit_rate:.2f} above ceiling {_HIT_RATE_CEILING:.2f}",
        }
    return {
        "tool": "cache_tune",
        "action": "no_change",
        "current_threshold": current_threshold,
        "proposed_threshold": current_threshold,
        "trigger": f"hit_rate {hit_rate:.2f} within band",
    }

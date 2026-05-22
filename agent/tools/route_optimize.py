"""Routing band optimizer — proposes adjustments to tier boundaries.

Pure calculation. The agent widens the cheap (low) band when low-tier
quality is consistently high and volume is meaningful — i.e. when there's
evidence the cheap tier handles its current band well, expand its remit.

Hard LOW_MAX_TOKENS_RANGE / HIGH_MIN_TOKENS_RANGE limits live in
agent.graph.validate_node, not here. This module just proposes; the
validator enforces.
"""

_LOW_QUALITY_FLOOR = 0.90
_LOW_VOLUME_MIN = 50
_WIDEN_STEP = 5


def propose(
    quality_by_tier: dict[str, float | None],
    volume_by_tier: dict[str, int],
    current_low_max: int,
    current_high_min: int,
) -> dict[str, object]:
    """Decide whether to widen the low-tier band.

    Per structure.md: if low-tier avg quality > 0.90 AND volume > 50 →
    relax low_max_tokens upward. v1 widens only the low band — high-band
    contraction would need different evidence and is left to a later
    iteration.
    """
    low_quality = quality_by_tier.get("low")
    low_volume = volume_by_tier.get("low", 0)

    if (
        low_quality is not None
        and low_quality > _LOW_QUALITY_FLOOR
        and low_volume > _LOW_VOLUME_MIN
    ):
        return {
            "tool": "route_optimize",
            "action": "widen_low",
            "current_low_max": current_low_max,
            "current_high_min": current_high_min,
            "proposed_low_max": current_low_max + _WIDEN_STEP,
            "proposed_high_min": current_high_min,
            "trigger": (
                f"low quality {low_quality:.2f} > {_LOW_QUALITY_FLOOR} "
                f"and low volume {low_volume} > {_LOW_VOLUME_MIN}"
            ),
        }

    reason = (
        "no scored low-tier requests" if low_quality is None
        else f"low quality {low_quality:.2f} or volume {low_volume} below thresholds"
    )
    return {
        "tool": "route_optimize",
        "action": "no_change",
        "current_low_max": current_low_max,
        "current_high_min": current_high_min,
        "proposed_low_max": current_low_max,
        "proposed_high_min": current_high_min,
        "trigger": reason,
    }

"""Per-tenant budget enforcement.

Checks monthly spend against the tenant's budget before any work is done.
Two thresholds:
  - soft_limit (80%): downgrades all calls to the cheapest tier (Haiku)
  - hard_limit (100%): rejects the request with 429

Spend is cached for 30 seconds to avoid hammering Postgres on every request.
"""

import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel

from proxy import ledger

logger = logging.getLogger(__name__)

SOFT_LIMIT_PCT: float = 80.0
HARD_LIMIT_PCT: float = 100.0
SPEND_CACHE_TTL_SEC: float = 30.0


class BudgetStatus(BaseModel):
    status: str  # "ok" | "soft_limit" | "hard_limit"
    spend_usd: float
    budget_usd: float
    utilization_pct: float
    resets_at: str


_spend_cache: dict[str, tuple[float, float]] = {}


def _month_reset_iso() -> str:
    """ISO timestamp of the first day of next month (UTC)."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return reset.isoformat()


async def check_budget(tenant_id: str, monthly_budget_usd: float) -> BudgetStatus:
    """Check whether a tenant is within budget for the current month.

    Returns BudgetStatus with status ok/soft_limit/hard_limit. The default
    tenant (anonymous, budget=0) always returns ok — zero budget means
    unlimited.
    """
    if monthly_budget_usd <= 0:
        return BudgetStatus(
            status="ok",
            spend_usd=0.0,
            budget_usd=0.0,
            utilization_pct=0.0,
            resets_at=_month_reset_iso(),
        )

    now = time.monotonic()
    cached = _spend_cache.get(tenant_id)
    if cached is not None:
        spend, expires_at = cached
        if expires_at > now:
            return _evaluate(spend, monthly_budget_usd)

    spend = await ledger.get_tenant_spend(tenant_id)
    _spend_cache[tenant_id] = (spend, now + SPEND_CACHE_TTL_SEC)

    return _evaluate(spend, monthly_budget_usd)


def _evaluate(spend: float, budget_usd: float) -> BudgetStatus:
    utilization = (spend / budget_usd) * 100.0 if budget_usd > 0 else 0.0

    if utilization >= HARD_LIMIT_PCT:
        status = "hard_limit"
    elif utilization >= SOFT_LIMIT_PCT:
        status = "soft_limit"
    else:
        status = "ok"

    return BudgetStatus(
        status=status,
        spend_usd=round(spend, 6),
        budget_usd=round(budget_usd, 2),
        utilization_pct=round(utilization, 1),
        resets_at=_month_reset_iso(),
    )


def clear_cache() -> None:
    """Invalidate spend cache (tests / key rotation)."""
    _spend_cache.clear()

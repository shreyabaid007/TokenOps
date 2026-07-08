"""Tests for proxy.budget — per-tenant budget enforcement."""

from unittest.mock import AsyncMock, patch

import pytest

from proxy.budget import (
    HARD_LIMIT_PCT,
    SOFT_LIMIT_PCT,
    BudgetStatus,
    check_budget,
    clear_cache,
)


@pytest.fixture(autouse=True)
def _clear_budget_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.mark.asyncio
async def test_zero_budget_always_ok():
    """The default tenant (budget=0) always passes."""
    result = await check_budget("default", 0.0)
    assert result.status == "ok"
    assert result.utilization_pct == 0.0


@pytest.mark.asyncio
async def test_ok_status():
    """Under 80% utilization → ok."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=300.0)
        result = await check_budget("tenant-a", 1000.0)

    assert result.status == "ok"
    assert result.spend_usd == 300.0
    assert result.budget_usd == 1000.0
    assert result.utilization_pct == 30.0


@pytest.mark.asyncio
async def test_soft_limit_at_80pct():
    """At 80% → soft_limit (downgrade to Haiku)."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=800.0)
        result = await check_budget("tenant-b", 1000.0)

    assert result.status == "soft_limit"
    assert result.utilization_pct == 80.0


@pytest.mark.asyncio
async def test_hard_limit_at_100pct():
    """At 100% → hard_limit (reject with 429)."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=1000.0)
        result = await check_budget("tenant-c", 1000.0)

    assert result.status == "hard_limit"
    assert result.utilization_pct == 100.0


@pytest.mark.asyncio
async def test_over_budget():
    """Over 100% → hard_limit."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=1500.0)
        result = await check_budget("tenant-d", 1000.0)

    assert result.status == "hard_limit"
    assert result.utilization_pct == 150.0


@pytest.mark.asyncio
async def test_cached_spend():
    """Second call within TTL uses cached spend, not DB."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=100.0)

        r1 = await check_budget("tenant-e", 500.0)
        r2 = await check_budget("tenant-e", 500.0)

    assert mock_ledger.get_tenant_spend.call_count == 1
    assert r1.status == r2.status == "ok"


@pytest.mark.asyncio
async def test_resets_at_present():
    """Every response includes a reset timestamp."""
    with patch("proxy.budget.ledger") as mock_ledger:
        mock_ledger.get_tenant_spend = AsyncMock(return_value=0.0)
        result = await check_budget("tenant-f", 500.0)

    assert result.resets_at is not None
    assert "T" in result.resets_at

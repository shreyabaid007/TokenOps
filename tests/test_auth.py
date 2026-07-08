"""Tests for proxy.auth — tenant resolution from API keys."""

import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from proxy.auth import (
    DEFAULT_TENANT_ID,
    DEFAULT_TENANT_NAME,
    TenantInfo,
    _CacheEntry,
    _tenant_cache,
    clear_cache,
    resolve_tenant,
    verify_agent_admin,
)


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    """Ensure each test starts with an empty tenant cache."""
    clear_cache()
    yield
    clear_cache()


def _make_db_row(
    tenant_id: str = "test-tenant",
    name: str = "Test",
    is_active: bool = True,
    budget: float = 500.0,
    redaction_config: dict | None = None,
) -> dict:
    return {
        "id": tenant_id,
        "name": name,
        "is_active": is_active,
        "monthly_budget_usd": budget,
        "redaction_config": redaction_config or {"enabled": True},
    }


@pytest.mark.asyncio
async def test_missing_key_returns_default():
    """No API key → anonymous default tenant (backward compat)."""
    info = await resolve_tenant(None)
    assert info.tenant_id == DEFAULT_TENANT_ID
    assert info.tenant_name == DEFAULT_TENANT_NAME
    assert info.is_active is True


@pytest.mark.asyncio
async def test_empty_key_returns_default():
    info = await resolve_tenant("")
    assert info.tenant_id == DEFAULT_TENANT_ID


@pytest.mark.asyncio
async def test_valid_key_resolves():
    """A valid key hits the DB and returns tenant info."""
    api_key = "tok_test_key_123"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    row = _make_db_row()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

    with patch("proxy.auth.ledger") as mock_ledger:
        mock_ledger._pool_or_raise.return_value = mock_pool
        info = await resolve_tenant(api_key)

    assert info.tenant_id == "test-tenant"
    assert info.tenant_name == "Test"
    assert info.is_active is True


@pytest.mark.asyncio
async def test_invalid_key_raises():
    """An unknown key raises ValueError."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

    with patch("proxy.auth.ledger") as mock_ledger:
        mock_ledger._pool_or_raise.return_value = mock_pool
        with pytest.raises(ValueError, match="invalid API key"):
            await resolve_tenant("bad-key")


@pytest.mark.asyncio
async def test_inactive_tenant_raises():
    """An inactive tenant's key raises ValueError."""
    api_key = "tok_inactive"
    row = _make_db_row(is_active=False)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

    with patch("proxy.auth.ledger") as mock_ledger:
        mock_ledger._pool_or_raise.return_value = mock_pool
        with pytest.raises(ValueError, match="deactivated"):
            await resolve_tenant(api_key)


@pytest.mark.asyncio
async def test_cache_hit_skips_db():
    """A cached entry is returned without touching the DB."""
    api_key = "tok_cached"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    expected = TenantInfo(
        tenant_id="cached-tenant",
        tenant_name="Cached",
        is_active=True,
        monthly_budget_usd=100.0,
        redaction_config={"enabled": False},
    )
    _tenant_cache[key_hash] = _CacheEntry(
        info=expected,
        expires_at=time.monotonic() + 600,
    )

    info = await resolve_tenant(api_key)
    assert info.tenant_id == "cached-tenant"


@pytest.mark.asyncio
async def test_expired_cache_hits_db():
    """An expired cache entry triggers a fresh DB lookup."""
    api_key = "tok_expired"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    _tenant_cache[key_hash] = _CacheEntry(
        info=TenantInfo("old", "Old", True, 0.0, {}),
        expires_at=time.monotonic() - 1,
    )

    row = _make_db_row(tenant_id="fresh-tenant", name="Fresh")

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

    with patch("proxy.auth.ledger") as mock_ledger:
        mock_ledger._pool_or_raise.return_value = mock_pool
        info = await resolve_tenant(api_key)

    assert info.tenant_id == "fresh-tenant"


def test_verify_agent_admin_skips_when_unset():
    with patch("proxy.config.settings") as mock_settings:
        mock_settings.agent_admin_key = None
        verify_agent_admin(None, None)


def test_verify_agent_admin_accepts_bearer():
    with patch("proxy.config.settings") as mock_settings:
        mock_settings.agent_admin_key = "secret-admin"
        verify_agent_admin("secret-admin", None)


def test_verify_agent_admin_accepts_header():
    with patch("proxy.config.settings") as mock_settings:
        mock_settings.agent_admin_key = "secret-admin"
        verify_agent_admin(None, "secret-admin")


def test_verify_agent_admin_rejects_bad_key():
    with patch("proxy.config.settings") as mock_settings:
        mock_settings.agent_admin_key = "secret-admin"
        with pytest.raises(ValueError, match="invalid or missing admin key"):
            verify_agent_admin("wrong", None)

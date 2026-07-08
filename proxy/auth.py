"""Tenant resolution from Bearer API keys.

Resolves an API key to a (tenant_id, tenant_name) pair. Uses an in-memory
cache with TTL to avoid hitting Postgres on every request. Missing or empty
keys resolve to the default anonymous tenant for backward compatibility.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass

from proxy import ledger

logger = logging.getLogger(__name__)

CACHE_TTL_SEC: float = 300.0
DEFAULT_TENANT_ID: str = "default"
DEFAULT_TENANT_NAME: str = "Anonymous"

# Presidio's default English NER model — install at image build time via:
#   pip install <en_core_web_sm wheel>
# or: python -m spacy download en_core_web_sm
SPACY_MODEL: str = "en_core_web_sm"


@dataclass
class TenantInfo:
    tenant_id: str
    tenant_name: str
    is_active: bool
    monthly_budget_usd: float
    redaction_config: dict


@dataclass
class _CacheEntry:
    info: TenantInfo
    expires_at: float


_tenant_cache: dict[str, _CacheEntry] = {}


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def clear_cache() -> None:
    """Invalidate the tenant cache (used after key rotation or in tests)."""
    _tenant_cache.clear()


async def resolve_tenant(api_key: str | None) -> TenantInfo:
    """Resolve a Bearer API key to tenant info.

    Returns the default anonymous tenant when no key is provided (backward
    compat with v1 clients). Raises ValueError on invalid or inactive keys.
    """
    if not api_key:
        return TenantInfo(
            tenant_id=DEFAULT_TENANT_ID,
            tenant_name=DEFAULT_TENANT_NAME,
            is_active=True,
            monthly_budget_usd=0.0,
            redaction_config={"enabled": False},
        )

    key_hash = _hash_key(api_key)
    now = time.monotonic()

    cached = _tenant_cache.get(key_hash)
    if cached is not None and cached.expires_at > now:
        if not cached.info.is_active:
            raise ValueError(f"tenant '{cached.info.tenant_id}' is deactivated")
        return cached.info

    info = await _lookup_tenant(key_hash)

    _tenant_cache[key_hash] = _CacheEntry(info=info, expires_at=now + CACHE_TTL_SEC)

    if not info.is_active:
        raise ValueError(f"tenant '{info.tenant_id}' is deactivated")

    return info


async def _lookup_tenant(key_hash: str) -> TenantInfo:
    """Query Postgres for a tenant by API key hash."""
    pool = ledger._pool_or_raise()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, is_active, monthly_budget_usd, redaction_config
            FROM tenants
            WHERE api_key_hash = $1
            """,
            key_hash,
        )
    if row is None:
        raise ValueError("invalid API key")

    redaction_config = row["redaction_config"]
    if isinstance(redaction_config, str):
        redaction_config = json.loads(redaction_config)

    return TenantInfo(
        tenant_id=row["id"],
        tenant_name=row["name"],
        is_active=row["is_active"],
        monthly_budget_usd=float(row["monthly_budget_usd"]),
        redaction_config=redaction_config or {"enabled": False},
    )


def verify_agent_admin(
    bearer_key: str | None,
    admin_header: str | None = None,
) -> None:
    """Validate admin credentials for /v1/agent/* endpoints.

    When AGENT_ADMIN_KEY is unset, verification is skipped (local dev only).
    When set, accepts the key via Bearer token or X-TokenOps-Admin-Key header.
    Raises ValueError on failure.
    """
    from proxy.config import settings

    required = settings.agent_admin_key
    if not required:
        return

    provided = bearer_key or admin_header
    if not provided or provided != required:
        raise ValueError("invalid or missing admin key")

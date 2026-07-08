"""Postgres ledger: cost-of-every-request log and routing-rules read.

Owns the asyncpg pool. All proxy DB I/O happens here so the rest of the
proxy stays pure. log_request is fire-and-forget and never raises — a DB
blip degrades observability, not user-facing traffic.
"""

import hashlib
import logging

import asyncpg

from proxy.config import RoutingRules, settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Open the asyncpg pool once at startup. Called from main.py lifespan."""
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
    )
    logger.info("asyncpg pool ready", extra={"min": 2, "max": 10})


async def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("asyncpg pool closed")


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("ledger pool not initialised — lifespan must run first")
    return _pool


def pool_stats() -> dict[str, int | None]:
    """Current pool sizing for /health. Safe before init (returns Nones)."""
    if _pool is None:
        return {"db_pool_size": None, "db_pool_free": None}
    return {
        "db_pool_size": _pool.get_size(),
        "db_pool_free": _pool.get_idle_size(),
    }


async def ping() -> bool:
    """Deep health probe: round-trip SELECT 1. Never raises."""
    try:
        async with _pool_or_raise().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception as exc:
        logger.warning("postgres ping failed", extra={"error": str(exc)})
        return False


async def log_request(
    request_id: str,
    prompt: str,
    tag: str,
    model: str,
    tier: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    counterfactual_cost_usd: float,
    cached: bool,
    latency_ms: float,
    tenant_id: str | None = None,
    redacted_entity_count: int = 0,
) -> None:
    """Insert one row into requests. Never raises out — a failed write is
    logged and discarded so a DB blip never bubbles into the response path.

    prompt_hash and prompt_snip are derived here; callers pass the full
    prompt and the ledger decides what to persist.
    """
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    prompt_snip = prompt[:120]
    try:
        async with _pool_or_raise().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests (
                    request_id, tenant_id, prompt_hash, prompt_snip, tag,
                    model, tier, tokens_in, tokens_out, cost_usd,
                    counterfactual_cost_usd, cached, latency_ms,
                    redacted_entity_count
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                request_id, tenant_id, prompt_hash, prompt_snip, tag,
                model, tier, tokens_in, tokens_out, cost_usd,
                counterfactual_cost_usd, cached, latency_ms,
                redacted_entity_count,
            )
    except Exception as exc:
        logger.warning(
            "ledger insert failed",
            extra={"request_id": request_id, "error": str(exc)},
        )


async def get_tenant_spend(tenant_id: str) -> float:
    """Total spend for a tenant in the current calendar month."""
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS total_spend
            FROM requests
            WHERE tenant_id = $1
              AND ts >= date_trunc('month', NOW())
            """,
            tenant_id,
        )
    return float(row["total_spend"]) if row else 0.0


async def get_usage_stats(
    tenant_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Aggregated usage stats for the /v1/usage endpoint."""
    date_filter = ""
    params: list = [tenant_id]
    idx = 2

    if start_date:
        date_filter += f" AND ts >= ${idx}::timestamptz"
        params.append(start_date)
        idx += 1
    if end_date:
        date_filter += f" AND ts < ${idx}::timestamptz"
        params.append(end_date)
        idx += 1

    async with _pool_or_raise().acquire() as conn:
        summary = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(cost_usd), 0) AS total_spend,
                COUNT(*) AS total_requests,
                COALESCE(AVG(CASE WHEN cached THEN 1.0 ELSE 0.0 END), 0) AS cache_hit_rate,
                COALESCE(SUM(counterfactual_cost_usd), 0) AS counterfactual_total
            FROM requests
            WHERE tenant_id = $1{date_filter}
            """,
            *params,
        )

        by_tag = await conn.fetch(
            f"""
            SELECT tag,
                   COALESCE(SUM(cost_usd), 0) AS spend,
                   COUNT(*) AS request_count
            FROM requests
            WHERE tenant_id = $1{date_filter}
            GROUP BY tag
            ORDER BY spend DESC
            """,
            *params,
        )

        by_model = await conn.fetch(
            f"""
            SELECT model,
                   COALESCE(SUM(cost_usd), 0) AS spend,
                   COUNT(*) AS request_count
            FROM requests
            WHERE tenant_id = $1{date_filter}
            GROUP BY model
            ORDER BY spend DESC
            """,
            *params,
        )

    total_spend = float(summary["total_spend"])
    counterfactual = float(summary["counterfactual_total"])
    savings_pct = (
        (1.0 - total_spend / counterfactual) * 100.0
        if counterfactual > 0
        else 0.0
    )

    return {
        "tenant_id": tenant_id,
        "total_spend_usd": round(total_spend, 6),
        "total_requests": int(summary["total_requests"]),
        "cache_hit_rate": round(float(summary["cache_hit_rate"]), 4),
        "savings_vs_opus_pct": round(savings_pct, 2),
        "breakdown_by_tag": [
            {"tag": r["tag"], "spend_usd": round(float(r["spend"]), 6), "requests": int(r["request_count"])}
            for r in by_tag
        ],
        "breakdown_by_model": [
            {"model": r["model"], "spend_usd": round(float(r["spend"]), 6), "requests": int(r["request_count"])}
            for r in by_model
        ],
    }


async def get_latest_rules() -> RoutingRules:
    """Read the most recent routing_rules row.

    Called from main.py lifespan on bootstrap and by config.reload_rules_loop
    every RULES_RELOAD_INTERVAL_SEC. Raises on empty table — that means
    db/schema.sql was never applied, which is a fatal configuration error.
    """
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, cache_threshold, low_max_tokens, high_min_tokens
            FROM routing_rules
            ORDER BY id DESC
            LIMIT 1
            """
        )
    if row is None:
        raise RuntimeError("routing_rules is empty — apply db/schema.sql first")
    return RoutingRules(
        id=row["id"],
        cache_threshold=float(row["cache_threshold"]),
        low_max_tokens=int(row["low_max_tokens"]),
        high_min_tokens=int(row["high_min_tokens"]),
    )

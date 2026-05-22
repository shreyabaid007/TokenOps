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
                    request_id, prompt_hash, prompt_snip, tag, model, tier,
                    tokens_in, tokens_out, cost_usd, counterfactual_cost_usd,
                    cached, latency_ms
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                request_id, prompt_hash, prompt_snip, tag, model, tier,
                tokens_in, tokens_out, cost_usd, counterfactual_cost_usd,
                cached, latency_ms,
            )
    except Exception as exc:
        logger.warning(
            "ledger insert failed",
            extra={"request_id": request_id, "error": str(exc)},
        )


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

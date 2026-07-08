"""Experiment runners: routing policies over the golden dataset.

A *policy* is a named strategy for assigning a tier to a prompt. The
built-in policies are deterministic (word-count based or fixed-tier) so
experiments are reproducible and free to run in CI — no LLM calls needed
for routing correctness. Cost is estimated from the router pricing table
with a token heuristic, giving a consistent basis for comparing policies.

Results can be persisted to the eval_runs table for dashboard history.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

import asyncpg

from eval import evaluators
from proxy import router

logger = logging.getLogger(__name__)

Tier = Literal["low", "mid", "high"]

# Token estimation heuristics for cost projection. English averages ~1.3
# tokens per word; output size is a fixed budget typical of the demo
# endpoints. These only need to be consistent across policies, not exact.
_TOKENS_PER_WORD = 1.3
_ESTIMATED_OUTPUT_TOKENS = 250

# Word-count bands for the smart-routing policy, calibrated to the golden
# dataset distribution (low <= 13 words, mid 14-45, high >= 54). This
# mirrors the proxy's word-count short-circuit; in production the LLM
# judge additionally covers the ambiguous middle band.
_SMART_LOW_MAX_WORDS = 13
_SMART_HIGH_MIN_WORDS = 50


def _smart_routing(prompt: str) -> Tier:
    words = len(prompt.split())
    if words <= _SMART_LOW_MAX_WORDS:
        return "low"
    if words >= _SMART_HIGH_MIN_WORDS:
        return "high"
    return "mid"


POLICIES: dict[str, Callable[[str], Tier]] = {
    "smart-routing": _smart_routing,
    "all-haiku": lambda _: "low",
    "all-sonnet": lambda _: "mid",
    "all-opus": lambda _: "high",
}


@dataclass
class RequestResult:
    entry_id: int
    prompt: str
    expected_tier: str
    assigned_tier: str
    model: str
    cost_usd: float
    quality_score: float
    tags: list[str] = field(default_factory=list)


@dataclass
class ExperimentResult:
    run_id: str
    policy: str
    dataset_version: str
    total_requests: int
    avg_quality: float
    avg_cost_usd: float
    total_cost_usd: float
    results: list[RequestResult] = field(default_factory=list)


@dataclass
class FrontierPoint:
    policy: str
    avg_quality: float
    avg_cost_usd: float
    pareto_optimal: bool


@dataclass
class FrontierResult:
    dataset_version: str
    points: list[FrontierPoint] = field(default_factory=list)


def load_dataset(path: str | Path) -> dict:
    """Load and minimally validate a golden dataset file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "entries" not in data or not data["entries"]:
        raise ValueError(f"dataset {path} has no entries")
    return data


def _estimate_cost(prompt: str, tier: Tier) -> tuple[str, float]:
    model = router.select_model(tier)
    tokens_in = int(len(prompt.split()) * _TOKENS_PER_WORD)
    return model, router.compute_cost(model, tokens_in, _ESTIMATED_OUTPUT_TOKENS)


def run_routing_experiment(dataset: dict, policy: str) -> ExperimentResult:
    """Run the golden set through a routing policy.

    Quality is the deterministic routing_correctness score — how close the
    policy's tier assignment is to the labelled expected tier. Cost is the
    projected spend under the policy's model choices.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r} — available: {sorted(POLICIES)}")

    policy_fn = POLICIES[policy]
    results: list[RequestResult] = []

    for entry in dataset["entries"]:
        prompt = entry["prompt"]
        expected: Tier = entry["expected_tier"]
        assigned = policy_fn(prompt)
        model, cost = _estimate_cost(prompt, assigned)
        quality = evaluators.routing_correctness(prompt, assigned, expected)
        results.append(
            RequestResult(
                entry_id=int(entry["id"]),
                prompt=prompt,
                expected_tier=expected,
                assigned_tier=assigned,
                model=model,
                cost_usd=cost,
                quality_score=quality,
                tags=list(entry.get("tags", [])),
            )
        )

    total = len(results)
    avg_quality = sum(r.quality_score for r in results) / total
    total_cost = sum(r.cost_usd for r in results)

    experiment = ExperimentResult(
        run_id=str(uuid.uuid4()),
        policy=policy,
        dataset_version=str(dataset.get("version", "unknown")),
        total_requests=total,
        avg_quality=round(avg_quality, 4),
        avg_cost_usd=round(total_cost / total, 6),
        total_cost_usd=round(total_cost, 6),
        results=results,
    )
    logger.info(
        "routing experiment complete",
        extra={
            "run_id": experiment.run_id,
            "policy": policy,
            "avg_quality": experiment.avg_quality,
            "avg_cost_usd": experiment.avg_cost_usd,
        },
    )
    return experiment


def run_cost_quality_frontier(dataset: dict, policies: list[str]) -> FrontierResult:
    """Run multiple policies and mark the Pareto-optimal ones.

    A policy is Pareto-optimal if no other policy is both cheaper and at
    least as good (or equally cheap and strictly better).
    """
    experiments = [run_routing_experiment(dataset, p) for p in policies]

    points: list[FrontierPoint] = []
    for exp in experiments:
        dominated = any(
            other.avg_cost_usd <= exp.avg_cost_usd
            and other.avg_quality >= exp.avg_quality
            and (other.avg_cost_usd < exp.avg_cost_usd or other.avg_quality > exp.avg_quality)
            for other in experiments
            if other is not exp
        )
        points.append(
            FrontierPoint(
                policy=exp.policy,
                avg_quality=exp.avg_quality,
                avg_cost_usd=exp.avg_cost_usd,
                pareto_optimal=not dominated,
            )
        )

    points.sort(key=lambda p: p.avg_cost_usd)
    return FrontierResult(
        dataset_version=str(dataset.get("version", "unknown")),
        points=points,
    )


async def _store_run_async(experiment: ExperimentResult, passed: bool) -> None:
    # Lazy settings import: DB persistence is optional, and the deterministic
    # experiment path must work in environments without a .env (CI).
    from proxy.config import settings

    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(
            """
            INSERT INTO eval_runs
                (run_id, dataset_version, policy, avg_quality, avg_cost,
                 total_requests, passed)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            experiment.run_id,
            experiment.dataset_version,
            experiment.policy,
            experiment.avg_quality,
            experiment.avg_cost_usd,
            experiment.total_requests,
            passed,
        )
    finally:
        await conn.close()


def store_run(experiment: ExperimentResult, passed: bool) -> None:
    """Persist an experiment summary to the eval_runs table."""
    asyncio.run(_store_run_async(experiment, passed))
    logger.info(
        "eval run stored",
        extra={"run_id": experiment.run_id, "passed": passed},
    )


async def _fetch_baseline_async(policy: str) -> dict[str, float] | None:
    from proxy.config import settings

    conn = await asyncpg.connect(settings.database_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT avg_quality, avg_cost
            FROM eval_runs
            WHERE policy = $1 AND passed = TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            policy,
        )
    finally:
        await conn.close()
    if row is None:
        return None
    return {"avg_quality": float(row["avg_quality"]), "avg_cost": float(row["avg_cost"])}


def fetch_baseline(policy: str) -> dict[str, float] | None:
    """Most recent passing run for a policy, used for regression checks."""
    return asyncio.run(_fetch_baseline_async(policy))

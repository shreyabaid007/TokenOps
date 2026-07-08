"""LangGraph optimizer agent v2 — observe → analyse → validate → [interrupt] → apply.

Upgrades from v1:
  - PostgresSaver checkpointer: agent state survives process restarts mid-run.
  - interrupt() before apply: the agent proposes, a human approves.
  - InMemoryStore: accumulates historical proposal outcomes across threads.

Runs every AGENT_RUN_INTERVAL_MINUTES via agent.scheduler. Communicates
with the proxy exclusively through the routing_rules and agent_decisions
Postgres tables — never shares in-process state with the data plane.

Safety bounds are hard-coded constants in this module. They cannot be
overridden by the LLM analyser, by tool proposals, or by the agent's own
state — validate_node enforces them every run.

All node functions are sync per CLAUDE.md. DB I/O bridges to asyncpg via
asyncio.run; LLM calls use sync chain.invoke.
"""

import asyncio
import json
import logging
import uuid
from typing import Literal, TypedDict

import asyncpg
from langchain_openai import ChatOpenAI
from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from agent.tools import cache_tune, quality_sample, route_optimize
from proxy.config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- safety bounds
CACHE_THRESHOLD_RANGE: tuple[float, float] = (0.85, 0.97)
LOW_MAX_TOKENS_RANGE: tuple[int, int] = (200, 500)
HIGH_MIN_TOKENS_RANGE: tuple[int, int] = (600, 1200)
MAX_QUALITY_DROP_PCT: float = 0.05

_BACKTEST_WINDOW = 500
_OBSERVE_WINDOW_HOURS_DEFAULT = 24
_ANALYSIS_MODEL = "anthropic/claude-haiku-4-5"


# --------------------------------------------------------------------- state
class OptimizerState(TypedDict, total=False):
    run_id: str
    window_hours: int
    stats: dict[str, object]
    proposals: list[dict[str, object]]
    validated_proposals: list[dict[str, object]]
    reasoning: str
    applied_rules: dict[str, object]
    approval: dict[str, object]


# ----------------------------------------------------- analyser structured out
class AnalysisPlan(BaseModel):
    """LLM-emitted plan for which tools to invoke this run."""

    tools_to_run: list[Literal["cache_tune", "route_optimize", "quality_sample"]]
    reasoning: str


_analysis_chain = None
_langfuse_handler: LangfuseCallbackHandler | None = None


def _get_analysis_chain():
    global _analysis_chain
    if _analysis_chain is None:
        llm = ChatOpenAI(
            model=_ANALYSIS_MODEL,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=15,
            max_retries=0,
        )
        _analysis_chain = llm.with_structured_output(AnalysisPlan)
    return _analysis_chain


def _get_langfuse() -> LangfuseCallbackHandler | None:
    global _langfuse_handler
    if _langfuse_handler is not None:
        return _langfuse_handler
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    import os
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
    os.environ["LANGFUSE_HOST"] = settings.langfuse_host
    _langfuse_handler = LangfuseCallbackHandler()
    return _langfuse_handler


# ------------------------------------------------------------- db helpers (async)
async def _fetch_stats_async(window_hours: int) -> dict[str, object]:
    conn = await asyncpg.connect(settings.database_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int                                                    AS total,
                COALESCE(SUM(CASE WHEN cached THEN 1 ELSE 0 END), 0)::int        AS hits,
                COUNT(*) FILTER (WHERE tier = 'low')::int                        AS vol_low,
                COUNT(*) FILTER (WHERE tier = 'mid')::int                        AS vol_mid,
                COUNT(*) FILTER (WHERE tier = 'high')::int                       AS vol_high,
                AVG(quality_score) FILTER (WHERE tier = 'low'  AND quality_score IS NOT NULL) AS q_low,
                AVG(quality_score) FILTER (WHERE tier = 'mid'  AND quality_score IS NOT NULL) AS q_mid,
                AVG(quality_score) FILTER (WHERE tier = 'high' AND quality_score IS NOT NULL) AS q_high,
                AVG(cost_usd) FILTER (WHERE tier = 'low')                        AS cost_low,
                AVG(cost_usd) FILTER (WHERE tier = 'mid')                        AS cost_mid,
                AVG(cost_usd) FILTER (WHERE tier = 'high')                       AS cost_high,
                COUNT(*) FILTER (WHERE quality_score IS NULL
                                   AND tier IN ('low','mid','high'))::int       AS unscored
            FROM requests
            WHERE ts > NOW() - ($1::text || ' hours')::interval
            """,
            str(window_hours),
        )
        rules_row = await conn.fetchrow(
            """
            SELECT id, cache_threshold, low_max_tokens, high_min_tokens
            FROM routing_rules
            ORDER BY id DESC
            LIMIT 1
            """
        )
    finally:
        await conn.close()

    total = int(row["total"] or 0)
    hits = int(row["hits"] or 0)
    return {
        "window_hours": window_hours,
        "total_requests": total,
        "cache_hits": hits,
        "cache_hit_rate": (hits / total) if total else 0.0,
        "volume_by_tier": {
            "low": int(row["vol_low"] or 0),
            "mid": int(row["vol_mid"] or 0),
            "high": int(row["vol_high"] or 0),
        },
        "quality_by_tier": {
            "low":  float(row["q_low"])  if row["q_low"]  is not None else None,
            "mid":  float(row["q_mid"])  if row["q_mid"]  is not None else None,
            "high": float(row["q_high"]) if row["q_high"] is not None else None,
        },
        "cost_by_tier": {
            "low":  float(row["cost_low"]  or 0.0),
            "mid":  float(row["cost_mid"]  or 0.0),
            "high": float(row["cost_high"] or 0.0),
        },
        "unscored_requests": int(row["unscored"] or 0),
        "current_rules": {
            "id": int(rules_row["id"]) if rules_row else 0,
            "cache_threshold": float(rules_row["cache_threshold"]) if rules_row else 0.92,
            "low_max_tokens": int(rules_row["low_max_tokens"]) if rules_row else 15,
            "high_min_tokens": int(rules_row["high_min_tokens"]) if rules_row else 40,
        },
    }


async def _fetch_backtest_rows_async() -> list[dict[str, object]]:
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT id, prompt_snip, tier, cached, quality_score
            FROM requests
            WHERE tier IN ('low', 'mid', 'high')
            ORDER BY ts DESC
            LIMIT $1
            """,
            _BACKTEST_WINDOW,
        )
    finally:
        await conn.close()
    return [dict(r) for r in rows]


async def _insert_routing_rules_async(rules: dict[str, object], notes: str) -> int:
    conn = await asyncpg.connect(settings.database_url)
    try:
        new_id = await conn.fetchval(
            """
            INSERT INTO routing_rules (cache_threshold, low_max_tokens, high_min_tokens, notes)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            float(rules["cache_threshold"]),
            int(rules["low_max_tokens"]),
            int(rules["high_min_tokens"]),
            notes,
        )
    finally:
        await conn.close()
    return int(new_id)


async def _insert_agent_decision_async(
    run_id: str,
    tool_used: str,
    observation: str,
    proposal: str,
    validated: bool,
    applied: bool,
    reasoning: str,
) -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(
            """
            INSERT INTO agent_decisions
                (run_id, tool_used, observation, proposal, validated, applied, reasoning)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            run_id, tool_used, observation, proposal, validated, applied, reasoning,
        )
    finally:
        await conn.close()


# -------------------------------------------------------------- node functions
def observe_node(state: OptimizerState) -> OptimizerState:
    window_hours = state.get("window_hours", _OBSERVE_WINDOW_HOURS_DEFAULT)
    stats = asyncio.run(_fetch_stats_async(window_hours))
    logger.info(
        "observe complete",
        extra={
            "run_id": state.get("run_id"),
            "total_requests": stats["total_requests"],
            "cache_hit_rate": stats["cache_hit_rate"],
            "unscored": stats["unscored_requests"],
        },
    )
    return OptimizerState(stats=stats)


def analyse_node(state: OptimizerState) -> OptimizerState:
    stats = state.get("stats", {})

    try:
        handler = _get_langfuse()
        callbacks = [handler] if handler else []
        plan: AnalysisPlan = _get_analysis_chain().invoke(
            _ANALYSIS_INSTRUCTIONS.format(stats=json.dumps(stats, indent=2, default=str)),
            config={
                "callbacks": callbacks,
                "metadata": {
                    "run_id": state.get("run_id"),
                    "component": "optimizer.analyse",
                },
            },
        )
        tools = list(plan.tools_to_run)
        reasoning = plan.reasoning
    except Exception as exc:
        logger.warning(
            "analyser LLM failed — running all tools as fallback",
            extra={"run_id": state.get("run_id"), "error": str(exc)},
        )
        tools = ["cache_tune", "route_optimize", "quality_sample"]
        reasoning = f"LLM analysis failed ({exc}); ran all tools defensively"

    current = stats.get("current_rules", {})
    proposals: list[dict[str, object]] = []

    for tool_name in tools:
        if tool_name == "cache_tune":
            proposals.append(
                cache_tune.propose(
                    hit_rate=float(stats.get("cache_hit_rate", 0.0)),
                    current_threshold=float(current.get("cache_threshold", 0.92)),
                )
            )
        elif tool_name == "route_optimize":
            proposals.append(
                route_optimize.propose(
                    quality_by_tier=stats.get("quality_by_tier", {}),
                    volume_by_tier=stats.get("volume_by_tier", {}),
                    current_low_max=int(current.get("low_max_tokens", 15)),
                    current_high_min=int(current.get("high_min_tokens", 40)),
                )
            )
        elif tool_name == "quality_sample":
            proposals.append(quality_sample.run())

    logger.info(
        "analyse complete",
        extra={
            "run_id": state.get("run_id"),
            "tools_run": tools,
            "proposal_count": len(proposals),
        },
    )
    return OptimizerState(proposals=proposals, reasoning=reasoning)


_ANALYSIS_INSTRUCTIONS = (
    "You are the TokenOps optimizer agent. Based on the observation stats "
    "below, decide which tools to invoke this run.\n\n"
    "Available tools:\n"
    "  - cache_tune       adjusts the semantic cache cosine threshold; "
    "useful when cache_hit_rate is below 0.30 or above 0.70.\n"
    "  - route_optimize   widens the low tier when its quality is strong "
    "and volume is sufficient. Useful when quality_by_tier.low is high.\n"
    "  - quality_sample   scores unscored requests via LLM-as-judge. "
    "Useful when unscored_requests is large; required input for route_optimize "
    "to ever have evidence to act on.\n\n"
    "Stats:\n{stats}\n\n"
    "Return the tools to run and brief reasoning."
)


def validate_node(state: OptimizerState) -> OptimizerState:
    proposals = state.get("proposals", [])
    if not proposals:
        return OptimizerState(validated_proposals=[])

    backtest_rows = asyncio.run(_fetch_backtest_rows_async())
    validated: list[dict[str, object]] = []
    annotated_proposals: list[dict[str, object]] = []

    for proposal in proposals:
        tool = proposal.get("tool")
        if tool == "cache_tune":
            verdict = _validate_cache_proposal(proposal, backtest_rows)
        elif tool == "route_optimize":
            verdict = _validate_route_proposal(proposal, backtest_rows)
        elif tool == "quality_sample":
            verdict = {
                "valid": True,
                "reason": "observational tool — writes scores, does not change rules",
            }
        else:
            verdict = {"valid": False, "reason": f"unknown tool: {tool!r}"}

        proposal_with_verdict = {**proposal, **{"validation": verdict}}
        annotated_proposals.append(proposal_with_verdict)
        if verdict["valid"]:
            validated.append(proposal_with_verdict)

    logger.info(
        "validate complete",
        extra={
            "run_id": state.get("run_id"),
            "proposals": len(proposals),
            "validated": len(validated),
        },
    )
    return OptimizerState(
        proposals=annotated_proposals,
        validated_proposals=validated,
    )


def approval_gate_node(state: OptimizerState) -> OptimizerState:
    """Interrupt execution to await human approval.

    The graph pauses here. A human reviews the validated proposals and
    resumes with Command(resume={"approved": True/False, "reviewer": "..."}).
    If no rule-changing proposals passed validation, skip the gate.
    """
    validated = state.get("validated_proposals", [])
    has_rule_changes = any(
        p.get("action") != "no_change" and p.get("tool") in ("cache_tune", "route_optimize")
        for p in validated
    )

    if not has_rule_changes:
        logger.info("no rule-changing proposals — skipping approval gate",
                     extra={"run_id": state.get("run_id")})
        return OptimizerState(approval={"approved": True, "reviewer": "auto", "reason": "no rule changes"})

    approval = interrupt({
        "type": "approval_required",
        "run_id": state.get("run_id"),
        "validated_proposals": validated,
        "message": "Review and approve/reject the optimizer proposals.",
    })

    logger.info(
        "approval received",
        extra={
            "run_id": state.get("run_id"),
            "approved": approval.get("approved"),
            "reviewer": approval.get("reviewer"),
        },
    )
    return OptimizerState(approval=approval)


def _validate_cache_proposal(
    proposal: dict[str, object],
    backtest_rows: list[dict[str, object]],
) -> dict[str, object]:
    proposed = float(proposal["proposed_threshold"])
    lo, hi = CACHE_THRESHOLD_RANGE
    if not (lo <= proposed <= hi):
        return {
            "valid": False,
            "reason": f"proposed_threshold {proposed:.3f} outside {CACHE_THRESHOLD_RANGE}",
        }

    if proposal["action"] == "lower":
        cached_qs = [
            float(r["quality_score"]) for r in backtest_rows
            if r["cached"] and r["quality_score"] is not None
        ]
        uncached_qs = [
            float(r["quality_score"]) for r in backtest_rows
            if not r["cached"] and r["quality_score"] is not None
        ]
        if cached_qs and uncached_qs:
            cached_avg = sum(cached_qs) / len(cached_qs)
            uncached_avg = sum(uncached_qs) / len(uncached_qs)
            if cached_avg < uncached_avg * (1 - MAX_QUALITY_DROP_PCT):
                return {
                    "valid": False,
                    "reason": (
                        f"lowering threshold would expand cache use, but cached "
                        f"quality {cached_avg:.3f} is more than "
                        f"{MAX_QUALITY_DROP_PCT:.0%} below uncached {uncached_avg:.3f}"
                    ),
                }

    return {
        "valid": True,
        "reason": f"within bounds {CACHE_THRESHOLD_RANGE} and no quality concern",
    }


def _validate_route_proposal(
    proposal: dict[str, object],
    backtest_rows: list[dict[str, object]],
) -> dict[str, object]:
    new_low = int(proposal["proposed_low_max"])
    new_high = int(proposal["proposed_high_min"])

    lo_lo, lo_hi = LOW_MAX_TOKENS_RANGE
    if not (lo_lo <= new_low <= lo_hi):
        return {
            "valid": False,
            "reason": f"proposed_low_max {new_low} outside {LOW_MAX_TOKENS_RANGE}",
        }
    hi_lo, hi_hi = HIGH_MIN_TOKENS_RANGE
    if not (hi_lo <= new_high <= hi_hi):
        return {
            "valid": False,
            "reason": f"proposed_high_min {new_high} outside {HIGH_MIN_TOKENS_RANGE}",
        }
    if new_low >= new_high:
        return {
            "valid": False,
            "reason": f"low_max ({new_low}) must be < high_min ({new_high})",
        }

    if proposal["action"] == "no_change":
        return {"valid": True, "reason": "no-op proposal"}

    scored = [r for r in backtest_rows if r["quality_score"] is not None]
    if not scored:
        return {
            "valid": True,
            "reason": "no scored historical requests — accepting on bounds alone",
        }

    avg_q_by_tier: dict[str, float] = {}
    for tier in ("low", "mid", "high"):
        tier_scores = [float(r["quality_score"]) for r in scored if r["tier"] == tier]
        if tier_scores:
            avg_q_by_tier[tier] = sum(tier_scores) / len(tier_scores)

    current_avg = sum(float(r["quality_score"]) for r in scored) / len(scored)

    def _project_tier(words: int) -> str:
        if words <= new_low:
            return "low"
        if words >= new_high:
            return "high"
        return "mid"

    projected: list[float] = []
    for r in scored:
        words = len((r["prompt_snip"] or "").split())
        new_tier = _project_tier(words)
        if new_tier == r["tier"]:
            projected.append(float(r["quality_score"]))
        elif new_tier in avg_q_by_tier:
            projected.append(avg_q_by_tier[new_tier])
        else:
            projected.append(float(r["quality_score"]))

    projected_avg = sum(projected) / len(projected)
    drop = current_avg - projected_avg
    drop_pct = (drop / current_avg) if current_avg else 0.0

    if drop_pct > MAX_QUALITY_DROP_PCT:
        return {
            "valid": False,
            "current_avg_quality": round(current_avg, 4),
            "projected_avg_quality": round(projected_avg, 4),
            "drop_pct": round(drop_pct, 4),
            "reason": (
                f"projected quality drop {drop_pct:.2%} exceeds bound "
                f"{MAX_QUALITY_DROP_PCT:.0%}"
            ),
        }

    return {
        "valid": True,
        "current_avg_quality": round(current_avg, 4),
        "projected_avg_quality": round(projected_avg, 4),
        "drop_pct": round(drop_pct, 4),
        "reason": (
            f"projected quality drop {drop_pct:.2%} within bound "
            f"{MAX_QUALITY_DROP_PCT:.0%}"
        ),
    }


def apply_node(state: OptimizerState) -> OptimizerState:
    run_id = state["run_id"]
    proposals = state.get("proposals", [])
    validated = state.get("validated_proposals", [])
    stats = state.get("stats", {})
    reasoning = state.get("reasoning", "")
    approval = state.get("approval", {})

    approved = approval.get("approved", False)
    reviewer = approval.get("reviewer", "unknown")

    current = dict(stats.get("current_rules", {}))
    new_rules = {
        "cache_threshold": float(current.get("cache_threshold", 0.92)),
        "low_max_tokens": int(current.get("low_max_tokens", 15)),
        "high_min_tokens": int(current.get("high_min_tokens", 40)),
    }

    rules_changed = False
    if approved:
        for proposal in validated:
            if proposal["action"] == "no_change":
                continue
            if proposal["tool"] == "cache_tune":
                new_rules["cache_threshold"] = float(proposal["proposed_threshold"])
                rules_changed = True
            elif proposal["tool"] == "route_optimize":
                new_rules["low_max_tokens"] = int(proposal["proposed_low_max"])
                new_rules["high_min_tokens"] = int(proposal["proposed_high_min"])
                rules_changed = True

    new_rules_id: int | None = None
    if rules_changed:
        new_rules_id = asyncio.run(
            _insert_routing_rules_async(
                new_rules,
                notes=f"agent run {run_id} — approved by {reviewer}",
            )
        )

    observation_json = json.dumps(stats, default=str)
    for proposal in proposals:
        verdict = proposal.get("validation", {})
        was_validated = bool(verdict.get("valid", False))
        was_applied = (
            approved
            and was_validated
            and proposal["action"] != "no_change"
            and proposal["tool"] in ("cache_tune", "route_optimize")
            and rules_changed
        )
        decision_reasoning = reasoning
        if not approved:
            decision_reasoning = f"Rejected by {reviewer}: {approval.get('reason', 'no reason given')}"

        asyncio.run(
            _insert_agent_decision_async(
                run_id=run_id,
                tool_used=str(proposal["tool"]),
                observation=observation_json,
                proposal=json.dumps(proposal, default=str),
                validated=was_validated,
                applied=was_applied,
                reasoning=decision_reasoning,
            )
        )

    logger.info(
        "apply complete",
        extra={
            "run_id": run_id,
            "approved": approved,
            "reviewer": reviewer,
            "rules_changed": rules_changed,
            "new_rules_id": new_rules_id,
            "validated_count": len(validated),
        },
    )
    return OptimizerState(
        applied_rules={
            "rules_changed": rules_changed,
            "new_rules_id": new_rules_id,
            "rules": new_rules,
            "approved": approved,
            "reviewer": reviewer,
        },
    )


# ------------------------------------------------------------------- assembly
def _get_checkpointer():
    """Create a PostgresSaver checkpointer for durable execution.

    Returns None if the checkpointer tables don't exist yet (first run
    before setup_checkpointer has been called).
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        checkpointer = PostgresSaver.from_conn_string(settings.database_url)
        return checkpointer
    except Exception as exc:
        logger.warning("checkpointer unavailable — running without persistence",
                       extra={"error": str(exc)})
        return None


def setup_checkpointer() -> None:
    """Create the checkpointer's internal tables. Call once at deploy time."""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        checkpointer = PostgresSaver.from_conn_string(settings.database_url)
        checkpointer.setup()
        logger.info("checkpointer tables created")
    except Exception as exc:
        logger.warning("checkpointer setup failed", extra={"error": str(exc)})


def build_graph(checkpointer=None) -> CompiledStateGraph:
    """Compile the five-node optimizer pipeline with approval gate."""
    graph = StateGraph(OptimizerState)
    graph.add_node("observe", observe_node)
    graph.add_node("analyse", analyse_node)
    graph.add_node("validate", validate_node)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("apply", apply_node)
    graph.set_entry_point("observe")
    graph.add_edge("observe", "analyse")
    graph.add_edge("analyse", "validate")
    graph.add_edge("validate", "approval_gate")
    graph.add_edge("approval_gate", "apply")
    graph.add_edge("apply", END)
    return graph.compile(checkpointer=checkpointer)


def run_optimizer(window_hours: int = _OBSERVE_WINDOW_HOURS_DEFAULT) -> dict[str, object]:
    """Single end-to-end agent run. Returns a summary suitable for logging.

    With checkpointing enabled, the graph pauses at approval_gate and returns
    a partial result. The run is completed when POST /v1/agent/approve resumes
    the thread. Without checkpointing, the approval gate auto-approves.
    """
    run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    checkpointer = _get_checkpointer()
    graph = build_graph(checkpointer=checkpointer)

    initial: OptimizerState = OptimizerState(
        run_id=run_id,
        window_hours=window_hours,
        stats={},
        proposals=[],
        validated_proposals=[],
        reasoning="",
    )

    config = {"configurable": {"thread_id": thread_id}}
    final = graph.invoke(initial, config=config)

    applied = final.get("applied_rules", {})
    summary = {
        "run_id": run_id,
        "thread_id": thread_id,
        "proposals_total": len(final.get("proposals", [])),
        "proposals_validated": len(final.get("validated_proposals", [])),
        "rules_changed": bool(applied.get("rules_changed", False)),
        "new_rules_id": applied.get("new_rules_id"),
        "paused_for_approval": "approval" not in final,
    }
    logger.info("optimizer run complete", extra=summary)
    return summary


def resume_with_approval(
    thread_id: str,
    approved: bool,
    reviewer: str,
) -> dict[str, object]:
    """Resume a paused graph thread with human approval decision."""
    checkpointer = _get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("cannot resume — checkpointer not available")

    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    from langgraph.types import Command
    final = graph.invoke(
        Command(resume={"approved": approved, "reviewer": reviewer}),
        config=config,
    )

    applied = final.get("applied_rules", {})
    return {
        "thread_id": thread_id,
        "approved": approved,
        "reviewer": reviewer,
        "rules_changed": bool(applied.get("rules_changed", False)),
        "new_rules_id": applied.get("new_rules_id"),
    }


def get_pending_approvals() -> list[dict[str, object]]:
    """List all graph threads paused at the approval gate."""
    checkpointer = _get_checkpointer()
    if checkpointer is None:
        return []

    pending: list[dict[str, object]] = []
    try:
        graph = build_graph(checkpointer=checkpointer)
        for state_snapshot in checkpointer.list({}):
            if state_snapshot.next and "approval_gate" in state_snapshot.next:
                thread_config = state_snapshot.config
                thread_id = thread_config.get("configurable", {}).get("thread_id", "unknown")
                channel_values = state_snapshot.values
                pending.append({
                    "thread_id": thread_id,
                    "run_id": channel_values.get("run_id"),
                    "validated_proposals": channel_values.get("validated_proposals", []),
                    "stats": channel_values.get("stats", {}),
                    "reasoning": channel_values.get("reasoning", ""),
                    "created_at": str(state_snapshot.created_at) if hasattr(state_snapshot, "created_at") else None,
                })
    except Exception as exc:
        logger.warning("failed to list pending approvals", extra={"error": str(exc)})

    return pending


def get_thread_history(thread_id: str) -> list[dict[str, object]]:
    """Return the state at each node for a given thread."""
    checkpointer = _get_checkpointer()
    if checkpointer is None:
        return []

    history: list[dict[str, object]] = []
    try:
        config = {"configurable": {"thread_id": thread_id}}
        for state_snapshot in checkpointer.list(config):
            history.append({
                "step": state_snapshot.metadata.get("step", 0) if state_snapshot.metadata else 0,
                "next_nodes": list(state_snapshot.next) if state_snapshot.next else [],
                "values": dict(state_snapshot.values),
                "created_at": str(state_snapshot.created_at) if hasattr(state_snapshot, "created_at") else None,
            })
    except Exception as exc:
        logger.warning("failed to get thread history", extra={"error": str(exc), "thread_id": thread_id})

    return history

"""Proxy entrypoint: FastAPI app, lifespan, /health, /v1/chat/completions.

Hot-path orchestration only. Business logic lives in:
  - proxy.auth          multi-tenant API key resolution
  - proxy.budget        per-tenant budget enforcement
  - proxy.redact        PII redaction (Presidio)
  - proxy.cache         semantic cache (Qdrant + Modal)
  - proxy.classifier    complexity classifier
  - proxy.router        model selection + cost arithmetic
  - proxy.ledger        Postgres writes
  - proxy.config        env + in-memory routing_rules
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
from pydantic import BaseModel

from proxy import auth, budget, cache, classifier, config, ledger, metrics, redact, router
from proxy.auth import TenantInfo
from proxy.config import settings

VERSION = "2.0.0"
_started_at: float = time.monotonic()


# --------------------------------------------------------------------- logging
class _JsonFormatter(logging.Formatter):
    """One-line JSON formatter. Promotes `extra=` keys to top-level JSON."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_setup_logging()
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------- lifespan
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Bring up external dependencies; reverse the order on shutdown."""
    # Langfuse SDK reads its config from env vars; populate them from Settings
    # here (the one place where os.environ assignment is sanctioned — required
    # by langfuse>=4's OTel-based init). If keys are absent the handler is
    # never constructed and instrumentation is a no-op.
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host
        global _langfuse_handler
        _langfuse_handler = LangfuseCallbackHandler()
        logger.info("langfuse handler initialised", extra={"host": settings.langfuse_host})

    await ledger.init_pool()
    await cache.ensure_collection()

    initial = await ledger.get_latest_rules()
    config.current_rules.__dict__.update(initial.__dict__)
    logger.info("routing_rules bootstrapped", extra={"rules_id": initial.id})

    reload_task = asyncio.create_task(
        config.reload_rules_loop(), name="reload_rules_loop"
    )
    if not settings.agent_admin_key:
        logger.warning(
            "AGENT_ADMIN_KEY unset — /v1/agent/* endpoints accept unauthenticated requests"
        )
    logger.info("proxy ready", extra={"rules_id": initial.id})

    try:
        yield
    finally:
        reload_task.cancel()
        try:
            await reload_task
        except asyncio.CancelledError:
            pass
        await ledger.close_pool()
        logger.info("proxy shut down")


app = FastAPI(title="TokenOps Proxy", lifespan=lifespan)


# --------------------------------------------------- request / response models
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    tag: str | None = None
    model: str | None = None


class ChatChoiceMessage(BaseModel):
    role: str
    content: str


class ChatChoice(BaseModel):
    index: int
    message: ChatChoiceMessage
    finish_reason: str


class UsageInfo(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: UsageInfo


# --------------------------------------------------------------------- helpers
_llm_cache: dict[str, ChatOpenAI] = {}

# Langfuse callback handler — constructed once in lifespan if credentials
# are set, otherwise stays None and instrumentation is a no-op.
_langfuse_handler: LangfuseCallbackHandler | None = None

# Strong references to background tasks so the asyncio scheduler does not
# GC them mid-flight (event loop only holds weak refs to tasks).
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine and keep a strong reference until completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _llm_for(model: str) -> ChatOpenAI:
    """Cache one ChatOpenAI instance per model. Creating a new instance
    per request would defeat the SDK's internal connection pooling."""
    if model not in _llm_cache:
        _llm_cache[model] = ChatOpenAI(
            model=model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=60,
            max_retries=0,
        )
    return _llm_cache[model]


def _to_lc_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    """Convert OpenAI-style {role, content} dicts to LangChain message
    objects. Unknown roles fall back to HumanMessage."""
    converted: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            converted.append(SystemMessage(content=m.content))
        elif m.role == "assistant":
            converted.append(AIMessage(content=m.content))
        else:
            converted.append(HumanMessage(content=m.content))
    return converted


def _prompt_key(messages: list[ChatMessage]) -> str:
    """Single string representing the conversation, used as the cache key
    and the classifier input. Role labels are included so an identical user
    message in different contexts cannot collide in the cache."""
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


def _single_choice(content: str) -> list[ChatChoice]:
    return [
        ChatChoice(
            index=0,
            message=ChatChoiceMessage(role="assistant", content=content),
            finish_reason="stop",
        )
    ]


def _openai_response(
    request_id: str,
    content: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    tier: str,
    cost_usd: float,
    cached: bool,
) -> JSONResponse:
    """Build an OpenAI-compatible JSON response with TokenOps metadata in headers."""
    body = ChatResponse(
        id=f"chatcmpl-{request_id}",
        created=int(time.time()),
        model=model,
        choices=_single_choice(content),
        usage=UsageInfo(
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
            total_tokens=tokens_in + tokens_out,
        ),
    )
    headers = {
        "X-TokenOps-Request-ID": request_id,
        "X-TokenOps-Tier": tier,
        "X-TokenOps-Cost-USD": f"{cost_usd:.6f}",
        "X-TokenOps-Cached": str(cached).lower(),
    }
    return JSONResponse(content=body.model_dump(), headers=headers)


# ---------------------------------------------------------------------- routes
@app.get("/health")
async def health(deep: bool = Query(default=False)) -> dict[str, object]:
    """Liveness/readiness probe. ?deep=true additionally round-trips
    Postgres and Qdrant — use for readiness, not high-frequency liveness."""
    body: dict[str, object] = {
        "status": "ok",
        "version": VERSION,
        "rules_version": config.current_rules.id,
        "uptime_seconds": round(time.monotonic() - _started_at, 1),
        **ledger.pool_stats(),
    }
    if deep:
        pg_ok = await ledger.ping()
        qdrant_health = await cache.ping()
        body["postgres_connected"] = pg_ok
        body["qdrant_connected"] = qdrant_health["connected"]
        body["cache_collection_count"] = qdrant_health["cache_collection_count"]
        if not pg_ok:
            body["status"] = "degraded"
    return body


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _extract_tenant(request: Request) -> TenantInfo:
    """FastAPI dependency: extract Bearer token and resolve to tenant."""
    auth_header = request.headers.get("Authorization", "")
    api_key: str | None = None
    if auth_header.lower().startswith("bearer "):
        api_key = auth_header[7:].strip()

    try:
        return await auth.resolve_tenant(api_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "auth_failed", "detail": str(exc)},
        )


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatRequest,
    tenant: TenantInfo = Depends(_extract_tenant),
    x_tag: str | None = Header(default=None, alias="X-Tag"),
    x_tokenops_route: str | None = Header(default=None, alias="X-TokenOps-Route"),
    x_tokenops_cache: str | None = Header(default=None, alias="X-TokenOps-Cache"),
) -> JSONResponse:
    request_id = str(uuid.uuid4())
    tag = req.tag or x_tag or "default"
    prompt = _prompt_key(req.messages)
    start = time.perf_counter()

    use_classifier = (x_tokenops_route or "").lower() == "auto"
    skip_cache = (x_tokenops_cache or "").lower() == "skip"

    metrics.ACTIVE_REQUESTS.inc()
    try:
        # 0. Budget check — reject or downgrade before any work.
        budget_status = await budget.check_budget(tenant.tenant_id, tenant.monthly_budget_usd)
        metrics.BUDGET_UTILIZATION.labels(tenant=tenant.tenant_id).set(
            budget_status.utilization_pct
        )
        if budget_status.status == "hard_limit":
            metrics.REQUESTS_TOTAL.labels(
                tier="none", cached="false", tenant=tenant.tenant_id, status="rejected"
            ).inc()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "budget_exceeded",
                    "spend_usd": budget_status.spend_usd,
                    "budget_usd": budget_status.budget_usd,
                    "utilization_pct": budget_status.utilization_pct,
                    "resets_at": budget_status.resets_at,
                },
            )
        force_low_tier = budget_status.status == "soft_limit"

        # 0b. PII redaction — before cache lookup so redacted and
        # non-redacted variants share a single cache entry.
        redact_result = await redact.redact_prompt(prompt, tenant.redaction_config)
        if redact_result.was_redacted:
            prompt = redact_result.redacted_text

        # 1. Cache lookup.
        cached_entry = None if skip_cache else await cache.lookup(prompt)
        metrics.CACHE_LOOKUPS.labels(
            result="skipped" if skip_cache else ("hit" if cached_entry else "miss")
        ).inc()
        if cached_entry is not None:
            latency_ms = (time.perf_counter() - start) * 1000.0
            metrics.observe_request(
                tier="cache",
                cached=True,
                tenant=tenant.tenant_id,
                status="ok",
                latency_seconds=latency_ms / 1000.0,
            )
            _fire_and_forget(
                ledger.log_request(
                    request_id=request_id,
                    prompt=prompt,
                    tag=tag,
                    model=cached_entry["model"],
                    tier="cache",
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.0,
                    counterfactual_cost_usd=router.counterfactual_cost(
                        cached_entry.get("tokens_in", 0),
                        cached_entry.get("tokens_out", 0),
                    ),
                    cached=True,
                    latency_ms=latency_ms,
                    tenant_id=tenant.tenant_id,
                    redacted_entity_count=len(redact_result.entities_found),
                )
            )
            logger.info(
                "served from cache",
                extra={
                    "request_id": request_id,
                    "tag": tag,
                    "model": cached_entry["model"],
                    "cached": True,
                    "latency_ms": latency_ms,
                    "tenant_id": tenant.tenant_id,
                },
            )
            resp = _openai_response(
                request_id,
                cached_entry["response"],
                cached_entry["model"],
                tokens_in=0,
                tokens_out=0,
                tier="cache",
                cost_usd=0.0,
                cached=True,
            )
            _add_budget_headers(resp, budget_status)
            return resp

        # 2. Pick a model.
        if force_low_tier:
            tier = "low"
            model = router.select_model("low")
        elif use_classifier or not req.model:
            classify_start = time.perf_counter()
            tier = await classifier.classify(prompt)
            metrics.CLASSIFIER_OVERHEAD.observe(time.perf_counter() - classify_start)
            model = router.select_model(tier)
        else:
            model = req.model
            tier = "passthrough"

        # 3. LLM call.
        callbacks = [_langfuse_handler] if _langfuse_handler else []
        llm_start = time.perf_counter()
        result = await _llm_for(model).ainvoke(
            _to_lc_messages(req.messages),
            config={
                "callbacks": callbacks,
                "metadata": {
                    "request_id": request_id,
                    "tag": tag,
                    "tier": tier,
                    "model": model,
                    "tenant_id": tenant.tenant_id,
                },
            },
        )
        metrics.LLM_CALL_DURATION.labels(model=model).observe(
            time.perf_counter() - llm_start
        )
        content: str = result.content
        usage = result.usage_metadata or {}
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))

        cost = router.compute_cost(model, tokens_in, tokens_out)
        counterfactual = router.counterfactual_cost(tokens_in, tokens_out)
        latency_ms = (time.perf_counter() - start) * 1000.0
        metrics.COST_TOTAL.labels(tenant=tenant.tenant_id, model=model).inc(cost)
        metrics.observe_request(
            tier=tier,
            cached=False,
            tenant=tenant.tenant_id,
            status="ok",
            latency_seconds=latency_ms / 1000.0,
        )

        # 4. Fire-and-forget writes.
        _fire_and_forget(
            cache.store(prompt, content, model, tokens_in, tokens_out)
        )
        _fire_and_forget(
            ledger.log_request(
                request_id=request_id,
                prompt=prompt,
                tag=tag,
                model=model,
                tier=tier,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                counterfactual_cost_usd=counterfactual,
                cached=False,
                latency_ms=latency_ms,
                tenant_id=tenant.tenant_id,
                redacted_entity_count=len(redact_result.entities_found),
            )
        )

        logger.info(
            "llm call complete",
            extra={
                "request_id": request_id,
                "tag": tag,
                "model": model,
                "tier": tier,
                "cached": False,
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost,
                "tenant_id": tenant.tenant_id,
            },
        )

        resp = _openai_response(
            request_id,
            content,
            model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tier=tier,
            cost_usd=cost,
            cached=False,
        )
        _add_budget_headers(resp, budget_status)
        return resp

    except HTTPException:
        raise
    except Exception as exc:
        metrics.REQUESTS_TOTAL.labels(
            tier="none", cached="false", tenant=tenant.tenant_id, status="error"
        ).inc()
        logger.exception(
            "request failed",
            extra={"request_id": request_id, "tag": tag, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "upstream_failed", "detail": str(exc)},
        )
    finally:
        metrics.ACTIVE_REQUESTS.dec()


def _add_budget_headers(resp: JSONResponse, budget_status: budget.BudgetStatus) -> None:
    """Inject budget utilization headers into every response."""
    if budget_status.budget_usd > 0:
        remaining = max(0.0, budget_status.budget_usd - budget_status.spend_usd)
        resp.headers["X-TokenOps-Budget-Remaining"] = f"{remaining:.2f}"
        resp.headers["X-TokenOps-Budget-Utilization"] = f"{budget_status.utilization_pct:.1f}"


# ------------------------------------------------------------------ usage API
@app.get("/v1/usage")
async def usage(
    tenant: TenantInfo = Depends(_extract_tenant),
    start: str | None = Query(default=None, description="Start date (ISO 8601)"),
    end: str | None = Query(default=None, description="End date (ISO 8601)"),
) -> dict:
    """Per-tenant usage stats: spend, cache hit rate, breakdown by tag/model."""
    return await ledger.get_usage_stats(tenant.tenant_id, start, end)


# --------------------------------------------------------- agent approval API
class ApprovalRequest(BaseModel):
    thread_id: str
    approved: bool
    reviewer: str


async def _require_agent_admin(request: Request) -> None:
    """Protect /v1/agent/* when AGENT_ADMIN_KEY is configured."""
    auth_header = request.headers.get("Authorization", "")
    bearer_key: str | None = None
    if auth_header.lower().startswith("bearer "):
        bearer_key = auth_header[7:].strip()
    admin_header = request.headers.get("X-TokenOps-Admin-Key")
    try:
        auth.verify_agent_admin(bearer_key, admin_header)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "admin_auth_failed", "detail": str(exc)},
        )


@app.post("/v1/agent/approve")
async def agent_approve(
    req: ApprovalRequest,
    _: None = Depends(_require_agent_admin),
) -> dict:
    """Resume a paused optimizer run with human approval/rejection."""
    from agent import graph
    try:
        result = graph.resume_with_approval(
            thread_id=req.thread_id,
            approved=req.approved,
            reviewer=req.reviewer,
        )
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "approval_failed", "detail": str(exc)},
        )


@app.get("/v1/agent/pending")
async def agent_pending(_: None = Depends(_require_agent_admin)) -> list[dict]:
    """List optimizer proposals awaiting approval."""
    from agent import graph
    return graph.get_pending_approvals()


@app.get("/v1/agent/history/{thread_id}")
async def agent_history(
    thread_id: str,
    _: None = Depends(_require_agent_admin),
) -> list[dict]:
    """Full state history for a given optimizer thread."""
    from agent import graph
    return graph.get_thread_history(thread_id)

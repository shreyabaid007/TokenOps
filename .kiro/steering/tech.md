---
inclusion: always
---

# Tech: TokenOps v2

## Runtime and language

- **Python 3.11+** — all services
- **Type hints required** on all function signatures
- **Pydantic v2** for all request/response models and config validation
- **No `Any` types** — be explicit

## Core stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Proxy API | FastAPI + Uvicorn | Async, OpenAI-compatible routing |
| LLM calls | `langchain-openai` | OpenRouter-compatible interface, usage metadata |
| Agent orchestration | LangGraph + PostgresSaver | Stateful agent loop, checkpointing, interrupt |
| Semantic cache | Qdrant | Local Docker, ANN search, payload filters |
| Embeddings | Modal + `BAAI/bge-small-en-v1.5` | GPU endpoint, 384-dim, normalized |
| Database | PostgreSQL via `asyncpg` | Cost ledger, routing rules, agent decisions, tenants |
| PII redaction | Presidio (analyzer + anonymizer) + spaCy `en_core_web_sm` | NER-based entity detection; model baked into Docker/Modal images |
| Dashboard | Streamlit | Fast to build, sufficient for portfolio |
| Scheduling | APScheduler (local) / Modal cron (prod) | Runs optimizer agent every 15 minutes |
| Config | `python-dotenv` + Pydantic Settings | Type-safe env loading |
| LLM observability | Langfuse | Per-call traces, token cost, prompt versioning |
| Metrics | prometheus_client | Prometheus-compatible /metrics endpoint |

## LLM model tiers

```python
MODEL_MAP = {
    "low":  "anthropic/claude-haiku-4-5",   # fast, cheap - simple tasks
    "mid":  "anthropic/claude-sonnet-4-5",  # balanced - moderate reasoning
    "high": "anthropic/claude-opus-4-5",    # full power - complex tasks
}

COST_PER_1K_TOKENS = {
    "anthropic/claude-haiku-4-5":  {"in": 0.00025, "out": 0.00125},
    "anthropic/claude-sonnet-4-5": {"in": 0.003,   "out": 0.015},
    "anthropic/claude-opus-4-5":   {"in": 0.015,   "out": 0.075},
}
```

## Key architectural constraints

**Data plane must be fast — agent plane must be off the hot path.**
The proxy handles every request synchronously. The optimizer agent runs
on a background schedule and communicates via the database only.
Never call the agent from within a request handler.

**Async everywhere in the proxy.**
Use `async def` for all FastAPI route handlers and database operations.
Use `asyncpg` connection pool (min=2, max=10). Cache writes and ledger
writes are fire-and-forget (`asyncio.create_task`) — they must not
block the response.

**Config hot-reload without restart.**
The proxy reads `routing_rules` from Postgres on startup and caches it
in memory. A background `asyncio` task polls for a newer row every 60
seconds and swaps the in-memory config atomically. The agent writes to
this table; the proxy reads it. They never share in-process state.

**Agent safety first.**
Every proposal from the optimizer agent goes through `validate_node`
before being applied. Validation back-tests the proposed rule against
the last 500 requests. If projected quality score drops > 5%, the
proposal is rejected and logged. Safety bounds are hard-coded constants,
not config. In v2, an additional `approval_gate` node interrupts
execution for human review before any rule change is applied.

**Multi-tenant isolation.**
Every request is attributed to a tenant via API key auth. Budget checks,
ledger writes, and usage queries are all scoped by `tenant_id`. The
default anonymous tenant provides backward compatibility with v1 clients.

**PII never reaches the LLM or cache.**
Redaction happens before cache lookup so redacted variants share entries.
The Qdrant cache stores only redacted prompts.

## Dependency management

```
# requirements.txt (exact pins for reproducibility)
fastapi==0.136.3
uvicorn[standard]==0.48.0
langchain==0.3.30
langchain-openai==0.3.0
langgraph==0.2.76
langgraph-checkpoint-postgres==2.0.7
qdrant-client==1.18.0
asyncpg==0.29.0
pydantic-settings==2.3.0
modal==1.4.3
streamlit==1.57.0
apscheduler==3.10.4
python-dotenv==1.0.1
httpx==0.27.0
langfuse==4.6.1
presidio-analyzer==2.2.356
presidio-anonymizer==2.2.356
prometheus-client==0.21.0
psycopg[binary]==3.3.4
```

Do not add dependencies not in this list without updating it first.
Prefer stdlib solutions when a dependency can be avoided.

## Environment variables

All loaded via Pydantic Settings in `proxy/config.py`. Never access
`os.environ` directly elsewhere in the codebase.

```
OPENROUTER_API_KEY         # required
OPENROUTER_BASE_URL        # default: https://openrouter.ai/api/v1
DATABASE_URL               # postgresql://... (asyncpg native URL, no +asyncpg suffix)
QDRANT_URL                 # full URL including https:// and port
QDRANT_API_KEY             # optional — leave blank for local Qdrant (Docker)
MODAL_EMBEDDER_APP         # modal app name for embedder
PROXY_URL                  # http://localhost:8000 (used by host_app)
AGENT_RUN_INTERVAL_MINUTES # default: 15
RULES_RELOAD_INTERVAL_SEC  # default: 60
AGENT_ADMIN_KEY            # optional — when set, /v1/agent/* requires Bearer or X-TokenOps-Admin-Key
LANGFUSE_PUBLIC_KEY        # optional — Langfuse LLM observability
LANGFUSE_SECRET_KEY        # optional
LANGFUSE_HOST              # default: https://us.cloud.langfuse.com
```

## Error handling conventions

- Proxy route handlers: catch all exceptions, log with structured logging,
  return HTTP 502 with `{"error": "upstream_failed", "detail": str(e)}`.
  Never let an unhandled exception reach the client.
- Auth failures: return 401 with `{"error": "auth_failed", "detail": ...}`.
- Budget hard limit: return 429 with spend, budget, utilization, reset date.
- Agent tools: return `{"success": False, "reason": str(e)}` on failure.
- Modal cold start: if embedding call times out after 4 seconds, log a
  warning and skip cache lookup — route the request normally.

## Logging

Use Python `logging` with structured output (JSON in production, plain in
development). Every log line from the proxy must include:
`request_id`, `tenant_id`, `tag`, `model`, `cached`, `latency_ms`.

Do not use `print()` anywhere in the codebase.

## Testing conventions

- Unit tests in `tests/` using `pytest` and `pytest-asyncio`
- Mock Modal, Qdrant, and Postgres in unit tests — no real network calls
- Integration tests in `tests/integration/` — require Docker services
- Test file naming: `test_<module_name>.py`
- Minimum coverage target: 70% on `proxy/` module

## Commands

```bash
# Development
uvicorn proxy.main:app --port 8000 --reload
uvicorn host_app.main:app --port 8001 --reload
python agent/scheduler.py
streamlit run dashboard/app.py

# Infrastructure
docker-compose up -d            # Qdrant + Postgres
psql $DATABASE_URL < db/schema.sql

# Modal
modal deploy modal_app/embedder.py
modal deploy modal_app/proxy_app.py
modal deploy modal_app/agent_app.py
./scripts/deploy_modal.sh          # all three + bootstrap notes

# Production DB bootstrap (schema + LangGraph checkpointer)
python scripts/setup_production_db.py

# Tests
pytest tests/ -v
pytest tests/integration/ -v --run-integration

# Eval pipeline
python -m eval.run --experiment routing --policy smart-routing
python -m eval.run --experiment frontier
python -m eval.ci_gate --junit-out eval-results.xml

# Demo
python scripts/seed_demo_traffic.py

# Production stack (Docker)
docker compose -f docker-compose.prod.yml up -d
```

---
inclusion: always
---

# Structure: TokenOps v2

## Repository layout

```
tokenops/
│
├── .kiro/
│   └── steering/
│       ├── product.md          # what we are building and why
│       ├── tech.md             # stack, conventions, commands
│       └── structure.md        # this file — where everything lives
│
├── proxy/                      # DATA PLANE — hot path, every LLM request
│   ├── main.py                 # FastAPI app, routes, lifespan
│   ├── auth.py                 # multi-tenant API key resolution
│   ├── budget.py               # per-tenant budget enforcement
│   ├── redact.py               # PII redaction (Presidio)
│   ├── cache.py                # Qdrant semantic cache (lookup + store)
│   ├── classifier.py           # LLM-as-judge complexity scorer
│   ├── router.py               # model selection + cost calculation
│   ├── ledger.py               # async Postgres writes per request
│   └── config.py               # Pydantic Settings, hot-reload logic
│
├── agent/                      # AGENT PLANE — off hot path, scheduled
│   ├── graph.py                # LangGraph state machine (v2: checkpointer + interrupt)
│   ├── scheduler.py            # APScheduler, fires graph every 15 min
│   └── tools/
│       ├── cache_tune.py       # adjusts cosine similarity threshold
│       ├── route_optimize.py   # adjusts tier token boundaries
│       └── quality_sample.py   # LLM-as-judge on sampled responses
│
├── host_app/                   # DEMO HOST — generates realistic traffic
│   ├── main.py                 # FastAPI, 4 AI endpoints
│   └── prompts.py              # prompt templates per endpoint
│
├── dashboard/
│   └── app.py                  # Streamlit, 5 panels (v2: approvals + budget)
│
├── modal_app/
│   ├── embedder.py             # Modal GPU endpoint, bge-small-en-v1.5
│   ├── proxy_app.py            # Modal web endpoint wrapping proxy
│   ├── dashboard_app.py        # Modal web endpoint wrapping dashboard
│   └── agent_app.py            # Modal cron function running optimizer
│
├── eval/                       # QUALITY PIPELINE — offline, never on hot path
│   ├── golden_dataset.json     # 50 labelled prompts across endpoints/tiers
│   ├── evaluators.py           # routing correctness, LLM-as-judge, faithfulness
│   ├── experiments.py          # routing experiments, cost-quality frontier
│   ├── run.py                  # CLI: python -m eval.run
│   └── ci_gate.py              # CI gate: python -m eval.ci_gate (JUnit XML)
│
├── db/
│   └── schema.sql              # all table definitions (source of truth)
│
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_budget.py
│   ├── test_redact.py
│   ├── test_graph_v2.py
│   ├── test_eval.py
│   ├── test_cache.py
│   ├── test_router.py
│   └── integration/
│       └── test_proxy_e2e.py
│
├── scripts/
│   └── seed_demo_traffic.py    # 200 synthetic requests for demo
│
├── deploy/
│   ├── prometheus.yml          # scrape config for the prod compose stack
│   └── helm/tokenops/          # Helm chart (proxy HPA, agent CronJob, dashboard, Qdrant)
│
├── docs/
│   ├── observability.md        # metrics catalog, OTel plan, alerting rules
│   └── runbook.md              # productionization checklist + operational procedures
│
├── .github/workflows/ci.yml    # lint + tests + eval gate → build → promote
├── Dockerfile                  # proxy (multi-stage, non-root)
├── Dockerfile.agent
├── Dockerfile.dashboard
├── docker-compose.yml          # local dev: Postgres + Qdrant
├── docker-compose.prod.yml     # prod-style stack: Traefik, replicas, Prometheus/Grafana
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── CLAUDE.md
└── README.md
```

## Module responsibilities — what lives where

### `proxy/main.py`
- FastAPI app instantiation and `lifespan` context manager
- Main route: `POST /v1/chat/completions` (OpenAI-compatible)
- Usage API: `GET /v1/usage` (per-tenant cost breakdown)
- Agent approval API: `POST /v1/agent/approve`, `GET /v1/agent/pending`,
  `GET /v1/agent/history/{thread_id}`
- Health check: `GET /health`
- Orchestrates: auth → budget → redact → cache → classify → route → call → store → log
- Does NOT contain business logic — delegates to other proxy modules

### `proxy/auth.py`
- `resolve_tenant(api_key) -> TenantInfo` — SHA-256 hash lookup with TTL cache
- Returns default anonymous tenant when no key provided (backward compat)
- Raises `ValueError` on invalid or inactive keys
- `clear_cache()` — invalidate after key rotation

### `proxy/budget.py`
- `check_budget(tenant_id, monthly_budget_usd) -> BudgetStatus`
- Thresholds: 80% soft limit (downgrade), 100% hard limit (reject)
- Spend cached 30s per tenant to avoid DB pressure
- Zero budget means unlimited (default tenant)

### `proxy/redact.py`
- `redact_prompt(text, config) -> RedactResult` — Presidio-based PII stripping
- Configurable per tenant: entity types, action (redact/mask/hash), enable/disable
- Runs before cache lookup so redacted variants share entries
- Graceful fallback if Presidio not installed

### `proxy/cache.py`
- `ensure_collection()` — idempotent Qdrant collection setup
- `lookup(prompt) -> dict | None` — embed + ANN search
- `store(prompt, response, model, tokens_in, tokens_out)` — embed + upsert
- Uses Modal embedder via `modal.Function.lookup()`
- 8-second timeout on Modal calls — returns None on timeout

### `proxy/classifier.py`
- `classify(prompt) -> Literal["low", "mid", "high"]`
- Two-stage: word-count bands → LLM-as-judge for ambiguous middle
- Soft-fails to `"mid"` on any classifier exception

### `proxy/router.py`
- `select_model(tier) -> str` — reads from MODEL_MAP
- `compute_cost(model, tokens_in, tokens_out) -> float`
- `counterfactual_cost(tokens_in, tokens_out) -> float`
- Pure functions, no I/O

### `proxy/ledger.py`
- `init_pool()` / `close_pool()` — asyncpg lifecycle
- `log_request(...)` — fire-and-forget insert with tenant_id, redacted_entity_count
- `get_latest_rules() -> RoutingRules`
- `get_tenant_spend(tenant_id) -> float` — monthly spend aggregate
- `get_usage_stats(tenant_id, start, end) -> dict` — full usage breakdown

### `proxy/config.py`
- `Settings` class — all env vars
- `RoutingRules` dataclass — in-memory snapshot
- `reload_rules_loop()` — polls DB every 60s

### `agent/graph.py`
- v2: 5-node pipeline: observe → analyse → validate → approval_gate → apply
- `OptimizerState` TypedDict with approval field
- `approval_gate_node` — calls `interrupt()` for human review
- `build_graph(checkpointer)` — compiles with PostgresSaver
- `run_optimizer()` — full agent run
- `resume_with_approval(thread_id, approved, reviewer)` — resume paused graph
- `get_pending_approvals()` — list threads awaiting approval
- `get_thread_history(thread_id)` — debug state at each node
- Safety bounds: hard-coded, not config

### `agent/tools/cache_tune.py`
- Pure proposal logic for cache threshold adjustments

### `agent/tools/route_optimize.py`
- Pure proposal logic for tier band adjustments

### `agent/tools/quality_sample.py`
- LLM-as-judge scoring of unscored requests

### `eval/` (quality pipeline — offline only)
- `evaluators.py` — `routing_correctness` (deterministic), `response_quality`
  and `faithfulness` (LLM-as-judge via Haiku, lazy settings import so the
  deterministic path runs without a .env)
- `experiments.py` — `run_routing_experiment(dataset, policy)` and
  `run_cost_quality_frontier(dataset, policies)` with Pareto marking;
  persists summaries to `eval_runs`
- `run.py` — CLI (`python -m eval.run --experiment routing|frontier`)
- `ci_gate.py` — fails CI when avg routing quality < 0.85 or avg cost
  regresses > 10% vs the last passing baseline; emits JUnit XML

### `proxy/metrics.py`
- Prometheus instruments (histograms/counters/gauges), `tokenops_` prefix
- `observe_request(...)` records terminal latency + outcome in one call
- Exposed at `GET /metrics`; recording is in-memory only — hot-path safe

### `host_app/main.py`
- Four demo endpoints: `/sql-analyst`, `/code-reviewer`, `/log-explainer`, `/doc-writer`

### `dashboard/app.py`
- Panel 1: overview metrics (spend, savings, cache hit rate, tier chart)
- Panel 2: cost by tag & model
- Panel 3: agent decisions + pending approval actions (approve/reject buttons)
- Panel 4: budget utilization gauges per tenant
- Panel 5: recent request feed with tenant_id and PII entity count

## Database tables

All definitions in `db/schema.sql`. Modules must NOT define schema in code.

| Table | Written by | Read by |
|-------|-----------|---------|
| `tenants` | admin / seed | proxy/auth.py |
| `routing_rules` | optimizer agent | proxy config hot-reload |
| `requests` | proxy ledger | agent, dashboard, budget.py |
| `agent_decisions` | optimizer agent | dashboard panel 3 |
| `eval_runs` | eval pipeline | dashboard (future) |

## Data flow — request lifecycle (v2)

```
host_app endpoint
    │  POST /v1/chat/completions  {messages, model?, tag?}
    │  Headers: Authorization, X-Tag, X-TokenOps-Route, X-TokenOps-Cache
    ▼
proxy/main.py
    │
    ├─ auth.resolve_tenant(api_key)              # → tenant_id, budget, redact config
    │
    ├─ budget.check_budget(tenant_id, budget)    # → ok / soft_limit / hard_limit (429)
    │
    ├─ redact.redact_prompt(prompt, config)       # → redacted text, entity count
    │
    ├─ cache.lookup(redacted_prompt)              # skipped if X-TokenOps-Cache: skip
    │       ├─ HIT  → fire-and-forget: ledger.log(cached=True, tenant_id)
    │       │         return + budget headers
    │       └─ MISS → continue
    │
    ├─ Routing decision:
    │       ├─ soft_limit → force tier="low" (Haiku)
    │       ├─ auto       → classifier.classify → router.select_model
    │       └─ passthrough → use req.model
    │
    ├─ LLM call (langchain-openai via OpenRouter)
    │
    ├─ asyncio.create_task(cache.store(...))
    ├─ asyncio.create_task(ledger.log_request(..., tenant_id, entity_count))
    │
    └─ return OpenAI-spec JSON + X-TokenOps-* + X-TokenOps-Budget-* headers
```

## Data flow — agent cycle (v2)

```
scheduler.py → agent/graph.py:run_optimizer()
    │
    ├─ observe_node: aggregate stats from requests
    │
    ├─ analyse_node (LLM): decides which tools to call
    │       ├─ tools/cache_tune.py      → proposal
    │       ├─ tools/route_optimize.py  → proposal
    │       └─ tools/quality_sample.py  → writes scores
    │
    ├─ validate_node: back-test proposals against last 500 requests
    │
    ├─ approval_gate_node: interrupt() ← pauses for human approval
    │       └─ resumed via POST /v1/agent/approve
    │
    └─ apply_node:
            ├─ if approved → INSERT into routing_rules
            └─ INSERT all decisions → agent_decisions (audit trail)
```

## Naming conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions and variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Database columns: `snake_case`
- API tags (cost attribution): `kebab-case` (e.g. `sql-analyst`)
- Tenant IDs: `kebab-case` (e.g. `genie-platform`)

## Where NOT to put things

- No business logic in `main.py` — it orchestrates only
- No database calls in `router.py` or `classifier.py` — pure functions
- No LLM calls in `cache.py` — it calls Modal, not Anthropic directly
- No agent code in the `proxy/` directory — strict plane separation
- No Streamlit imports outside `dashboard/`
- No hardcoded API keys anywhere — always from `config.Settings`
- No PII in cache or logs — always redact first

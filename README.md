# TokenOps

**LLM cost governance platform — multi-tenant attribution, budget enforcement, PII redaction, semantic caching, and a self-optimizing agent with human-in-the-loop approval.**

Point your base URL at TokenOps. Every LLM call is authenticated to a tenant, checked against its budget, scrubbed of PII, served from semantic cache when possible, and routed to the cheapest capable model — while a scheduled optimizer agent proposes rule changes that a human approves before they apply.

```
Your app ──▶ TokenOps proxy ──▶ LLM providers (via OpenRouter)
                  │
   ATTRIBUTE      ├── tenant auth          API key → tenant, per-team cost ledger
                  ├── budget enforcement   80% soft warn, 100% hard block (HTTP 429)
   GUARD          ├── PII redaction        Presidio NER before LLM and cache
                  ├── semantic cache       meaning-match, not string-match
   OPTIMIZE       ├── complexity router    Haiku / Sonnet / Opus by task
                  └── optimizer agent      proposes rule changes → human approves → applies
```

### Demo results (350+ requests)

| Metric | Value |
|--------|-------|
| Cost reduction vs all-Opus | **68%** ($1.86 actual vs $3.94 baseline) |
| Cache hit rate | **52%** — 186 of 359 requests served free |
| Proxy overhead (p99) | **< 50ms** added latency |
| Optimizer interventions | Cache threshold tuned 0.92 → 0.86 autonomously |

---

## Dashboard

<p>
  <img src="streamlit1.png" alt="Dashboard — overview and cost by tag" width="100%">
</p>
<p>
  <img src="streamlit2.png" alt="Dashboard — agent decisions and request feed" width="100%">
</p>

Five live panels reading directly from Postgres: headline metrics with tier breakdown, cost by tag and tenant, per-tenant budget gauges, pending agent approvals with one-click review, and a sortable request feed showing model, tier, cache status, latency, and cost for every call.

---

## How it works

Three planes, strictly separated — they communicate only through Postgres tables. The agent can never touch the request hot path.

![Architecture](architecture.png)

### Data plane — the proxy (port 8000)

Every request flows through `POST /v1/chat/completions` (OpenAI-compatible).

```
request arrives (Authorization: Bearer tok_...)
    │
    ├── resolve tenant (SHA-256 key hash, 5-min cache)
    │
    ├── budget check — soft warn at 80%, hard block at 100% (HTTP 429)
    │
    ├── PII redaction (Presidio NER) — before cache and before LLM
    │
    ├── cache lookup (Qdrant ANN on redacted-prompt embeddings)
    │       hit  → return cached response instantly, cost = $0.00
    │       miss → continue
    │
    ├── classify complexity (word-count short-circuit + Haiku LLM-as-judge)
    │
    ├── route to cheapest capable model (Haiku / Sonnet / Opus)
    │
    ├── call LLM (ChatOpenAI via OpenRouter)
    │
    ├── fire-and-forget: cache.store + ledger.log_request (with tenant_id)
    │
    └── return OpenAI-spec JSON + X-TokenOps-* metadata headers
```

Responses are standard OpenAI format. TokenOps metadata — tier, cost, cache status, budget utilization, request ID — rides in response headers so existing client code doesn't break.

**Routing modes:**
- **Passthrough (default)** — honours the caller's `model` field. Cost is tracked and cached, but no classifier runs.
- **Auto** — send `X-TokenOps-Route: auto` to enable classifier-based routing.
- **Cache skip** — send `X-TokenOps-Cache: skip` to bypass the semantic cache.

**Multi-tenancy:** every tenant gets an API key (only the SHA-256 hash is stored), a monthly budget, and a redaction policy. `GET /v1/usage` returns spend, budget utilization, and cache savings scoped to the authenticated tenant. Requests without a key fall back to an anonymous default tenant, so v1 clients keep working.

**PII guardrail:** Presidio detects entities (names, emails, phone numbers, credit cards) and redacts them *before* the cache lookup and the LLM call — PII never reaches the provider, the cache, or the logs. Only entity counts are recorded.

### Semantic cache

Not exact match — **meaning match**. Prompts are embedded on a Modal GPU endpoint (`BAAI/bge-small-en-v1.5`, 384-dim) and matched via cosine similarity in Qdrant. "What's the capital of France?" and "France's capital city?" resolve to the same cache entry.

The similarity threshold isn't static — the optimizer agent tunes it. During the demo it went from 0.92 → 0.86, pushing hit rate from ~10% to 52%.

### Agent plane — the optimizer (scheduled, human-approved)

A LangGraph state machine running in a separate process on a schedule. It reads traffic data, proposes rule changes, back-tests them against real traffic, then **pauses at an approval gate** — a human reviews and approves (or rejects) before anything is applied.

```
observe ──▶ analyse ──▶ validate ──▶ approval gate ──▶ apply
   │           │            │             │              │
 aggregate   pick tools   back-test    LangGraph       write to
 last 24h    to invoke    vs 500 reqs  interrupt():    routing_rules
               │           reject if    waits for
               ├── cache_tune           human via
               ├── route_optimize      POST /v1/agent/approve
               └── quality_sample
```

**Safety:** bounds on every tunable parameter are hard-coded constants the agent cannot override. Back-testing rejects any proposal that would drop projected quality by more than 5%. The approval gate uses LangGraph's `interrupt()` with a Postgres checkpointer — paused runs survive restarts. Every decision is an append-only audit row.

**Approval API** (protected by `AGENT_ADMIN_KEY` when set):
- `GET /v1/agent/pending` — runs waiting for review
- `POST /v1/agent/approve` — approve or reject a paused run
- `GET /v1/agent/history/{thread_id}` — full state trail of a run

### Control plane — the dashboard (port 8501)

Streamlit. Five panels: headline metrics, cost by tag/tenant, budget gauges, pending approvals with review buttons, and a request-level feed. Auto-refreshes every 10 seconds.

---

## Evaluation pipeline

Routing quality is tested, not assumed. `eval/` contains a 50-prompt golden dataset with expected tiers, evaluators (routing correctness, LLM-as-judge answer quality, faithfulness), and a CI gate that fails the build if routing quality drops below threshold:

```bash
python -m eval.run --experiment routing --policy smart-routing   # routing accuracy
python -m eval.run --experiment frontier                         # cost-quality frontier
python -m eval.ci_gate --junit-out eval-results.xml              # CI quality gate
```

## Observability

- `GET /metrics` — Prometheus counters and histograms: request latency, cache hit rate, cost, budget blocks, redacted entities.
- `GET /health?deep=true` — liveness plus real Postgres/Qdrant pings, pool stats, uptime.
- Structured JSON logs with `request_id`, `tenant_id`, `tag`, `model`, `cached`, `latency_ms` on every line.
- Alerting rules and an OpenTelemetry rollout plan in [`docs/observability.md`](docs/observability.md); operational procedures in [`docs/runbook.md`](docs/runbook.md).

---

## Quick start

**Prerequisites:** Docker, Python 3.11+, a [Modal](https://modal.com) account, an [OpenRouter](https://openrouter.ai) API key.

```bash
# Infrastructure
docker-compose up -d                                     # Qdrant + Postgres
docker compose exec -T postgres psql -U tokenops -d tokenops < db/schema.sql

# Environment
cp .env.example .env                                     # fill in OPENROUTER_API_KEY
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m spacy download en_core_web_sm                  # PII redaction model

# GPU embedding endpoint (one-time)
modal setup && modal deploy modal_app/embedder.py

# Create a tenant (prints the API key once — only the hash is stored)
python scripts/create_tenant.py --id my-team --name "My Team" --budget 500
```

Run four processes (each in its own terminal with the venv active):

```bash
python -m uvicorn proxy.main:app --port 8000 --reload    # proxy
python -m uvicorn host_app.main:app --port 8001 --reload  # demo host app
python -m agent.scheduler                                  # optimizer agent
streamlit run dashboard/app.py                             # dashboard
```

Seed demo traffic and watch the dashboard:

```bash
curl http://localhost:8000/health
python -m scripts.seed_demo_traffic                       # 200 requests
# open http://localhost:8501
```

### Production deployment

Three supported paths:

- **Modal (recommended):** `./scripts/deploy_modal.sh` deploys the embedder, proxy, and agent cron against Neon Postgres + Qdrant Cloud. One-time DB bootstrap: `python scripts/setup_production_db.py`. Set `AGENT_ADMIN_KEY` in the `tokenops-prod` Modal secret.
- **Docker Compose:** `docker compose -f docker-compose.prod.yml up -d` — full stack (proxy, agent, dashboard, Postgres, Qdrant, Prometheus) with hardened multi-stage images.
- **Kubernetes:** Helm chart in `deploy/helm/tokenops/` with HPA, network policies, and CronJob agent.

CI (GitHub Actions) runs lint, unit tests, the eval quality gate, and dependency audit on every push.

---

## Project structure

```
proxy/                  DATA PLANE — every LLM request flows through here
  main.py                 FastAPI app, request orchestration, OpenAI-compat response
  auth.py                 API key → tenant resolution (SHA-256, TTL cache)
  budget.py               soft/hard budget enforcement per tenant
  redact.py               Presidio PII redaction (before cache and LLM)
  cache.py                Qdrant semantic cache (embed → ANN lookup → store)
  classifier.py           word-count pre-classifier + Haiku LLM-as-judge
  router.py               model selection + cost math (pure functions, no I/O)
  ledger.py               async Postgres writes (fire-and-forget)
  metrics.py              Prometheus instruments
  config.py               Pydantic Settings + hot-reload from routing_rules

agent/                  AGENT PLANE — off the hot path, human-approved
  graph.py                LangGraph: observe → analyse → validate → approval gate → apply
  scheduler.py            APScheduler entry point
  tools/                  cache_tune, route_optimize, quality_sample

eval/                   EVALUATION — golden dataset, evaluators, CI gate
host_app/               DEMO — 4 AI endpoints generating realistic traffic
dashboard/              CONTROL PLANE — Streamlit, 5 panels incl. approvals
modal_app/              Modal cloud deploy (proxy, agent, dashboard, embedder)
deploy/                 Helm chart + Prometheus config
scripts/                create_tenant, setup_production_db, deploy_modal, seed traffic
db/schema.sql           single source of truth for all table definitions
docs/                   observability plan + operational runbook
```

### Database tables

| Table | Written by | Read by | Purpose |
|-------|-----------|---------|---------|
| `tenants` | `scripts/create_tenant.py` | Proxy auth | API key hashes, budgets, redaction config |
| `routing_rules` | Optimizer agent (post-approval) | Proxy (60s hot-reload) | Cache threshold, tier bands |
| `requests` | Proxy ledger | Agent + Dashboard | One row per request with tenant attribution |
| `agent_decisions` | Optimizer agent | Dashboard | Append-only audit trail with reasoning |

---

## Tech stack

| Layer | Technology | Role |
|-------|-----------|------|
| Proxy API | FastAPI + Uvicorn | Async request handling, OpenAI-compatible endpoint |
| LLM calls | LangChain + OpenRouter | Provider-agnostic model calls with usage metadata |
| Agent | LangGraph + PostgresSaver | Stateful loop with interrupt-based human approval |
| PII redaction | Presidio + spaCy | NER-based entity detection and masking |
| Semantic cache | Qdrant | ANN vector search on prompt embeddings |
| Embeddings | Modal + `bge-small-en-v1.5` | Serverless GPU endpoint, scales to zero |
| Database | PostgreSQL + asyncpg | Ledger, rules, tenants, decisions, checkpoints |
| Metrics | prometheus_client | `/metrics` endpoint for Prometheus/Grafana |
| Observability | Langfuse | Per-call traces, token costs, prompt versioning |
| Dashboard | Streamlit | Real-time cost, budget, and approval visibility |

---

## Tests

```bash
pytest tests/ -v                                    # unit tests (62 passing)
pytest tests/integration/ -v --run-integration      # requires Docker services
python -m eval.ci_gate                              # routing quality gate
```

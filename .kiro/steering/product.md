---
inclusion: always
---

# Product: TokenOps v2 — LLM Cost Governance Platform

## Purpose

TokenOps is an **LLM governance layer** that sits beside any LLM gateway —
reading telemetry, attributing cost per tenant, enforcing budgets, redacting
PII, and autonomously optimizing routing. The proxy is a reference
implementation; the closed-loop governance is the product.

The problem it solves: platform teams building internal AI services for
multiple consuming teams have no answer to "which team is spending what,
is it optimized, and is PII leaking into model calls?" TokenOps provides
the ATTRIBUTE → OPTIMIZE → GUARD loop that no gateway ships natively.

## Three capabilities

### ATTRIBUTE — Per-tenant cost ledger + chargeback
Multi-tenant API key auth resolves every request to a tenant. The cost
ledger tracks spend by tenant, tag, model, and time window. The `/v1/usage`
API returns chargeback-ready spend breakdowns.

### OPTIMIZE — LangGraph optimizer with human-in-the-loop
A LangGraph agent runs on schedule, observes traffic patterns, proposes
routing and cache rule changes, back-tests them against historical quality
data, and pauses for human approval before applying. Durable execution
via Postgres-backed checkpointing ensures proposals survive restarts.

### GUARD — Budget enforcement + PII redaction
Per-tenant monthly budgets with soft limits (downgrade to cheapest model
at 80%) and hard limits (reject at 100%). Presidio-based PII redaction
strips personal data before cache storage and LLM calls. Per-tenant
config allows teams to enable/disable redaction based on use case.

## Core features (v2)

1. **Multi-tenant API key auth** — SHA-256 hashed keys, in-memory cache
   with TTL, anonymous default tenant for backward compat.

2. **Semantic cache** — ANN lookup on prompt embeddings via Qdrant. Cache
   key uses the *redacted* prompt so PII variants share entries.

3. **Complexity classifier + model router** — two-stage: word-count
   short-circuit + LLM-as-judge for the ambiguous middle band.

4. **Cost ledger** — one Postgres row per request with tenant_id, model,
   tokens, cost, tag, latency, cache hit, quality score, PII entity count.

5. **Budget enforcement** — soft limit at 80% (downgrade to Haiku), hard
   limit at 100% (reject with 429). Spend cached 30s to avoid DB pressure.

6. **PII redaction** — Presidio-based, configurable per tenant (entity
   types, action: redact/mask/hash, enable/disable).

7. **Optimizer agent v2** — LangGraph with PostgresSaver checkpointer,
   interrupt() for human-in-the-loop approval, and InMemoryStore for
   cross-thread historical context.

8. **Usage API** — `GET /v1/usage` returns spend, cache hit rate, savings,
   breakdowns by tag and model, scoped to the authenticated tenant.

9. **Agent approval API** — `POST /v1/agent/approve`, `GET /v1/agent/pending`,
   `GET /v1/agent/history/{thread_id}`.

10. **Dashboard** — Streamlit. Five panels: overview metrics, cost by tag,
    agent decisions + pending approvals, budget utilization gauges, request feed.

## What this is NOT (v2)

- Not a streaming proxy (`stream: true` not supported in v2)
- Not a fine-tuned classifier (prompt-based complexity scoring only)
- Not a full FinOps platform (no Stripe billing, no FOCUS export yet)

## Success metrics (demo-provable)

| Metric | Target | How measured |
|--------|--------|-------------|
| Cost reduction vs single-model baseline | >= 60% | `1 - (actual / counterfactual)` from ledger |
| Cache hit rate after 200 requests | >= 40% | `cache_hits / total` |
| Optimizer proposals back-tested | 100% | `agent_decisions` audit trail |
| Quality regression from optimization | < 5% | G-Eval faithfulness on golden set |
| Budget breach detection -> alert | < 60 seconds | Sentinel latency measurement |
| Chargeback report accuracy | 100% row-match | `SUM(cost)` per tenant vs usage API |

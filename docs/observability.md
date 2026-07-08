# TokenOps Observability

What is implemented today, and the documented plan for what comes next.

## Implemented

### Prometheus metrics — `GET /metrics`

Instrumented in `proxy/metrics.py`, recorded on the hot path (in-memory,
nanosecond-scale, no I/O). Metrics catalog:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `tokenops_request_latency_seconds` | Histogram | tier, cached, tenant | End-to-end proxy latency incl. LLM call |
| `tokenops_requests_total` | Counter | tier, cached, tenant, status | Requests by outcome (ok / rejected / error) |
| `tokenops_cache_lookups_total` | Counter | result | hit / miss / skipped |
| `tokenops_llm_call_duration_seconds` | Histogram | model | Upstream LLM call duration |
| `tokenops_budget_utilization_pct` | Gauge | tenant | Monthly budget utilization |
| `tokenops_classifier_overhead_seconds` | Histogram | — | Classifier latency (short-circuit or LLM judge) |
| `tokenops_embedding_latency_seconds` | Histogram | — | Modal embedder call time |
| `tokenops_active_requests` | Gauge | — | In-flight requests |
| `tokenops_cost_usd_total` | Counter | tenant, model | Cumulative LLM spend |

Label cardinality is bounded by design: tiers (5), cached (2), models (3),
tenants (one per team — expected < 50).

### Enhanced health check — `GET /health`

Shallow (default, for liveness probes):

```json
{
  "status": "ok",
  "version": "2.0.0",
  "rules_version": 3,
  "uptime_seconds": 8241.7,
  "db_pool_size": 4,
  "db_pool_free": 3
}
```

Deep (`GET /health?deep=true`, for readiness probes) additionally
round-trips Postgres (`SELECT 1`) and Qdrant (collection count, 2s
timeout) and reports `postgres_connected`, `qdrant_connected`,
`cache_collection_count`. Status becomes `degraded` when Postgres is
unreachable.

### Structured JSON logging

Every proxy log line carries `request_id`, `tenant_id`, `tag`, `model`,
`cached`, `latency_ms` (see `_JsonFormatter` in `proxy/main.py`).

### Langfuse LLM tracing (optional)

Per-call traces with token usage flow to Langfuse when
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set.

## Planned — OpenTelemetry distributed tracing

Not yet wired; the implementation path is:

1. **Dependencies** (add to `requirements.txt` and `tech.md` together):
   `opentelemetry-api`, `opentelemetry-sdk`,
   `opentelemetry-instrumentation-fastapi`,
   `opentelemetry-instrumentation-asyncpg`,
   `opentelemetry-exporter-otlp`.
2. **Auto-instrumentation**: `FastAPIInstrumentor.instrument_app(app)` and
   `AsyncPGInstrumentor().instrument()` in the lifespan, before pool init.
3. **Manual spans** around the five hot-path phases: `redact`,
   `cache_lookup`, `classify`, `llm_call`, `cache_store`. Each span carries
   `request_id`, `tenant_id`, `tier` attributes.
4. **Context propagation to Modal**: inject `traceparent` into the embed
   call payload; the embedder starts a linked span server-side.
5. **Exporters**: console in dev; OTLP/gRPC in prod — compatible with
   Jaeger, Datadog, and Arize Phoenix collectors. Endpoint via
   `OTEL_EXPORTER_OTLP_ENDPOINT` (standard env var, read by the SDK
   itself, exempt from the Settings rule).

## Planned — response streaming

`stream: true` is unsupported in v2. Blockers and path:

- The semantic cache stores complete responses; streaming requires
  buffering the full completion server-side before the cache write
  (double-delivery) or a cache-miss-only streaming mode.
- Cost accounting needs final usage metadata, which arrives in the last
  SSE chunk — the ledger write moves to a stream-complete callback.
- Implementation order: SSE passthrough on cache miss → header streaming
  → token-by-token forward with an accumulating buffer for cache/ledger.

## Alerting rules (documented, not implemented)

| Alert | Condition | Severity | Rationale |
|---|---|---|---|
| High proxy latency | p99 `tokenops_request_latency_seconds{cached="true"}` > 500ms for 5m | page | Cache hits should be near-instant; slowness means Qdrant/Modal trouble |
| Cache hit-rate collapse | hit rate drops > 20 points within 1h | ticket | Threshold misconfiguration or embedder outage |
| Budget breach | `tokenops_budget_utilization_pct` >= 100 | notify tenant owner | Hard limit engaged; requests are being rejected |
| Agent proposals rejected 3x | 3 consecutive `agent_decisions` rows with `applied=false` for the same tool | ticket | Optimizer is stuck proposing invalid changes |
| Embedding timeout rate | > 10% of `tokenops_cache_lookups_total{result="miss"}` attributable to embed timeouts over 15m | ticket | Modal cold-start or capacity problem |
| Error rate | `tokenops_requests_total{status="error"}` > 1% of total over 5m | page | Upstream (OpenRouter) or internal failure |

## Performance notes

- asyncpg pool is min=2 / max=10; pool exhaustion surfaces as latency in
  `tokenops_request_latency_seconds` while `db_pool_free` in `/health`
  reads 0.
- The classifier word-count short-circuit keeps
  `tokenops_classifier_overhead_seconds` near zero for the majority of
  traffic; only the ambiguous middle band pays the LLM-judge cost.
- Cold start: Qdrant client and Modal function handles are lazy singletons
  (`proxy/cache.py`), so import cost is deferred off the boot path.

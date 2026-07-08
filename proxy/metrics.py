"""Prometheus metrics for the proxy hot path.

All instruments are module-level singletons from prometheus_client's
default registry, exposed at GET /metrics by main.py. Recording a metric
is a nanosecond-scale in-memory operation — safe on the hot path.

Naming follows Prometheus conventions: unit-suffixed, snake_case,
`tokenops_` prefix. Label cardinality is deliberately bounded: tier (5
values), cached (2), model (3-ish), tenant (small, one per team).
"""

from prometheus_client import Counter, Gauge, Histogram

# Full request latency including the upstream LLM call.
REQUEST_LATENCY = Histogram(
    "tokenops_request_latency_seconds",
    "End-to-end proxy request latency",
    labelnames=("tier", "cached", "tenant"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

CACHE_LOOKUPS = Counter(
    "tokenops_cache_lookups_total",
    "Semantic cache lookups by outcome",
    labelnames=("result",),  # hit | miss | skipped
)

LLM_CALL_DURATION = Histogram(
    "tokenops_llm_call_duration_seconds",
    "Upstream LLM call duration",
    labelnames=("model",),
    buckets=(0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0),
)

BUDGET_UTILIZATION = Gauge(
    "tokenops_budget_utilization_pct",
    "Monthly budget utilization percentage",
    labelnames=("tenant",),
)

CLASSIFIER_OVERHEAD = Histogram(
    "tokenops_classifier_overhead_seconds",
    "Complexity classifier latency (word-count short-circuit or LLM judge)",
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
)

EMBEDDING_LATENCY = Histogram(
    "tokenops_embedding_latency_seconds",
    "Modal embedder call latency (cache lookups and stores)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
)

ACTIVE_REQUESTS = Gauge(
    "tokenops_active_requests",
    "Requests currently in flight through the proxy",
)

REQUESTS_TOTAL = Counter(
    "tokenops_requests_total",
    "Requests served, by outcome",
    labelnames=("tier", "cached", "tenant", "status"),  # status: ok | rejected | error
)

COST_TOTAL = Counter(
    "tokenops_cost_usd_total",
    "Cumulative LLM spend in USD",
    labelnames=("tenant", "model"),
)


def observe_request(
    *,
    tier: str,
    cached: bool,
    tenant: str,
    status: str,
    latency_seconds: float,
) -> None:
    """Record the terminal metrics for one request in a single call."""
    cached_label = "true" if cached else "false"
    REQUEST_LATENCY.labels(tier=tier, cached=cached_label, tenant=tenant).observe(
        latency_seconds
    )
    REQUESTS_TOTAL.labels(
        tier=tier, cached=cached_label, tenant=tenant, status=status
    ).inc()

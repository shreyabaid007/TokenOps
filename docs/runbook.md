# TokenOps — Productionization Checklist & Operational Runbook

Status legend: **done** · **partial** · **todo**. Each item maps to the
spec that implements it.

## 1. Security

| Status | Item | Where |
|---|---|---|
| done | API key auth on `/v1/chat/completions` and `/v1/usage` (Spec 1) | `proxy/auth.py` |
| done | Auth on agent endpoints — `/v1/agent/*` require `AGENT_ADMIN_KEY` when set (Bearer or `X-TokenOps-Admin-Key`) | `proxy/auth.py`, `proxy/main.py` |
| done | PII redaction before LLM calls and cache storage (Spec 3) | `proxy/redact.py` |
| todo | `prompt_snip` purged after 30 days (retention cron) | needs a scheduled `DELETE`/`UPDATE` job |
| done | No secrets in code — everything via `proxy/config.py` Settings | `proxy/config.py` |
| todo | CORS configured for dashboard origin only | dashboard reads DB directly today; needed if it moves to the API |
| done | Budget-based rate limiting per tenant (Spec 2) | `proxy/budget.py` |
| partial | Dependency vulnerability scanning in CI — `pip-audit` runs advisory-only; flip to hard-fail once baseline is clean | `.github/workflows/ci.yml` |
| todo | HTTPS/TLS termination at load balancer | Traefik/Ingress config per environment |
| todo | Database credentials rotated quarterly | operational policy |

## 2. Reliability

| Status | Item | Where |
|---|---|---|
| done | Graceful shutdown — lifespan cancels the reload loop and closes the pool; uvicorn drains in-flight requests | `proxy/main.py` |
| done | Cache failure = silent degradation, never error | `proxy/cache.py` |
| done | Modal timeout = skip cache, route normally | `proxy/cache.py` (`MODAL_TIMEOUT_SEC`) |
| done | Agent failure = proxy continues with last known rules | plane separation via `routing_rules` |
| partial | Pool exhaustion handling — visible via `/health` (`db_pool_free`) and latency metrics; no queue-depth alarm yet | `proxy/ledger.py` |
| todo | Circuit breaker on OpenRouter (fast-fail after N failures for M seconds) | would live in `_llm_for` call path |
| todo | Retry with backoff on transient LLM errors (429/503) — `max_retries=0` today, deliberate to bound latency; revisit with budget-aware retry | `proxy/main.py` |

## 3. Observability

| Status | Item | Where |
|---|---|---|
| done | Structured JSON logging with request_id, tenant_id, tag, model, cached, latency_ms | `proxy/main.py` |
| done | Prometheus metrics endpoint (Spec 6) | `proxy/metrics.py`, `GET /metrics` |
| todo | OpenTelemetry distributed tracing — implementation path documented | `docs/observability.md` |
| done | Alerting rules documented (Spec 6) | `docs/observability.md` |
| partial | Dashboard shows system health — cost/budget/agent panels exist; no live health panel yet | `dashboard/app.py` |

## 4. Data governance

| Status | Item | Where |
|---|---|---|
| done | Full prompts never stored in the ledger (hash + 120-char snippet) | `proxy/ledger.py` |
| todo | Prompt snippets purged after retention period | same cron as §1 |
| done | Cache stores redacted prompts only (Spec 3) | redaction precedes cache in `proxy/main.py` |
| done | Agent decisions are an append-only audit trail | `agent_decisions` table |
| done | Per-tenant data isolation — tenant_id on ledger writes, usage queries scoped by authenticated tenant | Spec 1 |
| todo | FOCUS-aligned cost export for FinOps tooling | extend `/v1/usage` with a `format=focus` mode |

## 5. Performance

| Status | Item | Target |
|---|---|---|
| partial | p99 proxy overhead (excluding LLM call) — measure via `tokenops_request_latency_seconds{cached="true"}` | < 50ms |
| partial | Cache lookup — measure via `tokenops_embedding_latency_seconds` + Qdrant query | < 20ms local / < 50ms cloud |
| done | Classifier short-circuit — word-count bands skip the LLM for prompts outside the ambiguous middle band | > 60% of traffic |
| done | Connection pool sized (min=2, max=10) | matches expected concurrency |
| todo | Load test at 100 concurrent requests | e.g. `k6`/`locust` against staging |

---

## Operational procedures

### First-time production bootstrap

1. Apply schema and LangGraph checkpointer tables:
   `python scripts/setup_production_db.py`
2. Install the Presidio spaCy model in every runtime image (Dockerfiles and
   Modal images bake `en_core_web_sm`; locally:
   `python -m spacy download en_core_web_sm`).
3. Set `AGENT_ADMIN_KEY` in production secrets before exposing `/v1/agent/*`.
4. Deploy services (Modal: `./scripts/deploy_modal.sh`; Compose:
   `docker compose -f docker-compose.prod.yml up -d`).

### Deploying a new version (zero downtime)

1. Merge to `main` → CI builds and pushes `:staging` images.
2. Verify staging: `curl https://staging-host/health?deep=true` and run
   `python scripts/seed_demo_traffic.py` against it.
3. Publish a GitHub release → CI promotes images to `:production`.
4. Kubernetes: `helm upgrade tokenops deploy/helm/tokenops` — the proxy
   Deployment rolls pods one at a time; readiness probes (`/health?deep=true`)
   gate traffic shifting. Compose: `docker compose -f docker-compose.prod.yml up -d --no-deps proxy`.

### Rolling back a bad deploy

- Kubernetes: `helm rollback tokenops <REVISION>` (list with `helm history tokenops`).
- Compose: retag the previous image (`:production` → previous SHA tag) and re-up.
- Schema changes are additive-only (`IF NOT EXISTS` / nullable columns), so
  a code rollback never requires a schema rollback.

### Handling a runaway agent proposal

1. List pending: `GET /v1/agent/pending` (requires admin auth when `AGENT_ADMIN_KEY` is set).
2. Reject it: `POST /v1/agent/approve` with
   `{"thread_id": "...", "approved": false, "reviewer": "<you>"}`.
3. If a bad rule already applied, insert a corrective row — the proxy picks
   it up within `RULES_RELOAD_INTERVAL_SEC` (60s):

   ```sql
   INSERT INTO routing_rules (cache_threshold, low_max_tokens, high_min_tokens, updated_by, notes)
   SELECT cache_threshold, low_max_tokens, high_min_tokens, 'operator', 'manual revert of rules id N'
   FROM routing_rules WHERE id = <last_good_id>;
   ```

4. Audit what happened: `SELECT * FROM agent_decisions ORDER BY ts DESC LIMIT 20;`

### Recovering from Postgres failure

1. Proxy behavior during outage: ledger writes fail silently (logged),
   budget checks serve from the 30s cache then fail open on errors is NOT
   the case — budget check raises, requests 502. Restore DB first.
2. Restore latest dump from the backups volume / S3:
   `psql $DATABASE_URL < tokenops-YYYYMMDD.sql`.
3. If restoring to an empty instance, apply schema first:
   `psql $DATABASE_URL < db/schema.sql` (idempotent, includes seed rules
   and demo tenants).
4. Restart the proxy so the lifespan re-bootstraps rules and the pool.
5. RTO target: 1 hour. RPO: 24 hours (daily dumps).

### Recovering from Qdrant failure

Cache is rebuildable — no restore required. Bring Qdrant back (volume
intact or fresh); `ensure_collection()` recreates the collection on next
proxy startup. Expect a cold-start penalty: hit rate near zero until the
cache re-warms from live traffic.

### Investigating a cost spike

```sql
-- Who and what, last 24h
SELECT tenant_id, tag, COUNT(*), SUM(cost_usd) AS spend
FROM requests
WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY tenant_id, tag
ORDER BY spend DESC;

-- Was it a tier shift (agent rule change)?
SELECT * FROM routing_rules ORDER BY id DESC LIMIT 5;

-- Was the cache underperforming?
SELECT date_trunc('hour', ts) AS hr,
       AVG(CASE WHEN cached THEN 1.0 ELSE 0.0 END) AS hit_rate
FROM requests WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY hr ORDER BY hr;
```

Cross-check `tokenops_cost_usd_total` rate in Prometheus/Grafana.

### Rotating a tenant API key

```sql
UPDATE tenants SET api_key_hash = '<sha256-of-new-key>' WHERE id = '<tenant>';
```

Then invalidate the proxy's auth cache: restart the proxy, or wait out the
5-minute TTL (`proxy/auth.py` `CACHE_TTL_SEC`). Distribute the new key to
the tenant out-of-band.

### Adding a new tenant

```sql
INSERT INTO tenants (id, name, api_key_hash, monthly_budget_usd)
VALUES ('<kebab-id>', '<Display Name>', '<sha256-of-key>', 500.00);
```

Generate the key and hash locally:
`python3 -c "import secrets,hashlib; k='tok_'+secrets.token_urlsafe(24); print(k, hashlib.sha256(k.encode()).hexdigest())"`.
No proxy restart needed — the tenant resolves on first request.

### Emergency: disabling the agent

- Kubernetes: `kubectl patch cronjob tokenops-agent -p '{"spec":{"suspend":true}}'`.
- Compose/local: stop the agent container / `agent/scheduler.py` process.
- The proxy continues serving with the current `routing_rules` row
  indefinitely; nothing else depends on the agent being alive.

-- TokenOps database schema — single source of truth.
--
-- Python code must NEVER define or alter tables. All schema changes live here.
-- Apply with:
--     psql $DATABASE_URL < db/schema.sql
--
-- Re-running this file is non-destructive: every CREATE uses IF NOT EXISTS,
-- and the seed INSERT is guarded so it runs only on an empty routing_rules.
--
-- Table ownership (per .kiro/steering/structure.md):
--
--   routing_rules     written by: agent/graph.py apply_node
--                     read by:    proxy/config.py reload loop
--                                 (via proxy/ledger.py:get_latest_rules)
--
--   requests          written by: proxy/ledger.py:log_request
--                     read by:    agent/graph.py observe_node,
--                                 agent/tools/quality_sample.py,
--                                 dashboard/app.py panels 1, 2, 4
--
--   agent_decisions   written by: agent/graph.py apply_node
--                     read by:    dashboard/app.py panel 3
--
-- The proxy and agent share no in-process state. These three tables are the
-- entire interface between the data plane and the agent plane.


-- routing_rules — the agent's only output channel into the proxy.
-- The proxy reads the most recent row (highest id) on startup and again
-- every RULES_RELOAD_INTERVAL_SEC seconds, swapping its in-memory copy
-- atomically when a newer id appears.
CREATE TABLE IF NOT EXISTS routing_rules (
    id              SERIAL PRIMARY KEY,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      TEXT NOT NULL DEFAULT 'agent',
    cache_threshold FLOAT NOT NULL DEFAULT 0.92,
    low_max_tokens  INT NOT NULL DEFAULT 300,
    high_min_tokens INT NOT NULL DEFAULT 800,
    notes           TEXT
);

-- Seed row: gives the proxy a rule to read on first startup before the
-- agent has run. Guarded so re-applying the schema does not pile up
-- duplicate seeds.
--
-- Narrow bands (low_max_tokens=15, high_min_tokens=40) override the
-- column defaults (300/800) so the LLM classifier engages on short
-- demo prompts — otherwise the word-count pre-classifier short-circuits
-- everything to "low" and the demo never demonstrates three tiers in
-- action. The optimizer agent will widen the bands over time once it
-- has quality evidence.
INSERT INTO routing_rules (low_max_tokens, high_min_tokens, notes)
SELECT
    15,
    40,
    'demo seed — narrow bands so the LLM classifier engages on short prompts; agent retunes from here'
WHERE NOT EXISTS (SELECT 1 FROM routing_rules);


-- requests — one row per LLM call that traverses the proxy, cache hits
-- included. prompt_snip is the first 120 chars of the prompt; truncation
-- is performed in proxy/ledger.py so this column is a plain TEXT.
-- quality_score is null at insert time and filled later by the optimizer
-- agent's quality_sample tool.
CREATE TABLE IF NOT EXISTS requests (
    id                       BIGSERIAL PRIMARY KEY,
    ts                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id               TEXT NOT NULL,
    prompt_hash              TEXT,
    prompt_snip              TEXT,
    tag                      TEXT NOT NULL,
    model                    TEXT NOT NULL,
    tier                     TEXT NOT NULL,
    tokens_in                INT,
    tokens_out               INT,
    cost_usd                 NUMERIC(10, 6),
    counterfactual_cost_usd  NUMERIC(10, 6),
    cached                   BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms               FLOAT,
    quality_score            FLOAT
);


-- agent_decisions — audit trail for every proposal the optimizer agent
-- considered, whether validated, and whether applied to routing_rules.
-- observation/proposal/reasoning are TEXT; tool outputs are serialised
-- to JSON strings by the agent before insert.
CREATE TABLE IF NOT EXISTS agent_decisions (
    id          SERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id      TEXT NOT NULL,
    tool_used   TEXT NOT NULL,
    observation TEXT,
    proposal    TEXT,
    validated   BOOLEAN,
    applied     BOOLEAN,
    reasoning   TEXT
);


-- Indexes — match the known query shapes from the steering files.

-- proxy hot-reload: always reads latest row
CREATE INDEX IF NOT EXISTS idx_routing_rules_updated_at
    ON routing_rules (updated_at DESC);

-- agent observe_node and dashboard: almost every query filters by ts
CREATE INDEX IF NOT EXISTS idx_requests_ts
    ON requests (ts DESC);

-- quality_sample tool: this is its entire WHERE clause
CREATE INDEX IF NOT EXISTS idx_requests_quality_score_null
    ON requests (id) WHERE quality_score IS NULL;

-- dashboard panel 3: always reads by recency
CREATE INDEX IF NOT EXISTS idx_agent_decisions_ts
    ON agent_decisions (ts DESC);

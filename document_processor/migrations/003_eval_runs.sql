-- Migration: 003_eval_runs
-- Description: Sprint 2 (Cycle C) — eval harness run history.
-- Date: 2026-05-05
--
-- Each row captures one invocation of an eval (HumanEval+,
-- SWE-bench-Lite, RAGAS, or Sprint 0 corpus).  Persistent so the
-- /admin/evals dashboard can show a 20-run line chart and drill
-- into pass/fail per case.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS eval_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Eval identifier.  Allowed values pinned by the route layer:
    --   "humaneval_plus_50"  — 50-instance HumanEval+ subset
    --   "swebench_lite_25"   — 25-instance SWE-bench-Lite subset
    --   "ragas_50"           — 50-query RAGAS sweep over LanceDB
    --   "sprint0_corpus"     — full 10-prompt Sprint 0 corpus
    -- A free-string column (not enum) to keep schema-evolution cheap;
    -- the API gate validates against the manifest in
    -- ``document_processor/api/admin_evals_routes.py:_EVAL_MANIFEST``.
    name VARCHAR(64) NOT NULL,

    -- Run lifecycle.
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|cancelled

    -- Backend snapshot at run-time.  Useful for the dashboard
    -- chart so a Sprint-1-vs-Sprint-2 swap is obvious in history.
    backend VARCHAR(32) NOT NULL DEFAULT 'ollama',
    git_sha CHAR(40),

    -- Eval-specific summary (pass@1, mean score, p50 latency, etc.).
    -- JSONB so the schema doesn't have to evolve with every new eval.
    -- Validated against ``_EVAL_MANIFEST[name].summary_schema`` at
    -- write time when present; permissive otherwise.
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Per-case results — array of {id, status, ...}.  May be NULL
    -- while the run is in-flight; final on completion.
    cases JSONB,

    -- Free-form note (human-typed; "rerun after KV q4 swap" etc.).
    note TEXT,

    -- Owner — the auth'd user who kicked the run.  Eval results are
    -- single-tenant today; this column makes a future row-level
    -- security policy cheap.
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    CONSTRAINT eval_runs_status_values CHECK (
        status IN ('pending', 'running', 'done', 'failed', 'cancelled')
    )
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_name_started
    ON eval_runs (name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status_started
    ON eval_runs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_user
    ON eval_runs (user_id, started_at DESC);

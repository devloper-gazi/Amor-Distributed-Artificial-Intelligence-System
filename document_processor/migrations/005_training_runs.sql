-- Migration: 005_training_runs
-- Description: Sprint 6 Day 4 (Cycle C) — ORPO training run history.
-- Date: 2026-05-04

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS training_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Run lifecycle.
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
        -- pending | running | trained | evaluated | promoted | failed | rejected

    -- Configuration snapshot — what the trainer was asked to do.
    -- JSONB so the schema doesn't have to evolve when the plan
    -- locks new defaults (e.g. ``lora_alpha`` change in Sprint 7+).
    config JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Inputs.
    pair_count INT NOT NULL DEFAULT 0,
    pair_jsonl_path TEXT,                       -- on-disk JSONL the trainer ate

    -- Outputs.
    peft_adapter_path TEXT,
    gguf_adapter_path TEXT,

    -- Eval-vs-baseline result (output of eval_adapter.diff_runs).
    eval_summary JSONB,

    -- Operator note + actor.
    note TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    CONSTRAINT training_runs_status_values CHECK (
        status IN ('pending', 'running', 'trained', 'evaluated',
                   'promoted', 'failed', 'rejected')
    )
);

CREATE INDEX IF NOT EXISTS idx_training_runs_started ON training_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_runs_status ON training_runs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_runs_user ON training_runs (user_id, started_at DESC);

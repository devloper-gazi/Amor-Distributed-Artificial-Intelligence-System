-- Migration: 004_preference_pairs
-- Description: Sprint 6 (Cycle C) — ORPO preference-pair persistence.
-- Date: 2026-05-04
--
-- Each row captures one user-supplied (chosen, rejected) pair from
-- the MessageActions hover bar (Sprint 4 Day 3 ✓ ▼ buttons).  The
-- weekly ORPO trainer (Sprint 6 Day 2) reads pairs accumulated
-- since the last run and emits a LoRA adapter that the operator
-- promotes through the manual gate UI.
--
-- Privacy
-- -------
-- The default mode stores ONLY the SHA-256 of the prompt + the
-- assistant outputs.  Raw text is opt-in via the ``opt_in_raw``
-- flag — surfaced in /admin/training so the operator can inspect
-- pairs but the rate buttons themselves NEVER auto-store raw
-- snippets.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS preference_pairs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Identity of the message that received the rate ± click.
    -- Comes from the ChatTurn.id surfaced in MessageActions.
    chosen_turn_id VARCHAR(128),
    rejected_turn_id VARCHAR(128),

    -- SHA-256 hashes of (prompt + chosen / rejected outputs).  Used
    -- as the dedup key — repeated rates of the same exchange
    -- collapse to one row.
    code_hash CHAR(64) NOT NULL,

    -- Mode the chat turn was generated in (build / research / ...).
    mode VARCHAR(16) NOT NULL DEFAULT 'build',

    -- Optional raw text — only present when the user ticks
    -- "include raw text" in /admin/training.  Default is NULL so
    -- accidental enable doesn't backfill.
    opt_in_raw BOOLEAN NOT NULL DEFAULT FALSE,
    prompt TEXT,
    chosen TEXT,
    rejected TEXT,

    -- Backend + model active when the pair was captured.  Lets the
    -- trainer skip pairs from a different generation than the one
    -- the operator wants to fine-tune.
    backend VARCHAR(32) NOT NULL DEFAULT 'ollama',
    model_tag VARCHAR(96),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Owner.  Single-tenant today; column is here for future RLS.
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    -- Tracks whether this pair has been included in a training
    -- run.  ``trained_in`` references training_runs(id) once that
    -- table lands in 005_training_runs.sql (Sprint 6 Day 2).
    trained_in UUID,

    CONSTRAINT preference_pairs_mode_values CHECK (
        mode IN ('build', 'research', 'thinking', 'consortium', 'sentinel', 'system')
    )
);

CREATE INDEX IF NOT EXISTS idx_preference_pairs_created
    ON preference_pairs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_preference_pairs_mode_created
    ON preference_pairs (mode, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_preference_pairs_hash
    ON preference_pairs (code_hash);
CREATE INDEX IF NOT EXISTS idx_preference_pairs_untrained
    ON preference_pairs (created_at DESC) WHERE trained_in IS NULL;
CREATE INDEX IF NOT EXISTS idx_preference_pairs_user
    ON preference_pairs (user_id, created_at DESC);

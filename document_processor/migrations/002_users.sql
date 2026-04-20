-- Migration: 002_users
-- Description: User accounts, refresh tokens, login attempts
-- Date: 2026-04-19

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "citext";

-- ============================================================================
-- USERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email CITEXT UNIQUE NOT NULL,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(128),
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_count INT NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    last_login_ip INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT username_format CHECK (username ~ '^[A-Za-z0-9_\-]{3,64}$'),
    CONSTRAINT role_values CHECK (role IN ('user','admin'))
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users (created_at DESC);

-- ============================================================================
-- REFRESH TOKENS (rotating, revocable)
-- We store a SHA-256 hash of the refresh token so DB compromise doesn't leak tokens.
-- ============================================================================
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash CHAR(64) UNIQUE NOT NULL,
    parent_id UUID REFERENCES refresh_tokens(id) ON DELETE SET NULL,
    user_agent TEXT,
    ip INET,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_reason VARCHAR(64),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens (user_id, revoked, expires_at);
CREATE INDEX IF NOT EXISTS idx_refresh_expires ON refresh_tokens (expires_at);

-- ============================================================================
-- LOGIN ATTEMPTS (for rate-limiting + audit)
-- ============================================================================
CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGSERIAL PRIMARY KEY,
    identifier CITEXT NOT NULL,     -- email or username attempted
    ip INET,
    user_agent TEXT,
    success BOOLEAN NOT NULL,
    failure_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (ip, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_attempts_id_time ON login_attempts (identifier, created_at DESC);

-- ============================================================================
-- Trigger: auto-update updated_at on users
-- ============================================================================
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

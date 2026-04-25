"""
Authentication service.

Responsibilities
----------------
- Bootstrap the `users`, `refresh_tokens`, `login_attempts` tables on startup.
- Hash/verify passwords using Argon2id (memory-hard; resistant to GPU cracking).
- Mint short-lived JWT access tokens and opaque, rotating refresh tokens.
- Track login attempts + lock accounts after repeated failures.
- Provide helpers for route handlers.

Design notes
------------
- Refresh tokens are opaque random strings; only SHA-256 hashes hit the DB.
- Access tokens carry the minimum viable claims (sub=user_id, jti, exp, iat).
- Refresh rotation: issuing a new access token revokes the old refresh row and
  inserts a new one chained via `parent_id`. Replay of a revoked token wipes
  the entire chain (family) — classic OWASP-recommended pattern.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.logging_config import logger
from ..config.settings import settings
from ..infrastructure.storage import storage_manager
from .models import PublicUser, User

# Argon2id parameters tuned for interactive login (~50-80ms on modern CPU).
# time_cost=3, memory_cost=64 MB, parallelism=4 hits OWASP 2023 guidance.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthError(Exception):
    """Base auth failure."""


class InvalidCredentials(AuthError):
    code = "invalid_credentials"


class AccountLocked(AuthError):
    code = "account_locked"


class AccountDisabled(AuthError):
    code = "account_disabled"


class DuplicateUser(AuthError):
    code = "duplicate_user"


class RateLimited(AuthError):
    code = "rate_limited"


class InvalidToken(AuthError):
    code = "invalid_token"


class WeakPassword(AuthError):
    """P2.4: Raised when a registration password fails the entropy check."""
    code = "weak_password"


_PASSWORD_MIN_LEN = 10


def _validate_password(pw: str) -> None:
    """
    P2.4: Enforce minimum password complexity at the service layer so the
    backend is the source of truth, not the (easily bypassed) frontend
    form. Rules: at least 10 chars, with at least one uppercase, one
    lowercase, and one digit.
    """
    if not isinstance(pw, str) or len(pw) < _PASSWORD_MIN_LEN:
        raise WeakPassword(
            f"Password must be at least {_PASSWORD_MIN_LEN} characters."
        )
    if not any(c.isupper() for c in pw):
        raise WeakPassword("Password must contain an uppercase letter.")
    if not any(c.islower() for c in pw):
        raise WeakPassword("Password must contain a lowercase letter.")
    if not any(c.isdigit() for c in pw):
        raise WeakPassword("Password must contain a digit.")


class AuthService:
    """High-level authentication operations."""

    def __init__(self) -> None:
        self._bootstrapped = False

    # ------------------------------------------------------------------ bootstrap

    async def bootstrap(self) -> None:
        """Create tables if they don't exist. Called from app lifespan."""
        if self._bootstrapped:
            return
        if not storage_manager._pg_connected:  # noqa: SLF001
            await storage_manager.connect_postgres()

        # Run each DDL statement in its own transaction so a benign failure
        # (e.g. another worker created the extension first) doesn't abort
        # every subsequent statement in the batch.
        for stmt in _USERS_DDL:
            try:
                async with storage_manager.pg_engine.begin() as conn:
                    await conn.execute(text(stmt))
            except Exception as exc:  # pragma: no cover - best-effort DDL
                logger.debug("auth_ddl_skipped", stmt=stmt[:80], error=str(exc))

        self._bootstrapped = True
        logger.info("auth_tables_ready")

    # ------------------------------------------------------------------ password

    def hash_password(self, plaintext: str) -> str:
        return _hasher.hash(plaintext)

    def verify_password(self, plaintext: str, stored_hash: str) -> bool:
        try:
            return _hasher.verify(stored_hash, plaintext)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        try:
            return _hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True

    # ------------------------------------------------------------------ registration

    async def register(
        self,
        username: str,
        password: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> User:
        await self.bootstrap()
        # P2.4: validate BEFORE hashing — no point spending Argon2 cost
        # on a password we'll reject anyway.
        _validate_password(password)
        password_hash = self.hash_password(password)
        email = (email or "").strip() or None

        async with self._session() as session:
            if ip and await self._register_rate_limited(session, ip):
                raise RateLimited("too many registration attempts from this address")

            # Username uniqueness is mandatory; email uniqueness only matters when one is set.
            if email:
                existing = await session.execute(
                    text(
                        "SELECT 1 FROM users "
                        "WHERE username = :username OR email = :email LIMIT 1"
                    ),
                    {"username": username, "email": email},
                )
            else:
                existing = await session.execute(
                    text("SELECT 1 FROM users WHERE username = :username LIMIT 1"),
                    {"username": username},
                )
            if existing.first():
                raise DuplicateUser("username or email already taken")

            row = await session.execute(
                text(
                    """
                    INSERT INTO users (email, username, password_hash, display_name)
                    VALUES (:email, :username, :password_hash, :display_name)
                    RETURNING id, email, username, display_name, role,
                              is_active, email_verified, created_at, updated_at,
                              last_login_at
                    """
                ),
                {
                    "email": email,
                    "username": username,
                    "password_hash": password_hash,
                    "display_name": display_name,
                },
            )
            record = row.mappings().first()
            await session.commit()

        return _user_from_row(record)

    # ------------------------------------------------------------------ login

    async def authenticate(
        self,
        identifier: str,
        password: str,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> User:
        await self.bootstrap()
        identifier = identifier.strip()

        async with self._session() as session:
            if ip and await self._login_rate_limited(session, ip):
                raise RateLimited("too many login attempts — slow down")

            row = await session.execute(
                text(
                    """
                    SELECT id, email, username, password_hash, display_name, role,
                           is_active, email_verified, failed_login_count, locked_until,
                           created_at, updated_at, last_login_at
                    FROM users
                    WHERE username = :ident OR (email IS NOT NULL AND email = :ident)
                    LIMIT 1
                    """
                ),
                {"ident": identifier},
            )
            record = row.mappings().first()

            if record is None:
                # Intentionally waste time to make timing attacks harder.
                _hasher.hash("decoy-password-for-timing")
                await self._log_attempt(session, identifier, ip, user_agent, False, "not_found")
                await session.commit()
                raise InvalidCredentials("invalid credentials")

            if not record["is_active"]:
                await self._log_attempt(session, identifier, ip, user_agent, False, "disabled")
                await session.commit()
                raise AccountDisabled("account disabled")

            locked_until = record["locked_until"]
            if locked_until and locked_until > _now():
                await self._log_attempt(session, identifier, ip, user_agent, False, "locked")
                await session.commit()
                raise AccountLocked("account temporarily locked")

            ok = self.verify_password(password, record["password_hash"])
            if not ok:
                failed = (record["failed_login_count"] or 0) + 1
                lock_until = None
                reason = "bad_password"
                if failed >= settings.auth_max_failed_logins:
                    lock_until = _now() + timedelta(minutes=settings.auth_lockout_minutes)
                    reason = "bad_password_locked"
                await session.execute(
                    text(
                        """
                        UPDATE users
                        SET failed_login_count = :failed, locked_until = :lock
                        WHERE id = :id
                        """
                    ),
                    {"failed": failed, "lock": lock_until, "id": record["id"]},
                )
                await self._log_attempt(session, identifier, ip, user_agent, False, reason)
                await session.commit()
                raise InvalidCredentials("invalid credentials")

            # Success — reset failure counters, update login audit.
            update = await session.execute(
                text(
                    """
                    UPDATE users
                    SET failed_login_count = 0,
                        locked_until = NULL,
                        last_login_at = NOW(),
                        last_login_ip = :ip
                    WHERE id = :id
                    RETURNING id, email, username, display_name, role,
                              is_active, email_verified, created_at, updated_at,
                              last_login_at
                    """
                ),
                {"id": record["id"], "ip": ip},
            )
            updated = update.mappings().first()

            # Transparent rehash when params change.
            if self.needs_rehash(record["password_hash"]):
                await session.execute(
                    text("UPDATE users SET password_hash = :h WHERE id = :id"),
                    {"h": self.hash_password(password), "id": record["id"]},
                )

            await self._log_attempt(session, identifier, ip, user_agent, True, None)
            await session.commit()

        return _user_from_row(updated)

    # ------------------------------------------------------------------ tokens

    def mint_access_token(self, user: User) -> Tuple[str, int]:
        ttl = settings.auth_access_token_ttl_minutes * 60
        now = _now()
        payload = {
            "sub": user.id,
            "usr": user.username,
            "rol": user.role,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            "jti": secrets.token_urlsafe(12),
            "typ": "access",
        }
        token = jwt.encode(
            payload,
            settings.auth_jwt_secret,
            algorithm=settings.auth_jwt_algorithm,
        )
        return token, ttl

    def decode_access_token(self, token: str) -> dict:
        try:
            claims = jwt.decode(
                token,
                settings.auth_jwt_secret,
                algorithms=[settings.auth_jwt_algorithm],
                options={"require": ["exp", "sub", "typ"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        if claims.get("typ") != "access":
            raise InvalidToken("wrong token type")
        return claims

    async def issue_refresh_token(
        self,
        user: User,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        raw = secrets.token_urlsafe(48)
        expires = _now() + timedelta(days=settings.auth_refresh_token_ttl_days)
        async with self._session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO refresh_tokens
                        (user_id, token_hash, parent_id, user_agent, ip, expires_at)
                    VALUES (:uid, :hash, :parent, :ua, :ip, :exp)
                    """
                ),
                {
                    "uid": user.id,
                    "hash": _sha256(raw),
                    "parent": parent_id,
                    "ua": (user_agent or "")[:512] or None,
                    "ip": ip,
                    "exp": expires,
                },
            )
            await session.commit()
        return raw

    async def rotate_refresh_token(
        self,
        raw_token: str,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[User, str, str, int]:
        """
        Validate, revoke, and replace a refresh token.

        Returns (user, new_access_token, new_refresh_token, access_ttl).
        Detects token reuse: if a token that was already revoked is presented,
        revoke the whole family as a defensive measure.
        """
        await self.bootstrap()
        token_hash = _sha256(raw_token)
        async with self._session() as session:
            row = await session.execute(
                text(
                    """
                    SELECT rt.id AS rt_id, rt.user_id, rt.revoked, rt.expires_at, rt.parent_id,
                           u.id, u.email, u.username, u.display_name, u.role,
                           u.is_active, u.email_verified, u.created_at, u.updated_at,
                           u.last_login_at
                    FROM refresh_tokens rt
                    JOIN users u ON u.id = rt.user_id
                    WHERE rt.token_hash = :h
                    LIMIT 1
                    """
                ),
                {"h": token_hash},
            )
            rec = row.mappings().first()
            if rec is None:
                raise InvalidToken("refresh token not recognised")

            if rec["revoked"]:
                # Reuse detected: revoke the entire user's active tokens.
                await session.execute(
                    text(
                        "UPDATE refresh_tokens SET revoked = TRUE, "
                        "revoked_reason = 'replay_detected' "
                        "WHERE user_id = :uid AND revoked = FALSE"
                    ),
                    {"uid": rec["user_id"]},
                )
                await session.commit()
                logger.warning("refresh_token_replay", user_id=str(rec["user_id"]))
                raise InvalidToken("refresh token reused")

            if rec["expires_at"] < _now():
                raise InvalidToken("refresh token expired")

            if not rec["is_active"]:
                raise AccountDisabled("account disabled")

            # Revoke old, log last_used, create chained replacement.
            await session.execute(
                text(
                    """
                    UPDATE refresh_tokens
                    SET revoked = TRUE, revoked_reason = 'rotated', last_used_at = NOW()
                    WHERE id = :id
                    """
                ),
                {"id": rec["rt_id"]},
            )
            await session.commit()

        user = _user_from_row(rec)
        access, ttl = self.mint_access_token(user)
        new_refresh = await self.issue_refresh_token(
            user, ip=ip, user_agent=user_agent, parent_id=str(rec["rt_id"])
        )
        return user, access, new_refresh, ttl

    async def revoke_refresh_token(self, raw_token: str, reason: str = "logout") -> None:
        async with self._session() as session:
            await session.execute(
                text(
                    """
                    UPDATE refresh_tokens
                    SET revoked = TRUE, revoked_reason = :r
                    WHERE token_hash = :h AND revoked = FALSE
                    """
                ),
                {"r": reason, "h": _sha256(raw_token)},
            )
            await session.commit()

    async def revoke_all_for_user(self, user_id: str, reason: str = "user_logout_all") -> None:
        async with self._session() as session:
            await session.execute(
                text(
                    "UPDATE refresh_tokens SET revoked = TRUE, revoked_reason = :r "
                    "WHERE user_id = :uid AND revoked = FALSE"
                ),
                {"uid": user_id, "r": reason},
            )
            await session.commit()

    async def get_user(self, user_id: str) -> Optional[User]:
        await self.bootstrap()
        async with self._session() as session:
            row = await session.execute(
                text(
                    """
                    SELECT id, email, username, display_name, role,
                           is_active, email_verified, created_at, updated_at,
                           last_login_at
                    FROM users WHERE id = :id
                    """
                ),
                {"id": user_id},
            )
            record = row.mappings().first()
            return _user_from_row(record) if record else None

    # ------------------------------------------------------------------ rate limiting

    async def _login_rate_limited(self, session: AsyncSession, ip: str) -> bool:
        result = await session.execute(
            text(
                """
                SELECT COUNT(*)::int AS c FROM login_attempts
                WHERE ip = :ip AND created_at > NOW() - INTERVAL '1 minute'
                """
            ),
            {"ip": ip},
        )
        count = result.scalar_one()
        return count >= settings.auth_ip_login_per_minute

    async def _register_rate_limited(self, session: AsyncSession, ip: str) -> bool:
        result = await session.execute(
            text(
                """
                SELECT COUNT(*)::int AS c FROM login_attempts
                WHERE ip = :ip AND failure_reason = 'registration'
                  AND created_at > NOW() - INTERVAL '1 hour'
                """
            ),
            {"ip": ip},
        )
        count = result.scalar_one()
        return count >= settings.auth_register_per_hour_per_ip

    async def _log_attempt(
        self,
        session: AsyncSession,
        identifier: str,
        ip: Optional[str],
        user_agent: Optional[str],
        success: bool,
        reason: Optional[str],
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO login_attempts (identifier, ip, user_agent, success, failure_reason)
                VALUES (:id, :ip, :ua, :s, :r)
                """
            ),
            {
                "id": identifier[:256],
                "ip": ip,
                "ua": (user_agent or "")[:512] or None,
                "s": success,
                "r": reason,
            },
        )

    # ------------------------------------------------------------------ helpers

    def _session(self):
        if storage_manager.pg_session_maker is None:
            raise RuntimeError("Postgres not initialised — call bootstrap() first")
        return storage_manager.pg_session_maker()


_USERS_DDL = [
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
    'CREATE EXTENSION IF NOT EXISTS "citext"',
    """
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        email CITEXT UNIQUE,
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
        CONSTRAINT username_format CHECK (username ~ '^[A-Za-z0-9_\\-]{3,64}$'),
        CONSTRAINT role_values CHECK (role IN ('user','admin'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users (username)",
    # Backwards-compat for deployments that created `users` before email became optional.
    "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users (created_at DESC)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens (user_id, revoked, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_refresh_expires ON refresh_tokens (expires_at)",
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id BIGSERIAL PRIMARY KEY,
        identifier CITEXT NOT NULL,
        ip INET,
        user_agent TEXT,
        success BOOLEAN NOT NULL,
        failure_reason VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (ip, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_id_time ON login_attempts (identifier, created_at DESC)",
]


def _user_from_row(record) -> User:
    return User(
        id=str(record["id"]),
        email=record["email"],
        username=record["username"],
        display_name=record.get("display_name"),
        role=record.get("role", "user"),
        is_active=bool(record.get("is_active", True)),
        email_verified=bool(record.get("email_verified", False)),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        last_login_at=record.get("last_login_at"),
    )


def public_user(user: User) -> PublicUser:
    return PublicUser(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        email_verified=user.email_verified,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def ct_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


auth_service = AuthService()

"""Pydantic models for the authentication layer."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


USERNAME_PATTERN = r"^[A-Za-z0-9_\-]{3,64}$"


class User(BaseModel):
    """Internal user record (never sent to clients as-is)."""

    id: str
    email: Optional[str] = None
    username: str
    display_name: Optional[str] = None
    role: str = "user"
    is_active: bool = True
    email_verified: bool = False
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None


class PublicUser(BaseModel):
    """Public projection — safe to return to clients."""

    id: str
    email: Optional[str] = None
    username: str
    display_name: Optional[str] = None
    role: str = "user"
    email_verified: bool = False
    created_at: datetime
    last_login_at: Optional[datetime] = None


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=10, max_length=256)
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, max_length=128)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        # Reject obvious weak patterns while staying lenient on symbols.
        if v.lower() == v or v.upper() == v:
            raise ValueError("password must contain both upper and lower case letters")
        if not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one digit")
        if " " in v[0:1] or " " in v[-1:]:
            raise ValueError("password must not start or end with whitespace")
        return v


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=256, description="email or username")
    password: str = Field(min_length=1, max_length=256)


class AuthTokens(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds
    user: PublicUser

"""FastAPI dependencies for authentication."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import User
from .service import AuthError, InvalidToken, auth_service

_bearer = HTTPBearer(auto_error=False, description="JWT access token")


async def _user_from_token(token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    try:
        claims = auth_service.decode_access_token(token)
    except InvalidToken:
        return None
    user = await auth_service.get_user(claims["sub"])
    if user is None or not user.is_active:
        return None
    return user


def _extract_token(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    if creds and creds.scheme.lower() == "bearer":
        return creds.credentials
    # Allow explicit header fallback for clients that can't set Authorization.
    header_token = request.headers.get("x-access-token")
    if header_token:
        return header_token
    # Query-string fallback for clients that can't set headers at all
    # (notably the browser's native EventSource, which has no header API).
    # Accept both `access_token` and the shorter `token` alias.
    qp_token = request.query_params.get("access_token") or request.query_params.get("token")
    return qp_token or None


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    token = _extract_token(request, creds)
    user = await _user_from_token(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_optional_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[User]:
    token = _extract_token(request, creds)
    try:
        return await _user_from_token(token)
    except AuthError:
        return None


# Type alias so routes can write `user: CurrentUser` once we wire PEP 695.
CurrentUser = User

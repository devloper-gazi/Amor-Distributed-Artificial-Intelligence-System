"""
Authentication HTTP routes.

Endpoints
---------
POST   /api/auth/register   — create an account
POST   /api/auth/login      — exchange credentials for access + refresh
POST   /api/auth/refresh    — rotate refresh + mint new access (cookie-based)
POST   /api/auth/logout     — revoke the current refresh token
POST   /api/auth/logout-all — revoke every session for the user
GET    /api/auth/me         — return the authenticated user
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status

from ..auth.dependencies import get_current_user
from ..auth.models import (
    AuthTokens,
    LoginRequest,
    PublicUser,
    RegisterRequest,
    User,
)
from ..auth.service import (
    AccountDisabled,
    AccountLocked,
    DuplicateUser,
    InvalidCredentials,
    InvalidToken,
    RateLimited,
    auth_service,
    public_user,
)
from ..config.settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/api/auth",
        max_age=settings.auth_refresh_token_ttl_days * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/api/auth",
    )


@router.post("/register", response_model=AuthTokens, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
) -> AuthTokens:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    try:
        user = await auth_service.register(
            username=payload.username,
            password=payload.password,
            email=payload.email,
            display_name=payload.display_name,
            ip=ip,
        )
    except DuplicateUser as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except RateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))

    access, ttl = auth_service.mint_access_token(user)
    refresh = await auth_service.issue_refresh_token(user, ip=ip, user_agent=ua)
    _set_refresh_cookie(response, refresh)
    return AuthTokens(access_token=access, expires_in=ttl, user=public_user(user))


@router.post("/login", response_model=AuthTokens)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> AuthTokens:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    try:
        user = await auth_service.authenticate(
            payload.identifier, payload.password, ip=ip, user_agent=ua
        )
    except (InvalidCredentials,):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    except AccountLocked as exc:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(exc))
    except AccountDisabled as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except RateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))

    access, ttl = auth_service.mint_access_token(user)
    refresh = await auth_service.issue_refresh_token(user, ip=ip, user_agent=ua)
    _set_refresh_cookie(response, refresh)
    return AuthTokens(access_token=access, expires_in=ttl, user=public_user(user))


@router.post("/refresh", response_model=AuthTokens)
async def refresh_token(
    request: Request,
    response: Response,
    refresh_cookie: Optional[str] = Cookie(None, alias=settings.auth_cookie_name),
) -> AuthTokens:
    raw = refresh_cookie or request.headers.get("x-refresh-token")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh token"
        )
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    try:
        user, access, new_refresh, ttl = await auth_service.rotate_refresh_token(
            raw, ip=ip, user_agent=ua
        )
    except InvalidToken:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token"
        )
    except AccountDisabled as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    _set_refresh_cookie(response, new_refresh)
    return AuthTokens(access_token=access, expires_in=ttl, user=public_user(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_cookie: Optional[str] = Cookie(None, alias=settings.auth_cookie_name),
) -> Response:
    if refresh_cookie:
        await auth_service.revoke_refresh_token(refresh_cookie, reason="logout")
    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    user: User = Depends(get_current_user),
) -> Response:
    await auth_service.revoke_all_for_user(user.id, reason="user_logout_all")
    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=PublicUser)
async def me(user: User = Depends(get_current_user)) -> PublicUser:
    return public_user(user)

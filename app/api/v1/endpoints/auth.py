"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep, CurrentUser, RequestContextDep, SessionDep
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterRequest,
    auth_service: AuthServiceDep,
    context: RequestContextDep,
    session: SessionDep,
) -> UserRead:
    user = await auth_service.register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        context=context,
    )
    await session.commit()
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for tokens")
async def login(
    payload: LoginRequest,
    auth_service: AuthServiceDep,
    context: RequestContextDep,
    session: SessionDep,
) -> TokenResponse:
    user = await auth_service.authenticate(
        email=payload.email, password=payload.password, context=context
    )
    tokens = auth_service.issue_tokens(user)
    # Commit after issuing: last_login_at and the audit entry are part of the
    # same transaction as the authentication itself.
    await session.commit()
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
    )


@router.post("/refresh", response_model=TokenResponse, summary="Exchange a refresh token")
async def refresh(payload: RefreshRequest, auth_service: AuthServiceDep) -> TokenResponse:
    _user, tokens = await auth_service.refresh(payload.refresh_token)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
    )


@router.get("/me", response_model=UserRead, summary="The authenticated user")
async def read_current_user(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)

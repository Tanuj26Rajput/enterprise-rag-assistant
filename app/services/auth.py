"""Registration, login and token issuance."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import (
    Conflict,
    InactiveAccount,
    InvalidCredentials,
    InvalidToken,
    ValidationFailed,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_dummy_password,
    verify_password,
)
from app.db.models import AuditAction, AuditOutcome, User
from app.repositories.audit import AuditRepository
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Where a request came from, for the audit trail."""

    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._audit = audit
        self._settings = settings

    # ------------------------------------------------------------ register ---
    async def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str | None,
        context: RequestContext | None = None,
    ) -> User:
        context = context or RequestContext()
        email = email.strip().lower()

        if len(password) < self._settings.password_min_length:
            raise ValidationFailed(
                f"Password must be at least {self._settings.password_min_length} characters."
            )

        if await self._users.exists_with_email(email):
            # Registration necessarily reveals that an address is taken; there
            # is no way to offer a unique-email signup without it. The login
            # endpoint, which an attacker can probe freely, does not.
            await self._audit.record(
                action=AuditAction.CREATE,
                event="auth.register",
                outcome=AuditOutcome.FAILURE,
                actor_email=email,
                resource_type="user",
                error_message="email already registered",
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                request_id=context.request_id,
            )
            raise Conflict("An account with this email address already exists.")

        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name.strip() if full_name else None,
            is_active=True,
        )
        await self._users.add(user)

        await self._audit.record(
            action=AuditAction.CREATE,
            event="auth.register",
            actor_id=user.id,
            actor_email=user.email,
            resource_type="user",
            resource_id=user.id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            request_id=context.request_id,
        )
        logger.info("Registered user %s", user.id)
        return user

    # --------------------------------------------------------- authenticate ---
    async def authenticate(
        self,
        *,
        email: str,
        password: str,
        context: RequestContext | None = None,
    ) -> User:
        context = context or RequestContext()
        email = email.strip().lower()
        user = await self._users.get_by_email(email)

        if user is None:
            # Hash anyway: skipping the work here would make "no such user"
            # measurably faster than "wrong password".
            verify_dummy_password(password)
            await self._record_failed_login(email, "unknown email", context)
            raise InvalidCredentials()

        is_valid, updated_hash = verify_password(password, user.hashed_password)
        if not is_valid:
            await self._record_failed_login(email, "incorrect password", context, user.id)
            raise InvalidCredentials()

        if not user.is_active:
            await self._record_failed_login(email, "inactive account", context, user.id)
            raise InactiveAccount()

        # Opportunistically upgrade a hash made with weaker parameters.
        if updated_hash:
            user.hashed_password = updated_hash

        user.last_login_at = datetime.now(UTC)
        await self._audit.record(
            action=AuditAction.LOGIN,
            event="auth.login",
            actor_id=user.id,
            actor_email=user.email,
            resource_type="user",
            resource_id=user.id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            request_id=context.request_id,
        )
        return user

    async def _record_failed_login(
        self,
        email: str,
        reason: str,
        context: RequestContext,
        user_id: uuid.UUID | None = None,
    ) -> None:
        await self._audit.record(
            action=AuditAction.LOGIN,
            event="auth.login",
            outcome=AuditOutcome.FAILURE,
            actor_id=user_id,
            actor_email=email,
            resource_type="user",
            resource_id=user_id,
            error_message=reason,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            request_id=context.request_id,
        )
        logger.warning("Failed login for %s: %s", email, reason)

    # --------------------------------------------------------------- tokens ---
    def issue_tokens(self, user: User) -> IssuedTokens:
        access_token, expires_at = create_access_token(user.id, self._settings)
        refresh_token, _ = create_refresh_token(user.id, self._settings)
        return IssuedTokens(access_token, refresh_token, expires_at)

    async def refresh(self, refresh_token: str) -> tuple[User, IssuedTokens]:
        claims = decode_token(refresh_token, self._settings, expected_type="refresh")
        user = await self._users.get_by_id(claims.subject)
        if user is None:
            raise InvalidToken("The token refers to an account that no longer exists.")
        if not user.is_active:
            raise InactiveAccount()
        return user, self.issue_tokens(user)

    async def resolve_access_token(self, token: str) -> User:
        """Identify the caller from a bearer token."""
        claims = decode_token(token, self._settings, expected_type="access")
        user = await self._users.get_by_id(claims.subject)
        if user is None:
            raise InvalidToken("The token refers to an account that no longer exists.")
        if not user.is_active:
            raise InactiveAccount()
        return user

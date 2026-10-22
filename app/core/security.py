"""Password hashing and JSON Web Tokens.

Argon2id is used for passwords: it is memory-hard, so an attacker with the
database dump cannot trade memory for parallelism the way they can against
bcrypt or PBKDF2. The parameters below are the pwdlib defaults (64 MiB,
3 passes), which meet the OWASP recommendation.

Tokens are signed with HMAC-SHA256 over ``SECRET_KEY``. That keeps the
deployment to a single secret; if tokens ever need to be verified by a service
that must not be able to mint them, this is the module to move to RS256.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import ExpiredSignatureError, JWTError, jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import Settings
from app.core.exceptions import ExpiredToken, InvalidToken

TokenType = Literal["access", "refresh"]

_password_hash = PasswordHash((Argon2Hasher(),))

# Verifying a throwaway hash makes a login attempt for an unknown email take
# the same time as one for a known email, so response timing does not reveal
# which addresses are registered.
_DUMMY_HASH = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Return an Argon2id hash, salt and parameters included."""
    return _password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> tuple[bool, str | None]:
    """Check a password.

    Returns ``(is_valid, updated_hash)``. ``updated_hash`` is non-None when the
    stored hash used weaker parameters than the current policy, in which case
    the caller should persist the new hash while it has the plaintext.
    """
    return _password_hash.verify_and_update(password, hashed_password)


def verify_dummy_password(password: str) -> None:
    """Burn the same CPU as a real verification, for unknown accounts."""
    _password_hash.verify(password, _DUMMY_HASH)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims this application trusts after a signature check."""

    subject: uuid.UUID
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime


def _create_token(
    subject: uuid.UUID,
    token_type: TokenType,
    expires_delta: timedelta,
    settings: Settings,
) -> tuple[str, datetime]:
    issued_at = datetime.now(UTC)
    expires_at = issued_at + expires_delta
    claims: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        # A unique id per token, so individual tokens can be revoked by
        # denylisting a jti once a revocation store exists.
        "jti": uuid.uuid4().hex,
        "iat": issued_at,
        "exp": expires_at,
    }
    token = jwt.encode(
        claims,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def create_access_token(subject: uuid.UUID, settings: Settings) -> tuple[str, datetime]:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        settings,
    )


def create_refresh_token(subject: uuid.UUID, settings: Settings) -> tuple[str, datetime]:
    return _create_token(
        subject,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
        settings,
    )


def decode_token(token: str, settings: Settings, *, expected_type: TokenType) -> TokenClaims:
    """Verify a token's signature, expiry and type.

    ``expected_type`` is enforced so a long-lived refresh token cannot be
    presented as an access token.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except ExpiredSignatureError as exc:
        raise ExpiredToken() from exc
    except JWTError as exc:
        raise InvalidToken() from exc

    if payload.get("typ") != expected_type:
        raise InvalidToken(f"Expected a {expected_type} token.")

    try:
        subject = uuid.UUID(str(payload["sub"]))
        issued_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
        expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
        jti = str(payload["jti"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidToken("The token is missing required claims.") from exc

    return TokenClaims(
        subject=subject,
        token_type=expected_type,
        jti=jti,
        issued_at=issued_at,
        expires_at=expires_at,
    )

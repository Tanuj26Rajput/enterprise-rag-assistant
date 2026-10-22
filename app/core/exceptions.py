"""Application error hierarchy.

Every error the API raises deliberately is an :class:`AppError`. A single
exception handler turns it into the response envelope, so handlers and
services never build error responses by hand and no unexpected exception can
leak a stack trace or a SQL fragment to a client.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors that map to a well-defined HTTP response."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.message = message or type(self).message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class AuthenticationError(AppError):
    """The caller could not be identified."""

    status_code = 401
    code = "authentication_failed"
    message = "Authentication credentials were missing or invalid."


class InvalidCredentials(AuthenticationError):
    code = "invalid_credentials"
    # Deliberately identical whether the email is unknown or the password is
    # wrong: a different message would turn login into an account oracle.
    message = "Incorrect email or password."


class InvalidToken(AuthenticationError):
    code = "invalid_token"
    message = "The access token is invalid."


class ExpiredToken(AuthenticationError):
    code = "token_expired"
    message = "The access token has expired."


class InactiveAccount(AuthenticationError):
    status_code = 403
    code = "inactive_account"
    message = "This account has been deactivated."


class PermissionDenied(AppError):
    """The caller is known, but is not allowed to perform this operation."""

    status_code = 403
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFound(AppError):
    """The resource does not exist, or the caller may not know that it does.

    Resources outside the caller's namespaces are reported as missing rather
    than forbidden. A 403 would confirm that a given id exists, which is
    enough to enumerate another tenant's namespaces and collections.
    """

    status_code = 404
    code = "not_found"
    message = "The requested resource was not found."


class Conflict(AppError):
    """The request collides with the current state of the resource."""

    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"
    message = "The request payload failed validation."


class ServiceUnavailable(AppError):
    """A dependency this request needed could not be reached.

    Distinct from a 500: nothing is wrong with the request, and retrying it
    later is a reasonable thing for a client to do. The provider's own error
    text is logged rather than returned, since it can quote the request back.
    """

    status_code = 503
    code = "service_unavailable"
    message = "An upstream service is temporarily unavailable. Please try again."


class UpstreamMisconfigured(ServiceUnavailable):
    """A dependency rejected us for a reason only an operator can fix.

    Still 503 - the feature genuinely is unavailable - but a separate code,
    because retrying will not help and the distinction is what tells an
    operator to go and look at the configuration.
    """

    code = "upstream_misconfigured"
    message = "The answer service is not correctly configured. An administrator has been notified."

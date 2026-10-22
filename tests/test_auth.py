"""Registration, login, token handling and /auth/me."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.security import create_access_token, hash_password, verify_password
from tests.conftest import DEFAULT_PASSWORD, auth_header, login, register
from tests.fakes import FakeStore


class TestRegistration:
    async def test_creates_an_account(self, client: AsyncClient) -> None:
        body = await register(client, "ada@example.com", full_name="Ada Lovelace")

        assert body["email"] == "ada@example.com"
        assert body["full_name"] == "Ada Lovelace"
        assert body["is_active"] is True
        assert body["is_superuser"] is False

    async def test_never_returns_the_password_or_its_hash(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "ada@example.com", "password": DEFAULT_PASSWORD},
        )
        raw = response.text

        assert "hashed_password" not in raw
        assert DEFAULT_PASSWORD not in raw
        assert "$argon2" not in raw

    async def test_duplicate_email_is_rejected(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")

        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "ada@example.com", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    async def test_duplicate_detection_ignores_case_and_padding(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")

        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "  ADA@Example.COM ", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 409

    async def test_short_password_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register", json={"email": "ada@example.com", "password": "short"}
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    async def test_malformed_email_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 422

    async def test_the_stored_hash_is_argon2_and_verifies(
        self, client: AsyncClient, store: FakeStore
    ) -> None:
        await register(client, "ada@example.com")
        user = next(iter(store.users.values()))

        assert user.hashed_password.startswith("$argon2id$")
        assert user.hashed_password != DEFAULT_PASSWORD
        assert verify_password(DEFAULT_PASSWORD, user.hashed_password)[0]


class TestLogin:
    async def test_valid_credentials_return_a_token_pair(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")

        body = await login(client, "ada@example.com")

        assert body["token_type"] == "bearer"
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["access_token"] != body["refresh_token"]
        assert body["expires_at"]

    async def test_invalid_password_is_rejected(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "ada@example.com", "password": "wrong-password-entirely"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_unknown_email_is_indistinguishable_from_a_wrong_password(
        self, client: AsyncClient
    ) -> None:
        """Otherwise login becomes an oracle for which addresses are registered."""
        await register(client, "ada@example.com")

        wrong_password = await client.post(
            "/api/v1/auth/login",
            json={"email": "ada@example.com", "password": "wrong-password-entirely"},
        )
        unknown_user = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-entirely"},
        )

        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.json()["error"] == unknown_user.json()["error"] | {
            "request_id": wrong_password.json()["error"]["request_id"]
        }

    async def test_a_failed_login_is_audited(self, client: AsyncClient, store: FakeStore) -> None:
        await register(client, "ada@example.com")
        await client.post(
            "/api/v1/auth/login",
            json={"email": "ada@example.com", "password": "wrong-password-entirely"},
        )

        failures = [e for e in store.events("auth.login") if e.outcome.value == "failure"]
        assert len(failures) == 1
        assert failures[0].error_message == "incorrect password"

    async def test_deactivated_account_cannot_log_in(
        self, client: AsyncClient, store: FakeStore
    ) -> None:
        await register(client, "ada@example.com")
        next(iter(store.users.values())).is_active = False

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "ada@example.com", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "inactive_account"

    async def test_login_records_last_login_at(self, client: AsyncClient, store: FakeStore) -> None:
        await register(client, "ada@example.com")
        assert next(iter(store.users.values())).last_login_at is None

        await login(client, "ada@example.com")

        assert next(iter(store.users.values())).last_login_at is not None


class TestCurrentUser:
    async def test_returns_the_authenticated_user(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com", full_name="Ada Lovelace")
        tokens = await login(client, "ada@example.com")

        response = await client.get("/api/v1/auth/me", headers=auth_header(tokens))

        assert response.status_code == 200
        assert response.json()["email"] == "ada@example.com"
        assert "hashed_password" not in response.text

    async def test_requires_a_token(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/auth/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"

    async def test_rejects_a_malformed_token(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"

    async def test_rejects_a_token_signed_with_another_secret(
        self, client: AsyncClient, store: FakeStore, settings: Settings
    ) -> None:
        await register(client, "ada@example.com")
        user_id = next(iter(store.users))
        forged = Settings(
            _env_file=None,
            DATABASE_URL=str(settings.database_url),
            SECRET_KEY="a-completely-different-secret",
        )
        token, _ = create_access_token(user_id, forged)

        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"

    async def test_rejects_an_expired_token(
        self, client: AsyncClient, store: FakeStore, settings: Settings
    ) -> None:
        await register(client, "ada@example.com")
        user_id = next(iter(store.users))

        # Mint a token whose lifetime has already elapsed.
        with patch("app.core.security.timedelta", side_effect=lambda **_: timedelta(minutes=-5)):
            expired, _ = create_access_token(user_id, settings)

        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "token_expired"

    async def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, client: AsyncClient
    ) -> None:
        """The `typ` claim stops a long-lived token standing in for a short one."""
        await register(client, "ada@example.com")
        tokens = await login(client, "ada@example.com")

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"

    async def test_rejects_a_non_bearer_scheme(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc123"})

        assert response.status_code == 401

    async def test_a_deactivated_user_loses_access_immediately(
        self, client: AsyncClient, store: FakeStore
    ) -> None:
        await register(client, "ada@example.com")
        tokens = await login(client, "ada@example.com")
        next(iter(store.users.values())).is_active = False

        response = await client.get("/api/v1/auth/me", headers=auth_header(tokens))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "inactive_account"


class TestRefresh:
    async def test_exchanges_a_refresh_token_for_a_new_pair(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")
        tokens = await login(client, "ada@example.com")

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 200
        refreshed = response.json()
        me = await client.get("/api/v1/auth/me", headers=auth_header(refreshed))
        assert me.status_code == 200

    async def test_an_access_token_cannot_be_used_to_refresh(self, client: AsyncClient) -> None:
        await register(client, "ada@example.com")
        tokens = await login(client, "ada@example.com")

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
        )

        assert response.status_code == 401


class TestPasswordHashing:
    def test_hashes_are_salted(self) -> None:
        assert hash_password("same-password") != hash_password("same-password")

    @pytest.mark.parametrize("wrong", ["", "nearly-right", "Password123!"])
    def test_rejects_incorrect_passwords(self, wrong: str) -> None:
        hashed = hash_password("the-real-password")

        assert verify_password(wrong, hashed)[0] is False

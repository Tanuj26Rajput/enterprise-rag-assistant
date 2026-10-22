"""Collection CRUD and its authorization rules."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import add_member, make_collection, make_namespace, signed_in


@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def namespace(client: AsyncClient, owner: dict[str, str]) -> dict:
    return await make_namespace(client, owner, "acme")


@pytest.fixture
async def collection(client: AsyncClient, owner: dict, namespace: dict) -> dict:
    return await make_collection(client, owner, namespace["id"], "policies")


def collections_url(namespace_id: str) -> str:
    return f"/api/v1/namespaces/{namespace_id}/collections"


class TestCreate:
    async def test_creates_a_collection(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        body = await make_collection(client, owner, namespace["id"], "policies")

        assert body["slug"] == "policies"
        assert body["namespace_id"] == namespace["id"]
        assert body["document_count"] == 0

    async def test_vector_collection_is_namespaced(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        """Two tenants using the same slug must not share an embedding space."""
        first = await make_collection(client, owner, namespace["id"], "policies")
        other_ns = await make_namespace(client, owner, "other-co")
        second = await make_collection(client, owner, other_ns["id"], "policies")

        assert first["vector_collection"] != second["vector_collection"]
        assert "acme" in first["vector_collection"]

    async def test_duplicate_slug_within_a_namespace_is_rejected(
        self, client: AsyncClient, namespace: dict, owner: dict, collection: dict
    ) -> None:
        response = await client.post(
            collections_url(namespace["id"]),
            headers=owner,
            json={"slug": "policies", "name": "Duplicate"},
        )

        assert response.status_code == 409

    async def test_the_same_slug_is_fine_in_a_different_namespace(
        self, client: AsyncClient, owner: dict, collection: dict
    ) -> None:
        other = await make_namespace(client, owner, "other-co")

        response = await client.post(
            collections_url(other["id"]),
            headers=owner,
            json={"slug": "policies", "name": "Policies"},
        )

        assert response.status_code == 201

    async def test_invalid_slug_is_rejected(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        response = await client.post(
            collections_url(namespace["id"]),
            headers=owner,
            json={"slug": "Not A Slug", "name": "X"},
        )

        assert response.status_code == 422


class TestAuthorization:
    @pytest.fixture
    async def viewer(self, client: AsyncClient, owner: dict, namespace: dict) -> dict[str, str]:
        headers = await signed_in(client, "viewer@example.com")
        await add_member(client, owner, namespace["id"], "viewer@example.com", "viewer")
        return headers

    @pytest.fixture
    async def member(self, client: AsyncClient, owner: dict, namespace: dict) -> dict[str, str]:
        headers = await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")
        return headers

    @pytest.fixture
    async def admin(self, client: AsyncClient, owner: dict, namespace: dict) -> dict[str, str]:
        headers = await signed_in(client, "admin@example.com")
        await add_member(client, owner, namespace["id"], "admin@example.com", "admin")
        return headers

    async def test_unauthenticated_requests_are_rejected(
        self, client: AsyncClient, namespace: dict
    ) -> None:
        response = await client.get(collections_url(namespace["id"]))

        assert response.status_code == 401

    async def test_an_outsider_cannot_list_collections(
        self, client: AsyncClient, namespace: dict, collection: dict
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.get(collections_url(namespace["id"]), headers=outsider)

        assert response.status_code == 404

    async def test_an_outsider_cannot_read_a_collection_by_id(
        self, client: AsyncClient, namespace: dict, collection: dict
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.get(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=outsider
        )

        assert response.status_code == 404

    async def test_a_collection_cannot_be_reached_through_another_namespace(
        self, client: AsyncClient, owner: dict, collection: dict
    ) -> None:
        """The caller owns both namespaces, so this is purely the scoping check."""
        other = await make_namespace(client, owner, "other-co")

        response = await client.get(
            f"{collections_url(other['id'])}/{collection['id']}", headers=owner
        )

        assert response.status_code == 404

    async def test_a_viewer_cannot_create_a_collection(
        self, client: AsyncClient, namespace: dict, viewer: dict
    ) -> None:
        response = await client.post(
            collections_url(namespace["id"]),
            headers=viewer,
            json={"slug": "new-one", "name": "New"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_viewer_can_read(
        self, client: AsyncClient, namespace: dict, viewer: dict, collection: dict
    ) -> None:
        response = await client.get(collections_url(namespace["id"]), headers=viewer)

        assert response.status_code == 200
        assert response.json()["total"] == 1

    async def test_a_viewer_cannot_update(
        self, client: AsyncClient, namespace: dict, viewer: dict, collection: dict
    ) -> None:
        response = await client.patch(
            f"{collections_url(namespace['id'])}/{collection['id']}",
            headers=viewer,
            json={"name": "Renamed"},
        )

        assert response.status_code == 403

    async def test_a_member_can_create_and_update(
        self, client: AsyncClient, namespace: dict, member: dict
    ) -> None:
        created = await make_collection(client, member, namespace["id"], "handbook")

        response = await client.patch(
            f"{collections_url(namespace['id'])}/{created['id']}",
            headers=member,
            json={"name": "Employee Handbook"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Employee Handbook"

    async def test_a_member_cannot_delete_a_collection(
        self, client: AsyncClient, namespace: dict, member: dict, collection: dict
    ) -> None:
        """Deleting retires every document in it, so it needs admin."""
        response = await client.delete(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=member
        )

        assert response.status_code == 403

    async def test_an_admin_can_delete_a_collection(
        self, client: AsyncClient, namespace: dict, admin: dict, collection: dict, owner: dict
    ) -> None:
        response = await client.delete(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=admin
        )

        assert response.status_code == 204
        follow_up = await client.get(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=owner
        )
        assert follow_up.status_code == 404

    async def test_an_outsider_cannot_delete(
        self, client: AsyncClient, namespace: dict, collection: dict
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.delete(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=outsider
        )

        assert response.status_code == 404


class TestReadAndUpdate:
    async def test_unknown_collection_id_is_not_found(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        response = await client.get(
            f"{collections_url(namespace['id'])}/{uuid.uuid4()}", headers=owner
        )

        assert response.status_code == 404

    async def test_an_empty_patch_is_rejected(
        self, client: AsyncClient, namespace: dict, owner: dict, collection: dict
    ) -> None:
        response = await client.patch(
            f"{collections_url(namespace['id'])}/{collection['id']}", headers=owner, json={}
        )

        assert response.status_code == 422

    async def test_metadata_round_trips(
        self, client: AsyncClient, namespace: dict, owner: dict, collection: dict
    ) -> None:
        response = await client.patch(
            f"{collections_url(namespace['id'])}/{collection['id']}",
            headers=owner,
            json={"metadata": {"retention": "7y"}},
        )

        assert response.status_code == 200
        assert response.json()["metadata"] == {"retention": "7y"}

    async def test_pagination(self, client: AsyncClient, namespace: dict, owner: dict) -> None:
        for index in range(4):
            await make_collection(client, owner, namespace["id"], f"col-{index}")

        page = (
            await client.get(f"{collections_url(namespace['id'])}?limit=3", headers=owner)
        ).json()

        assert page["total"] == 4
        assert len(page["items"]) == 3
        assert page["limit"] == 3

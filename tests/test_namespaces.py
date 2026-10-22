"""Namespace CRUD, membership and the authorization rules around them."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import add_member, make_namespace, signed_in
from tests.fakes import FakeStore


@pytest.fixture
async def owner(client: AsyncClient) -> dict[str, str]:
    return await signed_in(client, "owner@example.com")


@pytest.fixture
async def namespace(client: AsyncClient, owner: dict[str, str]) -> dict:
    return await make_namespace(client, owner, "acme")


class TestCreate:
    async def test_creator_becomes_owner(self, client: AsyncClient, owner: dict) -> None:
        body = await make_namespace(client, owner, "acme", name="Acme Corp")

        assert body["slug"] == "acme"
        assert body["role"] == "owner"

    async def test_creator_gets_a_membership_row(
        self, client: AsyncClient, owner: dict, store: FakeStore
    ) -> None:
        body = await make_namespace(client, owner, "acme")

        members = [m for m in store.members.values() if str(m.namespace_id) == body["id"]]
        assert len(members) == 1
        assert members[0].role.value == "owner"

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/namespaces", json={"slug": "acme", "name": "Acme"})

        assert response.status_code == 401

    async def test_duplicate_slug_is_rejected(self, client: AsyncClient, owner: dict) -> None:
        await make_namespace(client, owner, "acme")

        response = await client.post(
            "/api/v1/namespaces", headers=owner, json={"slug": "acme", "name": "Other"}
        )

        assert response.status_code == 409

    @pytest.mark.parametrize("slug", ["Acme", "a", "has space", "trailing-", "-leading", "a_b"])
    async def test_invalid_slugs_are_rejected(
        self, client: AsyncClient, owner: dict, slug: str
    ) -> None:
        response = await client.post(
            "/api/v1/namespaces", headers=owner, json={"slug": slug, "name": "X"}
        )

        assert response.status_code == 422, slug


class TestTenantIsolation:
    async def test_list_only_shows_your_own_namespaces(
        self, client: AsyncClient, owner: dict
    ) -> None:
        await make_namespace(client, owner, "acme")
        outsider = await signed_in(client, "outsider@example.com")
        await make_namespace(client, outsider, "other-co")

        response = await client.get("/api/v1/namespaces", headers=owner)

        body = response.json()
        assert body["total"] == 1
        assert [item["slug"] for item in body["items"]] == ["acme"]

    async def test_a_non_member_is_told_the_namespace_does_not_exist(
        self, client: AsyncClient, namespace: dict
    ) -> None:
        """404 rather than 403: a 403 would confirm the id is real."""
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=outsider)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_a_non_member_cannot_update(self, client: AsyncClient, namespace: dict) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}", headers=outsider, json={"name": "Seized"}
        )

        assert response.status_code == 404

    async def test_a_non_member_cannot_delete(self, client: AsyncClient, namespace: dict) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        response = await client.delete(f"/api/v1/namespaces/{namespace['id']}", headers=outsider)

        assert response.status_code == 404

    async def test_an_unknown_id_looks_the_same_as_someone_elses(
        self, client: AsyncClient, namespace: dict
    ) -> None:
        outsider = await signed_in(client, "outsider@example.com")

        unknown = await client.get(f"/api/v1/namespaces/{uuid.uuid4()}", headers=outsider)
        foreign = await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=outsider)

        assert unknown.status_code == foreign.status_code == 404
        assert unknown.json()["error"]["message"] == foreign.json()["error"]["message"]


class TestRoleEnforcement:
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

    async def test_a_member_can_read(
        self, client: AsyncClient, namespace: dict, member: dict
    ) -> None:
        response = await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=member)

        assert response.status_code == 200
        assert response.json()["role"] == "member"

    async def test_a_member_cannot_update_the_namespace(
        self, client: AsyncClient, namespace: dict, member: dict
    ) -> None:
        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}", headers=member, json={"name": "Renamed"}
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_viewer_cannot_update_the_namespace(
        self, client: AsyncClient, namespace: dict, viewer: dict
    ) -> None:
        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}", headers=viewer, json={"name": "Renamed"}
        )

        assert response.status_code == 403

    async def test_an_admin_can_update_the_namespace(
        self, client: AsyncClient, namespace: dict, admin: dict
    ) -> None:
        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}", headers=admin, json={"name": "Renamed"}
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_an_admin_cannot_delete_the_namespace(
        self, client: AsyncClient, namespace: dict, admin: dict
    ) -> None:
        """Deletion destroys every document in the tenant; owners only."""
        response = await client.delete(f"/api/v1/namespaces/{namespace['id']}", headers=admin)

        assert response.status_code == 403

    async def test_the_owner_can_delete_the_namespace(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        response = await client.delete(f"/api/v1/namespaces/{namespace['id']}", headers=owner)

        assert response.status_code == 204
        assert (
            await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=owner)
        ).status_code == 404

    async def test_a_viewer_cannot_invite_members(
        self, client: AsyncClient, namespace: dict, viewer: dict
    ) -> None:
        await signed_in(client, "newcomer@example.com")

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/members",
            headers=viewer,
            json={"email": "newcomer@example.com", "role": "member"},
        )

        assert response.status_code == 403

    async def test_an_admin_cannot_mint_an_owner(
        self, client: AsyncClient, namespace: dict, admin: dict
    ) -> None:
        """Otherwise an admin could escalate past their own authority."""
        await signed_in(client, "newcomer@example.com")

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/members",
            headers=admin,
            json={"email": "newcomer@example.com", "role": "owner"},
        )

        assert response.status_code == 403

    async def test_an_admin_cannot_demote_an_owner(
        self, client: AsyncClient, namespace: dict, owner: dict, admin: dict
    ) -> None:
        members = (
            await client.get(f"/api/v1/namespaces/{namespace['id']}/members", headers=owner)
        ).json()["items"]
        owner_member = next(m for m in members if m["role"] == "owner")

        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}/members/{owner_member['id']}",
            headers=admin,
            json={"role": "member"},
        )

        assert response.status_code == 403


class TestMembership:
    async def test_inviting_an_unknown_email_is_a_404(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/members",
            headers=owner,
            json={"email": "ghost@example.com", "role": "member"},
        )

        assert response.status_code == 404

    async def test_inviting_an_existing_member_is_a_conflict(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        await signed_in(client, "member@example.com")
        await add_member(client, owner, namespace["id"], "member@example.com", "member")

        response = await client.post(
            f"/api/v1/namespaces/{namespace['id']}/members",
            headers=owner,
            json={"email": "member@example.com", "role": "viewer"},
        )

        assert response.status_code == 409

    async def test_the_last_owner_cannot_be_demoted(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        """A namespace with no owner could never be administered or deleted."""
        members = (
            await client.get(f"/api/v1/namespaces/{namespace['id']}/members", headers=owner)
        ).json()["items"]
        owner_member = members[0]

        response = await client.patch(
            f"/api/v1/namespaces/{namespace['id']}/members/{owner_member['id']}",
            headers=owner,
            json={"role": "admin"},
        )

        assert response.status_code == 409

    async def test_the_last_owner_cannot_be_removed(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        members = (
            await client.get(f"/api/v1/namespaces/{namespace['id']}/members", headers=owner)
        ).json()["items"]

        response = await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/members/{members[0]['id']}", headers=owner
        )

        assert response.status_code == 409

    async def test_a_member_id_from_another_namespace_is_not_found(
        self, client: AsyncClient, owner: dict
    ) -> None:
        first = await make_namespace(client, owner, "acme")
        second = await make_namespace(client, owner, "other-co")
        foreign = (
            await client.get(f"/api/v1/namespaces/{first['id']}/members", headers=owner)
        ).json()["items"][0]

        response = await client.patch(
            f"/api/v1/namespaces/{second['id']}/members/{foreign['id']}",
            headers=owner,
            json={"role": "admin"},
        )

        assert response.status_code == 404

    async def test_a_removed_member_loses_access(
        self, client: AsyncClient, namespace: dict, owner: dict
    ) -> None:
        guest = await signed_in(client, "guest@example.com")
        member = await add_member(client, owner, namespace["id"], "guest@example.com", "member")
        assert (
            await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=guest)
        ).status_code == 200

        await client.delete(
            f"/api/v1/namespaces/{namespace['id']}/members/{member['id']}", headers=owner
        )

        assert (
            await client.get(f"/api/v1/namespaces/{namespace['id']}", headers=guest)
        ).status_code == 404


class TestPagination:
    async def test_limit_and_offset_walk_the_result_set(
        self, client: AsyncClient, owner: dict
    ) -> None:
        for index in range(5):
            await make_namespace(client, owner, f"ns-{index}")

        first = (await client.get("/api/v1/namespaces?limit=2", headers=owner)).json()
        second = (await client.get("/api/v1/namespaces?limit=2&offset=2", headers=owner)).json()

        assert first["total"] == second["total"] == 5
        assert len(first["items"]) == len(second["items"]) == 2
        assert {n["slug"] for n in first["items"]}.isdisjoint({n["slug"] for n in second["items"]})

    @pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
    async def test_out_of_range_pagination_is_rejected(
        self, client: AsyncClient, owner: dict, query: str
    ) -> None:
        response = await client.get(f"/api/v1/namespaces?{query}", headers=owner)

        assert response.status_code == 422

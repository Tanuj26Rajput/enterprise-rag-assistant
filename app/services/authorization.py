"""Namespace authorization.

Two rules decide every answer in this module:

1. **A caller who is not a member of a namespace is told it does not exist.**
   Returning 403 would confirm the id is real, which is enough to enumerate
   other tenants' namespaces one UUID at a time. 404 leaks nothing.
2. **A caller who *is* a member but lacks the role is told 403.** They already
   know the resource exists, so hiding it serves no purpose and makes a
   legitimate permission problem impossible to diagnose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.exceptions import NotFound, PermissionDenied
from app.db.models import Collection, Namespace, NamespaceRole, User
from app.repositories.namespace import NamespaceMemberRepository, NamespaceRepository


def require_active_collection(collection: Collection) -> None:
    """Refuse to add to, or search, a collection that has been frozen.

    ``is_active`` is offered on the update endpoint, so it has to mean
    something: a flag a user can set that changes nothing is worse than no
    flag at all. Deactivating holds a collection still - no new documents,
    no retrieval - while leaving it visible and reversible.
    """
    if not collection.is_active:
        raise PermissionDenied(
            f"The collection {collection.slug!r} is deactivated. Reactivate it before "
            "adding to it or searching it."
        )


@dataclass(frozen=True, slots=True)
class NamespaceContext:
    """A verified (user, namespace, role) triple.

    Handlers receive this instead of a bare namespace id, so it is impossible
    to reach one without having passed an authorization check.
    """

    user: User
    namespace: Namespace
    role: NamespaceRole

    @property
    def namespace_id(self) -> uuid.UUID:
        return self.namespace.id

    def require(self, minimum: NamespaceRole) -> None:
        """Raise unless the caller holds at least ``minimum``."""
        if self.role.rank > minimum.rank:
            raise PermissionDenied(
                f"This action requires the {minimum.value} role; you have {self.role.value}."
            )

    def require_write(self) -> None:
        if not self.role.can_write():
            raise PermissionDenied("Viewers have read-only access to this namespace.")
        if not self.namespace.is_active:
            # Suspension is a write freeze, not a blackout: members can still
            # read what is already there, and an admin can still reactivate -
            # that path goes through `require()` with a role minimum rather
            # than through here, so suspending a workspace cannot lock the
            # people who could undo it out of it.
            raise PermissionDenied(
                "This workspace is deactivated. An admin can reactivate it before "
                "any further changes are made."
            )


class NamespaceAuthorizer:
    def __init__(
        self,
        namespaces: NamespaceRepository,
        members: NamespaceMemberRepository,
    ) -> None:
        self._namespaces = namespaces
        self._members = members

    async def authorize(
        self,
        user: User,
        namespace_id: uuid.UUID,
        *,
        minimum_role: NamespaceRole | None = None,
    ) -> NamespaceContext:
        """Resolve a namespace for this user, or raise.

        Raises :class:`NotFound` when the namespace does not exist, has been
        soft-deleted, or the caller is not a member of it - the three cases
        are deliberately indistinguishable from outside.
        """
        namespace = await self._namespaces.get(namespace_id)
        if namespace is None:
            raise NotFound("Namespace not found.")

        role = await self._resolve_role(user, namespace)
        if role is None:
            raise NotFound("Namespace not found.")

        context = NamespaceContext(user=user, namespace=namespace, role=role)
        if minimum_role is not None:
            context.require(minimum_role)
        return context

    async def _resolve_role(self, user: User, namespace: Namespace) -> NamespaceRole | None:
        # Platform operators act as owners. This is the only cross-tenant
        # path in the system and it exists for support and incident response.
        if user.is_superuser:
            return NamespaceRole.OWNER

        membership = await self._members.get(namespace.id, user.id)
        if membership is not None:
            return membership.role

        # A namespace's owner is a member by construction, but fall back to
        # the ownership column so a missing membership row can never lock an
        # owner out of their own namespace.
        if namespace.owner_id == user.id:
            return NamespaceRole.OWNER

        return None

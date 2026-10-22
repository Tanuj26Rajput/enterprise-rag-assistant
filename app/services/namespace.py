"""Namespace and membership operations."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.core.exceptions import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.db.models import (
    AuditAction,
    Namespace,
    NamespaceMember,
    NamespaceRole,
    User,
)
from app.repositories.audit import AuditRepository
from app.repositories.namespace import NamespaceMemberRepository, NamespaceRepository
from app.schemas.namespace import NamespaceCreate, NamespaceUpdate
from app.services.authorization import NamespaceAuthorizer, NamespaceContext

logger = logging.getLogger(__name__)


class NamespaceService:
    def __init__(
        self,
        namespaces: NamespaceRepository,
        members: NamespaceMemberRepository,
        audit: AuditRepository,
        authorizer: NamespaceAuthorizer,
    ) -> None:
        self._namespaces = namespaces
        self._members = members
        self._audit = audit
        self._authorizer = authorizer

    # ----------------------------------------------------------- namespaces ---
    async def create(self, user: User, payload: NamespaceCreate) -> tuple[Namespace, NamespaceRole]:
        if await self._namespaces.get_by_slug(payload.slug):
            raise Conflict(f"The namespace slug {payload.slug!r} is already taken.")

        namespace = Namespace(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            owner_id=user.id,
            settings=payload.settings,
        )
        await self._namespaces.add(namespace)

        # The creator gets an explicit membership row as well as the ownership
        # column, so membership queries need no special case for the owner.
        await self._members.add(
            NamespaceMember(
                namespace_id=namespace.id,
                user_id=user.id,
                role=NamespaceRole.OWNER,
            )
        )

        await self._audit.record(
            action=AuditAction.CREATE,
            event="namespace.create",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace.id,
            resource_type="namespace",
            resource_id=namespace.id,
            resource_label=namespace.slug,
        )
        return namespace, NamespaceRole.OWNER

    async def list_for_user(
        self, user: User, *, limit: int, offset: int
    ) -> tuple[list[tuple[Namespace, NamespaceRole]], int]:
        return await self._namespaces.list_for_user(user.id, limit=limit, offset=offset)

    async def get(self, user: User, namespace_id: uuid.UUID) -> NamespaceContext:
        return await self._authorizer.authorize(user, namespace_id)

    async def update(
        self, user: User, namespace_id: uuid.UUID, payload: NamespaceUpdate
    ) -> NamespaceContext:
        context = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.ADMIN
        )
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise ValidationFailed("No fields to update.")

        for field, value in changes.items():
            setattr(context.namespace, field, value)

        await self._audit.record(
            action=AuditAction.UPDATE,
            event="namespace.update",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="namespace",
            resource_id=namespace_id,
            resource_label=context.namespace.slug,
            metadata={"fields": sorted(changes)},
        )
        return context

    async def delete(self, user: User, namespace_id: uuid.UUID) -> None:
        # Deletion destroys every collection, document and conversation in the
        # tenant, so it is restricted to owners - admins cannot do it.
        context = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.OWNER
        )
        await self._namespaces.soft_delete(context.namespace, at=datetime.now(UTC))

        await self._audit.record(
            action=AuditAction.DELETE,
            event="namespace.delete",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="namespace",
            resource_id=namespace_id,
            resource_label=context.namespace.slug,
        )
        logger.info("Namespace %s soft-deleted by %s", namespace_id, user.id)

    # ----------------------------------------------------------- membership ---
    async def list_members(
        self, user: User, namespace_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[NamespaceMember], int]:
        await self._authorizer.authorize(user, namespace_id)
        return await self._members.list_for_namespace(namespace_id, limit=limit, offset=offset)

    async def add_member(
        self, user: User, namespace_id: uuid.UUID, *, email: str, role: NamespaceRole
    ) -> NamespaceMember:
        context = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.ADMIN
        )
        # An admin must not be able to mint an owner and so escalate past
        # their own authority.
        if role is NamespaceRole.OWNER:
            context.require(NamespaceRole.OWNER)

        invitee = await self._members.find_user_by_email(email)
        if invitee is None:
            raise NotFound("No user with that email address.")
        if await self._members.get(namespace_id, invitee.id) is not None:
            raise Conflict("That user is already a member of this namespace.")

        member = await self._members.add(
            NamespaceMember(
                namespace_id=namespace_id,
                user_id=invitee.id,
                role=role,
                invited_by_id=user.id,
            )
        )
        await self._audit.record(
            action=AuditAction.CREATE,
            event="namespace.member.add",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="namespace_member",
            resource_id=member.id,
            resource_label=invitee.email,
            metadata={"role": role.value},
        )
        return member

    async def update_member_role(
        self, user: User, namespace_id: uuid.UUID, member_id: uuid.UUID, role: NamespaceRole
    ) -> NamespaceMember:
        context = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.ADMIN
        )
        member = await self._members.get_by_id(namespace_id, member_id)
        if member is None:
            raise NotFound("Membership not found.")

        # Only an owner may create or demote another owner.
        if role is NamespaceRole.OWNER or member.role is NamespaceRole.OWNER:
            context.require(NamespaceRole.OWNER)

        if member.role is NamespaceRole.OWNER and role is not NamespaceRole.OWNER:
            await self._guard_last_owner(namespace_id)

        member.role = role
        await self._audit.record(
            action=AuditAction.UPDATE,
            event="namespace.member.update",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="namespace_member",
            resource_id=member.id,
            metadata={"role": role.value},
        )
        return member

    async def remove_member(
        self, user: User, namespace_id: uuid.UUID, member_id: uuid.UUID
    ) -> None:
        context = await self._authorizer.authorize(
            user, namespace_id, minimum_role=NamespaceRole.ADMIN
        )
        member = await self._members.get_by_id(namespace_id, member_id)
        if member is None:
            raise NotFound("Membership not found.")

        if member.role is NamespaceRole.OWNER:
            context.require(NamespaceRole.OWNER)
            await self._guard_last_owner(namespace_id)

        await self._members.remove(member)
        await self._audit.record(
            action=AuditAction.DELETE,
            event="namespace.member.remove",
            actor_id=user.id,
            actor_email=user.email,
            namespace_id=namespace_id,
            resource_type="namespace_member",
            resource_id=member_id,
        )

    async def _guard_last_owner(self, namespace_id: uuid.UUID) -> None:
        """A namespace with no owner cannot be administered or deleted."""
        if await self._members.count_owners(namespace_id) <= 1:
            raise Conflict(
                "This is the last owner of the namespace. Promote another member to owner first."
            )


__all__ = ["NamespaceService", "PermissionDenied"]

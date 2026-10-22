"""Advisory action-linking (docs/modules/change_management/spec.md): associate
a `ChangeRequest` with the patch/deploy/provision action that implements it.

Linking is polymorphic (a `ChangeLink` row names an `action_type` and an
`action_id` rather than carrying a foreign key per possible target) and is
purely advisory: it records that a change and an action are related, and
never reads back into or mutates the linked action row. Nothing in this
module writes to `PatchRun`, `Deployment`, `ProvisionRun`, or `PatchApproval`
-- the only queries against those tables are read-only existence checks.

The load-bearing security property is the org-scoped resolution guard in
`add_link`: the target existence check is always
`WHERE model.org_id == org_id AND model.id == action_id`. An `action_id`
that belongs to another org is therefore indistinguishable from one that
doesn't exist at all -- both resolve to `None` and raise
`ChangeValidationError`. This is invariant #5 (tenant scoping): a change
must never be able to link an action outside its own org, even if the
caller can guess or enumerate another org's ids.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import ChangeLink, ChangeRequest, Deployment, PatchApproval, PatchRun, ProvisionRun
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError

# action_type -> the model to existence-check the target against. Every
# entry here must be an org-scoped, UUID-`id`-keyed model.
_LINK_RESOLVERS: dict[str, type] = {
    "patch_run": PatchRun,
    "deployment": Deployment,
    "provision_run": ProvisionRun,
    "patch_approval": PatchApproval,
}


async def _verify_change_exists(session: AsyncSession, org_id: UUID, change_id: UUID) -> None:
    exists = (
        await session.execute(
            select(ChangeRequest.id).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise ChangeNotFoundError(tr("change not found"))


async def add_link(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    change_id: UUID,
    action_type: str,
    action_id: UUID,
) -> ChangeLink:
    """Advisory-link `change_id` to `(action_type, action_id)`.

    Never mutates the linked action -- only existence-checks it, org-scoped,
    before recording the link. Raises `ChangeNotFoundError` (a
    `ChangeValidationError` subclass, maps to HTTP 404) if the change itself
    doesn't exist under `org_id`. Raises plain `ChangeValidationError` (HTTP
    400) for: an unknown `action_type`, the target not resolving under
    `org_id` (nonexistent OR belonging to another org -- the two are
    indistinguishable by design, and this is a bad *request body* field, not
    a missing path resource), or a duplicate link.
    """
    await _verify_change_exists(session, org_id, change_id)

    model = _LINK_RESOLVERS.get(action_type)
    if model is None:
        raise ChangeValidationError(
            tr("unknown action_type {action_type}", action_type=repr(action_type))
        )

    # Org-scoped resolution: an action_id from another org is invisible here,
    # so it raises exactly like a nonexistent one (invariant #5).
    target_exists = (
        await session.execute(
            select(model.id).where(model.org_id == org_id, model.id == action_id)  # type: ignore[attr-defined]
        )
    ).scalar_one_or_none()
    if target_exists is None:
        raise ChangeValidationError(tr("linked action not found"))

    # Pre-check for a duplicate rather than relying on the unique constraint,
    # so a bad insert never poisons the transaction. This is the ordinary
    # sequential-duplicate fast path and its error is unchanged.
    duplicate = (
        await session.execute(
            select(ChangeLink.id).where(
                ChangeLink.org_id == org_id,
                ChangeLink.change_id == change_id,
                ChangeLink.action_type == action_type,
                ChangeLink.action_id == action_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ChangeValidationError(tr("action already linked to this change"))

    # The actual insert still goes through `uq_change_link`
    # (org_id, change_id, action_type, action_id): two concurrent add_link
    # calls for the identical link can both pass the pre-check above (miss)
    # before either has written. `ON CONFLICT DO NOTHING` + explicit conflict
    # target swallows only that constraint (an FK/check violation still
    # raises); a swallowed insert means we lost the race, and the loser gets
    # the SAME typed error the pre-check would have raised -- never someone
    # else's row -- since this is a single-caller CREATE, not a
    # get-or-create.
    link_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(ChangeLink)
            .values(
                id=link_id,
                org_id=org_id,
                change_id=change_id,
                action_type=action_type,
                action_id=action_id,
                linked_by=actor_id,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "change_id", "action_type", "action_id"]
            )
            .returning(ChangeLink.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise ChangeValidationError(tr("action already linked to this change"))

    link = (
        await session.execute(select(ChangeLink).where(ChangeLink.id == inserted_id))
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.linked",
        target_type="change",
        target_id=str(change_id),
        metadata={"action_type": action_type, "action_id": str(action_id)},
    )
    await session.flush()

    return link


async def remove_link(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    change_id: UUID,
    link_id: UUID,
) -> None:
    """Remove an advisory link, org-scoped. Raises `ChangeNotFoundError` (a
    `ChangeValidationError` subclass, maps to HTTP 404) if the link doesn't
    exist under `org_id`/`change_id` (nonexistent or cross-org)."""
    link = (
        await session.execute(
            select(ChangeLink).where(
                ChangeLink.org_id == org_id,
                ChangeLink.change_id == change_id,
                ChangeLink.id == link_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise ChangeNotFoundError(tr("link not found"))

    await session.delete(link)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.unlinked",
        target_type="change",
        target_id=str(change_id),
        metadata={"link_id": str(link_id)},
    )
    await session.flush()


async def list_links(session: AsyncSession, org_id: UUID, change_id: UUID) -> list[ChangeLink]:
    return list(
        (
            await session.execute(
                select(ChangeLink).where(
                    ChangeLink.org_id == org_id, ChangeLink.change_id == change_id
                )
            )
        )
        .scalars()
        .all()
    )

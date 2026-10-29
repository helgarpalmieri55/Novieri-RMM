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

THE SAME HOLDS FOR CLIENTS (MSP clients plan 2026-10-26 Task 7, research-5
§E6). A change is one client's, so what it links is that client's too: the
resolver reads the target's client and an action of another client answers
exactly like a missing one ("linked action not found"), word for word -- a
narrowed session cannot see it at all, and a wider one is told nothing
more. A target with no client is every client's: a provisioning run with no
device yet links to any client's change. A GLOBAL PATCH APPROVAL is the
exception (decision C8, plan 2026-10-26 Task 14 review): Novieri decides
it, so only the INTERNAL client's change -- whose id is the organisation's
-- authorizes it (`gates.has_authorizing_change`). Linked to a customer's
change it would authorize nothing and the gate would withhold it in
silence, so that link is refused like a missing one too. Linking and
unlinking are writes on the change, so they need change management in the
change's client (`load_change_for_write`).
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import ChangeLink, Deployment, PatchApproval, PatchRun, ProvisionRun
from app.modules.change_mgmt.service import load_change_for_write
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError

#: A target, as its resolver finds it: absent (`None`), or present with its
#: client -- `(None,)` for a row with no client, which is every client's.
Found = tuple[UUID | None] | None

Resolver = Callable[[AsyncSession, UUID, UUID], Awaitable[Found]]


def _client_column(model: type) -> Resolver:
    """A target whose table carries its client: one org-scoped read of it."""

    async def resolve(session: AsyncSession, org_id: UUID, action_id: UUID) -> Found:
        row = (
            await session.execute(
                select(model.client_id).where(  # type: ignore[attr-defined]
                    model.org_id == org_id,  # type: ignore[attr-defined]
                    model.id == action_id,  # type: ignore[attr-defined]
                )
            )
        ).one_or_none()
        return None if row is None else (row[0],)

    return resolve


# action_type -> how to find the target and its client. Every entry here must
# resolve org-scoped (`org_id ==` on every table it reads).
_LINK_RESOLVERS: dict[str, Resolver] = {
    "patch_run": _client_column(PatchRun),
    # Its client is its target's, written by trigger (migration 0239).
    "deployment": _client_column(Deployment),
    "provision_run": _client_column(ProvisionRun),
    "patch_approval": _client_column(PatchApproval),
}


def _may_authorize(
    action_type: str, target_client: UUID | None, *, change_client: UUID, org_id: UUID
) -> bool:
    """Whether a change of `change_client` may be linked to a target of
    `target_client`: the same client, or a target with no client -- except a
    global patch approval, which only the internal client's change (id = the
    organisation's) authorizes (C8; module docstring)."""
    if target_client is None:
        return action_type != "patch_approval" or change_client == org_id
    return target_client == change_client


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
    change = await load_change_for_write(session, org_id, change_id)

    resolve = _LINK_RESOLVERS.get(action_type)
    if resolve is None:
        raise ChangeValidationError(
            tr("unknown action_type {action_type}", action_type=repr(action_type))
        )

    # Org-scoped resolution: an action_id from another org is invisible here,
    # so it raises exactly like a nonexistent one (invariant #5). One of
    # another CLIENT raises exactly the same, and so does a global patch
    # approval on any change but the internal client's (module docstring).
    found = await resolve(session, org_id, action_id)
    if found is None or not _may_authorize(
        action_type, found[0], change_client=change.client_id, org_id=org_id
    ):
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
        client_id=change.client_id,
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
    exist under `org_id`/`change_id` (nonexistent or cross-org). The change
    is loaded first: its client needs change management."""
    change = await load_change_for_write(session, org_id, change_id)
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
        client_id=change.client_id,
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

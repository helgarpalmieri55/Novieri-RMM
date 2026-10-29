"""Patch approval service (docs/modules/patch/spec.md).

Approval is the security gate for patch application: a patch can only be
applied to a device once it has been approved for that device's ring. This
module creates/revokes `patch_approvals` rows and flips the affected devices'
`device_patch_status` rows accordingly. Every approve/revoke is written to the
audit log.

PER CLIENT, OR GLOBAL (MSP clients plan 2026-10-26 Task 14). An approval is
one client's -- it clears the patch for THAT client's machines in the ring --
or global, for every client's. The owner is decided by the caller (the route,
with `client_for_new_rows`); a global approval is written only under the
every-client scope. Approving or revoking flips the ring's machines of the
approval's owner only, and a revoke leaves alone a machine another live
approval still covers. Every event is filed under the approval's client.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.global_rows import refuse_unless_every_client
from app.models import DevicePatchStatus, Patch, PatchApproval
from app.modules.patch.policy import resolve_ring_populations

#: The largest selection `approve_patches` will act on in one call. It is the
#: ceiling `GET /patch/patches` already puts on a page, so a console can approve
#: everything it is able to show and no more -- a batch nobody could have read
#: before clicking is not a decision.
BULK_APPROVAL_MAX = 500


class UnknownPatchError(Exception):
    """One or more of the patch ids named are not patches in this organisation.

    NOT MERELY A 404 FOR TIDINESS. `patch_approvals.patch_id` is a foreign key
    into `patches`, and PostgreSQL runs referential-integrity checks with row
    security BYPASSED -- so an insert naming a neighbour tenant's patch id
    SUCCEEDS, writing this org an approval against an update it has no record
    of. Nothing crosses outward, but the gate that decides which software may
    reach a machine ends up holding a clearance nobody in this tenant can read,
    and in a four-hundred-id batch nobody would ever spot the one that did it.
    """

    def __init__(self, patch_ids: Sequence[UUID]) -> None:
        self.patch_ids = list(patch_ids)
        super().__init__(  # i18n-exempt: api.py words the 404 itself, this text is never shown
            f"{len(self.patch_ids)} patch id(s) are not patches in this organisation"
        )


@dataclass(frozen=True)
class ApprovalOutcome:
    """What one `(patch, ring)` decision actually did.

    `created` is False for a re-approval of a clearance already in force: that
    call changes nothing and deliberately writes NO audit event, so a caller
    reporting "3 approved" after a batch of which two were already cleared would
    be describing writes that never happened. `devices_flipped` counts the
    `device_patch_status` rows this call moved from `missing` to `approved`.
    """

    approval: PatchApproval
    created: bool
    devices_flipped: int


async def _assert_patches_in_org(
    session: AsyncSession, org_id: UUID, patch_ids: Sequence[UUID]
) -> None:
    if not patch_ids:
        return
    found = set(
        (
            await session.execute(
                select(Patch.id).where(Patch.org_id == org_id, Patch.id.in_(patch_ids))
            )
        )
        .scalars()
        .all()
    )
    missing = [patch_id for patch_id in patch_ids if patch_id not in found]
    if missing:
        raise UnknownPatchError(missing)


async def _devices_in_ring(
    session: AsyncSession, org_id: UUID, ring_name: str, *, client_id: UUID | None = None
) -> list[UUID]:
    """The devices one ring covers -- of `client_id` only, when given: a
    client's approval reaches that client's machines in the ring and no
    other's (MSP clients plan 2026-10-26 Task 14).

    RESOLVED FOR THE WHOLE FLEET IN ONE STATEMENT (`resolve_ring_populations`),
    not per device. The previous form ran `resolve_patch_policy` once per device
    and this function was called once per patch, so a batch approval multiplied
    the two; `tests/test_patch_policy.py` pins the fast form against the slow one
    device by device, because an optimisation that quietly re-rings a machine
    approves software onto a machine nobody cleared it for.
    """
    for population in await resolve_ring_populations(session, org_id, client_id=client_id):
        if population.ring_name == ring_name:
            return list(population.device_ids)
    return []


devices_in_ring = _devices_in_ring


async def _approve_one(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    patch_id: UUID,
    ring_name: str,
    device_ids: list[UUID],
    client_id: UUID | None,
) -> ApprovalOutcome:
    """One `(patch, ring)` clearance, against a ring population already resolved.

    `device_ids` is passed IN rather than resolved here: a batch resolves the
    ring once and reuses it, and the answer cannot change part-way through a
    single transaction anyway. `approve_patch` below is this function with a
    population of one ring's worth resolved for it -- of `client_id`'s
    machines, for a client's approval.

    `client_id` is the approval's OWNER: a client, or None for the global
    row. The row this is, or races, is that owner's:
    `uq_patch_approvals_client_patch_ring` is per client, NULLS NOT DISTINCT
    (migration 0231, Task 8b), so X's approval and the global one of the same
    (patch, ring) are two rows, and each is looked up by its own owner.
    """
    owner = (
        PatchApproval.client_id.is_(None)
        if client_id is None
        else PatchApproval.client_id == client_id
    )
    existing = (
        await session.execute(
            select(PatchApproval).where(
                PatchApproval.org_id == org_id,
                owner,
                PatchApproval.patch_id == patch_id,
                PatchApproval.ring_name == ring_name,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and not existing.revoked:
        # ALREADY IN FORCE: nothing changes, and NO AUDIT EVENT IS WRITTEN. The
        # log records what an operator changed; an event for a clearance that
        # was already open would put a second "who approved this" answer in the
        # trail for a decision made once, by somebody else, at another time.
        return ApprovalOutcome(approval=existing, created=False, devices_flipped=0)
    elif existing is not None and existing.revoked:
        # Re-approval of a previously revoked (patch, ring): un-revoke the
        # existing row in place rather than inserting a new one, since
        # (org_id, patch_id, ring_name) is unique.
        existing.approved_by = actor_id
        existing.approved_at = datetime.now(UTC)
        existing.revoked = False
        existing.revoked_by = None
        existing.revoked_at = None
        session.add(existing)
        approval = existing
    else:
        # First-ever approval for (org_id, patch_id, ring_name). The actual
        # insert still goes through `uq_patch_approvals_client_patch_ring`: two
        # concurrent first-time approve calls for the identical triple can
        # both pass the pre-check above (miss) before either has written.
        # `ON CONFLICT DO NOTHING` + explicit conflict target swallows only
        # that constraint (an FK/check violation still raises). A race loser
        # must not flip devices or emit a second audit event for a row it
        # did not write, so on a lost race it re-selects the winner's row
        # and returns it immediately -- the identical no-op shape as the
        # "already approved and not revoked" branch above.
        #
        # A GLOBAL approval is a bulk INSERT `app.core.global_rows`'s flush
        # guard never sees (the re-approval above, an ORM update, it does): the
        # every-client refusal is asked here. A client's approval needs none.
        if client_id is None:
            refuse_unless_every_client(session, "patch_approvals")
        inserted_id = (
            await session.execute(
                pg_insert(PatchApproval)
                .values(
                    id=uuid7(),
                    org_id=org_id,
                    client_id=client_id,
                    patch_id=patch_id,
                    ring_name=ring_name,
                    approved_by=actor_id,
                    revoked=False,
                )
                .on_conflict_do_nothing(
                    index_elements=["org_id", "client_id", "patch_id", "ring_name"]
                )
                .returning(PatchApproval.id)
            )
        ).scalar_one_or_none()
        if inserted_id is None:
            winner = (
                await session.execute(
                    select(PatchApproval).where(
                        PatchApproval.org_id == org_id,
                        owner,
                        PatchApproval.patch_id == patch_id,
                        PatchApproval.ring_name == ring_name,
                    )
                )
            ).scalar_one()
            return ApprovalOutcome(approval=winner, created=False, devices_flipped=0)
        approval = (
            await session.execute(select(PatchApproval).where(PatchApproval.id == inserted_id))
        ).scalar_one()
    await session.flush()

    devices_flipped = 0
    if device_ids:
        statuses = (
            await session.execute(
                select(DevicePatchStatus).where(
                    DevicePatchStatus.org_id == org_id,
                    DevicePatchStatus.patch_id == patch_id,
                    DevicePatchStatus.device_id.in_(device_ids),
                    DevicePatchStatus.state == "missing",
                )
            )
        ).scalars().all()
        for status in statuses:
            status.state = "approved"
            session.add(status)
            devices_flipped += 1

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="patch.approved",
        target_type="patch",
        target_id=str(patch_id),
        metadata={"ring_name": ring_name, "devices_flipped": devices_flipped},
        client_id=client_id,
    )

    await session.flush()
    return ApprovalOutcome(
        approval=approval, created=True, devices_flipped=devices_flipped
    )


async def approve_patch(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    patch_id: UUID,
    ring_name: str,
    client_id: UUID | None = None,
) -> PatchApproval:
    """Clear one patch for one ring -- for `client_id`'s machines, or with
    None (the default, phase 1's only kind) for every client's. Raises
    `UnknownPatchError` if the patch is not this org's -- see that exception
    for why an FK is not the check."""
    await _assert_patches_in_org(session, org_id, [patch_id])
    outcome = await _approve_one(
        session,
        org_id=org_id,
        actor_id=actor_id,
        patch_id=patch_id,
        ring_name=ring_name,
        device_ids=await _devices_in_ring(session, org_id, ring_name, client_id=client_id),
        client_id=client_id,
    )
    return outcome.approval


async def approve_patches(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    patch_ids: Sequence[UUID],
    ring_name: str,
    client_id: UUID | None = None,
) -> list[ApprovalOutcome]:
    """Clear a SELECTION of patches for one ring, in the order they were named
    -- for `client_id`'s machines, or with None for every client's (the
    owner is the caller's to check: the route loads it with
    `client_for_new_rows`).

    THE OPERATOR'S REPORT, 2026-08-24: "it is super hard to approve all updates
    one by one". Approval has always been fleet-wide -- `PatchApproval` is
    `(org_id, patch_id, ring_name)` and carries no device column, so a single row
    clears a patch for every machine in the ring. What was per-row was the
    CONSOLE, which spent one POST and one confirmation dialog on each catalogue
    line. This is the shape that lets the decision be made once.

    IT IS A LOOP, AND THAT IS THE POINT RATHER THAN A SHORTCUT NOT TAKEN.
    Invariant #4 wants the audit log to answer "who cleared KB5031354, and
    when" -- one event naming forty patches answers a different, weaker
    question. So each clearance goes through `_approve_one` and writes its own
    `patch.approved` event, in the caller's transaction, so the rows and the
    hash-chained appends commit together or not at all.

    WHAT IS HOISTED IS THE RING, NOT THE AUDIT. `_devices_in_ring` is resolved
    once for the batch instead of once per patch; that is arithmetic the answer
    does not depend on, unlike the events.

    ALL-OR-NOTHING ON AN UNKNOWN ID. The org check runs over the whole selection
    BEFORE anything is written, so a batch carrying one id this org cannot see
    approves none of it -- a partly-applied batch is a set of clearances the
    operator never consciously granted, and they would have to diff the audit
    log against their own selection to find out which.
    """
    unique: list[UUID] = list(dict.fromkeys(patch_ids))
    await _assert_patches_in_org(session, org_id, unique)
    device_ids = await _devices_in_ring(session, org_id, ring_name, client_id=client_id)
    return [
        await _approve_one(
            session,
            org_id=org_id,
            actor_id=actor_id,
            patch_id=patch_id,
            ring_name=ring_name,
            device_ids=device_ids,
            client_id=client_id,
        )
        for patch_id in unique
    ]


@dataclass(frozen=True)
class ApprovalListing:
    """One approval, and the update it clears NAMED.

    WHY THE NAME TRAVELS WITH THE ROW. `patch_approvals` is `(org_id, patch_id,
    ring_name)`; the identifier an operator recognises -- `KB5031354`,
    `linux-image-6.8` -- lives in `patches`. Without this field a console had
    only the uuid, and the one this repo ships solved that by fetching a
    500-row page of `GET /patch/patches` on every visit to build an id ->
    identifier map. That is five hundred rows to render a handful, and it is
    silently WRONG past the cap: an approval whose patch fell outside the page
    rendered as a raw uuid, with nothing on screen saying so.

    `patch_identifier` IS NULLABLE, AND THE NULL MEANS SOMETHING. It is `None`
    when this org has no `patches` row for `approval.patch_id`. That is not a
    formatting gap -- it is the anomaly `UnknownPatchError` exists to prevent
    (an FK check runs with row security BYPASSED, so a row naming a neighbour's
    patch can exist even though no route will write one) surfacing rather than
    being papered over. The join is a LEFT OUTER one for exactly this reason:
    such a row must stay in the page, because `DELETE /approvals/{id}` is the
    only way to get rid of it and the id is the only handle on it.
    """

    approval: PatchApproval
    patch_identifier: str | None


async def patch_identifiers(
    session: AsyncSession, *, org_id: UUID, patch_ids: Sequence[UUID]
) -> dict[UUID, str]:
    """`patch_id -> identifier` for THIS ORG's patches, for the ids named.

    An id this org has no patch for is simply absent from the map rather than
    raising: the callers are the approve routes, which have already run
    `_assert_patches_in_org` over the same ids, so a miss here can only be the
    cross-tenant anomaly above and the honest answer for it is "unnamed".
    """
    if not patch_ids:
        return {}
    rows = await session.execute(
        select(Patch.id, Patch.identifier).where(
            Patch.org_id == org_id, Patch.id.in_(list(patch_ids))
        )
    )
    return {patch_id: identifier for patch_id, identifier in rows}


async def list_approvals(
    session: AsyncSession,
    *,
    org_id: UUID,
    patch_id: UUID | None = None,
    ring_name: str | None = None,
    revoked: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ApprovalListing], int]:
    """This org's approvals, newest first, with the total before paging.

    Exists so `revoke_approval` is reachable at all. An approval id was only
    ever returned by the POST that created it, so an approval made in an
    earlier session could not be revoked over HTTP — the gate that decides
    which patches may reach a machine was one-way (audit
    `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 4).

    A revoked approval is INCLUDED by default. Approvals are soft-revoked
    precisely so the trail holds (spec §5); hiding them by default would make
    the list disagree with the audit log about what was ever approved.
    `revoked=False` is how a caller asks for the ones still in force.

    Newest first, and `PatchApproval.id` is a uuid7, so ordering on it is
    ordering by creation time without trusting `approved_at` — which moves
    when a revoked (patch, ring) is re-approved in place.
    """
    filters = [PatchApproval.org_id == org_id]
    if patch_id is not None:
        filters.append(PatchApproval.patch_id == patch_id)
    if ring_name is not None:
        filters.append(PatchApproval.ring_name == ring_name)
    if revoked is not None:
        filters.append(PatchApproval.revoked.is_(revoked))

    # COUNTED OFF `patch_approvals` ALONE, deliberately. The join below is a
    # LEFT OUTER one onto a primary key, so it can neither add nor drop a row
    # -- but a `total` computed from the joined statement would start agreeing
    # with the join rather than with the table, and this number is what tells a
    # console a short page from the end of the list.
    total = (
        await session.execute(
            select(func.count()).select_from(PatchApproval).where(*filters)
        )
    ).scalar_one()
    # `Patch.org_id == org_id` IS IN THE ON CLAUSE AND IS NOT REDUNDANT. RLS
    # scopes `patches` under `org_session`, so deleting it turns no route test
    # red; `test_the_approval_name_join_cannot_name_a_neighbours_patch` runs
    # this on the BYPASSRLS role, where it is the only scoping there is. In the
    # ON clause rather than the WHERE because an outer join must keep the row
    # whose patch does not match -- moving it to WHERE silently drops it.
    rows = (
        await session.execute(
            select(PatchApproval, Patch.identifier)
            .outerjoin(
                Patch, and_(Patch.id == PatchApproval.patch_id, Patch.org_id == org_id)
            )
            .where(*filters)
            .order_by(PatchApproval.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        ApprovalListing(approval=approval, patch_identifier=identifier)
        for approval, identifier in rows
    ], total


async def revoke_approval(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    approval_id: UUID,
) -> bool:
    approval = (
        await session.execute(
            select(PatchApproval).where(
                PatchApproval.org_id == org_id, PatchApproval.id == approval_id
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        return False

    owner = approval.client_id
    now = datetime.now(UTC)
    approval.revoked = True
    approval.revoked_by = actor_id
    approval.revoked_at = now
    session.add(approval)

    # WHO ELSE STILL COVERS THIS (patch, ring): the owners of the other live
    # approvals. A global one covers every machine, so nothing goes back to
    # `missing`; a client's covers that client's. Read under the request's
    # RLS, like the ring itself.
    covering = set(
        (
            await session.execute(
                select(PatchApproval.client_id).where(
                    PatchApproval.org_id == org_id,
                    PatchApproval.patch_id == approval.patch_id,
                    PatchApproval.ring_name == approval.ring_name,
                    PatchApproval.revoked.is_(False),
                    PatchApproval.id != approval.id,
                )
            )
        ).scalars()
    )
    device_ids = (
        []
        if None in covering
        else await _devices_in_ring(session, org_id, approval.ring_name, client_id=owner)
    )
    if device_ids:
        statuses = (
            await session.execute(
                select(DevicePatchStatus).where(
                    DevicePatchStatus.org_id == org_id,
                    DevicePatchStatus.patch_id == approval.patch_id,
                    DevicePatchStatus.device_id.in_(device_ids),
                    DevicePatchStatus.client_id.not_in(covering),
                    DevicePatchStatus.state == "approved",
                )
            )
        ).scalars().all()
        for status in statuses:
            status.state = "missing"
            session.add(status)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="patch.revoked",
        target_type="patch",
        target_id=str(approval.patch_id),
        metadata={"ring_name": approval.ring_name},
        client_id=owner,
    )

    await session.flush()
    return True

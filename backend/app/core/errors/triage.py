"""Triage writes on an error group: resolve, and its inverse
(docs/superpowers/specs/2026-08-15-error-monitoring-design.md §5).

WHY A SERVICE AND NOT TWO ROUTE BODIES. Resolve and reopen are the same
transaction with the timestamp going in opposite directions, and each has to
carry an audit append that stands or falls with the row change. Two handlers
each doing that by hand is two places for one of them to lose the append --
which is exactly the state this module was written to correct: resolve had
been shipping without one.

THE UNDO IS NOT "SET THE FLAG BACK", BECAUSE `resolved_at` IS THE WHOLE
RECORD. `platform_error_group` carries no `resolved_by` and no resolution
note (spec §4); the timestamp is the only evidence that anybody ever resolved
the group. Clearing it therefore destroys history rather than merely changing
state, and an undo that does that silently is a worse defect than the missing
undo it fixes.

So both directions are audited, and the reopen event carries the exact
instant it blanked in `previous_resolved_at`. What the column gave up, the
hash-chained log keeps -- including the actor on each side, which the column
never held in the first place. The pair is deliberate: a reopen event with no
resolve event to answer would assert that a resolution was undone that the
log never shows happening.

THE AUDIT ROW IS TENANT-SCOPED; THE GROUP IS NOT. The group is platform
information (spec §4.1 -- `org_id` on the event table is a nullable
correlation column, not a tenancy key) and authorisation is by the
platform-wide `platform.errors.write` code, so any holder may reopen any
group. `audit_events` is ordinary tenant data, so the record of WHO did it
belongs to the acting operator's org and lands in that org's chain. Callers
must therefore hand this an `org_session` pinned to the actor's org: the
audit insert's RLS check needs the GUC, and `platform_error_group`'s own
policy (`FOR ALL TO app_user USING (true)`, migration 0056) is indifferent to
it, so pinning it introduces no row filter on the group.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import PlatformErrorGroup

# Action names mirror the route verbs (`/resolve`, `/unresolve`) so that a
# line in the audit log grep-matches the code path that wrote it.
RESOLVED_ACTION = "platform_error.resolved"
UNRESOLVED_ACTION = "platform_error.unresolved"
TARGET_TYPE = "platform_error_group"


class ErrorGroupNotFoundError(LookupError):
    """No `platform_error_group` row carries that id."""


def _iso(moment: datetime | None) -> str | None:
    """Audit metadata is JSON, so the instant travels as a string. Full
    precision, unlike `jobs.canonical.rfc3339`'s second-resolution form: this
    value exists to identify the exact resolution that was undone, and two
    resolutions of the same group within one second are ordinary."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).isoformat()
    return moment.isoformat()


async def _load(session: AsyncSession, group_id: UUID) -> PlatformErrorGroup:
    group = await session.get(PlatformErrorGroup, group_id)
    if group is None:
        raise ErrorGroupNotFoundError(str(group_id))
    return group


async def resolve_group(
    session: AsyncSession,
    *,
    group_id: UUID,
    actor_org_id: UUID,
    actor_user_id: UUID,
) -> PlatformErrorGroup:
    """Mark a group resolved (spec §5), and record who did.

    Idempotent: resolving an already-resolved group moves the timestamp
    forward rather than erroring, matching `app/modules/health/api.py`'s
    alert-lifecycle transitions. The audit event carries the timestamp it
    replaced, so a second resolve is legible as one.
    """
    group = await _load(session, group_id)
    previous = group.resolved_at
    group.resolved_at = datetime.now(UTC)
    session.add(group)
    await audit_service.record(
        session,
        org_id=actor_org_id,
        actor_type="user",
        actor_id=actor_user_id,
        action=RESOLVED_ACTION,
        target_type=TARGET_TYPE,
        target_id=str(group_id),
        # Fingerprint, not exception text -- the group row has no text to
        # leak (spec §0.1 falsifier #1) and this log cannot be redacted
        # afterwards, so it may only hold what the API already returns.
        metadata={
            "fingerprint": group.fingerprint,
            "resolved_at": _iso(group.resolved_at),
            "previous_resolved_at": _iso(previous),
        },
    )
    await session.flush()
    return group


async def unresolve_group(
    session: AsyncSession,
    *,
    group_id: UUID,
    actor_org_id: UUID,
    actor_user_id: UUID,
) -> PlatformErrorGroup:
    """Reopen a group -- clear `resolved_at` -- and record the instant that
    was cleared (B76: resolve was one-way).

    Idempotent in the same sense resolve is: reopening an already-open group
    answers 200 and changes nothing. It is still audited, with
    `previous_resolved_at` null. The log records what an operator DID, and a
    reopen that found nothing to reopen is a fact about the operator, not a
    state delta to be optimised away -- an audit trail with holes where the
    no-ops were is one an investigator cannot reason from.
    """
    group = await _load(session, group_id)
    previous = group.resolved_at
    group.resolved_at = None
    session.add(group)
    await audit_service.record(
        session,
        org_id=actor_org_id,
        actor_type="user",
        actor_id=actor_user_id,
        action=UNRESOLVED_ACTION,
        target_type=TARGET_TYPE,
        target_id=str(group_id),
        metadata={
            "fingerprint": group.fingerprint,
            # THE POINT OF THIS EVENT. The column no longer holds it, so if
            # this line is ever dropped the resolution becomes unrecoverable.
            "previous_resolved_at": _iso(previous),
        },
    )
    await session.flush()
    return group

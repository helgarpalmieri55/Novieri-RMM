"""Change-management reporting (docs/modules/change_management/spec.md §4).

Pure read-only aggregation over the change-management module's tables. This
module performs **no writes**, dispatches **no jobs**, and records **no
audit events** -- it exists solely to answer "what happened on this one
change, end to end" (SOC 2 evidence export), "what does that look like across
every change in the org" (fleet evidence export), "what changes are
scheduled in this window, and do any of them collide" (the change calendar),
and "what is the approval-decision history for this one change" (the plain
`list_change_approvals` read used to render a change's timeline on view --
distinct from evidence export precisely because it never audits).

`change.evidence_exported` is an audit event that belongs to the *caller* of
this module, not to it: the API route that serves an evidence export (Task
10) is responsible for recording it. Nothing in this file writes to
`audit_events` -- it only ever reads from it, org-scoped, via `ix_audit_target`.

Every query below filters on `org_id` for every table involved
(`ChangeRequest`, `ChangeApproval`, `ChangeLink`, `AuditEvent`) per the
tenancy invariant (#5) -- RLS is the backstop, not the only line of defense.
In particular, `audit_refs` is scoped by `org_id` in addition to
`target_type`/`target_id`, so another org's audit events -- even ones that
happen to reference the same `target_id` string, which cannot actually
happen since ids are unique, but the filter is explicit regardless -- can
never leak into an evidence export.
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import AuditEvent, ChangeApproval, ChangeLink, ChangeRequest
from app.modules.change_mgmt.state import ChangeNotFoundError


def _empty_evidence() -> dict[str, Any]:
    """The "no such change in this org" shape -- mirrors
    `provisioning.reporting.provision_run_status`'s "no history" convention:
    the requested change simply isn't there, so everything else is empty."""
    return {"change": None, "approvals": [], "links": [], "audit_refs": []}


def _approval_dict(row: ChangeApproval) -> dict[str, Any]:
    """The approval-dict shape shared by `change_evidence` and
    `list_change_approvals`."""
    return {
        "id": row.id,
        "approver_id": row.approver_id,
        "decision": row.decision,
        "comment": row.comment,
        "is_retrospective": row.is_retrospective,
        "decided_at": row.decided_at,
    }


async def _fetch_approvals(
    session: AsyncSession, org_id: UUID, change_id: UUID
) -> list[ChangeApproval]:
    """Every `ChangeApproval` row for `change_id`, org-scoped, ordered by
    `decided_at` ascending. No existence check on `change_id` itself --
    callers that need a 404 for an unknown change check that separately."""
    return list(
        (
            await session.execute(
                select(ChangeApproval)
                .where(ChangeApproval.org_id == org_id, ChangeApproval.change_id == change_id)
                .order_by(ChangeApproval.decided_at)
            )
        )
        .scalars()
        .all()
    )


async def list_change_approvals(
    session: AsyncSession, org_id: UUID, change_id: UUID
) -> list[dict[str, Any]]:
    """The approval-decision history for one change, org-scoped -- the plain
    read used to render a change's approval timeline for display.

    Unlike `change_evidence`, this is not an evidence export: it writes NO
    audit event (viewing a change must never be indistinguishable from
    exporting its evidence, invariant #4's whole point here is that only the
    explicit `/evidence` export route records `change.evidence_exported`).
    It does still 404 for an unknown change -- via `ChangeNotFoundError`,
    mirroring how `change_evidence` treats a missing change, except this
    helper raises rather than returning an empty-evidence shape, so the API
    layer maps it to a 404 the same way it does for `_verify_change_exists`
    in `links.py`.
    """
    found_id = (
        await session.execute(
            select(ChangeRequest.id).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()
    if found_id is None:
        raise ChangeNotFoundError(tr("change {change_id} not found", change_id=change_id))

    approvals = await _fetch_approvals(session, org_id, change_id)
    return [_approval_dict(a) for a in approvals]


async def change_evidence(session: AsyncSession, org_id: UUID, change_id: UUID) -> dict[str, Any]:
    """One change's full SOC 2 evidence bundle, org-scoped.

    Returns `change` (the change's core fields, or `None` if no such change
    exists in this org -- see `_empty_evidence`), `approvals` (every
    `ChangeApproval` for this change, ordered by `decided_at` ascending),
    `links` (every `ChangeLink` for this change), and `audit_refs` (the ids
    of every `audit_events` row recorded against this change, i.e.
    `target_type == "change"` and `target_id == str(change_id)`, scoped to
    `org_id`).
    """
    change = (
        await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()

    if change is None:
        return _empty_evidence()

    approvals = await _fetch_approvals(session, org_id, change_id)

    links = (
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

    audit_refs = (
        (
            await session.execute(
                select(AuditEvent.id).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "change",
                    AuditEvent.target_id == str(change_id),
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        "change": {
            "id": change.id,
            "type": change.type,
            "title": change.title,
            "risk": change.risk,
            "window_start": change.window_start,
            "window_end": change.window_end,
            "state": change.state,
            "created_by": change.created_by,
            "created_at": change.created_at,
        },
        "approvals": [_approval_dict(a) for a in approvals],
        "links": [
            {
                "id": link.id,
                "action_type": link.action_type,
                "action_id": link.action_id,
                "linked_at": link.linked_at,
                "linked_by": link.linked_by,
            }
            for link in links
        ],
        "audit_refs": list(audit_refs),
    }


async def fleet_change_evidence(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """One `change_evidence`-shaped record per change in the org."""
    change_ids = (
        (await session.execute(select(ChangeRequest.id).where(ChangeRequest.org_id == org_id)))
        .scalars()
        .all()
    )
    return [await change_evidence(session, org_id, change_id) for change_id in change_ids]


def _affected_set(affected: list[Any]) -> set[tuple[str, str]]:
    """Normalizes a change's `affected` JSONB list (`[{"kind", "id"}, ...]`)
    into a set of `(kind, id)` pairs for set-intersection comparison."""
    return {(item["kind"], item["id"]) for item in affected}


def _find_conflicts(
    entries: list[tuple[UUID, datetime, datetime, set[tuple[str, str]]]],
) -> list[tuple[UUID, UUID]]:
    """Pure helper: given `(change_id, window_start, window_end, affected)`
    tuples, returns every pair of change ids whose time windows overlap
    (`a.window_start < b.window_end AND b.window_start < a.window_end`) AND
    whose `affected` sets intersect. Each conflicting pair is emitted exactly
    once (in input order, no duplicates, no self-pairs) -- no DB, no I/O.
    """
    conflicts: list[tuple[UUID, UUID]] = []
    n = len(entries)
    for i in range(n):
        id_a, start_a, end_a, affected_a = entries[i]
        for j in range(i + 1, n):
            id_b, start_b, end_b, affected_b = entries[j]
            if start_a < end_b and start_b < end_a and (affected_a & affected_b):
                conflicts.append((id_a, id_b))
    return conflicts


async def change_calendar(
    session: AsyncSession, org_id: UUID, *, frm: datetime, to: datetime
) -> dict[str, Any]:
    """Changes whose `[window_start, window_end]` intersects `[frm, to]`
    (both bounds must be set -- a change with no scheduled window never
    appears on the calendar), plus the set of conflicting pairs among them.

    Returns `entries` (one dict per change: `change_id`, `title`, `type`,
    `risk`, `state`, `window_start`, `window_end`) and `conflicts` (a list of
    `{"change_a", "change_b"}` id pairs, computed by the pure
    `_find_conflicts` helper over the fetched entries).
    """
    changes = (
        (
            await session.execute(
                select(ChangeRequest)
                .where(
                    ChangeRequest.org_id == org_id,
                    ChangeRequest.window_start.is_not(None),
                    ChangeRequest.window_end.is_not(None),
                    ChangeRequest.window_start <= to,
                    ChangeRequest.window_end >= frm,
                )
                .order_by(ChangeRequest.window_start, ChangeRequest.id)
            )
        )
        .scalars()
        .all()
    )

    entries = [
        {
            "change_id": change.id,
            "title": change.title,
            "type": change.type,
            "risk": change.risk,
            "state": change.state,
            "window_start": change.window_start,
            "window_end": change.window_end,
        }
        for change in changes
    ]

    # The query above requires both bounds to be non-null, so this cast just
    # tells mypy what the WHERE clause already guarantees at runtime.
    conflict_entries = [
        (
            change.id,
            cast(datetime, change.window_start),
            cast(datetime, change.window_end),
            _affected_set(change.affected),
        )
        for change in changes
    ]
    conflicts = [{"change_a": a, "change_b": b} for a, b in _find_conflicts(conflict_entries)]

    return {"entries": entries, "conflicts": conflicts}

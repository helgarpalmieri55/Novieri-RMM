"""Routing rules, the webhook egress allowlist, and queue membership
(docs/modules/notification/spec.md §2.3, §2.4, §2.6, §6, §8).

Mirrors `channels.py`'s shape: an explicit `org_id` predicate on every query
(invariant #5), audit-then-flush on every mutation (invariant #4), and typed
errors from `errors.py` rather than raw `IntegrityError`s reaching a caller.

CROSS-ORG ID VALIDATION (invariant #5, the `default_queue_id`/
`approver_role_id` bug class). Every id a caller hands in that references
another table -- `channel_id`, `queue_id`, `user_id` -- is re-verified against
THIS org with an explicit `org_id` predicate before it is written onto a row
here. The FK declared on the column is not a substitute: Postgres FK checks
BYPASS row-level security, so a queue/user id belonging to a different org
satisfies the FK just as well as one in the caller's own org. A cross-org id
and a nonexistent id are deliberately indistinguishable -- both raise
`NotificationNotFoundError` -- so a caller can never use a real foreign id as
an existence oracle for another tenant.

MODULE BOUNDARY (spec §1). This module "imports nothing from
`app.modules.service_desk`" -- `queue_id` resolution needs `TicketQueue`
(owned by that module) purely for its bare id/org_id columns, so, exactly
like `channels.py` already does for `Credential` and even for
`NotificationChannel` itself, it is imported through the shared `app.models`
registry (`from app.models import TicketQueue`) rather than reaching directly
into `app.modules.service_desk.models`. No ticket-domain behaviour is used --
only the same existence check every other cross-ref resolver in this
codebase performs (`service_desk.refdata.resolve_queue`/`resolve_role`).

RACE SAFETY (invariant #2). Two of this module's three unique constraints are
guarded here:

- `uq_allowlist_org_host_port` (`add_allowlist_entry`): a CREATE, same shape
  as `channels.create_channel` -- a pre-check SELECT is a fast path only; the
  actual write goes through `ON CONFLICT DO NOTHING ... RETURNING id`, and a
  `None` result raises the identical typed `NotificationValidationError` the
  pre-check raises. A race loser never receives the winner's row.
- `uq_queue_member` (`add_queue_member`): the one deliberate exception to
  "CREATE, not get-or-create" (see the docstring on `add_queue_member` below
  for the reasoning) -- idempotent by design, so its `ON CONFLICT DO NOTHING`
  is followed by a SELECT of whatever row now exists (ours or the other
  caller's), never an error.

`notification_rules` carries neither a unique constraint nor any other
plausible source of a write-time race in `create_rule` beyond the staff-only
CHECK constraints, which are answered by an unconditional, non-racy
service-layer guard instead (see `_reject_staff_only_to_requester`).

THE STAFF-ONLY RULE (spec §6.4, invariant on `create_rule`). Some event types
must never reach the ticket's requester at all: `ticket.internal_note_added`
(staff-only discussion about the customer) and `ticket.sla_breached` (the
organisation's own missed deadline, rendered staff-facing). The set is
declared once, in `app.core.events.STAFF_ONLY_EVENT_TYPES`, and enforced in
three layers here, not two: `RuleCreate`'s own Pydantic validator (schema.py),
the DB's `ck_rule_no_internal_to_requester` /
`ck_rule_no_sla_breach_to_requester` CHECK constraints, and this module's own
`_reject_staff_only_to_requester`, run unconditionally inside `create_rule`
regardless of what the caller's `RuleCreate` instance did or didn't validate
(a `model_construct`-built instance skips Pydantic validators entirely).
This is not a check-then-act race the way the unique
constraints above are: the pairing being rejected depends only on the
SUPPLIED data, never on concurrently-changing database state, so running the
check unconditionally before the insert makes the CHECK constraint
unreachable from this code path -- the DB constraints exist purely as the
last-resort backstop spec §6.4 asks for against a direct-SQL write, not
because this function could otherwise hit it.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.events import staff_only_rejection
from app.core.i18n import tr
from app.models import NotificationRule, QueueMember, TicketQueue, User, WebhookAllowlistEntry
from app.modules.notification.channels import get_channel
from app.modules.notification.errors import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from app.modules.notification.schema import AllowlistEntryCreate, RuleCreate

# ---------------------------------------------------------------------------
# Cross-org resolvers (invariant #5)
# ---------------------------------------------------------------------------


async def _resolve_queue(session: AsyncSession, *, org_id: UUID, queue_id: UUID) -> UUID:
    """Org-scoped existence check for a `queue_id` (spec §2.3/§2.4). See the
    module docstring for why `TicketQueue` is imported via `app.models`
    rather than from `app.modules.service_desk` directly, and why the FK
    alone is not enough."""
    found = (
        await session.execute(
            select(TicketQueue.id).where(
                TicketQueue.org_id == org_id, TicketQueue.id == queue_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotificationNotFoundError(tr("queue {queue_id} not found", queue_id=queue_id))
    return found


async def _resolve_user(session: AsyncSession, *, org_id: UUID, user_id: UUID) -> UUID:
    """Org-scoped existence check for a `user_id`. Same trap as `_resolve_queue`:
    a user id belonging to a different org satisfies `queue_members.user_id`'s
    FK just as well as one in this org."""
    found = (
        await session.execute(
            select(User.id).where(User.org_id == org_id, User.id == user_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotificationNotFoundError(tr("user {user_id} not found", user_id=user_id))
    return found


def _reject_staff_only_to_requester(event_type: str, recipient_kind: str) -> None:
    """Service-layer half of spec §6.4's guard -- see the module docstring's
    "THE STAFF-ONLY RULE" section for why this exists as a THIRD,
    independent layer alongside `RuleCreate`'s validator and the DB's
    `ck_rule_no_internal_to_requester` / `ck_rule_no_sla_breach_to_requester`
    CHECK constraints, rather than relying on either.

    Ticket core's internal notes are staff-only discussion about a customer,
    and an SLA breach is the organisation's own missed deadline; a requester
    is that ticket's external customer. Routing either to them is a
    disclosure decision nobody made, not a UX nit. The forbidden set is
    `app.core.events.STAFF_ONLY_EVENT_TYPES` -- one declaration, three
    layers."""
    reason = staff_only_rejection(event_type, recipient_kind)
    if reason is not None:
        raise NotificationValidationError(reason)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


async def create_rule(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, data: RuleCreate
) -> NotificationRule:
    """Create a routing rule. `channel_id` is resolved through `get_channel`
    (this module's own load-bearing org-scoped lookup, already covered by
    `test_notification_channels.py`); `queue_id`, when supplied, through
    `_resolve_queue` above. Either raises `NotificationNotFoundError` for a
    cross-org or nonexistent id, before anything is written.

    See the module docstring for why the staff-only pairings are rejected
    unconditionally here rather than only relying on `RuleCreate`'s own
    validator or the DB CHECK constraints, and why `notification_rules` needs
    no `ON CONFLICT` handling (no unique constraint exists on this table)."""
    channel = await get_channel(session, org_id=org_id, channel_id=data.channel_id)

    queue_id: UUID | None = None
    if data.queue_id is not None:
        queue_id = await _resolve_queue(session, org_id=org_id, queue_id=data.queue_id)

    _reject_staff_only_to_requester(data.event_type, data.recipient_kind)

    rule = NotificationRule(
        org_id=org_id,
        event_type=data.event_type,
        channel_id=channel.id,
        queue_id=queue_id,
        recipient_kind=data.recipient_kind,
        active=data.active,
        created_by=actor_id,
    )
    session.add(rule)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.rule_created",
        target_type="notification_rule",
        target_id=str(rule.id),
        metadata={
            "event_type": data.event_type,
            "channel_id": str(channel.id),
            "recipient_kind": data.recipient_kind,
            "queue_id": str(queue_id) if queue_id is not None else None,
        },
    )
    await session.flush()
    return rule


async def update_rule(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, rule_id: UUID, active: bool | None
) -> NotificationRule:
    """Apply an `active` toggle to a rule, org-scoped-loaded (Task 10, spec
    §9's `PATCH /rules/{id}`). `event_type`/`channel_id`/`recipient_kind`/
    `queue_id` are deliberately not editable here at all -- see
    `schema.RuleUpdate`'s docstring for why: they together define what a
    rule routes, and letting them drift post-create would reopen the §6.4
    internal-note guard's write-time check against a row that already
    exists rather than one being inserted. `active=None` (the field simply
    omitted from the request) is a no-op, matching this module's other
    `*Update` handlers' `exclude_unset` discipline."""
    rule = await _get_rule(session, org_id=org_id, rule_id=rule_id)
    if active is None or rule.active == active:
        return rule

    rule.active = active
    session.add(rule)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.rule_updated",
        target_type="notification_rule",
        target_id=str(rule.id),
        metadata={"active": active},
    )
    await session.flush()
    return rule


async def list_rules(session: AsyncSession, *, org_id: UUID) -> list[NotificationRule]:
    """Plain org-scoped list, explicitly ordered. No audit -- a read."""
    stmt = (
        select(NotificationRule)
        .where(NotificationRule.org_id == org_id)
        .order_by(NotificationRule.created_at, NotificationRule.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _get_rule(session: AsyncSession, *, org_id: UUID, rule_id: UUID) -> NotificationRule:
    rule = (
        await session.execute(
            select(NotificationRule).where(
                NotificationRule.org_id == org_id, NotificationRule.id == rule_id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotificationNotFoundError(
            tr(
                "notification rule {rule_id} not found",
                rule_id=rule_id,
            )
        )
    return rule


async def delete_rule(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, rule_id: UUID
) -> None:
    """Org-scoped delete. Audits BEFORE the row is gone (matches
    `channels.delete_channel`)."""
    rule = await _get_rule(session, org_id=org_id, rule_id=rule_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.rule_deleted",
        target_type="notification_rule",
        target_id=str(rule.id),
        metadata={"event_type": rule.event_type},
    )
    await session.delete(rule)
    await session.flush()


# ---------------------------------------------------------------------------
# Webhook egress allowlist
# ---------------------------------------------------------------------------


async def _reject_duplicate_allowlist_entry(
    session: AsyncSession, org_id: UUID, host: str, port: int
) -> None:
    """Pre-check for `uq_allowlist_org_host_port` -- a FAST PATH, not the
    sole guard; see `add_allowlist_entry`, which follows this with a
    concurrency-safe `ON CONFLICT DO NOTHING` write of its own, exactly
    `channels.create_channel`'s pattern."""
    existing = (
        await session.execute(
            select(WebhookAllowlistEntry.id).where(
                WebhookAllowlistEntry.org_id == org_id,
                WebhookAllowlistEntry.host == host,
                WebhookAllowlistEntry.port == port,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise NotificationValidationError(
            tr("allowlist entry already exists for {host}:{port}", host=host, port=port)
        )


async def add_allowlist_entry(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, data: AllowlistEntryCreate
) -> WebhookAllowlistEntry:
    """Create an allowlist entry. Two concurrent calls for the same
    `(org_id, host, port)` can both miss `_reject_duplicate_allowlist_entry`'s
    pre-check before either has written; the actual insert therefore goes
    through `ON CONFLICT DO NOTHING` against `uq_allowlist_org_host_port`,
    and a `None` RETURNING result raises the identical typed
    `NotificationValidationError` the pre-check raises -- never a raw
    `IntegrityError`, and a race loser never ends up holding the winner's
    row (this is a CREATE, not a get-or-create)."""
    await _reject_duplicate_allowlist_entry(session, org_id, data.host, data.port)

    entry_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(WebhookAllowlistEntry)
            .values(
                id=entry_id,
                org_id=org_id,
                host=data.host,
                port=data.port,
                note=data.note,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "host", "port"])
            .returning(WebhookAllowlistEntry.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise NotificationValidationError(
            tr("allowlist entry already exists for {host}:{port}", host=data.host, port=data.port)
        )

    entry = (
        await session.execute(
            select(WebhookAllowlistEntry).where(
                WebhookAllowlistEntry.org_id == org_id,
                WebhookAllowlistEntry.id == inserted_id,
            )
        )
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.allowlist_added",
        target_type="webhook_allowlist_entry",
        target_id=str(entry.id),
        metadata={"host": data.host, "port": data.port},
    )
    await session.flush()
    return entry


async def list_allowlist(
    session: AsyncSession, *, org_id: UUID
) -> list[WebhookAllowlistEntry]:
    """Plain org-scoped list, explicitly ordered. No audit -- a read."""
    stmt = (
        select(WebhookAllowlistEntry)
        .where(WebhookAllowlistEntry.org_id == org_id)
        .order_by(WebhookAllowlistEntry.created_at, WebhookAllowlistEntry.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _get_allowlist_entry(
    session: AsyncSession, *, org_id: UUID, entry_id: UUID
) -> WebhookAllowlistEntry:
    entry = (
        await session.execute(
            select(WebhookAllowlistEntry).where(
                WebhookAllowlistEntry.org_id == org_id,
                WebhookAllowlistEntry.id == entry_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise NotificationNotFoundError(
            tr(
                "allowlist entry {entry_id} not found",
                entry_id=entry_id,
            )
        )
    return entry


async def delete_allowlist_entry(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, entry_id: UUID
) -> None:
    """Org-scoped delete. Audits BEFORE the row is gone."""
    entry = await _get_allowlist_entry(session, org_id=org_id, entry_id=entry_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.allowlist_removed",
        target_type="webhook_allowlist_entry",
        target_id=str(entry.id),
        metadata={"host": entry.host, "port": entry.port},
    )
    await session.delete(entry)
    await session.flush()


async def allowlisted_hosts(session: AsyncSession, *, org_id: UUID) -> set[tuple[str, int]]:
    """The egress guard's consumer shape (`app/core/egress.py::post_json`'s
    `allowlisted_hosts: set[tuple[str, int]]` parameter, Task 9). Migration
    0026 made `port` part of the allowlist's identity -- an entry names a
    host AND a port together, not a host and every port on it -- so this
    returns `(host, port)` pairs, never bare hosts. No audit -- a read."""
    stmt = select(WebhookAllowlistEntry.host, WebhookAllowlistEntry.port).where(
        WebhookAllowlistEntry.org_id == org_id
    )
    rows = (await session.execute(stmt)).all()
    return {(host, port) for host, port in rows}


# ---------------------------------------------------------------------------
# Queue membership (spec §2.4, the G2 model)
# ---------------------------------------------------------------------------


async def add_queue_member(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, queue_id: UUID, user_id: UUID
) -> QueueMember:
    """Add a user to a queue's membership.

    IDEMPOTENCY DECISION: unlike `create_channel`/`add_allowlist_entry`
    (CREATEs, where a race loser must get the identical typed error the
    pre-check raises and never the winner's row), `add_queue_member` treats
    adding an already-current member as a successful no-op rather than an
    error. A queue's membership is a SET of users -- "this user is in this
    queue" is either already true or becomes true, and there is no second
    piece of caller-supplied state (unlike a channel's `config` or an
    allowlist entry's `note`) that a second `add_queue_member` call for the
    same pair could conflict over. Modeling it as a hard error would force
    every caller (a bulk "add these five users to this queue" admin action,
    a sync job re-asserting the same membership) to first list current
    members and diff, purely to avoid a 400 for a request that is, in
    effect, already satisfied.

    RACE SAFETY: `queue_id`/`user_id` are resolved org-scoped first (both
    plain SELECTs, no lock). The write itself goes through
    `ON CONFLICT DO NOTHING` against `uq_queue_member`, followed by a SELECT
    of whatever row now exists for this `(queue_id, user_id)` pair -- ours,
    if we won, or the other caller's, if we lost. Because this function is
    deliberately idempotent (see above), returning the other caller's row
    here is correct, not the "never return the winner's row" violation it
    would be for a CREATE -- both callers asked for the same end state and
    both get it. This still never raises a raw `IntegrityError`: `ON
    CONFLICT DO NOTHING` means the conflicting insert never reaches the
    constraint at all.

    AUDIT: only written when this call actually inserted a new row. A repeat
    call that changes nothing is not a "queue-membership change" (invariant
    #4's audit list) any more than re-issuing an identical GET is -- auditing
    every no-op re-assertion of an unchanged membership would degrade the
    hash-chained log's signal for no security benefit."""
    await _resolve_queue(session, org_id=org_id, queue_id=queue_id)
    await _resolve_user(session, org_id=org_id, user_id=user_id)

    member_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(QueueMember)
            .values(
                id=member_id,
                org_id=org_id,
                queue_id=queue_id,
                user_id=user_id,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["queue_id", "user_id"])
            .returning(QueueMember.id)
        )
    ).scalar_one_or_none()
    newly_added = inserted_id is not None

    member = (
        await session.execute(
            select(QueueMember).where(
                QueueMember.org_id == org_id,
                QueueMember.queue_id == queue_id,
                QueueMember.user_id == user_id,
            )
        )
    ).scalar_one()
    await session.flush()

    if newly_added:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="notification.queue_member_added",
            target_type="queue_member",
            target_id=str(member.id),
            metadata={"queue_id": str(queue_id), "user_id": str(user_id)},
        )
        await session.flush()
    return member


async def remove_queue_member(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, queue_id: UUID, user_id: UUID
) -> None:
    """Remove a user from a queue's membership.

    Unlike `add_queue_member`, this follows this module's uniform delete
    semantics (`delete_channel`/`delete_rule`/`delete_allowlist_entry`): the
    targeted row must exist, org-scoped, or the call raises
    `NotificationNotFoundError` -- a delete of something not there is not
    silently accepted. There is no unique-constraint race to guard on a
    DELETE the way there is on an INSERT; two concurrent removals of the
    same membership simply mean the second one, by the time it looks, finds
    the row already gone and correctly reports not-found -- an ordinary
    outcome, not a raw constraint violation."""
    member = (
        await session.execute(
            select(QueueMember).where(
                QueueMember.org_id == org_id,
                QueueMember.queue_id == queue_id,
                QueueMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotificationNotFoundError(
            tr(
                "user {user_id} is not a member of queue {queue_id}",
                user_id=user_id,
                queue_id=queue_id,
            )
        )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.queue_member_removed",
        target_type="queue_member",
        target_id=str(member.id),
        metadata={"queue_id": str(queue_id), "user_id": str(user_id)},
    )
    await session.delete(member)
    await session.flush()


async def list_queue_members(
    session: AsyncSession, *, org_id: UUID, queue_id: UUID
) -> list[QueueMember]:
    """Plain org- and queue-scoped list, explicitly ordered. No audit -- a
    read. Both predicates are load-bearing: `queue_id` alone (dropping
    `org_id`) would still, in the common case, return nothing for a
    cross-org queue id simply because no membership row happens to exist
    under it in THIS org's data -- see `test_list_queue_members_is_org_scoped`
    for the discriminating version of this test, which populates a real
    membership row under the foreign queue id first so that dropping the
    `org_id` predicate demonstrably leaks it."""
    stmt = (
        select(QueueMember)
        .where(QueueMember.org_id == org_id, QueueMember.queue_id == queue_id)
        .order_by(QueueMember.created_at, QueueMember.id)
    )
    return list((await session.execute(stmt)).scalars().all())

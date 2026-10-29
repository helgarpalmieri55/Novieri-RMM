"""Kernel transactional outbox (docs/modules/notification/spec.md §3).

Publishers write an event row on the CALLER'S session, inside the caller's
existing transaction. This module deliberately does three things it would be
easy to do differently, and each one is load-bearing:

* It does not commit. Committing here would let an event outlive a rolled-back
  state change, so the system would notify about something that never happened.
* It does not open its own transaction, for the same reason.
* It does not dispatch. Dispatching inline would put an outbound network call
  inside a database transaction, holding a database connection open for the
  duration of someone else's HTTP timeout.

Draining and delivery are the worker's job (spec §10).

This lives in `app/core/` rather than in `app/modules/notification/` because
Phase 16 (endpoint health & alerting) publishes to the same facility. A module
registers into this; it does not own it.
"""

from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import DomainEvent

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ticket.assigned",
        "ticket.status_changed",
        "ticket.comment_added",
        # Spec §6.4: internal notes carry their OWN event type rather than a
        # visibility flag on comment_added. That is what lets a rule pairing
        # them with `requester` be rejected at WRITE time; with a shared type
        # the guard could only run at render time, one template edit away from
        # disclosing staff discussion to the requester.
        "ticket.internal_note_added",
        "ticket.approval_requested",
        # A request landing on `pending_approval` (creation, or -- per
        # `lifecycle.py`'s `new -> pending_approval` request edge --
        # `transitions.change_status`) is a NEED-TO-ACT notification: someone
        # must go decide it. `ticket.approval_decided` is the DIFFERENT event
        # for when that decision has actually been made
        # (`approvals.decide_approval`), carrying its own `decision` field.
        # Overloading one type for both was the original defect; see
        # `service_desk/approvals.py`'s module docstring.
        "ticket.approval_decided",
        "ticket.resolved",
        # Published by app.modules.sla (docs/modules/sla/spec.md §6) when the
        # sweep marks a response or resolution deadline breached. Delivery is
        # the notification module's job; this module ships no mail/webhook.
        # STAFF-ONLY -- see STAFF_ONLY_EVENT_TYPES below.
        "ticket.sla_breached",
        # Published by app.modules.health (docs/modules/endpoint-health-alerting/
        # spec.md §9) with no human actor at all -- an alert opens because a
        # threshold was crossed. FOUR types rather than two, deliberately:
        # escalation reusing `alert_raised` would double-fire every rule routed
        # to the on-call channel, and an approval request with no type of its
        # own would make the autonomy dial's middle setting silently mean
        # "never" -- the same defect this registry already records and already
        # fixed for `ticket.approval_requested` vs `ticket.approval_decided`.
        # ALL FOUR ARE STAFF-ONLY -- see STAFF_ONLY_EVENT_TYPES below.
        "health.alert_raised",
        "health.alert_cleared",
        "health.alert_escalated",
        "health.remediation_requires_approval",
        # Published by app.modules.ipam (docs/modules/ipam/spec.md §10), both
        # machine-origin with no human actor.
        #
        # `ipam.drift_detected` -- the reconcile sweep (§4.4), and ONLY when an
        # item reaches `consecutive_detections = 2` unacknowledged and
        # unsuppressed. One event per item, deliberately not an aggregate, so a
        # rule can route `conflict` differently from `documented_absent`; the
        # deduplication is the two-detection rule, not batching. One sweep that
        # ran while a switch was rebooting is not news.
        #
        # `ipam.sweep_rejected` -- the §6.4 result-scope check, when an agent
        # returns addresses outside the scope its job was signed for. THIS IS A
        # SECURITY SIGNAL, NOT AN OPERATIONS ONE.
        #
        # NEITHER IS STAFF-ONLY (§10) and neither belongs in
        # STAFF_ONLY_EVENT_TYPES: that set exists to keep a notification away
        # from a TICKET'S EXTERNAL REQUESTER, and an IPAM event has no ticket
        # and no requester -- so there is no `notification_rules` CHECK for
        # these and no notification migration. The real routing limitation is
        # the other one: `recipient_kind` resolves `assignee`/`requester`/
        # `queue_members` from a ticket-shaped payload, so in v1 these are
        # routable only through `channel_default` rules.
        "ipam.drift_detected",
        "ipam.sweep_rejected",
        # Published by `app.core.health.notify` when a platform probe stays
        # unhealthy for several consecutive samples, and when it recovers.
        # Machine-origin, no ticket and no requester, so not staff-only and
        # routable through `channel_default` rules only. The payload carries
        # the probe name, status and reason CODE, never exception text.
        "platform.probe_unhealthy",
        "platform.probe_recovered",
        # Published by `app.modules.subscriptions.alerts`' daily sweep, once
        # per subscription per deadline. No ticket, no requester: routable
        # through `channel_default` rules only.
        "subscription.notice_deadline_approaching",
        "subscription.renewal_approaching",
        # Published by `app.services.enrollment.enroll_via_link` (install-link
        # spec §1.5) when a machine joins through the install link and starts
        # waiting for review. Machine-origin, no ticket and no requester:
        # routable through `channel_default` rules only.
        "device.joined_via_link",
        # Published by `app.services.device_review` when a rejected machine's
        # agent may still be installed -- the rejection ended `timeout`,
        # `agent_did_not_confirm` or `agent_cannot_uninstall` -- so someone has
        # to remove it by hand. Machine-origin, no ticket and no requester:
        # routable through `channel_default` rules only.
        "device.removal_incomplete",
        # Published by `app.modules.agent_push.runs` when a confirmed subnet
        # push's last target reaches a terminal state (design 2026-09-30
        # §1.5). Machine-origin, no ticket and no requester: routable through
        # `channel_default` rules only. The payload carries counts and the
        # prefix, never a credential.
        "push.run.completed",
        # Published by the same pacer when the lockout guard stops a run after
        # repeated sign-in failures (design 2026-09-30 §1.4a). Someone must
        # check the credential and resume, so this one needs a person.
        # Machine-origin, `channel_default` rules only; it names the credential
        # by its label and nothing more.
        "push.run.stopped",
    }
)

class StaffOnlyEvent(NamedTuple):
    """How a staff-only event type describes itself in a rejection message."""

    noun: str
    spec_ref: str


# Event types whose notification MUST NEVER reach the ticket's requester --
# the requester is the external customer, and these events are internal to the
# organisation serving them. THE single declaration behind all three
# notification guard layers (`RuleCreate`'s Pydantic validator,
# `rules.create_rule`'s service check, and `notification_rules`' DB CHECK
# constraints), so the set cannot drift between them.
#
# `ticket.internal_note_added` (notification spec §6.4): an internal note is
# staff-only discussion ABOUT the customer.
#
# `ticket.sla_breached` (SLA spec §6): the breach mail is written staff-facing
# ("this ticket breached its SLA ... sign in to review it"). Telling the
# customer their supplier missed a contractual deadline is an outcome nobody
# chose, and it is refused at WRITE time for the same reason §6.4 gives for
# internal notes: a render-time filter is one template edit away from failing
# open.
#
# CORRECTION (endpoint health spec §9.2). An earlier version of this comment
# claimed that `recipient_kind="requester"` resolves the recipient from the
# TICKET rather than from the event payload, and concluded that omitting
# `requester_id` from the SLA payload would not have made the pairing
# unroutable. `app/modules/notification/routing.py` does not do that:
# `_expand_rule` resolves a `requester` rule from `event.payload["requester_id"]`
# (`_uuid_or_none(payload.get("requester_id"))` -> `_resolve_single_user`), so
# an absent `requester_id` genuinely does resolve to `[]`.
# `delivery.py::_render_ticket_sla_breached`'s docstring already said what
# routing.py does; this comment was the outlier. The GUARD'S CONCLUSION IS
# UNCHANGED and the guard stays: relying on a payload key's absence is relying
# on a convention that any future publisher can break by adding the key, and
# these events must be refused at WRITE time regardless of what the payload
# happens to carry. Health alert payloads (health spec §9.1) in fact carry
# `requester_id` present-and-null, so for them the pairing would have been
# routable the moment a publisher populated it.
#
# The four `health.*` types (endpoint health spec §9): an alert is an internal
# operational signal about the customer's estate. Its mail names hostnames,
# thresholds and observed values, an escalation says the org's own on-call did
# not respond, and a remediation approval request offers to execute code as
# SYSTEM/root -- none of which is the external customer's to read or to answer.
#
# The DB CHECK constraints are per-event-type and literal (SQL cannot read this
# dict): `ck_rule_no_internal_to_requester` (migration 0025),
# `ck_rule_no_sla_breach_to_requester` (migration 0034) and the four
# `ck_rule_no_health_*_to_requester` (migration 0040). Adding an entry here
# therefore also requires a new CHECK in a new migration.
STAFF_ONLY_EVENT_TYPES: dict[str, StaffOnlyEvent] = {
    "ticket.internal_note_added": StaffOnlyEvent("internal notes", "notification spec §6.4"),
    "ticket.sla_breached": StaffOnlyEvent("SLA breach notifications", "SLA spec §6"),
    "health.alert_raised": StaffOnlyEvent(
        "endpoint health alerts", "endpoint health spec §9.2"
    ),
    "health.alert_cleared": StaffOnlyEvent(
        "endpoint health alert clears", "endpoint health spec §9.2"
    ),
    "health.alert_escalated": StaffOnlyEvent(
        "endpoint health alert escalations", "endpoint health spec §9.2"
    ),
    "health.remediation_requires_approval": StaffOnlyEvent(
        "endpoint remediation approval requests", "endpoint health spec §9.2"
    ),
}


def staff_only_rejection(event_type: str, recipient_kind: str) -> str | None:
    """The one place the "is this pairing forbidden" question is answered.

    Returns the human-readable reason a rule pairing `event_type` with
    `recipient_kind` must be refused, or `None` when the pairing is fine.
    Both notification application-layer guards call this; the DB CHECKs
    restate it in SQL because a constraint cannot call Python.
    """
    if recipient_kind != "requester":
        return None
    staff_only = STAFF_ONLY_EVENT_TYPES.get(event_type)
    if staff_only is None:
        return None
    return tr(
        "{noun} cannot be routed to the requester ({spec_ref}): the requester is the ticket's "
        "external customer, and this event is internal to the organisation serving them",
        noun=staff_only.noun,
        spec_ref=staff_only.spec_ref,
    )


class UnknownEventType(ValueError):
    """Raised when publishing an event type absent from the registry.

    A typo'd event type would otherwise match no rule and deliver nothing --
    a silent failure that looks identical to "no rules configured".
    """


async def publish(
    session: AsyncSession,
    *,
    org_id: UUID,
    event_type: str,
    subject_type: str,
    subject_id: UUID,
    payload: dict[str, Any],
    actor_user_id: UUID | None,
    client_id: UUID | None,
) -> None:
    """Record a domain event on the caller's session and transaction.

    `actor_user_id` is deliberately allowed to be None: machine-origin events
    (a Phase 16 alert, a scheduled sweep) have no human actor, and inventing a
    synthetic user to satisfy a NOT NULL would put a fake person in the record.

    `payload` must carry only publishable content -- never a raw model dump.
    An internal note's text, for instance, never enters an event, so even a
    mis-routed delivery cannot disclose it (spec §6.4).

    `client_id` is the client of the subject, and has NO default: every call
    decides. None only for an organisation-wide subject (a platform probe):
    `domain_events` is under the generic O policy since migration 0243 (plan
    2026-10-26 Task 15), so a NULL client is written only under scope all,
    and a narrowed session that tries is refused before the flush
    (`app/core/global_rows.py`, a 403).
    """
    if event_type not in EVENT_TYPES:
        raise UnknownEventType(event_type)
    now = datetime.now(UTC)
    session.add(
        DomainEvent(
            org_id=org_id,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            actor_user_id=actor_user_id,
            client_id=client_id,
            occurred_at=now,
            created_at=now,
        )
    )

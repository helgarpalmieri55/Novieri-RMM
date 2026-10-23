"""Notification core: channels, routing rules, queue membership, deliveries,
and the webhook egress allowlist.

docs/modules/notification/spec.md §2 (data model) and §8 (RBAC/tenancy).
Every column here must match `backend/migrations/versions/0025_notification_core.py`
exactly in type, length, and nullability -- `test_schema_drift.py` checks it.

NOTE ON DEFAULTS. Unlike 0023, migration 0025 declares `created_at`/`updated_at`
with no DB-side DEFAULT, so the Python-side defaults below are load-bearing: an
insert that omitted them would fail the NOT NULL constraint rather than quietly
picking up `now()`. The drift test does not compare defaults, so it would not
have caught this.

The outbox model itself (`DomainEvent`) lives in `app/models/core.py`, not here
-- it is kernel state that Phase 16 alerting also writes, and putting it in this
package would make alerting import from notification.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationChannel(Base):
    """A delivery destination: an outbound webhook, or an SMTP relay."""

    __tablename__ = "notification_channels"
    __table_args__ = (
        CheckConstraint("kind IN ('webhook', 'email')", name="ck_channel_kind"),
        UniqueConstraint("org_id", "name", name="uq_channel_org_name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    # Spec §2.2: NEVER holds a secret. `config` is returned by the API, so a
    # secret placed here would be a credential disclosure (invariant #3).
    # Secrets go to the vault and are referenced by `credential_id`.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id"), default=None
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class NotificationRule(Base):
    """Which event types reach which channel, and who the humans are."""

    __tablename__ = "notification_rules"
    __table_args__ = (
        CheckConstraint(
            "recipient_kind IN ('queue_members', 'assignee', 'requester', 'channel_default')",
            name="ck_rule_recipient_kind",
        ),
        # Spec §6.4 defence in depth: the service rejects these pairings at
        # write time; the constraints make them unrepresentable even via
        # direct SQL. One CHECK per staff-only event type (a constraint cannot
        # read `app.core.events.STAFF_ONLY_EVENT_TYPES`, so the set is
        # restated here in SQL -- adding an entry there requires a new CHECK
        # in a new migration).
        CheckConstraint(
            "NOT (event_type = 'ticket.internal_note_added' AND recipient_kind = 'requester')",
            name="ck_rule_no_internal_to_requester",
        ),
        # SLA spec §6 / migration 0034. The breach mail is written
        # staff-facing, and `recipient_kind='requester'` resolves from
        # `event.payload["requester_id"]` (routing.py::_expand_rule), so the
        # SLA payload omitting `requester_id` is a convention any future
        # publisher can break -- only this constraint is a guarantee. (An
        # earlier version of this comment, and of `app/core/events.py`'s,
        # claimed the recipient came from the TICKET; it does not. Endpoint
        # health spec §9.2 records the correction.)
        CheckConstraint(
            "NOT (event_type = 'ticket.sla_breached' AND recipient_kind = 'requester')",
            name="ck_rule_no_sla_breach_to_requester",
        ),
        # Endpoint health spec §9.2 / migration 0040. Four types, four
        # constraints: an alert names hostnames and thresholds, an escalation
        # says the org's own on-call did not respond, and an approval request
        # offers to execute code as SYSTEM/root. None of that is the external
        # customer's to read or to answer.
        CheckConstraint(
            "NOT (event_type = 'health.alert_raised' AND recipient_kind = 'requester')",
            name="ck_rule_no_health_alert_raised_to_requester",
        ),
        CheckConstraint(
            "NOT (event_type = 'health.alert_cleared' AND recipient_kind = 'requester')",
            name="ck_rule_no_health_alert_cleared_to_requester",
        ),
        CheckConstraint(
            "NOT (event_type = 'health.alert_escalated' AND recipient_kind = 'requester')",
            name="ck_rule_no_health_alert_escalated_to_requester",
        ),
        CheckConstraint(
            "NOT (event_type = 'health.remediation_requires_approval' "
            "AND recipient_kind = 'requester')",
            name="ck_rule_no_health_remediation_requires_approval_to_requester",
        ),
        Index("ix_rules_org_event", "org_id", "event_type"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("notification_channels.id"))
    # NULL means "any queue" (spec §2.3).
    queue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_queue.id"), default=None
    )
    recipient_kind: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class QueueMember(Base):
    """Spec §2.4 -- the user-to-queue membership model the ticket core deferred
    here, because routing needed the same concept and got to choose its shape."""

    __tablename__ = "queue_members"
    __table_args__ = (UniqueConstraint("queue_id", "user_id", name="uq_queue_member"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    queue_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_queue.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class NotificationDelivery(Base):
    """One attempt-tracked delivery of one event to one recipient."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'dead')", name="ck_delivery_status"
        ),
        Index("ix_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("domain_events.id"))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("notification_channels.id"))
    # Spec §2.5: an email address, or a webhook HOST -- never a full URL, which
    # can carry a token in its query string.
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # Spec §4.4: a bounded, sanitized excerpt -- never a raw response body.
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    last_status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class WebhookAllowlistEntry(Base):
    """Spec §2.6 -- names a HOST, never a CIDR. Allowlisting a range is how an
    allowlist quietly becomes a blanket permit.

    Task 4b: `port` is an explicit, required part of the entry (migration
    0026) -- an admin allowlists a host AND a port together, not a host and
    every port on it. NOT NULL, no NULL-means-"any port" escape hatch: that
    would silently reopen the task-4 review's Finding 4 (allowlisting a host
    granting every TCP port on it). Existing rows backfill to 443 at the DB
    level (see the migration); the ORM default mirrors that for new rows
    constructed without an explicit port.
    """

    __tablename__ = "webhook_allowlist_entries"
    __table_args__ = (
        UniqueConstraint("org_id", "host", "port", name="uq_allowlist_org_host_port"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, server_default="443", default=443)
    note: Mapped[str] = mapped_column(String(200), server_default="", default="")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

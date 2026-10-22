"""Service Desk: ticket core -- queues, categories, tickets, comments,
attachments, approvals, status history.

docs/modules/service-desk/spec.md §2 (data model) and §8 (RBAC/tenancy).
Every column here must match `backend/migrations/versions/0023_service_desk.py`
exactly in type, length, and nullability -- `test_schema_drift.py` checks it.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class TicketQueue(Base):
    __tablename__ = "ticket_queue"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_ticket_queue_org_key"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketCategory(Base):
    __tablename__ = "ticket_category"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_ticket_category_org_key"),
        # Spec §2: requires_approval is meaningful only for requests.
        CheckConstraint(
            "type = 'request' OR requires_approval = false",
            name="ck_category_approval_is_request_only",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(16))
    requires_approval: Mapped[bool] = mapped_column(default=False)
    approver_role_id: Mapped[UUID | None] = mapped_column(ForeignKey("roles.id"), default=None)
    default_queue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_queue.id"), default=None
    )
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketNumberCounter(Base):
    """No surrogate id: `org_id` is the primary key (spec §2)."""

    __tablename__ = "ticket_number_counter"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    next_incident: Mapped[int] = mapped_column(Integer, default=1)
    next_request: Mapped[int] = mapped_column(Integer, default=1)


class Ticket(Base):
    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("org_id", "number", name="uq_ticket_org_number"),
        Index("ix_ticket_org_status", "org_id", "status"),
        Index("ix_ticket_org_queue_status", "org_id", "queue_id", "status"),
        Index("ix_ticket_org_assigned_status", "org_id", "assigned_to", "status"),
        Index("ix_ticket_org_requester", "org_id", "requester_id"),
        Index("ix_ticket_org_device", "org_id", "device_id"),
        CheckConstraint(
            "(type = 'incident' AND status IN"
            " ('new','open','pending','resolved','closed','cancelled'))"
            " OR (type = 'request' AND status IN"
            " ('new','pending_approval','in_progress','fulfilled','closed',"
            " 'rejected','cancelled'))",
            name="ck_ticket_status_matches_type",
        ),
        # Spec §2: a machine-origin ticket has no human requester or author,
        # and inventing a synthetic user to satisfy NOT NULL would put a fake
        # person in the audit trail. Every other origin must keep its human
        # attribution. `origin="integration"` (migration 0024, finding A3)
        # is NOT exempted here -- unlike `"alert"`, it still carries a real
        # human requester/author; only its CREATE-action attribution
        # (`actor_type` on the history row/audit event) differs.
        CheckConstraint(
            "origin = 'alert' OR (requester_id IS NOT NULL AND created_by IS NOT NULL)",
            name="ck_ticket_human_attribution",
        ),
        # Migration 0024 (finding A3): pins the origin domain at the DB
        # layer for the first time -- `schema.py`'s `TicketOrigin` Literal
        # was previously the only enforcement. Must be kept in agreement by
        # eye with that Literal, the same discipline
        # `ck_ticket_status_matches_type` already demands of `lifecycle.py`.
        CheckConstraint(
            "origin IN ('technician', 'alert', 'portal', 'email', 'integration')",
            name="ck_ticket_origin_valid",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    number: Mapped[str] = mapped_column(String(24))
    type: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_category.id"))
    queue_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_queue.id"))
    status: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    subject: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    requester_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    origin: Mapped[str] = mapped_column(String(16))
    # Reserved for Phase 15 (spec §2, §7.2): deliberately no FK, same reasoning
    # as `subscriptions.license_ref` -- the two modules stay independently
    # deployable and a dangling reference is tolerated rather than blocking.
    source_alert_id: Mapped[UUID | None] = mapped_column(default=None)
    # Email intake (docs/modules/email-intake/spec.md §2.3, §5.2): the
    # per-ticket reply token, `secrets.token_urlsafe(32)`. NULL until this
    # ticket first becomes the subject of outbound mail. UNIQUE because it
    # is resolved BACK to a ticket from an inbound recipient address.
    #
    # The token identifies the ticket; it does NOT authorise the sender. It
    # appears in the headers of every message in the thread and is forwarded
    # wherever the requester forwards it. Anything resolving a ticket by
    # token must ALSO check the sender (spec §0.2).
    email_token: Mapped[str | None] = mapped_column(String(43), unique=True, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class TicketComment(Base):
    __tablename__ = "ticket_comment"
    __table_args__ = (
        Index("ix_ticket_comment_org_ticket", "org_id", "ticket_id"),
        # Spec §2: a machine comment is never attributed to a person.
        CheckConstraint(
            "(author_type = 'user' AND author_id IS NOT NULL)"
            " OR (author_type = 'system' AND author_id IS NULL)",
            name="ck_comment_author",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticket.id"))
    author_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    author_type: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketAttachment(Base):
    __tablename__ = "ticket_attachment"
    __table_args__ = (
        Index("ix_ticket_attachment_org_ticket", "org_id", "ticket_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticket.id"))
    object_key: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(255))
    byte_size: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketApproval(Base):
    __tablename__ = "ticket_approval"
    __table_args__ = (
        UniqueConstraint("org_id", "ticket_id", name="uq_ticket_approval_org_ticket"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticket.id"))
    state: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketStatusHistory(Base):
    __tablename__ = "ticket_status_history"
    __table_args__ = (
        Index("ix_ticket_status_history_org_ticket", "org_id", "ticket_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticket.id"))
    from_status: Mapped[str | None] = mapped_column(String(32), default=None)
    to_status: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketCannedResponse(Base):
    """A saved reply an operator can insert into a ticket comment (migration
    0130). Titles are unique per org, ignoring case."""

    __tablename__ = "ticket_canned_response"
    __table_args__ = (
        Index(
            "uq_ticket_canned_response_org_title",
            "org_id",
            text("lower(title)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    title: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime | None] = mapped_column(default=None)

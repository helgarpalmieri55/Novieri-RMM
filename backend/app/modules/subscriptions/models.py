"""Subscriptions: subscription, seat assignment, subscription event.

docs/modules/subscriptions/spec.md §2 (data model) and §8 (security invariants).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        Index("ix_subscription_org_vendor", "org_id", "vendor_id"),
        Index("ix_subscription_org_renews_at", "org_id", "renews_at"),
        Index("ix_subscription_org_owner_user", "org_id", "owner_user_id"),
        client_org_fk("subscription"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0236):
    # the client the caller names (`service.create_subscription`). Never
    # changes: no update schema carries it, and its seats hold its people.
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendor.id"), default=None)
    service_name: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    billing_cycle: Mapped[str] = mapped_column(String(10))
    cost_per_cycle: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    seats: Mapped[int] = mapped_column(Integer, default=0)
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    started_at: Mapped[date | None] = mapped_column(Date, default=None)
    renews_at: Mapped[date | None] = mapped_column(Date, default=None)
    auto_renew: Mapped[bool] = mapped_column(default=False)
    notice_days: Mapped[int] = mapped_column(Integer, default=0)
    payment_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    license_ref: Mapped[UUID | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(10), default="active")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SeatAssignment(Base):
    __tablename__ = "seat_assignment"
    __table_args__ = (
        Index("ix_seat_assignment_org_subscription", "org_id", "subscription_id"),
        Index("ix_seat_assignment_org_person", "org_id", "person_id"),
        # The person holding a seat is one of the subscription's client's
        # people (migration 0236). A foreign key ignores RLS, so under scope
        # all this is the only thing between X's subscription and Y's person;
        # a NULL person_id skips it.
        ForeignKeyConstraint(
            ["person_id", "client_id"],
            ["person.id", "person.client_id"],
            name="fk_seat_assignment_person_client",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # subscription (migration 0236): never sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscription.id"))
    assignee_name: Mapped[str] = mapped_column(String(255))
    assignee_email: Mapped[str] = mapped_column(String(255))
    # The person this seat belongs to (docs/modules/people/spec.md §0.4). The
    # free-text name and email stay: they are what the seat was recorded as.
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("person.id"), default=None)
    assigned_at: Mapped[datetime] = mapped_column(server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class SubscriptionEvent(Base):
    __tablename__ = "subscription_event"
    __table_args__ = (
        Index(
            "ix_subscription_event_org_subscription_created",
            "org_id",
            "subscription_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # subscription (migration 0236): never sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscription.id"))
    type: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SubscriptionAlertSent(Base):
    """One notice-deadline or renewal alert already published (migration 0086).

    The unique key is what makes the daily sweep announce each deadline once;
    a renewal that moves `renews_at` is a new deadline.
    """

    __tablename__ = "subscription_alert_sent"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "subscription_id", "kind", "deadline", name="uq_subscription_alert_sent"
        ),
        CheckConstraint("kind IN ('notice', 'renewal')", name="ck_subscription_alert_sent_kind"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # subscription (migration 0236): never sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscription.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(10))
    deadline: Mapped[date] = mapped_column(Date)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())

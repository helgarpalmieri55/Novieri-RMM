"""SLA engine: business calendars, resolution policies, and per-ticket SLA
state.

docs/modules/sla/spec.md §2. Every column here must match
`backend/migrations/versions/0033_sla.py` exactly in type, length and
nullability -- `test_schema_drift.py` compares `Base.metadata` to the live
schema. The drift test does NOT compare defaults, so any Python-side default
below is load-bearing on its own.
"""

from datetime import UTC, date, datetime, time
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SlaCalendar(Base):
    """A named business calendar: working windows + holidays in one timezone
    (spec §2.1)."""

    __tablename__ = "sla_calendar"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_sla_calendar_org_name"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(100))
    # IANA name (e.g. "Europe/London"), validated against zoneinfo at the API
    # layer, never at sweep time -- a bad timezone discovered by the sweep
    # would break every ticket on that calendar at once (spec §2.1).
    timezone: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SlaCalendarWindow(Base):
    """One working window on one weekday of a calendar (spec §2.2).

    Multiple rows per weekday are expected -- a lunch break is two windows
    rather than a special case in the clock. A weekday with no rows is a
    non-working day. A window spanning midnight is expressed as two windows
    on adjacent days; the clock does not handle wrapping (spec §2.2), which
    is what `ck_sla_calendar_window_start_before_end` enforces.

    **`end_time = '00:00:00'` is the one legal exception**, meaning midnight
    at the end of the day (Task 2 review F2; migration 0035). Postgres `TIME`
    has no "24:00" representation, so `start_time < end_time` can never be
    true when `end_time` is midnight -- the plain `<` check made a 24/7
    calendar and the second half of a night shift impossible to store. The
    constraint below is `start_time < end_time OR end_time = '00:00:00'`;
    `app/modules/sla/calendar.py`'s `CalendarSpec` is what interprets that
    sentinel as 1440 minutes rather than 0.
    """

    __tablename__ = "sla_calendar_window"
    __table_args__ = (
        CheckConstraint(
            "start_time < end_time OR end_time = '00:00:00'",
            name="ck_sla_calendar_window_start_before_end",
        ),
        # Migration 0034. Python's `date.weekday()` only ever returns 0-6, so
        # an ISO Sunday-is-7 typo would otherwise be a working window that
        # silently never applies -- the calendar quietly loses a day and
        # nothing errors anywhere.
        CheckConstraint(
            "weekday BETWEEN 0 AND 6", name="ck_sla_calendar_window_weekday_valid"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    calendar_id: Mapped[UUID] = mapped_column(ForeignKey("sla_calendar.id"))
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday .. 6 = Sunday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


class SlaCalendarHoliday(Base):
    """A date on which a calendar's windows do not apply at all (spec §2.3).
    Half-day holidays are deferred (§13); a holiday removes the whole day."""

    __tablename__ = "sla_calendar_holiday"
    __table_args__ = (
        UniqueConstraint(
            "calendar_id", "holiday_date", name="uq_sla_calendar_holiday_cal_date"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    calendar_id: Mapped[UUID] = mapped_column(ForeignKey("sla_calendar.id"))
    holiday_date: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(100))


class SlaPolicy(Base):
    """Response/resolution targets selected by (queue, priority) -- spec §2.4,
    §3.

    `queue_id`/`priority` NULL are wildcards, resolved most-specific-first
    (spec §3). A NULL never collides with another NULL in a Postgres unique
    constraint, so a plain `UNIQUE(org_id, queue_id, priority)` only actually
    enforces uniqueness for the fully-specific case (level 1, neither NULL);
    an org could otherwise hold two conflicting `(NULL, NULL)` fallback
    policies (or two conflicting queue-only / priority-only ones), and
    resolution would then pick one arbitrarily. The migration therefore
    creates FOUR partial unique indexes, one per resolution level, instead of
    a single plain unique constraint.
    """

    __tablename__ = "sla_policy"
    __table_args__ = (
        client_org_fk("sla_policy"),
        CheckConstraint(
            "response_target_minutes > 0", name="ck_sla_policy_response_positive"
        ),
        CheckConstraint(
            "resolution_target_minutes > 0", name="ck_sla_policy_resolution_positive"
        ),
        # Mirrors ck_ticket_origin_valid's discipline (service_desk/models.py):
        # `ticket.priority` has no shared enum table to reference, so this
        # domain is kept in agreement by eye with that column's constraint.
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent') OR priority IS NULL",
            name="ck_sla_policy_priority_valid",
        ),
        # The four partial unique indexes, one per §3 resolution level. They
        # are declared here as well as in migrations 0033/0036 because
        # `test_schema_drift.py` deliberately does not compare indexes: an
        # index that exists only in the migration is one `alembic revision
        # --autogenerate` away from a migration proposing to DROP it, and
        # these are what stop an org holding two conflicting policies at the
        # same level.
        #
        # Every predicate ends `AND active` (migration 0036, Task 4 review
        # F2). §3 only ever considers `active` policies, so the invariant is
        # "at most one ACTIVE policy per (org, resolution level)"; two RETIRED
        # policies at the same level are harmless history resolution never
        # looks at. Without `AND active` a policy that `delete_policy` refuses
        # to delete (because a `ticket_sla` row references it) could be
        # deactivated but never REPLACED -- the retired row went on blocking
        # its own level forever. `policies.py::_POLICY_INDEX_BY_LEVEL` repeats
        # these predicates verbatim as its ON CONFLICT arbiters; all three
        # copies must agree exactly or Postgres cannot infer the index.
        Index(
            "uq_sla_policy_queue_and_priority",
            "org_id",
            "queue_id",
            "priority",
            unique=True,
            postgresql_where=text("queue_id IS NOT NULL AND priority IS NOT NULL AND active"),
        ),
        Index(
            "uq_sla_policy_queue_wildcard_priority",
            "org_id",
            "queue_id",
            unique=True,
            postgresql_where=text("queue_id IS NOT NULL AND priority IS NULL AND active"),
        ),
        Index(
            "uq_sla_policy_wildcard_queue_priority",
            "org_id",
            "priority",
            unique=True,
            postgresql_where=text("queue_id IS NULL AND priority IS NOT NULL AND active"),
        ),
        Index(
            "uq_sla_policy_wildcard_both",
            "org_id",
            unique=True,
            postgresql_where=text("queue_id IS NULL AND priority IS NULL AND active"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    queue_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket_queue.id"), default=None)
    priority: Mapped[str | None] = mapped_column(String(16), default=None)
    calendar_id: Mapped[UUID] = mapped_column(ForeignKey("sla_calendar.id"))
    response_target_minutes: Mapped[int] = mapped_column(Integer)
    resolution_target_minutes: Mapped[int] = mapped_column(Integer)
    escalate_priority: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    escalate_to_queue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_queue.id"), default=None
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class TicketSla(Base):
    """One row per ticket: the resolved policy and the clock's state (spec
    §2.5). Multiple concurrent SLAs are deferred (§13); `ticket_id` is UNIQUE
    to hold that boundary at the DB layer, not only in application code."""

    __tablename__ = "ticket_sla"
    __table_args__ = (
        CheckConstraint(
            "policy_source IN ('initial', 'retriage', 'escalation')",
            name="ck_ticket_sla_policy_source_valid",
        ),
        # Migration 0037. `working_minutes_between` floors at zero so a pause
        # can never buy time back; this says the same at the database, where it
        # also binds any writer that is not `tracking.resume`.
        CheckConstraint(
            "paused_minutes_total >= 0", name="ck_ticket_sla_paused_minutes_non_negative"
        ),
        # Spec §2.5/§7: what make the sweep an index scan rather than a table
        # walk. Declared here as well as in migrations 0033/0039 for the same
        # reason as `sla_policy`'s partial unique indexes -- see that model.
        #
        # REDEFINED BY MIGRATION 0039, and the change is `<target>_met_at IS
        # NULL` LEAVING the partial condition. §7's sweep no longer asks
        # whether a clock was stopped, it asks whether it was stopped ON TIME
        # (`met_at IS NULL OR met_at > due_at`), because a reply that landed
        # after the deadline did not keep the promise. A `met_at IS NULL`
        # partial index does not contain those rows at all, so Postgres could
        # not use it for the widened query and the sweep degraded to a
        # sequential scan on every cycle. `met_at` cannot be re-expressed in
        # the partial condition either -- it is now compared to another column
        # rather than to NULL, and a partial index predicate cannot do that --
        # so it is filtered on the heap and the index keeps only the three
        # plain `IS NULL` tests, which are the half of the predicate that has
        # survived every widening it has taken.
        Index(
            "ix_ticket_sla_org_response_due",
            "org_id",
            "response_due_at",
            postgresql_where=text(
                "response_breached_at IS NULL AND paused_at IS NULL AND voided_at IS NULL"
            ),
        ),
        Index(
            "ix_ticket_sla_org_resolution_due",
            "org_id",
            "resolution_due_at",
            postgresql_where=text(
                "resolution_breached_at IS NULL AND paused_at IS NULL AND voided_at IS NULL"
            ),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticket.id"), unique=True)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("sla_policy.id"))
    calendar_id: Mapped[UUID] = mapped_column(ForeignKey("sla_calendar.id"))
    response_due_at: Mapped[datetime] = mapped_column()
    resolution_due_at: Mapped[datetime] = mapped_column()
    response_met_at: Mapped[datetime | None] = mapped_column(default=None)
    resolution_met_at: Mapped[datetime | None] = mapped_column(default=None)
    response_breached_at: Mapped[datetime | None] = mapped_column(default=None)
    resolution_breached_at: Mapped[datetime | None] = mapped_column(default=None)
    paused_at: Mapped[datetime | None] = mapped_column(default=None)
    # Working minutes already given back by pauses that have ENDED (migration
    # 0037, spec §5). `paused_at` and this column answer different questions
    # and neither is derivable from the other: `paused_at` is "is the clock
    # stopped right now" (the sweep skips those rows), this is "how much has
    # already been given back", which is what §0.3's recompute must fold back
    # into the target -- without it, re-deriving from `created_at` silently
    # destroys the credit of every completed pause. See migration 0037.
    paused_minutes_total: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # Migration 0038. The instant this ticket reached a terminal-not-resolved
    # status (`lifecycle.FINAL_STATUSES`: `cancelled`/`rejected`) and its
    # outstanding promise stopped meaning anything. Neither met NOR
    # breached -- it is void, which is why it needs a column of its own rather
    # than borrowing one of the four stop columns above. Without it both clocks
    # keep running on a finished ticket, so every cancelled ticket breaches
    # with certainty once its deadline passes, and §6's escalation then raises
    # inside the sweep on a ticket it refuses to touch.
    #
    # It records only that the promise was voided; it erases nothing. A clock
    # that had ALREADY stopped keeps its `*_met_at`/`*_breached_at`, so
    # cancelling a ticket cannot launder away a breach that really happened --
    # the mirror of why cancellation must not met-stamp (that would let
    # "cancel it" inflate attainment). See migration 0038 and
    # `tracking.note_cancelled`.
    voided_at: Mapped[datetime | None] = mapped_column(default=None)
    # Makes escalation once-only regardless of how often the sweep sees the
    # row (spec §2.5, §6).
    escalated_at: Mapped[datetime | None] = mapped_column(default=None)
    # §0.3's attribution requirement and §0.4's loop guard are the same
    # column: "why does the current policy apply".
    policy_source: Mapped[str] = mapped_column(String(16))
    last_recomputed_at: Mapped[datetime] = mapped_column(default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SlaOrgSettings(Base):
    """Per-org auto-close setting (spec §2.6). A dedicated table because no
    general per-org settings table exists in this codebase (`budget_settings`
    is module-scoped); inventing a platform-wide one from inside this module
    would be defining shared structure, which §1 forbids."""

    __tablename__ = "sla_org_settings"
    __table_args__ = (
        CheckConstraint(
            "auto_close_after_days > 0", name="ck_sla_org_settings_auto_close_positive"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), unique=True)
    auto_close_after_days: Mapped[int] = mapped_column(Integer, server_default="7", default=7)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

"""Error groups and events -- the drain's target table pair
(docs/superpowers/specs/2026-08-15-error-monitoring-design.md §4).

TWO MORE TABLES WITHOUT A TENANT COLUMN, after `platform_health_sample`
(`app/models/platform_health.py`). RLS is ENABLED and FORCED on both with no
tenant policy -- deny-all in Postgres -- plus exactly one policy granting
`app_user`. `tests/test_tenant_column_sweep.py`'s `TENANTLESS_BY_DESIGN`
names both, with why.

`PlatformErrorEvent.org_id` (and the other correlation ids) is a NULLABLE
CORRELATION column, not a tenancy key (spec §4.1): an error at process
startup belongs to no org, and the fact that a code path is broken is
platform information regardless of which org's request tripped it.
Authorisation is by the platform-wide `platform.errors.read` code, not by row
scope -- so there is deliberately no `ForeignKey` from `org_id` to
`organizations.id` either; a dangling id (the org row itself long gone) must
not block recording the failure.

Neither table stores exception text -- see `app.core.errors.capture` for the
invariant this pair inherits.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class PlatformErrorGroup(Base):
    """One row per fingerprint. Updated in place as events arrive -- never
    accumulates a second row for the same `(exc_type, module, lineno,
    qualname)` -- so `event_count`/`last_seen` are the running history and the
    table's cardinality is bounded by distinct raise sites, not by volume."""

    __tablename__ = "platform_error_group"
    __table_args__ = (Index("ix_peg_last_seen", "last_seen"),)

    id: Mapped[UUID] = uuid7_pk()
    fingerprint: Mapped[str] = mapped_column(String(16), unique=True)
    # WHICH FINGERPRINT RULE NAMED THIS ROW (`app.core.errors.capture.
    # FINGERPRINT_VERSION`). 1: the traceback's last frame, so a row whose
    # `module` is `httpx`/`asyncpg` is a library frame this platform could not
    # see past. 2: the deepest `app.*` frame. The default is 1 BECAUSE THAT IS
    # WHAT A ROW WITHOUT ONE IS: every row written before migration 0069 was
    # produced by the old rule, and no backfill can move them -- this table
    # stores the derived site and never the traceback, so the application
    # frame a v1 group should have been named after is not recoverable from
    # anything here. They age out; they are not rewritten. The column is not
    # part of the unique key: the version is hashed INTO `fingerprint`, so the
    # two eras cannot collide on it (see `fingerprint_of`).
    fingerprint_version: Mapped[int] = mapped_column(
        SmallInteger(), default=1, server_default="1"
    )
    exc_type: Mapped[str] = mapped_column(String(255))
    module: Mapped[str] = mapped_column(String(255))
    lineno: Mapped[int] = mapped_column()
    qualname: Mapped[str] = mapped_column(String(255))
    # Column is `where_`, not `where` -- `WHERE` is a SQL keyword. Matches
    # `CapturedError.where`: "api" | "worker" | "gateway".
    where_: Mapped[str] = mapped_column(String(16))
    first_seen: Mapped[datetime] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column()
    event_count: Mapped[int] = mapped_column(BigInteger(), default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)


class PlatformErrorEvent(Base):
    """One row per captured occurrence. No exception text; only the
    correlation ids read from the contextvars at capture time, each `None`
    -- not `""` -- when the capture happened outside that context."""

    __tablename__ = "platform_error_event"
    __table_args__ = (Index("ix_pee_group_occurred", "group_id", "occurred_at"),)

    id: Mapped[UUID] = uuid7_pk()
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_error_group.id", ondelete="CASCADE")
    )
    occurred_at: Mapped[datetime] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Correlation only -- see the module docstring. Deliberately no
    # `ForeignKey`: this must record even a failure whose org was deleted
    # since, and this table carries no tenancy meaning to enforce.
    org_id: Mapped[UUID | None] = mapped_column(nullable=True, default=None)
    user_id: Mapped[UUID | None] = mapped_column(nullable=True, default=None)
    job_id: Mapped[UUID | None] = mapped_column(nullable=True, default=None)
    device_id: Mapped[UUID | None] = mapped_column(nullable=True, default=None)

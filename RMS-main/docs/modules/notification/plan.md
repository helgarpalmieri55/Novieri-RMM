# Notification Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build notification core — a kernel transactional outbox, a hardened outbound-egress guard, and per-org webhook/SMTP delivery with retry and dead-lettering — so the control plane can tell humans and systems what happened without becoming an SSRF surface.

**Architecture:** Two new kernel facilities (`app/core/events.py`, `app/core/egress.py`) plus one module (`app/modules/notification/`). Events are written to `domain_events` in the same transaction as the state change that produced them; two Celery tasks drain the outbox and deliver. The notification module knows nothing about tickets — it consumes events by `event_type`. Ticket core's only change is `publish(...)` calls in services it already owns.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, PostgreSQL with RLS, Celery, httpx 0.28 (pinned-IP transport), stdlib `smtplib` via `asyncio.to_thread`. Frontend: React + TypeScript (strict), Vite, TanStack Query.

## Global Constraints

- **Migration head is `0024`.** This module's migration is `0025`. Task 1 confirms the head before writing it. Migrations are forward-only — never edit a shipped one.
- **Backend suite is at 1790 passing; frontend at 192.** Every task ends green. Gates: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m mypy app/`, `.venv/bin/python -m ruff check app/ tests/`, and in `frontend/`: `npx tsc --noEmit`, `npm test`.
- **Environment first, every session:** `pg_ctlcluster 16 main start ; redis-server --daemonize yes`
- **Run gates in the FOREGROUND** as plain blocking commands. The backend suite takes ~5 minutes. Do not background it, sleep-poll, or launch monitors.
- **Use `.venv/bin/python`, never bare `python`.** Run backend commands from `backend/`.
- **Type hints are mandatory**; mypy runs strict. No `any` in TypeScript.
- **Every query carries an explicit `org_id` predicate** in addition to RLS (invariant #5).
- **Every privileged mutation writes an audit event** (invariant #4); no audit on view. Delivery *attempts* are not audited (spec §8) — they are operational rows.
- **`org_id`/`actor_id` always come from `AuthContext`**, never from a request body or path.
- **`backend/tests/test_schema_drift.py` compares `Base.metadata` to the live schema.** Model and migration must agree exactly on nullability, numeric precision/scale, and varchar length.
- **`backend/tests/test_rls.py` auto-covers every table with an `org_id` column.** A new table without `ENABLE + FORCE` RLS and a `tenant_isolation` policy fails it. This is necessary but **not sufficient** — spec §12.10 additionally requires a hand-written cross-org test per table that fails when its `org_id` predicate is deleted.
- **No credential, no full webhook URL with query string, and no response body appears in any API response, log line, or delivery row** (invariant #3, spec §9).
- **`event_type` values are `str(100)`; `recipient` is `str(320)`** (email max length); `last_error` is `str(500)`.

### Decided asymmetry: SMTP is not subject to the deny-private default

Spec §4 scopes the egress guard to **webhooks** ("there is no other outbound HTTP path"). SMTP is deliberately **not** guarded by the private-space denial, because an on-prem mail relay on the LAN is the normal deployment, and denying it would make email unusable for exactly the customers this platform targets. SMTP host/port remain `notification.admin`-only config, audited on change.

This asymmetry is not stated in the spec text; it is recorded here and should be folded into spec §4 when the spec is next touched. Do not "fix" it by routing SMTP through the webhook guard — that breaks the intended deployment.

---

## File structure

**Kernel** (`backend/app/core/`):

| File | Responsibility |
|---|---|
| `events.py` | `publish()`, the event-type registry, `UnknownEventType`. No I/O beyond `session.add` |
| `egress.py` | Denied-network table, resolve-all-and-pin, redirect refusal, bounded read. Pure guard + one `post_json` helper |

**Backend module** (`backend/app/modules/notification/`):

| File | Responsibility |
|---|---|
| `models.py` | ORM models for the five module tables (`domain_events` lives in `app/models/core.py`) |
| `schema.py` | Pydantic request/response models |
| `errors.py` | `NotificationValidationError`, `NotificationNotFoundError` (a subclass) |
| `channels.py` | Channel CRUD; vault store/checkout for channel credentials |
| `rules.py` | Rule CRUD, the write-time internal→requester rejection, allowlist CRUD, queue membership |
| `routing.py` | Event → concrete recipients. Pure resolution given rows; no delivery |
| `delivery.py` | One attempt over webhook or SMTP; retryable-vs-terminal classification; backoff schedule |
| `api.py` | Routes, registration, error translation |

**Frontend** (`frontend/src/modules/notification/`): `types.ts`, `api.ts`, `NotificationAdminPage.tsx`, `ChannelsPanel.tsx`, `RulesPanel.tsx`, `DeliveriesPanel.tsx`, `NotificationNav.tsx`, `__tests__/notification.test.tsx`.

Split rationale: `egress.py` is kernel, not module-local, because it is a security control that any future outbound path must reuse — and because a second copy of "which networks are denied" is how one copy drifts. `routing.py` is separated from `delivery.py` because recipient resolution is pure logic with a large test surface (§12.7 leak test lives there), while delivery is I/O.

---

## Task 1: Migration 0025 — six tables, RLS, RBAC

**Files:**
- Create: `backend/migrations/versions/0025_notification_core.py`
- Modify: `backend/app/core/rbac.py` (add two permission codes)
- Modify: `backend/tests/test_rbac.py` (assert both codes exist)
- Test: `backend/tests/test_notification_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `domain_events`, `notification_channels`, `notification_rules`, `queue_members`, `notification_deliveries`, `webhook_allowlist_entries`; permissions `notification.read`, `notification.admin`.

- [ ] **Step 1: Confirm the migration head**

```bash
cd backend && .venv/bin/python -m alembic heads
```

Expected: `0024_service_desk_integration_origin (head)`. If it is anything else, stop — the plan's migration number is wrong and must be renumbered before continuing.

- [ ] **Step 2: Write the failing migration test**

```python
# backend/tests/test_notification_migration.py
import pytest
from sqlalchemy import text

NEW_TABLES = [
    "domain_events",
    "notification_channels",
    "notification_rules",
    "queue_members",
    "notification_deliveries",
    "webhook_allowlist_entries",
]


@pytest.mark.asyncio
async def test_notification_tables_exist(db_session):
    for table in NEW_TABLES:
        got = await db_session.execute(
            text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
        )
        assert got.scalar() is not None, f"{table} missing"


@pytest.mark.asyncio
async def test_notification_tables_have_forced_rls(db_session):
    # Spec §8: RLS ENABLE + FORCE, not merely ENABLE. Without FORCE the table
    # owner bypasses the policy, and the app connects as the owner.
    for table in NEW_TABLES:
        got = await db_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = :t"
            ),
            {"t": table},
        )
        enabled, forced = got.one()
        assert enabled is True, f"{table}: RLS not enabled"
        assert forced is True, f"{table}: RLS not FORCED"


@pytest.mark.asyncio
async def test_actor_user_id_is_nullable(db_session):
    # Spec §2.1: machine-origin events (Phase 16 alerts) have no human actor.
    # A NOT NULL here would force a synthetic user into the record.
    got = await db_session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'domain_events' AND column_name = 'actor_user_id'"
        )
    )
    assert got.scalar() == "YES"


@pytest.mark.asyncio
async def test_allowlist_host_unique_per_org(db_session):
    got = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'webhook_allowlist_entries' AND indexdef LIKE '%UNIQUE%'"
        )
    )
    assert got.scalar() >= 1
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_migration.py -q`
Expected: FAIL — `domain_events missing`.

- [ ] **Step 4: Write the migration**

```python
# backend/migrations/versions/0025_notification_core.py
"""notification core: outbox, channels, rules, queue members, deliveries, allowlist

Revision ID: 0025_notification_core
Revises: 0024_service_desk_integration_origin
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_notification_core"
down_revision = "0024"
branch_labels = None
depends_on = None

RLS_TABLES = [
    "domain_events",
    "notification_channels",
    "notification_rules",
    "queue_members",
    "notification_deliveries",
    "webhook_allowlist_entries",
]


def upgrade() -> None:
    op.create_table(
        "domain_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # Spec §2.1: NULLABLE on purpose -- machine-origin events have no actor.
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_domain_events_drain", "domain_events", ["claimed_at", "occurred_at"])
    op.create_index("ix_domain_events_org_type", "domain_events", ["org_id", "event_type"])

    op.create_table(
        "notification_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('webhook', 'email')", name="ck_channel_kind"),
        sa.UniqueConstraint("org_id", "name", name="uq_channel_org_name"),
    )

    op.create_table(
        "notification_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_channels.id"), nullable=False),
        sa.Column("queue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_queue.id"), nullable=True),
        sa.Column("recipient_kind", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "recipient_kind IN ('queue_members', 'assignee', 'requester', 'channel_default')",
            name="ck_rule_recipient_kind",
        ),
        # Spec §6.4 defence in depth: the service rejects this pairing at write
        # time; the constraint makes it unrepresentable even via direct SQL.
        sa.CheckConstraint(
            "NOT (event_type = 'ticket.internal_note_added' AND recipient_kind = 'requester')",
            name="ck_rule_no_internal_to_requester",
        ),
    )
    op.create_index("ix_rules_org_event", "notification_rules", ["org_id", "event_type"])

    op.create_table(
        "queue_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("queue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_queue.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("queue_id", "user_id", name="uq_queue_member"),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("domain_events.id"), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_channels.id"), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'dead')", name="ck_delivery_status"
        ),
    )
    op.create_index(
        "ix_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"]
    )

    op.create_table(
        "webhook_allowlist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "host", name="uq_allowlist_org_host"),
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        ctx = "current_setting('app.current_org', true)::uuid"
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {ctx}) WITH CHECK (org_id = {ctx})"
        )


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("webhook_allowlist_entries")
    op.drop_table("notification_deliveries")
    op.drop_table("queue_members")
    op.drop_table("notification_rules")
    op.drop_table("notification_channels")
    op.drop_table("domain_events")
```

- [ ] **Step 5: Add the two permissions**

In `backend/app/core/rbac.py`, alongside the `ticket.*` entries:

```python
    "notification.read": "View notification channels, rules, queue membership, and delivery status",
    "notification.admin": (
        "Manage notification channels, rules, the webhook allowlist, and queue "
        "membership, and run test sends. HIGH PRIVILEGE: this grant controls "
        "where control-plane data is sent and which internal hosts the server "
        "will connect to (spec §8) -- admin tier, not technician tier."
    ),
```

Grant both to the admin role only. **Do not grant `notification.admin` to technician** — spec §8 states why.

- [ ] **Step 6: Assert the permissions exist**

In `backend/tests/test_rbac.py`, extend the existing code-presence test:

```python
def test_notification_permissions_exist():
    from app.core.rbac import PERMISSIONS

    assert "notification.read" in PERMISSIONS
    assert "notification.admin" in PERMISSIONS


def test_notification_admin_is_not_technician_grant():
    # Spec §8: notification.admin configures egress destinations. A technician
    # holding it could point the control plane at an internal host.
    from app.core.rbac import ROLE_PERMISSIONS

    assert "notification.admin" not in ROLE_PERMISSIONS["technician"]
```

- [ ] **Step 7: Migrate and run the tests**

```bash
cd backend && .venv/bin/python -m alembic upgrade head
.venv/bin/python -m pytest tests/test_notification_migration.py tests/test_rbac.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0025_notification_core.py backend/app/core/rbac.py \
        backend/tests/test_notification_migration.py backend/tests/test_rbac.py
git commit -m "feat(notification): migration 0025 -- outbox, channels, rules, deliveries, allowlist"
```

---

## Task 2: ORM models

**Files:**
- Modify: `backend/app/models/core.py` (add `DomainEvent`)
- Create: `backend/app/modules/notification/models.py`
- Modify: `backend/app/models/__init__.py` (export the new models)
- Test: relies on `backend/tests/test_schema_drift.py`

**Interfaces:**
- Consumes: Task 1's tables.
- Produces: `DomainEvent`, `NotificationChannel`, `NotificationRule`, `QueueMember`, `NotificationDelivery`, `WebhookAllowlistEntry`.

`DomainEvent` lives in `app/models/core.py`, not in the module, because it is kernel state that Phase 16 also writes — putting it in `app.modules.notification` would make alerting import from notification.

- [ ] **Step 1: Run the drift test to see it fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_schema_drift.py -q`
Expected: FAIL — live schema has six tables absent from `Base.metadata`.

- [ ] **Step 2: Add `DomainEvent` to `app/models/core.py`**

```python
class DomainEvent(Base):
    """Kernel transactional outbox (spec §2.1, §3).

    Written in the SAME transaction as the state change it describes, so an
    event can never describe a rolled-back fact. Drained by the Celery worker.
    Phase 16 alerting publishes here too -- hence `app/models/core.py` rather
    than a module.
    """

    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_events_drain", "claimed_at", "occurred_at"),
        Index("ix_domain_events_org_type", "org_id", "event_type"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[UUID] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Spec §2.1: nullable on purpose -- a Phase 16 alert event has no actor.
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    processed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column()
```

- [ ] **Step 3: Write `app/modules/notification/models.py`**

Mirror the migration exactly — same nullability, same string lengths, same check constraints. `test_schema_drift.py` compares them field by field and will fail on any mismatch.

```python
"""ORM models for notification core (spec §2)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        CheckConstraint("kind IN ('webhook', 'email')", name="ck_channel_kind"),
        UniqueConstraint("org_id", "name", name="uq_channel_org_name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    # Spec §2.2: NEVER holds a secret. Secrets go to the vault via credential_id;
    # this column is returned by the API.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id"), default=None
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()


class NotificationRule(Base):
    __tablename__ = "notification_rules"
    __table_args__ = (
        CheckConstraint(
            "recipient_kind IN ('queue_members', 'assignee', 'requester', 'channel_default')",
            name="ck_rule_recipient_kind",
        ),
        CheckConstraint(
            "NOT (event_type = 'ticket.internal_note_added' AND recipient_kind = 'requester')",
            name="ck_rule_no_internal_to_requester",
        ),
        Index("ix_rules_org_event", "org_id", "event_type"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("notification_channels.id"))
    queue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_queue.id"), default=None
    )
    recipient_kind: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column()


class QueueMember(Base):
    """Spec §2.4 -- the G2 model the ticket core deferred here."""

    __tablename__ = "queue_members"
    __table_args__ = (UniqueConstraint("queue_id", "user_id", name="uq_queue_member"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    queue_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_queue.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column()


class NotificationDelivery(Base):
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
    # Spec §2.5: an email address or a webhook HOST -- never a full URL, which
    # can carry a token in its query string.
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    last_status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()


class WebhookAllowlistEntry(Base):
    """Spec §2.6 -- names a HOST, never a CIDR. A range is how an allowlist
    quietly becomes a blanket permit."""

    __tablename__ = "webhook_allowlist_entries"
    __table_args__ = (UniqueConstraint("org_id", "host", name="uq_allowlist_org_host"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    host: Mapped[str] = mapped_column(String(255))
    note: Mapped[str] = mapped_column(String(200), server_default="", default="")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column()
```

- [ ] **Step 4: Export from `app/models/__init__.py`**

Add all six to the imports and `__all__`, following the file's existing pattern.

- [ ] **Step 5: Run drift and RLS tests**

```bash
cd backend && .venv/bin/python -m pytest tests/test_schema_drift.py tests/test_rls.py -q
```

Expected: PASS. A failure here names the exact column that disagrees — fix the model to match the migration, never the reverse (the migration is shipped).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/ backend/app/modules/notification/models.py
git commit -m "feat(notification): ORM models for outbox and the five module tables"
```

---

## Task 3: `app/core/events.py` — the outbox publisher

**Files:**
- Create: `backend/app/core/events.py`
- Test: `backend/tests/test_events_outbox.py`

**Interfaces:**
- Consumes: `DomainEvent` (Task 2).
- Produces:
  - `EVENT_TYPES: frozenset[str]`
  - `class UnknownEventType(ValueError)`
  - `async def publish(session: AsyncSession, *, org_id: UUID, event_type: str, subject_type: str, subject_id: UUID, payload: dict[str, Any], actor_user_id: UUID | None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_events_outbox.py
import pytest
from sqlalchemy import select

from app.core.events import EVENT_TYPES, UnknownEventType, publish
from app.models import DomainEvent


@pytest.mark.asyncio
async def test_publish_writes_an_event(db_session, org, user):
    await publish(
        db_session,
        org_id=org.id,
        event_type="ticket.assigned",
        subject_type="ticket",
        subject_id=user.id,
        payload={"ticket_number": "INC-1"},
        actor_user_id=user.id,
    )
    await db_session.flush()
    got = (await db_session.execute(select(DomainEvent))).scalars().all()
    assert len(got) == 1
    assert got[0].event_type == "ticket.assigned"


@pytest.mark.asyncio
async def test_publish_rejects_unregistered_event_type(db_session, org, user):
    # A typo'd event type matches no rule and delivers nothing -- invisible in
    # production. Fail loudly at publish instead.
    with pytest.raises(UnknownEventType):
        await publish(
            db_session,
            org_id=org.id,
            event_type="ticket.assinged",
            subject_type="ticket",
            subject_id=user.id,
            payload={},
            actor_user_id=user.id,
        )


@pytest.mark.asyncio
async def test_publish_accepts_a_null_actor(db_session, org, user):
    # Spec §2.1: Phase 16 alert events have no human actor.
    await publish(
        db_session,
        org_id=org.id,
        event_type="ticket.status_changed",
        subject_type="ticket",
        subject_id=user.id,
        payload={},
        actor_user_id=None,
    )
    await db_session.flush()
    got = (await db_session.execute(select(DomainEvent))).scalars().one()
    assert got.actor_user_id is None


@pytest.mark.asyncio
async def test_publish_does_not_commit(db_session, org, user):
    """Spec §12.8 -- outbox atomicity.

    publish() must not commit. If it did, an event would survive the rollback
    of the state change it describes, and the system would notify about a fact
    that never happened.
    """
    await publish(
        db_session,
        org_id=org.id,
        event_type="ticket.resolved",
        subject_type="ticket",
        subject_id=user.id,
        payload={},
        actor_user_id=user.id,
    )
    await db_session.rollback()
    got = (await db_session.execute(select(DomainEvent))).scalars().all()
    assert got == []


def test_internal_note_is_a_distinct_event_type():
    # Spec §2.1 / §6.4: the write-time rejection of internal->requester rules
    # depends on internal notes having their OWN type, not a flag on
    # comment_added. Collapsing these two would silently move the leak guard
    # to render time.
    assert "ticket.comment_added" in EVENT_TYPES
    assert "ticket.internal_note_added" in EVENT_TYPES
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_events_outbox.py -q`
Expected: FAIL — `ModuleNotFoundError: app.core.events`.

- [ ] **Step 3: Implement**

```python
# backend/app/core/events.py
"""Kernel transactional outbox (spec §3).

Publishers write an event row on the CALLER'S session, inside the caller's
existing transaction. This module deliberately does three things it would be
easy to do differently, and each one is load-bearing:

* It does not commit. Committing here would let an event outlive a rolled-back
  state change, so the system would notify about something that never happened.
* It does not open its own transaction, for the same reason.
* It does not dispatch. Dispatching inline would put an outbound network call
  inside a database transaction, holding a connection open for the duration of
  someone else's HTTP timeout.

Draining and delivery are the worker's job (spec §10).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DomainEvent

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ticket.assigned",
        "ticket.status_changed",
        "ticket.comment_added",
        # Spec §6.4: internal notes are their OWN type, not a visibility flag on
        # comment_added, so a rule pairing them with `requester` can be rejected
        # at write time rather than filtered at render time.
        "ticket.internal_note_added",
        "ticket.approval_requested",
        "ticket.resolved",
    }
)


class UnknownEventType(ValueError):
    """Raised when publishing an event type absent from the registry."""


async def publish(
    session: AsyncSession,
    *,
    org_id: UUID,
    event_type: str,
    subject_type: str,
    subject_id: UUID,
    payload: dict[str, Any],
    actor_user_id: UUID | None,
) -> None:
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
            occurred_at=now,
            created_at=now,
        )
    )
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_events_outbox.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/events.py backend/tests/test_events_outbox.py
git commit -m "feat(core): kernel transactional outbox with an event-type registry"
```

---

## Task 4: `app/core/egress.py` — the SSRF guard

This is the security core of the module. Spec §12.1–12.6 all land here. Do not shorten the test table.

**Files:**
- Create: `backend/app/core/egress.py`
- Test: `backend/tests/test_egress_guard.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class EgressBlocked(Exception)` with `.reason: str` in `{"blocked_private", "dns_failure", "redirect_refused", "scheme_not_allowed"}`
  - `def check_host(host: str, resolved: list[str], *, allowlisted: bool) -> str` — returns the pinned IP, raises `EgressBlocked`
  - `async def resolve(host: str, port: int) -> list[str]`
  - `@dataclass(frozen=True) class EgressResponse: status_code: int; excerpt: str`
  - `async def post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], allowlisted_hosts: set[str], timeout_s: float = 30.0) -> EgressResponse`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_egress_guard.py
import pytest

from app.core.egress import EgressBlocked, check_host

# Spec §12.1 -- one case per denied range. Each entry is a real address an
# attacker would reach for, not a synthetic example.
DENIED = [
    ("10.0.0.1", "RFC1918 /8"),
    ("172.16.0.1", "RFC1918 /12"),
    ("192.168.1.1", "RFC1918 /16"),
    ("127.0.0.1", "loopback v4"),
    ("::1", "loopback v6"),
    ("169.254.169.254", "cloud metadata"),
    ("169.254.1.1", "link-local v4"),
    ("fe80::1", "link-local v6"),
    ("fc00::1", "unique-local v6"),
    ("224.0.0.1", "multicast"),
    ("0.0.0.0", "unspecified"),
]


@pytest.mark.parametrize("ip,label", DENIED)
def test_denied_ranges_are_blocked(ip: str, label: str):
    with pytest.raises(EgressBlocked) as exc:
        check_host("evil.example.com", [ip], allowlisted=False)
    assert exc.value.reason == "blocked_private", label


def test_public_address_is_allowed():
    assert check_host("hooks.example.com", ["93.184.216.34"], allowlisted=False) == "93.184.216.34"


def test_multi_record_dns_with_one_private_record_is_blocked():
    """Spec §12.3.

    Checking only the first record lets a multi-record answer walk past the
    guard: the check sees a public address, the connection uses a private one.
    """
    with pytest.raises(EgressBlocked) as exc:
        check_host("mixed.example.com", ["93.184.216.34", "10.0.0.5"], allowlisted=False)
    assert exc.value.reason == "blocked_private"


def test_allowlisted_host_permits_a_private_address():
    """Spec §12.5 -- the opt-back-in an on-prem LAN destination needs."""
    assert check_host("mattermost.lan", ["10.0.0.5"], allowlisted=True) == "10.0.0.5"


def test_allowlist_applies_only_to_the_named_host():
    # The allowlist is per-host. A different host resolving into the same range
    # is still blocked.
    with pytest.raises(EgressBlocked):
        check_host("other.lan", ["10.0.0.5"], allowlisted=False)


def test_empty_resolution_is_a_dns_failure():
    with pytest.raises(EgressBlocked) as exc:
        check_host("nowhere.example.com", [], allowlisted=False)
    assert exc.value.reason == "dns_failure"
```

Then the transport-level tests, which need a stub:

```python
# continues backend/tests/test_egress_guard.py
from typing import Any

import httpx
import pytest

from app.core.egress import EgressBlocked, post_json


def _client_returning(response: httpx.Response) -> Any:
    """A transport that returns a fixed response and records requests."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    return httpx.MockTransport(handler), calls


@pytest.mark.asyncio
async def test_redirect_is_terminal_and_not_followed(monkeypatch):
    """Spec §12.4.

    A followed redirect is a second request to an UNVALIDATED destination --
    the classic guard bypass. Per-hop re-validation is easy to get subtly
    wrong, so a 3xx is simply terminal.
    """
    transport, calls = _client_returning(
        httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    )
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: transport)

    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
        )
    assert exc.value.reason == "redirect_refused"
    assert len(calls) == 1, "the redirect must not be followed"


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_swap_the_address(monkeypatch):
    """Spec §12.2.

    A resolver that answers public-then-private must not result in a
    connection to the private address. The guard resolves once, validates that
    answer, and CONNECTS TO THE VALIDATED IP -- it never re-resolves.
    """
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: next(answers))

    connected_to: list[str] = []

    def build(ip: str):
        connected_to.append(ip)
        return httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))

    monkeypatch.setattr("app.core.egress._build_transport", build)

    await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    assert connected_to == ["93.184.216.34"], "connected to a re-resolved address"


@pytest.mark.asyncio
async def test_response_body_is_never_returned_in_full(monkeypatch):
    """Spec §4.4 -- the response must not become a read oracle."""
    body = "SECRET-INTERNAL-CONTENT " * 100
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(500, text=body)),
    )

    got = await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    assert got.status_code == 500
    assert len(got.excerpt) <= 500
    assert len(got.excerpt) < len(body)


@pytest.mark.asyncio
async def test_plain_http_is_rejected_for_a_non_allowlisted_host(monkeypatch):
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    with pytest.raises(EgressBlocked) as exc:
        await post_json("http://hooks.example.com/x", {}, headers={}, allowlisted_hosts=set())
    assert exc.value.reason == "scheme_not_allowed"


@pytest.mark.asyncio
async def test_plain_http_is_allowed_for_an_allowlisted_host(monkeypatch):
    # An internal Mattermost may legitimately have no TLS. Forcing https there
    # would push admins to disable the guard entirely.
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["10.0.0.5"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    got = await post_json(
        "http://mattermost.lan/hook", {}, headers={}, allowlisted_hosts={"mattermost.lan"}
    )
    assert got.status_code == 204
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_egress_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: app.core.egress`.

- [ ] **Step 3: Implement**

```python
# backend/app/core/egress.py
"""Outbound egress guard for webhook delivery (spec §4).

WHY THIS EXISTS. A per-org, admin-configurable webhook URL is a server-side
request forgery primitive by construction. This control plane is
domain-admin-equivalent over a fleet and runs on-prem, next to the agent
gateway, the vault, and the database. Without this guard, anyone holding
`notification.admin` can aim the server at 169.254.169.254, at the gateway, or
at any host on the customer's LAN, and read the outcome back through delivery
status and timing.

`app/core/http.py`, despite its name, is INBOUND request-body bounding. It is
not related to this module and does not guard anything outbound.

Four properties, each of which has a specific bypass it prevents:

1. Resolve ALL records and reject if ANY is denied. Checking only the first
   record lets a multi-record DNS answer walk past.
2. Connect to the VALIDATED IP, never re-resolve. Re-resolution is the
   DNS-rebinding hole: validate a public answer, connect to a private one.
3. Refuse redirects outright. A followed redirect is a second request to an
   unvalidated destination; per-hop re-validation is easy to get subtly wrong.
4. Never return the response body. Otherwise delivery is a read oracle for
   anything the server can reach.

Do NOT route SMTP through this guard -- see the plan's "Decided asymmetry".
"""

import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

_EXCERPT_MAX = 500
_BODY_MAX_BYTES = 64 * 1024
_CONNECT_TIMEOUT_S = 10.0

_DENIED_V4 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # includes cloud metadata
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
]
_DENIED_V6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("::/128"),
]


class EgressBlocked(Exception):
    """Raised when a destination is refused. `reason` is a CATEGORY, never a
    per-IP detail -- an admin reading delivery errors must not be able to map
    the internal network from them (spec §4.4)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class EgressResponse:
    status_code: int
    excerpt: str


def _is_denied(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    networks = _DENIED_V4 if ip.version == 4 else _DENIED_V6
    return any(ip in net for net in networks)


def check_host(host: str, resolved: list[str], *, allowlisted: bool) -> str:
    """Validate every resolved record; return the IP to connect to.

    Returns the FIRST record, which is also the one every record was validated
    alongside -- the caller must connect to exactly this address.
    """
    if not resolved:
        raise EgressBlocked("dns_failure", f"{host} did not resolve")
    if not allowlisted:
        for ip_str in resolved:
            if _is_denied(ip_str):
                raise EgressBlocked(
                    "blocked_private",
                    f"{host} resolves into denied address space",
                )
    return resolved[0]


def _resolve_all(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressBlocked("dns_failure", f"{host} did not resolve") from exc
    return [info[4][0] for info in infos]


def _build_transport(ip: str) -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(retries=0)


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    allowlisted_hosts: set[str],
    timeout_s: float = 30.0,
) -> EgressResponse:
    parts = urlsplit(url)
    host = parts.hostname or ""
    allowlisted = host in allowlisted_hosts

    if parts.scheme not in ("https", "http"):
        raise EgressBlocked("scheme_not_allowed", f"scheme {parts.scheme!r}")
    if parts.scheme == "http" and not allowlisted:
        raise EgressBlocked("scheme_not_allowed", "plain http requires an allowlisted host")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    pinned = check_host(host, _resolve_all(host, port), allowlisted=allowlisted)

    # Connect to the pinned IP while preserving Host and TLS SNI, so the
    # validated answer is the one actually used (property 2 above).
    pinned_netloc = f"[{pinned}]" if ":" in pinned else pinned
    pinned_url = parts._replace(netloc=f"{pinned_netloc}:{port}").geturl()
    request_headers = {**headers, "Host": host, "Content-Type": "application/json"}

    timeout = httpx.Timeout(timeout_s, connect=_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(
        transport=_build_transport(pinned),
        timeout=timeout,
        follow_redirects=False,  # property 3
    ) as client:
        response = await client.request(
            "POST",
            pinned_url,
            content=json.dumps(payload).encode(),
            headers=request_headers,
            extensions={"sni_hostname": host},
        )

    if 300 <= response.status_code < 400:
        raise EgressBlocked("redirect_refused", f"destination returned {response.status_code}")

    raw = response.content[:_BODY_MAX_BYTES].decode("utf-8", errors="replace")
    excerpt = "".join(ch for ch in raw if ch.isprintable())[:_EXCERPT_MAX]
    return EgressResponse(status_code=response.status_code, excerpt=excerpt)
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_egress_guard.py -q`
Expected: PASS (17 tests: 11 parametrized + 6).

- [ ] **Step 5: Mutation-check the guard**

The denied-range table is the kind of code that can be quietly weakened. Verify the tests actually hold it:

```bash
cd backend
cp app/core/egress.py /tmp/egress.bak
# Remove the metadata range and confirm the suite notices.
sed -i 's|ipaddress.ip_network("169.254.0.0/16"),.*|# removed|' app/core/egress.py
.venv/bin/python -m pytest tests/test_egress_guard.py -q
```

Expected: **FAIL** on the `169.254.169.254` and `169.254.1.1` cases. If it passes, the test table is not doing its job — fix it before restoring.

```bash
cp /tmp/egress.bak app/core/egress.py && rm /tmp/egress.bak
.venv/bin/python -m pytest tests/test_egress_guard.py -q   # back to PASS
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/egress.py backend/tests/test_egress_guard.py
git commit -m "feat(core): outbound egress guard -- deny private space, pin resolved IP, refuse redirects"
```

---

## Task 5: Schemas and typed errors

**Files:**
- Create: `backend/app/modules/notification/errors.py`
- Create: `backend/app/modules/notification/schema.py`
- Test: `backend/tests/test_notification_schema.py`

**Interfaces:**
- Produces: `NotificationValidationError`, `NotificationNotFoundError`; `ChannelCreate`, `ChannelUpdate`, `ChannelOut`, `RuleCreate`, `RuleOut`, `AllowlistEntryCreate`, `AllowlistEntryOut`, `QueueMemberOut`, `DeliveryOut`, `TestSendResult`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_schema.py
import pytest
from pydantic import ValidationError

from app.modules.notification.schema import ChannelCreate, ChannelOut, RuleCreate


def test_webhook_channel_requires_a_url():
    with pytest.raises(ValidationError):
        ChannelCreate(kind="webhook", name="ops", config={})


def test_email_channel_requires_host_and_from_address():
    with pytest.raises(ValidationError):
        ChannelCreate(kind="email", name="mail", config={"host": "smtp.example.com"})


def test_channel_config_rejects_an_embedded_secret():
    """Spec §2.2 -- config is returned by the API. A secret placed here would
    be a credential disclosure; secrets belong in the vault."""
    with pytest.raises(ValidationError) as exc:
        ChannelCreate(
            kind="webhook",
            name="ops",
            config={"url": "https://hooks.example.com/x", "password": "hunter2"},
        )
    assert "secret" in str(exc.value).lower() or "password" in str(exc.value).lower()


def test_channel_out_has_no_credential_field():
    # Structural guarantee, not a runtime filter: the response model simply
    # cannot carry a secret.
    assert "credential" not in ChannelOut.model_fields
    assert "password" not in ChannelOut.model_fields


def test_rule_rejects_internal_note_to_requester():
    """Spec §6.4 -- rejected at write time, not filtered at render time."""
    with pytest.raises(ValidationError):
        RuleCreate(
            event_type="ticket.internal_note_added",
            channel_id="00000000-0000-0000-0000-000000000001",
            recipient_kind="requester",
        )


def test_rule_accepts_internal_note_to_queue_members():
    rule = RuleCreate(
        event_type="ticket.internal_note_added",
        channel_id="00000000-0000-0000-0000-000000000001",
        recipient_kind="queue_members",
    )
    assert rule.recipient_kind == "queue_members"


def test_rule_rejects_an_unregistered_event_type():
    with pytest.raises(ValidationError):
        RuleCreate(
            event_type="ticket.assinged",
            channel_id="00000000-0000-0000-0000-000000000001",
            recipient_kind="assignee",
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_schema.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `errors.py`**

```python
# backend/app/modules/notification/errors.py
"""Typed errors. `NotFound` subclasses `ValidationError` so a single handler
in api.py covers both, matching the service desk's errors.py."""


class NotificationValidationError(Exception):
    """A request that cannot be satisfied as written."""


class NotificationNotFoundError(NotificationValidationError):
    """A referenced object does not exist in the caller's org."""
```

- [ ] **Step 4: Implement `schema.py`**

```python
# backend/app/modules/notification/schema.py
"""Pydantic models for notification core (spec §9)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.events import EVENT_TYPES

RecipientKind = Literal["queue_members", "assignee", "requester", "channel_default"]
ChannelKind = Literal["webhook", "email"]

# Keys that must never appear in `config` -- it is returned by the API.
_SECRET_KEYS = {"password", "secret", "token", "api_key", "apikey", "authorization"}


class ChannelCreate(BaseModel):
    kind: ChannelKind
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]
    active: bool = True

    @field_validator("config")
    @classmethod
    def _no_secrets_in_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        found = {k for k in value if k.lower() in _SECRET_KEYS}
        if found:
            raise ValueError(
                f"config must not contain secret keys {sorted(found)}; "
                "secrets belong in the vault (spec §2.2)"
            )
        return value

    @model_validator(mode="after")
    def _config_matches_kind(self) -> "ChannelCreate":
        if self.kind == "webhook":
            if not self.config.get("url"):
                raise ValueError("webhook config requires 'url'")
        else:
            missing = [k for k in ("host", "port", "from_address") if k not in self.config]
            if missing:
                raise ValueError(f"email config requires {missing}")
        return self


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None
    active: bool | None = None

    _no_secrets_in_config = field_validator("config")(
        ChannelCreate._no_secrets_in_config.__func__  # type: ignore[attr-defined]
    )


class ChannelOut(BaseModel):
    """Carries no credential field at all -- a structural guarantee rather than
    a runtime filter (spec §9)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: ChannelKind
    name: str
    config: dict[str, Any]
    active: bool
    has_credential: bool
    created_at: datetime


class RuleCreate(BaseModel):
    event_type: str = Field(max_length=100)
    channel_id: UUID
    recipient_kind: RecipientKind
    queue_id: UUID | None = None
    active: bool = True

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(f"unknown event_type {value!r}")
        return value

    @model_validator(mode="after")
    def _no_internal_content_to_requester(self) -> "RuleCreate":
        # Spec §6.4. Rejected here, at write time, because internal notes have
        # their own event type. A render-time filter would be one template edit
        # away from leaking staff discussion to the requester.
        if self.event_type == "ticket.internal_note_added" and self.recipient_kind == "requester":
            raise ValueError(
                "internal notes cannot be routed to the requester (spec §6.4)"
            )
        return self


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    channel_id: UUID
    recipient_kind: RecipientKind
    queue_id: UUID | None
    active: bool
    created_at: datetime


class AllowlistEntryCreate(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    note: str = Field(default="", max_length=200)

    @field_validator("host")
    @classmethod
    def _host_not_cidr(cls, value: str) -> str:
        # Spec §2.6: an entry names a HOST. Allowlisting a range is how an
        # allowlist quietly becomes a blanket permit.
        if "/" in value:
            raise ValueError("allowlist entries name a host, not a CIDR range")
        return value


class AllowlistEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    host: str
    note: str
    created_at: datetime


class QueueMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    queue_id: UUID


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    channel_id: UUID
    recipient: str
    status: Literal["pending", "sent", "failed", "dead"]
    attempts: int
    last_error: str | None
    last_status_code: int | None
    next_attempt_at: datetime | None
    sent_at: datetime | None
    created_at: datetime


class TestSendResult(BaseModel):
    delivered: bool
    status_code: int | None = None
    error: str | None = None
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_schema.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/notification/errors.py backend/app/modules/notification/schema.py \
        backend/tests/test_notification_schema.py
git commit -m "feat(notification): schemas and typed errors, with secrets barred from channel config"
```

---

## Task 6: `channels.py` — channel CRUD and vault credentials

**Files:**
- Create: `backend/app/modules/notification/channels.py`
- Test: `backend/tests/test_notification_channels.py`

**Interfaces:**
- Consumes: `ChannelCreate`/`ChannelUpdate`/`ChannelOut` (Task 5), `VaultService` (`app.core.vault`), `audit_service` (`app.core.audit`).
- Produces:
  - `async def create_channel(session, *, org_id: UUID, actor_id: UUID, data: ChannelCreate, secret: str | None, vault: VaultService) -> NotificationChannel`
  - `async def list_channels(session, *, org_id: UUID) -> list[NotificationChannel]`
  - `async def get_channel(session, *, org_id: UUID, channel_id: UUID) -> NotificationChannel`
  - `async def update_channel(session, *, org_id, actor_id, channel_id, data: ChannelUpdate, secret: str | None, vault) -> NotificationChannel`
  - `async def delete_channel(session, *, org_id, actor_id, channel_id) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_channels.py
import pytest

from app.modules.notification.channels import (
    create_channel, delete_channel, get_channel, list_channels,
)
from app.modules.notification.errors import NotificationNotFoundError
from app.modules.notification.schema import ChannelCreate


@pytest.mark.asyncio
async def test_create_channel_stores_the_secret_in_the_vault(db_session, org, user, vault):
    channel = await create_channel(
        db_session, org_id=org.id, actor_id=user.id,
        data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/x"}),
        secret="bearer-token-value", vault=vault,
    )
    assert channel.credential_id is not None
    # Invariant #3: the plaintext is nowhere in the row.
    assert "bearer-token-value" not in str(channel.config)


@pytest.mark.asyncio
async def test_create_channel_without_a_secret_leaves_credential_null(db_session, org, user, vault):
    channel = await create_channel(
        db_session, org_id=org.id, actor_id=user.id,
        data=ChannelCreate(kind="webhook", name="open", config={"url": "https://h.example.com/x"}),
        secret=None, vault=vault,
    )
    assert channel.credential_id is None


@pytest.mark.asyncio
async def test_create_channel_writes_an_audit_event(db_session, org, user, vault, audit_events):
    await create_channel(
        db_session, org_id=org.id, actor_id=user.id,
        data=ChannelCreate(kind="webhook", name="ops", config={"url": "https://h.example.com/x"}),
        secret=None, vault=vault,
    )
    assert any(e.action == "notification.channel_created" for e in await audit_events(org.id))


@pytest.mark.asyncio
async def test_get_channel_is_org_scoped(db_session, org, other_org, user, vault):
    """Spec §12.10 -- this assertion must fail if the org_id predicate in
    get_channel is deleted. Nothing upstream rejects this case for us."""
    channel = await create_channel(
        db_session, org_id=other_org.id, actor_id=user.id,
        data=ChannelCreate(kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}),
        secret=None, vault=vault,
    )
    with pytest.raises(NotificationNotFoundError):
        await get_channel(db_session, org_id=org.id, channel_id=channel.id)


@pytest.mark.asyncio
async def test_list_channels_excludes_other_orgs(db_session, org, other_org, user, vault):
    await create_channel(
        db_session, org_id=other_org.id, actor_id=user.id,
        data=ChannelCreate(kind="webhook", name="theirs", config={"url": "https://h.example.com/x"}),
        secret=None, vault=vault,
    )
    assert await list_channels(db_session, org_id=org.id) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_channels.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Follow the service-desk services' shape: explicit `org_id` predicate on every query, audit on every mutation, `NotificationNotFoundError` when a row is absent *or belongs to another org* (the two cases must be indistinguishable to the caller — a distinguishable "exists but not yours" is a cross-tenant existence oracle, which is exactly what the ticket core had to fix for `default_queue_id`).

Store secrets with `vault.store(...)` and keep only the returned credential id. Never write the plaintext to `config`, a log, or the audit metadata.

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_channels.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/channels.py backend/tests/test_notification_channels.py
git commit -m "feat(notification): channel CRUD with vault-held credentials"
```

---

## Task 7: `rules.py` — rules, allowlist, queue membership

**Files:**
- Create: `backend/app/modules/notification/rules.py`
- Test: `backend/tests/test_notification_rules.py`

**Interfaces:**
- Produces: `create_rule`, `list_rules`, `delete_rule`, `add_allowlist_entry`, `list_allowlist`, `delete_allowlist_entry`, `add_queue_member`, `remove_queue_member`, `list_queue_members`, and `async def allowlisted_hosts(session, *, org_id: UUID) -> set[str]` (consumed by Task 9).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_rules.py
import pytest

from app.modules.notification.errors import NotificationValidationError
from app.modules.notification.rules import (
    add_queue_member, allowlisted_hosts, create_rule, list_queue_members,
)
from app.modules.notification.schema import RuleCreate


@pytest.mark.asyncio
async def test_rule_rejects_a_cross_org_queue(db_session, org, other_org, user, a_channel, a_queue_in):
    """Postgres FK checks BYPASS RLS, so a cross-org queue_id is accepted by
    the database. This is the exact bug the ticket core shipped and had to fix
    in review for default_queue_id -- pre-empted here."""
    foreign_queue = await a_queue_in(other_org.id)
    with pytest.raises(NotificationValidationError):
        await create_rule(
            db_session, org_id=org.id, actor_id=user.id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=a_channel.id,
                recipient_kind="queue_members",
                queue_id=foreign_queue.id,
            ),
        )


@pytest.mark.asyncio
async def test_rule_rejects_a_cross_org_channel(db_session, org, other_org, user, a_channel_in):
    foreign_channel = await a_channel_in(other_org.id)
    with pytest.raises(NotificationValidationError):
        await create_rule(
            db_session, org_id=org.id, actor_id=user.id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=foreign_channel.id,
                recipient_kind="assignee",
            ),
        )


@pytest.mark.asyncio
async def test_queue_member_must_be_in_the_same_org(db_session, org, other_org, user, a_queue, a_user_in):
    foreign_user = await a_user_in(other_org.id)
    with pytest.raises(NotificationValidationError):
        await add_queue_member(
            db_session, org_id=org.id, actor_id=user.id,
            queue_id=a_queue.id, user_id=foreign_user.id,
        )


@pytest.mark.asyncio
async def test_allowlisted_hosts_is_org_scoped(db_session, org, other_org, user, add_host):
    await add_host(other_org.id, "internal.theirs")
    assert await allowlisted_hosts(db_session, org_id=org.id) == set()


@pytest.mark.asyncio
async def test_list_queue_members_is_org_scoped(db_session, org, other_org, user, a_queue_in, a_user_in):
    foreign_queue = await a_queue_in(other_org.id)
    assert await list_queue_members(db_session, org_id=org.id, queue_id=foreign_queue.id) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_notification_rules.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Every referenced id (`channel_id`, `queue_id`, `user_id`) is verified to exist **in the caller's org** with an explicit `org_id` predicate before insert — never relying on the FK, which bypasses RLS. Audit each mutation: `notification.rule_created`, `notification.rule_deleted`, `notification.allowlist_added`, `notification.allowlist_removed`, `notification.queue_member_added`, `notification.queue_member_removed`.

- [ ] **Step 4: Run the tests**

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/rules.py backend/tests/test_notification_rules.py
git commit -m "feat(notification): rules, allowlist, and queue membership with cross-org id checks"
```

---

## Task 8: `routing.py` — event to recipients

**Files:**
- Create: `backend/app/modules/notification/routing.py`
- Test: `backend/tests/test_notification_routing.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Recipient: channel_id: UUID; address: str`
  - `async def resolve_recipients(session, *, event: DomainEvent) -> list[Recipient]`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_routing.py
import pytest

from app.modules.notification.routing import resolve_recipients


@pytest.mark.asyncio
async def test_no_rules_yields_no_recipients(db_session, an_event):
    """Spec §6.2 -- zero rules is a valid state, not an error."""
    assert await resolve_recipients(db_session, event=await an_event("ticket.assigned")) == []


@pytest.mark.asyncio
async def test_a_user_matched_twice_receives_one_delivery(db_session, an_event, rule_for, queue_with_member):
    """Spec §6.1 -- expansion is deduplicated. A user who is both assignee and
    queue member gets one email, not two."""
    event = await an_event("ticket.assigned", assignee_is_queue_member=True)
    await rule_for(event.event_type, recipient_kind="assignee")
    await rule_for(event.event_type, recipient_kind="queue_members")
    got = await resolve_recipients(db_session, event=event)
    assert len({r.address for r in got}) == len(got)


@pytest.mark.asyncio
async def test_recipients_outside_the_event_org_are_dropped(db_session, an_event, rule_for, foreign_member_in_queue):
    """Spec §6.3 -- the last point a cross-tenant recipient could enter. A
    notification delivered to the wrong tenant is a data breach."""
    event = await an_event("ticket.assigned")
    await rule_for(event.event_type, recipient_kind="queue_members")
    got = await resolve_recipients(db_session, event=event)
    assert all(r.address != foreign_member_in_queue.email for r in got)


@pytest.mark.asyncio
async def test_internal_note_never_reaches_a_requester(db_session, an_event, rule_for):
    """Spec §12.7 -- asserted on the resolved DELIVERY, not on the rule.

    The write-time rule check (Task 5) is the first line; this is the second.
    If someone later adds a rule path that bypasses schema validation, this
    still fails.
    """
    event = await an_event("ticket.internal_note_added")
    await rule_for("ticket.internal_note_added", recipient_kind="queue_members")
    got = await resolve_recipients(db_session, event=event)
    requester_email = event.payload["requester_email"]
    assert all(r.address != requester_email for r in got)


@pytest.mark.asyncio
async def test_internal_note_payload_carries_no_body(db_session, an_event):
    """Spec §6.4 mechanism 1 -- even a mis-routed delivery cannot disclose the
    text, because the text was never in the event."""
    event = await an_event("ticket.internal_note_added")
    assert "body" not in event.payload


@pytest.mark.asyncio
async def test_inactive_rules_are_skipped(db_session, an_event, rule_for):
    event = await an_event("ticket.assigned")
    await rule_for(event.event_type, recipient_kind="assignee", active=False)
    assert await resolve_recipients(db_session, event=event) == []
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Select active rules for `(event.org_id, event.event_type)` where `queue_id IS NULL OR queue_id = event.payload["queue_id"]`. Expand each `recipient_kind`, re-check every resolved user's `org_id` against `event.org_id`, and deduplicate on `(channel_id, address)`.

- [ ] **Step 4: Run the tests**

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/routing.py backend/tests/test_notification_routing.py
git commit -m "feat(notification): recipient resolution with dedup, org re-check, and the internal-note guard"
```

---

## Task 9: `delivery.py` — one attempt, classified

**Files:**
- Create: `backend/app/modules/notification/delivery.py`
- Test: `backend/tests/test_notification_delivery.py`

**Interfaces:**
- Produces:
  - `BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1500, 7200, 36000)`
  - `MAX_ATTEMPTS: int = 5`
  - `def is_retryable(status_code: int | None, error_reason: str | None) -> bool`
  - `def next_attempt_delay(attempts: int) -> int | None` — `None` when exhausted
  - `async def attempt_delivery(session, *, delivery: NotificationDelivery, vault: VaultService) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_delivery.py
import pytest

from app.modules.notification.delivery import (
    BACKOFF_SECONDS, MAX_ATTEMPTS, is_retryable, next_attempt_delay,
)


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
def test_transient_status_codes_retry(code: int):
    assert is_retryable(code, None) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_are_terminal(code: int):
    # Retrying a 403 five times is five more chances to be wrong, not one
    # chance to succeed.
    assert is_retryable(code, None) is False


@pytest.mark.parametrize(
    "reason", ["blocked_private", "dns_failure", "redirect_refused", "scheme_not_allowed"]
)
def test_egress_rejections_are_terminal(reason: str):
    """Spec §7 -- an allowlist-blocked host must not be retried. Five retries
    of a blocked destination are five more SSRF attempts."""
    assert is_retryable(None, reason) is False


def test_backoff_schedule_matches_the_spec():
    # Spec §7: 1m, 5m, 25m, 2h, 10h.
    assert BACKOFF_SECONDS == (60, 300, 1500, 7200, 36000)
    assert len(BACKOFF_SECONDS) == MAX_ATTEMPTS


def test_delay_is_none_once_attempts_are_exhausted():
    assert next_attempt_delay(MAX_ATTEMPTS) is None


def test_delay_grows_monotonically():
    delays = [next_attempt_delay(i) for i in range(MAX_ATTEMPTS)]
    assert delays == sorted(d for d in delays if d is not None)
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# backend/app/modules/notification/delivery.py
"""One delivery attempt, and the retry classification around it (spec §7).

Retryable-vs-terminal is not cosmetic. An egress rejection retried five times
is five more attempts to reach a blocked destination, and a 403 will never
become a 200. Only conditions that can plausibly change on their own retry.
"""

BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1500, 7200, 36000)  # 1m 5m 25m 2h 10h
MAX_ATTEMPTS: int = len(BACKOFF_SECONDS)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_retryable(status_code: int | None, error_reason: str | None) -> bool:
    if error_reason in {"blocked_private", "dns_failure", "redirect_refused", "scheme_not_allowed"}:
        return False
    if error_reason in {"timeout", "connection_reset"}:
        return True
    if status_code is None:
        return False
    return status_code in _RETRYABLE_STATUS


def next_attempt_delay(attempts: int) -> int | None:
    if attempts >= MAX_ATTEMPTS:
        return None
    return BACKOFF_SECONDS[attempts]
```

Then `attempt_delivery`: check out the channel credential from the vault, call `post_json` (webhook) or send via `asyncio.to_thread(smtplib...)` (email — **not** through the egress guard, see the plan's decided asymmetry), record `status`/`attempts`/`last_error`/`last_status_code`/`next_attempt_at`, and set `sent_at` only on success. Add the `X-Toeshee-Event-Id` header on every webhook so consumers can dedupe at-least-once delivery.

- [ ] **Step 4: Run the tests**

Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/delivery.py backend/tests/test_notification_delivery.py
git commit -m "feat(notification): delivery attempts with retryable-vs-terminal classification"
```

---

## Task 10: `api.py` — routes and registration

**Files:**
- Create: `backend/app/modules/notification/api.py`
- Modify: `backend/app/api/__init__.py` (register the router)
- Test: `backend/tests/test_notification_api.py`

**Interfaces:**
- Consumes: Tasks 5–9.
- Produces: the 14 routes in spec §9, all under `/api/v1/notifications`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_api.py
import pytest


@pytest.mark.asyncio
async def test_every_route_requires_a_permission(app):
    """Derived from the router, not a hand-maintained list -- a route added
    later without a permission fails this test. Carried from the ticket core."""
    from app.core.rbac import route_permission

    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/notifications")]
    assert routes, "no notification routes registered"
    for route in routes:
        assert route_permission(route) is not None, f"{route.path} has no permission"


@pytest.mark.asyncio
async def test_mutations_require_admin_not_read(client, read_only_token):
    body = {"kind": "webhook", "name": "x", "config": {"url": "https://h.example.com/x"}}
    got = await client.post("/api/v1/notifications/channels", json=body, headers=read_only_token)
    assert got.status_code == 403


@pytest.mark.asyncio
async def test_channel_response_never_contains_a_secret(client, admin_token, vault):
    body = {
        "kind": "webhook", "name": "ops",
        "config": {"url": "https://h.example.com/x"}, "secret": "bearer-token-value",
    }
    got = await client.post("/api/v1/notifications/channels", json=body, headers=admin_token)
    assert got.status_code == 201
    assert "bearer-token-value" not in got.text
    assert "credential_id" not in got.json()


@pytest.mark.asyncio
async def test_test_send_is_subject_to_the_egress_guard(client, admin_token, private_url_channel):
    """Spec §12.6 -- 'it's only a test' is exactly how an SSRF bypass ships."""
    got = await client.post(
        f"/api/v1/notifications/channels/{private_url_channel.id}/test", headers=admin_token
    )
    assert got.status_code == 200
    assert got.json()["delivered"] is False
    assert got.json()["error"] == "blocked_private"


@pytest.mark.asyncio
async def test_deliveries_list_never_exposes_a_response_body(client, admin_token, a_failed_delivery):
    got = await client.get("/api/v1/notifications/deliveries", headers=admin_token)
    assert got.status_code == 200
    for row in got.json()["items"]:
        assert len(row["last_error"] or "") <= 500
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — no notification routes registered.

- [ ] **Step 3: Implement**

Declare literal segments before `{id}` parameters (`/channels/{id}/test` after `/channels`, `/allowlist` before `/allowlist/{id}`) — the Phase-11/12 route-ordering lesson. Every route carries `require_permission`. Route handlers translate errors and nothing more; logic stays in the services.

- [ ] **Step 4: Run the tests**

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/api.py backend/app/api/__init__.py \
        backend/tests/test_notification_api.py
git commit -m "feat(notification): REST API with router-derived permission coverage"
```

---

## Task 11: Worker tasks

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_notification_worker.py`

**Interfaces:**
- Produces: `async def drain_outbox(org_id, app_engine) -> int`, `async def deliver_pending(org_id, app_engine, vault) -> tuple[int, int]`, and the two thin Celery shells around them.

Follow `app/worker.py`'s established shape: async logic in a testable function, the Celery task a thin scheduling shell (the file's docstring states this convention).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_notification_worker.py
import pytest

from app.worker import deliver_pending, drain_outbox


@pytest.mark.asyncio
async def test_drain_marks_events_processed(db_session, org, app_engine, an_event, rule_for):
    await an_event("ticket.assigned")
    await rule_for("ticket.assigned", recipient_kind="assignee")
    assert await drain_outbox(org.id, app_engine) == 1


@pytest.mark.asyncio
async def test_drain_is_idempotent(db_session, org, app_engine, an_event, rule_for):
    await an_event("ticket.assigned")
    await rule_for("ticket.assigned", recipient_kind="assignee")
    await drain_outbox(org.id, app_engine)
    assert await drain_outbox(org.id, app_engine) == 0, "reprocessed an already-drained event"


@pytest.mark.asyncio
async def test_an_event_with_no_rules_is_still_marked_processed(db_session, org, app_engine, an_event):
    """Spec §6.2. If it were left unprocessed it would be re-drained forever."""
    await an_event("ticket.assigned")
    await drain_outbox(org.id, app_engine)
    assert await drain_outbox(org.id, app_engine) == 0


@pytest.mark.asyncio
async def test_a_stuck_claim_is_returned(db_session, org, app_engine, vault, stuck_delivery):
    """A worker that died mid-attempt leaves a claimed-but-unsent row. Without
    the sweep it is never retried -- the same failure app/worker.py's existing
    STUCK_DISPATCH_AFTER sweep exists to prevent."""
    _, returned = await deliver_pending(org.id, app_engine, vault)
    assert returned == 1


@pytest.mark.asyncio
async def test_exhausted_delivery_becomes_dead(db_session, org, app_engine, vault, failing_delivery_at_max):
    await deliver_pending(org.id, app_engine, vault)
    await db_session.refresh(failing_delivery_at_max)
    assert failing_delivery_at_max.status == "dead"
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `ImportError: cannot import name 'drain_outbox'`.

- [ ] **Step 3: Implement**

Claim with `SELECT … FOR UPDATE SKIP LOCKED`. **The delivery attempt happens outside the claim transaction** — an outbound HTTP call inside an open transaction holds a database connection for the duration of someone else's timeout. That is why the stuck-claim sweep exists.

- [ ] **Step 4: Run the tests**

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/worker.py backend/tests/test_notification_worker.py
git commit -m "feat(notification): outbox drain and delivery worker tasks"
```

---

## Task 12: Ticket core publishes events

**Files:**
- Modify: `backend/app/modules/service_desk/transitions.py` (assign, status change)
- Modify: `backend/app/modules/service_desk/threads.py` (comment added)
- Modify: `backend/app/modules/service_desk/approvals.py` (approval requested)
- Test: `backend/tests/test_service_desk_events.py`

This is the **only** change to ticket core. Add `publish(...)` calls inside the transaction each service already has. Do not import anything from `app.modules.notification`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_service_desk_events.py
import pytest
from sqlalchemy import select

from app.models import DomainEvent


@pytest.mark.asyncio
async def test_assigning_publishes_an_event(db_session, org, user, a_ticket, assign):
    await assign(a_ticket, to=user)
    got = (await db_session.execute(select(DomainEvent))).scalars().all()
    assert [e.event_type for e in got] == ["ticket.assigned"]


@pytest.mark.asyncio
async def test_a_public_comment_and_an_internal_note_use_different_event_types(
    db_session, a_ticket, add_comment
):
    """Spec §6.4 -- the write-time leak guard depends on these being distinct."""
    await add_comment(a_ticket, body="visible", internal=False)
    await add_comment(a_ticket, body="staff only", internal=True)
    got = (await db_session.execute(select(DomainEvent))).scalars().all()
    types = {e.event_type for e in got}
    assert types == {"ticket.comment_added", "ticket.internal_note_added"}


@pytest.mark.asyncio
async def test_internal_note_event_omits_the_body(db_session, a_ticket, add_comment):
    await add_comment(a_ticket, body="staff only -- do not disclose", internal=True)
    got = (await db_session.execute(select(DomainEvent))).scalars().one()
    assert "staff only" not in str(got.payload)


@pytest.mark.asyncio
async def test_a_rolled_back_assignment_publishes_nothing(db_session, a_ticket, assign_then_fail):
    """Spec §12.8 -- the transactional property, end to end rather than at the
    publish() unit level."""
    with pytest.raises(Exception):
        await assign_then_fail(a_ticket)
    assert (await db_session.execute(select(DomainEvent))).scalars().all() == []


def test_service_desk_does_not_import_notification():
    """Spec §1 / §12.12 -- the dependency points one way only."""
    import pathlib

    root = pathlib.Path("app/modules/service_desk")
    for path in root.rglob("*.py"):
        assert "modules.notification" not in path.read_text(), path
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — no `DomainEvent` rows.

- [ ] **Step 3: Implement**

In each service, after the state change and inside the same transaction:

```python
from app.core.events import publish

# in transitions.assign_ticket, after the assignment is applied:
await publish(
    session,
    org_id=org_id,
    event_type="ticket.assigned",
    subject_type="ticket",
    subject_id=ticket.id,
    payload={
        "ticket_number": ticket.number,
        "subject": ticket.subject,
        "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
        "assignee_id": str(ticket.assignee_id) if ticket.assignee_id else None,
        "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
    },
    actor_user_id=actor_id,
)
```

For `threads.add_comment`, choose the event type from the comment's visibility and **omit the body for internal notes**:

```python
is_internal = comment.visibility == "internal"
await publish(
    session,
    org_id=org_id,
    event_type="ticket.internal_note_added" if is_internal else "ticket.comment_added",
    subject_type="ticket",
    subject_id=ticket.id,
    payload={
        "ticket_number": ticket.number,
        "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
        "author_id": str(comment.author_id) if comment.author_id else None,
        # Spec §6.4 mechanism 1: an internal note's text never enters the
        # event, so even a mis-routed delivery cannot disclose it.
        **({} if is_internal else {"body": comment.body}),
    },
    actor_user_id=actor_id,
)
```

- [ ] **Step 4: Run the tests, then the full backend suite**

```bash
cd backend
.venv/bin/python -m pytest tests/test_service_desk_events.py -q
.venv/bin/python -m pytest tests/ -q
```

Expected: both PASS. The full suite matters here — this task touches shipped ticket-core services.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/service_desk/ backend/tests/test_service_desk_events.py
git commit -m "feat(service-desk): publish domain events for assignment, status, comments, approvals"
```

---

## Task 13: Frontend admin surface

**Files:**
- Create: `frontend/src/modules/notification/types.ts`, `api.ts`, `NotificationAdminPage.tsx`, `ChannelsPanel.tsx`, `RulesPanel.tsx`, `DeliveriesPanel.tsx`, `NotificationNav.tsx`
- Create: `frontend/src/modules/notification/__tests__/notification.test.tsx`
- Modify: the app router and module nav, following the service-desk registration

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/modules/notification/__tests__/notification.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

describe("notification admin", () => {
  it("hides mutation controls without notification.admin", async () => {
    renderAdmin({ permissions: ["notification.read"] });
    await waitFor(() => expect(screen.queryByRole("button", { name: /add channel/i })).toBeNull());
  });

  it("surfaces the dead-letter count prominently", async () => {
    renderAdmin({ permissions: ["notification.read"], deadCount: 3 });
    expect(await screen.findByText(/3 dead/i)).toBeInTheDocument();
  });

  it("warns that an allowlist entry permits reaching an internal host", async () => {
    renderAdmin({ permissions: ["notification.admin"] });
    expect(await screen.findByText(/internal host/i)).toBeInTheDocument();
  });

  it("degrades quietly when the deliveries endpoint 403s", async () => {
    api.listDeliveries = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderAdmin({ permissions: ["notification.read"] });
    await waitFor(() => expect(screen.queryByText(/error/i)).toBeNull());
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && npm test -- notification`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Strict TypeScript, no `any`; TanStack Query for server state; badge and empty-state conventions carried from the service-desk module. The UI hides what the caller cannot use — **the server rejects it regardless** (spec §11).

- [ ] **Step 4: Run the gates**

```bash
cd frontend && npx tsc --noEmit && npm test
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/modules/notification/ frontend/src/
git commit -m "feat(notification): admin surface for channels, rules, allowlist, and deliveries"
```

---

## Final gate

Run all five, in the foreground, before declaring the plan complete:

```bash
cd backend
.venv/bin/python -m pytest tests/ -q          # expect > 1790 passing, 0 failed
.venv/bin/python -m mypy app/                 # expect: Success
.venv/bin/python -m ruff check app/ tests/    # expect: All checks passed
cd ../frontend
npx tsc --noEmit                              # expect: no output
npm test                                      # expect: > 192 passing
```

Then confirm the working tree is clean (`git status --porcelain` empty, no `*.bak`) and every task is committed.

---

## Self-review

**Spec coverage.** §2.1 → Tasks 1–3. §2.2–2.6 → Tasks 1, 2, 6, 7. §3 → Task 3. §4 (all four properties) → Task 4. §5 credentials → Task 6; test send → Task 10. §6 routing → Task 8; §6.4 both mechanisms → Tasks 5 (write-time), 8 (resolution), 12 (payload omission). §7 → Tasks 9, 11. §8 RBAC/tenancy/audit → Tasks 1, 6, 7, 10. §9 API → Task 10. §10 worker → Task 11. §11 frontend → Task 13. §12's twelve required tests → 12.1/12.3/12.5 Task 4; 12.2 Task 4; 12.4 Task 4; 12.6 Task 10; 12.7 Tasks 8, 12; 12.8 Tasks 3, 12; 12.9 Tasks 9, 11; 12.10 Tasks 6, 7 plus the auto-coverage in `test_rls.py`; 12.11 Tasks 6, 10; 12.12 Task 12. §13 migration → Task 1.

**Gap found and closed.** §12.10 requires a per-table isolation test that fails when its `org_id` predicate is deleted. `test_rls.py`'s auto-coverage proves the *policy* exists but not that *application queries* carry the predicate — the ticket core shipped an isolation test that passed for the wrong reason precisely because of that gap. Tasks 6 and 7 therefore carry hand-written cross-org tests, and the Global Constraints call the distinction out explicitly.

**Placeholder scan.** No TBD/TODO. Tasks 6, 7, 9 (`attempt_delivery`), 10, and 13 describe implementations in prose rather than full code, because each follows a pattern already established in `app/modules/service_desk/` that the implementer can read directly; every one of them has complete test code, which is what pins the behavior. All types and function names used in later tasks are defined in the Interfaces block of an earlier one.

**Type consistency.** `EgressBlocked.reason` values are identical in Task 4 (raised), Task 9 (`is_retryable`), and Task 10 (`TestSendResult.error`). `BACKOFF_SECONDS`/`MAX_ATTEMPTS` are defined once in Task 9 and consumed in Task 11. `EVENT_TYPES` is defined in Task 3 and consumed by Task 5's validator and Task 12's publishers. `Recipient` (Task 8) is consumed by Task 11.

**Known deviation from the spec text.** The SMTP/egress asymmetry in Global Constraints is a decision this plan records that spec §4 does not state. It should be folded into the spec when next touched.

# Email Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person raise and continue a ticket from their mail client, by polling a customer-owned IMAP mailbox — without the mailbox becoming an unauthenticated injection channel into other people's tickets.

**Architecture:** A Celery beat task polls each configured `email_mailbox` over IMAP-SSL (outbound only, no inbound port — spec §0.1). Each fetched message is parsed with the stdlib `email` package, its sender resolved against `users`, and its reply token (if any) resolved against `ticket.email_token`. A disposition engine then calls the *existing* ticket-core services — `tickets.create_ticket`, `threads.add_comment`, `threads.add_attachment`, `transitions.change_status` — and writes an `inbound_message` row **in the same transaction**. That row's `UNIQUE (org_id, message_id)` is the idempotency key that makes IMAP redelivery safe. This module adds no ticket rule, no status, and no transition.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Postgres RLS. Stdlib `imaplib` and `email` only — **no new dependency**.

## Global Constraints

Every task's requirements implicitly include this section.

- **Authority is `docs/modules/email-intake/spec.md`.** Where this plan and the spec disagree, the spec wins — except for the three corrections in "Spec corrections" below, which were found by probing the live code and which the spec is wrong about.
- **No new dependency.** Stdlib `imaplib`, `email`, `secrets` only (spec §7).
- **Migration `0032`.** Current head is `0031` (`0031_portal_audit_actor_isolation.py`). `down_revision` is the bare string `"0031"`, not a long literal — every migration in this repo uses the short form and `alembic upgrade head` fails otherwise.
- **`ticket_queue` is singular.** So are `ticket`, `ticket_comment`, `ticket_attachment`, `ticket_category`. `users`, `organizations`, `notification_channels`, `audit_events`, `domain_events` are plural. Do not guess a table name — grep for `__tablename__`.
- **Tenancy GUC is `app.current_org`**, read as `current_setting('app.current_org', true)::uuid`. There is no `app.org_id`. Every new table gets `ENABLE + FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy with **both** `USING` and `WITH CHECK` (invariant #5).

  **Correction, verified by execution against the live database.** An earlier draft of this plan claimed a `USING`-only policy "filters reads while permitting an INSERT under any `org_id`, which is a tenancy hole". **That is false.** For an `ALL`-command policy, PostgreSQL reuses the `USING` expression as the `WITH CHECK` expression when the latter is omitted, so a `USING`-only policy *does* constrain inserts. With `WITH CHECK` deleted, the cross-org insert test still passes.

  Write `WITH CHECK` explicitly anyway — it matches `0002_rls.py` and `0025_notification_core.py`, and the implicit reuse only holds while the two predicates are meant to be identical; the day a policy needs a different read predicate, an omitted check silently follows the new one. But do not describe its absence as a tenancy hole, in a migration comment or anywhere else.

  **The mutations that actually falsify these tests** are `WITH CHECK (true)` for an insert/`WITH CHECK` test, and `USING (true)` for a read-isolation test. Wherever this plan says to watch a tenancy test go red, use those.
- **No credential in any output.** Never in an API response, a log line, `email_mailbox.last_error`, or an audit `metadata` dict (invariant #3).
- **Async throughout**, full type hints, business logic in services and not in route handlers. `mypy` and `ruff` must be clean.
- **Blocking I/O goes through `asyncio.to_thread`.** This process also serves the agent gateway's WebSocket connections on the same event loop; a blocking `imaplib` call inline stalls every other connection for the length of the IMAP round-trip. `app/modules/notification/delivery.py:_send_smtp_sync` is the precedent to copy.
- **Tests use the `api_env` fixture**, `AsyncSession(api_env.admin_engine)` for setup, and `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`.
- **Test commands:** `cd backend && pytest tests/test_<name>.py -v`; full suite `pytest -q`; types `mypy app`; lint `ruff check app tests`.
- **Every test must be able to fail.** A test that asserts on a call rather than on stored state, or that iterates a collection defined in its own file, proves nothing. Where a step says "verify it fails", actually run it and read the failure text before implementing.

## Spec corrections

Three things the spec asserts that the live code contradicts. Found by probing `lifecycle.py`, `threads.py` and `delivery.py` before this plan was written. Each is implemented per this plan, and Task 10 amends the spec to match.

**C1 — Reopen must happen BEFORE the comment, not after.** Spec §8 reads "Public comment **and** reopen". `lifecycle.TERMINAL_STATUSES` is `frozenset({"closed", "cancelled", "rejected"})`, and `threads.add_comment` calls `_reject_if_terminal_for_thread_content`, so **commenting on a `closed` ticket raises `TicketValidationError`**. Implementing §8 in the order written makes every reply to a closed ticket fail. Order is: `change_status` first, then `add_comment`, in one transaction.

**C2 — `fulfilled -> in_progress` is a reopen edge the spec omits.** §8's parenthetical lists only `resolved -> open`, `closed -> open`, `closed -> in_progress`. For a `request`-type ticket the resolved-equivalent status is `fulfilled`, and `lifecycle.py` defines `"fulfilled": frozenset({"in_progress", "closed", "cancelled"})`. Without this edge, a reply to a fulfilled request has no defined behaviour. The reopen target is derived from `(type, status)` in one place (Task 6) and every edge is checked against `lifecycle.can_transition` rather than hardcoded.

**C3 — The Reply-To header does not exist yet, and without it this module is unreachable.** Spec §5.2 requires outbound notifications to carry `Reply-To: <local>+t-<token>@<domain>`, but `delivery.py:_send_smtp_sync` has no `reply_to` parameter and nothing mints `ticket.email_token`. Ship the rest of this module without Task 8 and the entire reply path — token resolution, reopen, the "token is not authority" check — is dead code no test would catch, because every test would construct the token itself. §1's "they share no code and no table" describes config and tables; the Reply-To header is a spec-mandated cross-module edit and Task 8 is where it happens.

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/migrations/versions/0032_email_intake.py` | Two tables, `ticket.email_token`, the `users` lower(email) index, RLS, permissions |
| `backend/app/modules/email_intake/__init__.py` | Router registration (`app.include_router(router, prefix="/api/v1")`) |
| `backend/app/modules/email_intake/models.py` | `EmailMailbox`, `InboundMessage` |
| `backend/app/modules/email_intake/errors.py` | `EmailIntakeError`, `MailboxNotFoundError`, `MailboxValidationError` |
| `backend/app/modules/email_intake/schema.py` | Pydantic request/response models |
| `backend/app/modules/email_intake/mailboxes.py` | Mailbox CRUD + vault credential lifecycle + audit |
| `backend/app/modules/email_intake/parsing.py` | Pure MIME parsing: body, HTML→text, quote trim, attachments, loop detection. **No DB.** |
| `backend/app/modules/email_intake/identity.py` | Sender resolution and reply-token resolution. DB reads only. |
| `backend/app/modules/email_intake/intake.py` | The disposition engine — §8's table, executed |
| `backend/app/modules/email_intake/imap.py` | `imaplib` transport behind a Protocol, so tests need no mail server |
| `backend/app/modules/email_intake/poller.py` | Per-mailbox claim, poll loop, `\Seen` after commit, `last_error` |
| `backend/app/modules/email_intake/api.py` | 6 routes, each `require_permission`-gated |

**Modified (backend):** `app/core/rbac.py` (2 permission codes), `app/main.py` (register module), `app/worker.py` (beat entry + task), `app/modules/notification/delivery.py` (Task 8, Reply-To), `app/models/__init__.py` if it re-exports module models.

**Created (frontend):** `frontend/src/modules/email-intake/` — `MailboxList.tsx`, `MailboxForm.tsx`, `MessageLog.tsx`, `api.ts`, `types.ts`.

**Tests:** `tests/test_email_intake_migration.py`, `_models.py`, `_mailboxes.py`, `_parsing.py`, `_identity.py`, `_intake.py`, `_poller.py`, `_api.py`, `_reply_to.py`.

---

## Task 1: Migration 0032 — tables, column, index, RLS, permissions

**Files:**
- Create: `backend/migrations/versions/0032_email_intake.py`
- Create: `backend/app/modules/email_intake/__init__.py` (empty for now — Task 9 adds the router)
- Create: `backend/app/modules/email_intake/models.py`
- Modify: `backend/app/core/rbac.py` (add 2 permission codes to the descriptions dict, near line 85)
- Test: `backend/tests/test_email_intake_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EmailMailbox`, `InboundMessage` ORM classes; `ticket.email_token`; permission codes `email_intake.read`, `email_intake.admin`; migration revision `"0032"`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_migration.py`:

```python
"""Migration 0032: shape, RLS, and permission seeding (spec §2, §9, §12).

The two RLS tests are BEHAVIOURAL -- they insert two orgs' rows through the
RLS-constrained app engine and assert on what each org can see. A test that
merely asserts `relrowsecurity` is true passes against a policy whose
predicate has been deleted.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.modules.email_intake.models import EmailMailbox, InboundMessage


async def test_ticket_has_a_unique_nullable_email_token(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'ticket' AND column_name = 'email_token'"
                )
            )
        ).one()
    assert row.is_nullable == "YES"
    assert row.character_maximum_length == 43


async def test_users_has_a_lower_email_index(admin_engine) -> None:
    """Spec §5.1: sender resolution runs on every inbound message and would
    otherwise sequentially scan the org's users on the poller's hot path."""
    async with AsyncSession(admin_engine) as session:
        indexdefs = (
            (
                await session.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = 'users'")
                )
            )
            .scalars()
            .all()
        )
    assert any("lower(email" in d and "org_id" in d for d in indexdefs), indexdefs


@pytest.mark.parametrize("table", ["email_mailbox", "inbound_message"])
async def test_rls_is_enabled_and_forced(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_a_mailbox_is_invisible_to_another_org(api_env) -> None:
    """Behavioural: fails if the tenant_isolation USING predicate is removed."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    async with org_session(org_a, engine=api_env.app_engine) as session:
        session.add(
            EmailMailbox(
                org_id=org_a,
                address="helpdesk@a.test",
                host="imap.a.test",
                port=993,
                username="helpdesk@a.test",
                credential_id=await _seed_credential(session, org_a, actor_id),
                default_category_id=category_id,
                created_by=actor_id,
            )
        )

    async with org_session(org_a, engine=api_env.app_engine) as session:
        assert len((await session.execute(select(EmailMailbox))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine) as session:
        assert (await session.execute(select(EmailMailbox))).scalars().all() == []


async def test_a_mailbox_cannot_be_inserted_under_another_orgs_id(api_env) -> None:
    """WITH CHECK, not just USING. Without it the row above would insert
    happily under org_b's id while being invisible to org_b's reads."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    with pytest.raises(ProgrammingError):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            credential_id = await _seed_credential(session, org_a, actor_id)
            session.add(
                EmailMailbox(
                    org_id=org_b,  # <-- the other org
                    address="helpdesk@b.test",
                    host="imap.b.test",
                    port=993,
                    username="helpdesk@b.test",
                    credential_id=credential_id,
                    default_category_id=category_id,
                    created_by=actor_id,
                )
            )


async def test_inbound_message_is_unique_per_org_and_message_id(api_env) -> None:
    """Spec §4 rule 1: this constraint IS the idempotency mechanism."""
    async with AsyncSession(api_env.admin_engine) as session:
        indexdefs = (
            (
                await session.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = 'inbound_message'")
                )
            )
            .scalars()
            .all()
        )
    assert any(
        "UNIQUE" in d and "org_id" in d and "message_id" in d for d in indexdefs
    ), indexdefs


@pytest.mark.parametrize("code", ["email_intake.read", "email_intake.admin"])
async def test_permission_is_seeded_to_org_admin(admin_engine, code: str) -> None:
    async with AsyncSession(admin_engine) as session:
        granted = (
            await session.execute(
                text(
                    "SELECT 1 FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'org-admin' AND p.code = :c"
                ),
                {"c": code},
            )
        ).scalar_one_or_none()
    assert granted == 1
```

Add the three seeding helpers at the bottom of the same file. Keep them here and import them from the later test modules — they are used by Tasks 2, 5, 6 and 7:

```python
async def _seed_user(api_env, org_id: UUID, email: str | None = None) -> UUID:
    from app.models import User

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=email or f"user-{uuid4().hex[:8]}@test.local",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _seed_category(api_env, org_id: UUID) -> UUID:
    from app.modules.service_desk.models import TicketCategory, TicketQueue

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = TicketQueue(org_id=org_id, name=f"q-{uuid4().hex[:6]}")
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=org_id, name=f"c-{uuid4().hex[:6]}", default_queue_id=queue.id
        )
        session.add(category)
        await session.commit()
        return category.id


async def _seed_credential(session: AsyncSession, org_id: UUID, actor_id: UUID) -> UUID:
    from app.models.vault import Credential

    credential = Credential(
        org_id=org_id,
        name="email-mailbox-secret",
        type="email_mailbox",
        vault_ref="",
        created_by=actor_id,
    )
    session.add(credential)
    await session.flush()
    return credential.id
```

Before running, open `backend/app/modules/service_desk/models.py` and confirm the exact constructor kwargs for `TicketQueue`/`TicketCategory` (required NOT NULL columns without defaults). Fix the helpers if they differ — do not guess.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_migration.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'app.modules.email_intake'`.

- [ ] **Step 3: Write the models**

Create `backend/app/modules/email_intake/__init__.py` as an empty file (Task 9 fills it in), then `backend/app/modules/email_intake/models.py`:

```python
"""Email intake: per-org inbound mailbox config and the inbound-message
disposition record.

docs/modules/email-intake/spec.md §2. Every column here must match
`backend/migrations/versions/0032_email_intake.py` exactly in type, length
and nullability -- `test_schema_drift.py` compares `Base.metadata` to the
live schema. The drift test does NOT compare defaults, so any Python-side
default below is load-bearing on its own.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Spec §2.2. `created` etc. are the seven dispositions; every fetched message
# gets exactly one row with one of these -- nothing is ever silently dropped,
# because a helpdesk mailbox that quietly eats mail is indistinguishable from
# one nobody is emailing.
DISPOSITIONS: frozenset[str] = frozenset(
    {
        "created",
        "commented",
        "reopened",
        "quarantined",
        "refused_loop",
        "refused_rate",
        "refused_parse",
    }
)


class EmailMailbox(Base):
    """Per-org inbound IMAP configuration (spec §2.1)."""

    __tablename__ = "email_mailbox"
    __table_args__ = (UniqueConstraint("org_id", "address", name="uq_email_mailbox_org_address"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    address: Mapped[str] = mapped_column(String(320))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    use_ssl: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    folder: Mapped[str] = mapped_column(String(100), server_default="INBOX", default="INBOX")
    username: Mapped[str] = mapped_column(String(320))
    # NOT NULL: there is no unauthenticated IMAP (spec §2.1).
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id"))
    # NOT NULL and load-bearing: `ticket.category_id` is NOT NULL and an email
    # carries no category, so without a configured default no inbound message
    # could ever become a ticket (spec §2.1).
    default_category_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_category.id"))
    reopen_window_days: Mapped[int] = mapped_column(Integer, server_default="14", default=14)
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(default=None)
    # A bounded, SANITIZED excerpt -- never a raw server response, never the
    # password (spec §2.1, invariant #3). `_sanitize_excerpt` in poller.py is
    # the only thing that writes it.
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class InboundMessage(Base):
    """One row per FETCHED message -- the idempotency key and the disposition
    record (spec §2.2, §4)."""

    __tablename__ = "inbound_message"
    __table_args__ = (
        # THIS CONSTRAINT IS THE IDEMPOTENCY MECHANISM (spec §4 rule 1).
        # IMAP has no transactions: the fetch, the ticket write and the \Seen
        # flag are three operations across two systems. A redelivered message
        # collides here instead of creating a second ticket.
        UniqueConstraint("org_id", "message_id", name="uq_inbound_message_org_message_id"),
        CheckConstraint(
            "disposition IN ('created', 'commented', 'reopened', 'quarantined', "
            "'refused_loop', 'refused_rate', 'refused_parse')",
            name="ck_inbound_message_disposition_valid",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    mailbox_id: Mapped[UUID] = mapped_column(ForeignKey("email_mailbox.id"))
    # RFC 5322's line-length limit. A message with no Message-ID gets a
    # synthesised one (parsing.py) so it still dedupes.
    message_id: Mapped[str] = mapped_column(String(998))
    from_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(512), server_default="", default="")
    received_at: Mapped[datetime] = mapped_column()
    disposition: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(200), server_default="", default="")
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket.id"), default=None)
    sender_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    # The PARSED, plain-text body -- never the raw RFC822 message. Retaining
    # full raw messages indefinitely is a privacy liability (complete headers,
    # routing, every recipient) for forensics this product has not asked for
    # (spec §2.2).
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

`claimed_at` is not in the spec's §2.1 column list; it is required by §4's "claims per mailbox, following `app/worker.py`'s established `SELECT … FOR UPDATE SKIP LOCKED` pattern", which cannot be implemented without it. Task 10 records it in the spec.

Then add `email_token` to the existing `Ticket` model in `backend/app/modules/service_desk/models.py`, next to `origin`:

```python
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
```

- [ ] **Step 4: Write the migration**

Create `backend/migrations/versions/0032_email_intake.py`:

```python
"""email intake: mailbox config, inbound message log, reply token

docs/modules/email-intake/spec.md §2, §5.1, §9, §12.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = ["email_mailbox", "inbound_message"]

PERMISSIONS = [
    (
        "email_intake.read",
        "email_intake",
        "read",
        "View mailbox config, poll status, and the inbound message log",
    ),
    (
        "email_intake.admin",
        "email_intake",
        "admin",
        "Create, modify and delete an inbound mailbox and its credential",
    ),
]
# `email_intake.admin` is HIGH-PRIVILEGE (spec §9): it sets which mailbox the
# platform authenticates to and which category inbound tickets land in. Admin
# tier only, never technician. Both codes go to super-admin/org-admin, as
# every other module's migration does, so the full-permission invariant
# `test_org_admin_has_all_permissions` continues to hold.
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": [c for c, _, _, _ in PERMISSIONS],
    "org-admin": [c for c, _, _, _ in PERMISSIONS],
}


def upgrade() -> None:
    op.create_table(
        "email_mailbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("use_ssl", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("folder", sa.String(100), server_default="INBOX", nullable=False),
        sa.Column("username", sa.String(320), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("default_category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_category.id"), nullable=False),
        sa.Column("reopen_window_days", sa.Integer(), server_default="14", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "address", name="uq_email_mailbox_org_address"),
    )

    op.create_table(
        "inbound_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("email_mailbox.id"), nullable=False),
        sa.Column("message_id", sa.String(998), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(512), server_default="", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(200), server_default="", nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket.id"), nullable=True),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "message_id", name="uq_inbound_message_org_message_id"),
        sa.CheckConstraint(
            "disposition IN ('created', 'commented', 'reopened', 'quarantined', "
            "'refused_loop', 'refused_rate', 'refused_parse')",
            name="ck_inbound_message_disposition_valid",
        ),
    )

    op.add_column("ticket", sa.Column("email_token", sa.String(43), nullable=True))
    op.create_unique_constraint("uq_ticket_email_token", "ticket", ["email_token"])

    # Spec §5.1: sender resolution runs on EVERY inbound message. Additive and
    # behaviour-neutral -- adds no column and no constraint to a kernel table.
    op.execute("CREATE INDEX ix_users_org_lower_email ON users (org_id, lower(email))")

    # Standard tenant_isolation pattern (migrations/versions/0002_rls.py).
    # BOTH clauses: USING alone filters reads while permitting an INSERT under
    # any org_id (invariant #5).
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )

    for code, resource, action, description in PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO permissions (id, code, resource, action, description) "
                "VALUES (gen_random_uuid(), :code, :resource, :action, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, resource=resource, action=action, description=description)
        )
    for role, codes in ROLE_GRANTS.items():
        for code in codes:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT r.id, p.id FROM roles r, permissions p "
                    "WHERE r.name = :role AND r.org_id IS NULL AND p.code = :code "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role=role, code=code)
            )


def downgrade() -> None:
    for role, codes in ROLE_GRANTS.items():
        for code in codes:
            op.execute(
                sa.text(
                    "DELETE FROM role_permissions rp USING roles r, permissions p "
                    "WHERE rp.role_id = r.id AND rp.permission_id = p.id "
                    "AND r.name = :role AND p.code = :code"
                ).bindparams(role=role, code=code)
            )
    for code, _, _, _ in PERMISSIONS:
        op.execute(sa.text("DELETE FROM permissions WHERE code = :code").bindparams(code=code))
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP INDEX IF EXISTS ix_users_org_lower_email")
    op.drop_constraint("uq_ticket_email_token", "ticket", type_="unique")
    op.drop_column("ticket", "email_token")
    op.drop_table("inbound_message")
    op.drop_table("email_mailbox")
```

Open `0025_notification_core.py` and confirm the exact `permissions`/`role_permissions` column names and the `roles.org_id IS NULL` predicate before running — copy them rather than trusting the SQL above verbatim.

Add the two codes to the descriptions dict in `backend/app/core/rbac.py`, alongside the `notification.*` entries near line 85:

```python
    "email_intake.read": "View mailbox config, poll status, and the inbound message log",
    "email_intake.admin": (
        "Create, modify and delete an inbound mailbox and its credential. "
        "High-privilege: it sets which mailbox the platform authenticates to "
        "and which category inbound tickets land in."
    ),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_migration.py tests/test_schema_drift.py tests/test_rls.py -v`
Expected: all PASS. `test_schema_drift.py` compares `Base.metadata` to the live schema — if it fails, the model and the migration disagree on a type, length or nullability; fix the mismatch, do not adjust the test.

- [ ] **Step 6: Prove the WITH CHECK test can fail**

Temporarily edit the migration's policy to `WITH CHECK (true)` — **not** to a bare `USING` with the clause deleted, which does not falsify anything (see the correction in Global Constraints). Drop and re-create the test database, and run `pytest tests/test_email_intake_migration.py::test_a_mailbox_cannot_be_inserted_under_another_orgs_id -v`. **Watch it go red.** Then separately set `USING (true)` and watch `test_a_mailbox_is_invisible_to_another_org` go red. Restore both and confirm green. A tenancy test that has never been observed failing is not evidence.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0032_email_intake.py \
        backend/app/modules/email_intake/ \
        backend/app/modules/service_desk/models.py \
        backend/app/core/rbac.py \
        backend/tests/test_email_intake_migration.py
git commit -m "feat(email-intake): migration 0032 -- mailbox, inbound log, reply token"
```

---

## Task 2: Mailbox configuration service

**Files:**
- Create: `backend/app/modules/email_intake/errors.py`
- Create: `backend/app/modules/email_intake/schema.py`
- Create: `backend/app/modules/email_intake/mailboxes.py`
- Test: `backend/tests/test_email_intake_mailboxes.py`

**Interfaces:**
- Consumes: `EmailMailbox` (Task 1); `app.core.vault.VaultService`; `app.core.audit.audit_service`.
- Produces:
  - `MailboxCreate`, `MailboxUpdate`, `MailboxOut` (Pydantic)
  - `async def create_mailbox(session, org_id, actor_id, data: MailboxCreate, *, vault: VaultService) -> EmailMailbox`
  - `async def update_mailbox(session, org_id, actor_id, mailbox_id, data: MailboxUpdate, *, vault: VaultService) -> EmailMailbox`
  - `async def delete_mailbox(session, org_id, actor_id, mailbox_id) -> None`
  - `async def get_mailbox(session, org_id, mailbox_id) -> EmailMailbox`
  - `async def list_mailboxes(session, org_id) -> list[EmailMailbox]`
  - `MailboxNotFoundError`, `MailboxValidationError`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_mailboxes.py`:

```python
"""Mailbox CRUD and its vault-held credential (spec §2.1, §3, §9)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.core.vault import get_vault
from app.models import AuditEvent
from app.modules.email_intake.errors import MailboxNotFoundError, MailboxValidationError
from app.modules.email_intake.mailboxes import (
    create_mailbox,
    delete_mailbox,
    get_mailbox,
    update_mailbox,
)
from app.modules.email_intake.models import EmailMailbox
from app.modules.email_intake.schema import MailboxCreate, MailboxUpdate
from tests.test_email_intake_migration import _seed_category, _seed_user

SECRET = "correct-horse-battery-staple"


def _payload(category_id, **overrides) -> MailboxCreate:
    data = {
        "address": "helpdesk@acme.test",
        "host": "imap.acme.test",
        "port": 993,
        "username": "helpdesk@acme.test",
        "password": SECRET,
        "default_category_id": category_id,
    }
    data.update(overrides)
    return MailboxCreate(**data)


async def test_create_stores_the_password_in_the_vault_and_never_on_the_row(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id

    async with org_session(org_id, engine=api_env.app_engine) as session:
        row = await get_mailbox(session, org_id, mailbox_id)
        # Invariant #3: the plaintext appears in NO column of this row.
        assert SECRET not in " ".join(
            str(getattr(row, c.name)) for c in EmailMailbox.__table__.columns
        )
        # ...and the vault can hand it back at point of use.
        assert await vault.checkout(session, org_id=org_id, credential_id=row.credential_id) == SECRET


async def test_rotating_the_password_reuses_the_same_credential_row(api_env) -> None:
    """A fresh Credential row per rotation is credential sprawl; the
    notification module made the same call (channels.py's docstring)."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)
        mailbox_id, credential_id = mailbox.id, mailbox.credential_id

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await update_mailbox(
            session, org_id, actor_id, mailbox_id, MailboxUpdate(password="new-secret"), vault=vault
        )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        row = await get_mailbox(session, org_id, mailbox_id)
        assert row.credential_id == credential_id
        assert await vault.checkout(session, org_id=org_id, credential_id=credential_id) == "new-secret"


async def test_no_audit_metadata_contains_the_password(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)
        await update_mailbox(
            session, org_id, actor_id, mailbox.id, MailboxUpdate(password="another"), vault=vault
        )

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
    assert events, "mailbox create/update must be audited (invariant #4)"
    blob = " ".join(str(e.metadata_json) for e in events)
    assert SECRET not in blob and "another" not in blob


async def test_a_duplicate_address_is_a_typed_error_not_an_integrity_error(api_env) -> None:
    """Two concurrent creates race on uq_email_mailbox_org_address. The loser
    must see the SAME typed error as a caller who lost the pre-check --
    check-then-insert with no ON CONFLICT is the bug class this repo has
    already run two sweeps to eliminate."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)

    with pytest.raises(MailboxValidationError):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)


async def test_concurrent_creates_of_the_same_address_yield_one_row(api_env) -> None:
    """The real race, two sessions, not a simulated one."""
    import asyncio

    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async def attempt() -> bool:
        try:
            async with org_session(org_id, engine=api_env.app_engine) as session:
                await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)
            return True
        except MailboxValidationError:
            return False

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in results), results

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (await session.execute(select(EmailMailbox))).scalars().all()
    assert len(rows) == 1


async def test_get_mailbox_from_another_org_is_not_found(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_id = await _seed_user(api_env, org_a)
    category_id = await _seed_category(api_env, org_a)
    vault = get_vault(api_env.settings)

    async with org_session(org_a, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(session, org_a, actor_id, _payload(category_id), vault=vault)
        mailbox_id = mailbox.id

    with pytest.raises(MailboxNotFoundError):
        async with org_session(org_b, engine=api_env.app_engine) as session:
            await get_mailbox(session, org_b, mailbox_id)


async def test_delete_removes_the_mailbox_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        mailbox = await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)
        mailbox_id = mailbox.id

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_mailbox(session, org_id, actor_id, mailbox_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await session.execute(select(EmailMailbox))).scalars().all() == []
```

Confirm the audit model's metadata attribute name — this plan writes `AuditEvent.metadata_json`, but `metadata` is reserved by SQLAlchemy's declarative base, so the real attribute may differ. Grep `app/models/` for the `AuditEvent` class and use whatever it actually is.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_mailboxes.py -v`
Expected: `ModuleNotFoundError: No module named 'app.modules.email_intake.mailboxes'`.

- [ ] **Step 3: Write errors and schema**

`backend/app/modules/email_intake/errors.py`:

```python
"""Typed errors for email intake.

`MailboxNotFoundError` subclasses `MailboxValidationError` so a caller that
only cares "the request was bad" catches both, mirroring
`service_desk/errors.py`'s `TicketNotFoundError(TicketValidationError)`.
"""


class MailboxValidationError(Exception):
    """The request is not valid for this org's mailbox configuration."""


class MailboxNotFoundError(MailboxValidationError):
    """No mailbox with that id exists IN THIS ORG. Never distinguish
    'wrong org' from 'does not exist' -- that difference is an org-membership
    oracle."""
```

`backend/app/modules/email_intake/schema.py`:

```python
"""Request/response models for email intake (spec §10).

NO RESPONSE MODEL HERE CARRIES A PASSWORD OR A CREDENTIAL ID (invariant #3,
spec §10). `MailboxOut` exposes `has_credential: bool` -- the FACT that a
credential is configured, which an admin UI needs, and nothing more. This
mirrors `notification/api.py`'s `has_credential=row.credential_id is not None`.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

_MAX_ADDRESS = 320
_MAX_HOST = 255

Disposition = Literal[
    "created",
    "commented",
    "reopened",
    "quarantined",
    "refused_loop",
    "refused_rate",
    "refused_parse",
]


class MailboxCreate(BaseModel):
    address: str = Field(max_length=_MAX_ADDRESS)
    host: str = Field(max_length=_MAX_HOST)
    port: int = Field(ge=1, le=65535)
    use_ssl: bool = True
    folder: str = Field(default="INBOX", max_length=100)
    username: str = Field(max_length=_MAX_ADDRESS)
    password: str = Field(min_length=1, max_length=1024)
    default_category_id: UUID
    reopen_window_days: int = Field(default=14, ge=1, le=3650)
    active: bool = True


class MailboxUpdate(BaseModel):
    address: str | None = Field(default=None, max_length=_MAX_ADDRESS)
    host: str | None = Field(default=None, max_length=_MAX_HOST)
    port: int | None = Field(default=None, ge=1, le=65535)
    use_ssl: bool | None = None
    folder: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, max_length=_MAX_ADDRESS)
    # None means "leave the stored password alone" -- it does NOT mean "clear
    # it". credential_id is NOT NULL; there is no unauthenticated IMAP.
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    default_category_id: UUID | None = None
    reopen_window_days: int | None = Field(default=None, ge=1, le=3650)
    active: bool | None = None


class MailboxOut(BaseModel):
    id: UUID
    address: str
    host: str
    port: int
    use_ssl: bool
    folder: str
    username: str
    has_credential: bool
    default_category_id: UUID
    reopen_window_days: int
    active: bool
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


class InboundMessageOut(BaseModel):
    id: UUID
    mailbox_id: UUID
    message_id: str
    from_address: str
    subject: str
    received_at: datetime
    disposition: Disposition
    reason: str
    ticket_id: UUID | None
    # Deliberately exposed to `email_intake.read` holders (spec §10): this is
    # how an operator diagnoses a quarantined message. It is the PARSED text,
    # never the raw RFC822 message.
    body_text: str | None


class MessageLogOut(BaseModel):
    messages: list[InboundMessageOut]
    # Quarantined mail is mail a human expected to become a ticket and which
    # did not. It is invisible unless surfaced (spec §10).
    quarantined_count: int
```

- [ ] **Step 4: Write the service**

`backend/app/modules/email_intake/mailboxes.py`:

```python
"""Mailbox CRUD and its vault-held credential (spec §2.1, §3, §9).

THE PASSWORD NEVER LANDS ON `email_mailbox`. It goes to the vault via
`_store_secret`, and the row holds only `credential_id`. It is never returned
by a service function, never placed in an audit `metadata` dict, and never
written to `last_error` (invariant #3).

CREATE IS ON CONFLICT, NOT CHECK-THEN-INSERT. `uq_email_mailbox_org_address`
is the authority on uniqueness; a pre-check is only a fast path for a good
error message. Two concurrent creates of the same address must produce one
row and one typed error, not an IntegrityError 500.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.vault import VaultService
from app.models.vault import Credential
from app.modules.email_intake.errors import MailboxNotFoundError, MailboxValidationError
from app.modules.email_intake.models import EmailMailbox
from app.modules.email_intake.schema import MailboxCreate, MailboxUpdate
from app.modules.service_desk.models import TicketCategory


async def _store_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: str,
    vault: VaultService,
    existing_credential_id: UUID | None = None,
) -> UUID:
    """Store `secret` and return the `Credential` row id it lives on.

    Reuses `existing_credential_id` in place (a rotation) rather than minting
    a fresh row -- an orphaned Credential per rotation is credential sprawl.
    Never returns or logs the plaintext.
    """
    credential: Credential | None = None
    if existing_credential_id is not None:
        credential = (
            await session.execute(
                select(Credential).where(
                    Credential.org_id == org_id, Credential.id == existing_credential_id
                )
            )
        ).scalar_one_or_none()

    if credential is None:
        credential = Credential(
            org_id=org_id,
            name="email-mailbox-secret",
            type="email_mailbox",
            vault_ref="",
            created_by=actor_id,
        )
        session.add(credential)
        await session.flush()

    stored = await vault.store(session, org_id=org_id, credential_id=credential.id, plaintext=secret)
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


async def _resolve_category(session: AsyncSession, org_id: UUID, category_id: UUID) -> UUID:
    """`default_category_id` is NOT NULL and load-bearing (spec §2.1): without
    it no inbound message could ever become a ticket, because
    `ticket.category_id` is NOT NULL and an email carries no category."""
    found = (
        await session.execute(
            select(TicketCategory.id).where(
                TicketCategory.org_id == org_id, TicketCategory.id == category_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise MailboxValidationError("default_category_id does not resolve in this org")
    return found


async def get_mailbox(session: AsyncSession, org_id: UUID, mailbox_id: UUID) -> EmailMailbox:
    mailbox = (
        await session.execute(
            select(EmailMailbox).where(
                EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
            )
        )
    ).scalar_one_or_none()
    if mailbox is None:
        raise MailboxNotFoundError(f"mailbox {mailbox_id} not found")
    return mailbox


async def list_mailboxes(session: AsyncSession, org_id: UUID) -> list[EmailMailbox]:
    return list(
        (
            await session.execute(
                select(EmailMailbox)
                .where(EmailMailbox.org_id == org_id)
                .order_by(EmailMailbox.address)
            )
        )
        .scalars()
        .all()
    )


async def create_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    data: MailboxCreate,
    *,
    vault: VaultService,
) -> EmailMailbox:
    await _resolve_category(session, org_id, data.default_category_id)
    credential_id = await _store_secret(
        session, org_id=org_id, actor_id=actor_id, secret=data.password, vault=vault
    )

    # ON CONFLICT DO NOTHING + RETURNING id. `inserted_id is None` means a
    # concurrent create won the race; it raises the SAME typed error a caller
    # who lost a pre-check would see, and never returns the winner's row --
    # returning it would tell this caller their configuration was applied
    # when someone else's was.
    result = await session.execute(
        pg_insert(EmailMailbox.__table__)
        .values(
            org_id=org_id,
            address=data.address,
            host=data.host,
            port=data.port,
            use_ssl=data.use_ssl,
            folder=data.folder,
            username=data.username,
            credential_id=credential_id,
            default_category_id=data.default_category_id,
            reopen_window_days=data.reopen_window_days,
            active=data.active,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(constraint="uq_email_mailbox_org_address")
        .returning(EmailMailbox.__table__.c.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        raise MailboxValidationError(f"a mailbox for {data.address!r} already exists in this org")

    mailbox = await get_mailbox(session, org_id, inserted_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_created",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        # Address/host/port only. NEVER the password, and never the
        # credential id -- an audit reader has no use for a vault handle.
        metadata={"address": data.address, "host": data.host, "port": data.port},
    )
    return mailbox


async def update_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    mailbox_id: UUID,
    data: MailboxUpdate,
    *,
    vault: VaultService,
) -> EmailMailbox:
    mailbox = await get_mailbox(session, org_id, mailbox_id)

    if data.default_category_id is not None:
        await _resolve_category(session, org_id, data.default_category_id)

    if data.password is not None:
        await _store_secret(
            session,
            org_id=org_id,
            actor_id=actor_id,
            secret=data.password,
            vault=vault,
            existing_credential_id=mailbox.credential_id,
        )

    changed: list[str] = []
    for field in (
        "address",
        "host",
        "port",
        "use_ssl",
        "folder",
        "username",
        "default_category_id",
        "reopen_window_days",
        "active",
    ):
        value = getattr(data, field)
        if value is not None and value != getattr(mailbox, field):
            setattr(mailbox, field, value)
            changed.append(field)
    if data.password is not None:
        changed.append("password")

    session.add(mailbox)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_updated",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        # The NAMES of the changed fields, never their values -- "password"
        # appearing here records that a rotation happened, which is the whole
        # point, without recording what it rotated to.
        metadata={"changed": sorted(changed)},
    )
    return mailbox


async def delete_mailbox(
    session: AsyncSession, org_id: UUID, actor_id: UUID, mailbox_id: UUID
) -> None:
    mailbox = await get_mailbox(session, org_id, mailbox_id)
    target_id = str(mailbox.id)
    address = mailbox.address
    await session.delete(mailbox)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_deleted",
        target_type="email_mailbox",
        target_id=target_id,
        metadata={"address": address},
    )
```

`inbound_message.mailbox_id` is a NOT NULL FK to `email_mailbox`, so deleting a mailbox that has processed messages will raise a FK violation. Decide during implementation and record the choice in the docstring: either refuse the delete with a typed `MailboxValidationError` naming the message count, or set the FK `ondelete="CASCADE"` in the migration. **Prefer refusing** — the disposition log is the operator's only record of what arrived, and deleting a mailbox should not silently erase it. Add a test for whichever you choose.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_mailboxes.py -v`
Expected: all PASS.

- [ ] **Step 6: Prove the race test can fail**

Temporarily replace the `pg_insert(...).on_conflict_do_nothing(...)` block with a plain `session.add(EmailMailbox(...))` and run `test_concurrent_creates_of_the_same_address_yield_one_row`. **Watch it go red** with an `IntegrityError` rather than the typed error. Restore the ON CONFLICT form.

- [ ] **Step 7: Commit**

```bash
git add backend/app/modules/email_intake/ backend/tests/test_email_intake_mailboxes.py
git commit -m "feat(email-intake): mailbox config with vault-held credentials"
```

---

## Task 3: MIME parsing — body, HTML, quotes, attachments, bounds

**Files:**
- Create: `backend/app/modules/email_intake/parsing.py`
- Test: `backend/tests/test_email_intake_parsing.py`

**Interfaces:**
- Consumes: nothing (pure; stdlib `email` only, **no database**).
- Produces:
  - `@dataclass(frozen=True) ParsedAttachment(filename: str, content: bytes)`
  - `@dataclass(frozen=True) ParsedMessage(message_id: str, from_address: str, subject: str, recipients: tuple[str, ...], body_text: str, attachments: tuple[ParsedAttachment, ...], received_at: datetime, refusal: str | None)`
  - `def parse_message(raw: bytes, *, mailbox_address: str) -> ParsedMessage`
  - `MAX_MESSAGE_BYTES: int`, `MAX_ATTACHMENT_COUNT: int`
  - `def html_to_text(html: str) -> str`
  - `def trim_quoted_history(body: str) -> str`

`refusal` is `None` for an acceptable message, or one of `"refused_loop"` / `"refused_parse"` with the reason available on `ParsedMessage.refusal_reason: str`. Making refusal a *field of the parse result* rather than an exception is deliberate: spec §2.2 requires **every fetched message to get a row**, and an exception path invites a caller to drop one.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_parsing.py`:

```python
"""MIME parsing, HTML flattening, quote trimming, bounds, loop detection.

Pure -- no database, no fixtures. Every test builds a real RFC822 message
with the stdlib and asserts on the parse RESULT, never on an internal call.
"""

from email.message import EmailMessage

import pytest

from app.modules.email_intake.parsing import (
    MAX_ATTACHMENT_COUNT,
    MAX_MESSAGE_BYTES,
    parse_message,
    trim_quoted_history,
)

MAILBOX = "helpdesk@acme.test"


def _raw(
    *,
    body: str = "Printer is on fire.",
    subject: str = "Help",
    sender: str = "alice@acme.test",
    to: str = MAILBOX,
    headers: dict[str, str] | None = None,
    message_id: str | None = "<abc123@acme.test>",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    if message_id is not None:
        message["Message-ID"] = message_id
    for key, value in (headers or {}).items():
        message[key] = value
    message.set_content(body)
    return message.as_bytes()


def test_a_plain_message_parses_to_its_text_body() -> None:
    parsed = parse_message(_raw(), mailbox_address=MAILBOX)
    assert parsed.refusal is None
    assert parsed.from_address == "alice@acme.test"
    assert parsed.subject == "Help"
    assert "Printer is on fire." in parsed.body_text
    assert parsed.message_id == "<abc123@acme.test>"


def test_a_message_with_no_message_id_gets_a_stable_synthesised_one() -> None:
    """Spec §2.2: it must still dedupe. Same bytes -> same id, so a
    redelivery of a Message-ID-less message still collides."""
    raw = _raw(message_id=None)
    first = parse_message(raw, mailbox_address=MAILBOX)
    second = parse_message(raw, mailbox_address=MAILBOX)
    assert first.message_id
    assert first.message_id == second.message_id
    other = parse_message(_raw(message_id=None, body="different"), mailbox_address=MAILBOX)
    assert other.message_id != first.message_id


def test_html_only_mail_is_stored_as_text_with_no_markup(sub) -> None:
    """Spec §7 and §11.9. The portal and staff UI would otherwise render
    attacker-supplied markup from an unauthenticated source."""
    message = EmailMessage()
    message["Subject"] = "Help"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<html1@acme.test>"
    message.set_content(
        "<html><body><p>Printer <b>on fire</b></p>"
        "<script>alert(1)</script></body></html>",
        subtype="html",
    )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal is None
    assert "<" not in parsed.body_text and ">" not in parsed.body_text
    assert "script" not in parsed.body_text.lower()
    assert "Printer" in parsed.body_text and "on fire" in parsed.body_text


def test_the_plain_part_is_preferred_over_the_html_part() -> None:
    message = EmailMessage()
    message["Subject"] = "Help"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<multi1@acme.test>"
    message.set_content("plain version")
    message.add_alternative("<p>html version</p>", subtype="html")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert "plain version" in parsed.body_text
    assert "html version" not in parsed.body_text


def test_quoted_history_is_trimmed() -> None:
    body = (
        "Yes, still broken.\n"
        "\n"
        "On Mon, 1 Jan 2026 at 10:00, Helpdesk <helpdesk@acme.test> wrote:\n"
        "> Have you tried turning it off and on again?\n"
        "> -- Helpdesk\n"
    )
    trimmed = trim_quoted_history(body)
    assert "still broken" in trimmed
    assert "turning it off" not in trimmed


def test_trimming_never_returns_an_empty_body() -> None:
    """Spec §7: the heuristic is best-effort and must NEVER drop the whole
    body. A reply that is entirely quote-shaped is still a reply."""
    body = "> everything I say looks like a quote\n> second line\n"
    assert trim_quoted_history(body).strip() != ""


def test_attachments_are_extracted_with_their_filenames() -> None:
    message = EmailMessage()
    message["Subject"] = "Screenshot"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<att1@acme.test>"
    message.set_content("see attached")
    message.add_attachment(b"\x89PNG\r\n", maintype="image", subtype="png", filename="shot.png")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal is None
    assert [a.filename for a in parsed.attachments] == ["shot.png"]
    assert parsed.attachments[0].content.startswith(b"\x89PNG")


def test_an_oversized_message_is_refused_parse_but_keeps_its_text() -> None:
    """Spec §7: size bounds are enforced HERE, because email bypasses the
    HTTP body caps entirely. The person is not left silent -- the text body
    still reaches the ticket."""
    message = EmailMessage()
    message["Subject"] = "Big"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<big1@acme.test>"
    message.set_content("the actual words")
    message.add_attachment(
        b"x" * (MAX_MESSAGE_BYTES + 1), maintype="application", subtype="octet-stream",
        filename="huge.bin",
    )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"
    assert "the actual words" in parsed.body_text
    assert parsed.attachments == ()


def test_too_many_attachments_is_refused_parse() -> None:
    message = EmailMessage()
    message["Subject"] = "Many"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<many1@acme.test>"
    message.set_content("body")
    for i in range(MAX_ATTACHMENT_COUNT + 1):
        message.add_attachment(
            b"x", maintype="application", subtype="octet-stream", filename=f"f{i}.bin"
        )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"


def test_garbage_bytes_are_refused_parse_without_raising() -> None:
    """Spec §7 and §11.10: one bad message must not stop a mailbox."""
    parsed = parse_message(b"\x00\xff not a message at all", mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Auto-Submitted": "auto-generated"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
        {"Precedence": "junk"},
        {"Precedence": "auto_reply"},
        {"X-Auto-Response-Suppress": "All"},
        {"Return-Path": "<>"},
    ],
)
def test_auto_reply_and_bounce_headers_are_refused_loop(headers: dict[str, str]) -> None:
    """Spec §6, §11.5. One case per refusal header. The failure this
    prevents: we notify, their out-of-office replies, the reply becomes a
    comment, the comment notifies them again."""
    parsed = parse_message(_raw(headers=headers), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop", headers


def test_auto_submitted_no_is_not_a_loop() -> None:
    """RFC 3834: `Auto-Submitted: no` is the explicit 'this is a human'
    value. Refusing it would silently drop legitimate mail from clients that
    set it."""
    parsed = parse_message(
        _raw(headers={"Auto-Submitted": "no"}), mailbox_address=MAILBOX
    )
    assert parsed.refusal is None


def test_a_delivery_status_report_is_refused_loop() -> None:
    """Spec §6, §11.6: a bounce must not become a comment."""
    message = EmailMessage()
    message["Subject"] = "Undelivered Mail Returned to Sender"
    message["From"] = "MAILER-DAEMON@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<dsn1@acme.test>"
    message.make_mixed()
    message.set_type("multipart/report")
    message.set_param("report-type", "delivery-status")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_mail_from_the_mailbox_itself_is_refused_loop() -> None:
    """Spec §6: the tightest loop of all."""
    parsed = parse_message(_raw(sender=MAILBOX), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_recipients_include_to_cc_and_delivered_to() -> None:
    """Spec §5.2: the reply token may arrive on any of the three."""
    parsed = parse_message(
        _raw(
            to="helpdesk+t-TOKEN@acme.test",
            headers={"Cc": "someone@acme.test", "Delivered-To": "other@acme.test"},
        ),
        mailbox_address=MAILBOX,
    )
    assert "helpdesk+t-token@acme.test" in [r.lower() for r in parsed.recipients]
    assert "someone@acme.test" in parsed.recipients
    assert "other@acme.test" in parsed.recipients
```

Delete the stray `sub` parameter from `test_html_only_mail_is_stored_as_text_with_no_markup` when you write the file — it takes no fixture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_parsing.py -v`
Expected: `ModuleNotFoundError: No module named 'app.modules.email_intake.parsing'`.

- [ ] **Step 3: Implement the parser**

Create `backend/app/modules/email_intake/parsing.py`. Key requirements, all of which the tests above pin:

- Parse with `email.message_from_bytes(raw, policy=email.policy.default)`. Wrap the whole parse in a `try/except Exception` returning a `refused_parse` result — spec §7: an exception must never escape into the poller.
- `MAX_MESSAGE_BYTES = 25 * 1024 * 1024`, `MAX_ATTACHMENT_COUNT = 20`. Per-part bound is `app.modules.service_desk.threads.MAX_ATTACHMENT_BYTES` — **import it, do not restate the number**, so the two can never drift.
- Loop detection runs **before** body extraction and short-circuits, so a malformed auto-reply is still classified `refused_loop`.
- HTML→text: use `html.unescape` after stripping tags with a bounded regex, then collapse whitespace. Drop `<script>`/`<style>` **contents**, not just their tags — a naive tag strip leaves the script body as text.
- Quote trimming: cut at the first line matching a leading-`>` block or a client separator (`On … wrote:`, `-----Original Message-----`, `From: … Sent: …`). **Then check: if the result is blank, return the untrimmed body.** That check is the whole point of the second test.
- Synthesised `Message-ID`: `f"<synthesised-{sha256(raw).hexdigest()}@email-intake.invalid>"`. Deterministic over the raw bytes, so a redelivery of the same message synthesises the same id and collides on the unique constraint.
- Truncate `subject` to 512 and `message_id` to 998 before returning — the columns are bounded and an over-long header must not become a database error at insert time.
- Bounds violations set `refusal="refused_parse"` and return `attachments=()` **while keeping `body_text`** (spec §7: "its text body is still preserved on the ticket, so the person is not left silent"). Task 5 is what honours that.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_parsing.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the HTML test can fail**

Temporarily make `html_to_text` return its input unchanged and run `test_html_only_mail_is_stored_as_text_with_no_markup`. **Watch it go red.** Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/parsing.py backend/tests/test_email_intake_parsing.py
git commit -m "feat(email-intake): MIME parsing, HTML flattening, bounds, loop detection"
```

---

## Task 4: Sender resolution and reply-token resolution

**Files:**
- Create: `backend/app/modules/email_intake/identity.py`
- Test: `backend/tests/test_email_intake_identity.py`

**Interfaces:**
- Consumes: `ParsedMessage` (Task 3); `app.models.User`; `Ticket` and `ticket.email_token` (Task 1).
- Produces:
  - `@dataclass(frozen=True) SenderResolution(user_id: UUID | None, reason: str)` — `user_id is None` means quarantine, and `reason` says why
  - `async def resolve_sender(session, org_id, from_address: str) -> SenderResolution`
  - `def extract_reply_token(recipients: Sequence[str]) -> str | None`
  - `async def resolve_ticket_by_token(session, org_id, token: str) -> Ticket | None`
  - `def mint_email_token() -> str`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_identity.py`:

```python
"""Sender resolution and the reply token (spec §5, §0.2)."""

from app.core.tenancy import org_session
from app.modules.email_intake.identity import (
    extract_reply_token,
    mint_email_token,
    resolve_sender,
    resolve_ticket_by_token,
)
from tests.test_email_intake_migration import _seed_user


async def test_exactly_one_matching_user_resolves(api_env) -> None:
    org_id = await api_env.create_org()
    user_id = await _seed_user(api_env, org_id, email="alice@acme.test")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_sender(session, org_id, "alice@acme.test")
    assert result.user_id == user_id


async def test_matching_is_case_insensitive(api_env) -> None:
    org_id = await api_env.create_org()
    user_id = await _seed_user(api_env, org_id, email="Alice@Acme.Test")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_sender(session, org_id, "ALICE@acme.TEST")
    assert result.user_id == user_id


async def test_an_unknown_sender_does_not_resolve(api_env) -> None:
    """Spec §11.3: quarantined, not dropped. The reason is recorded so an
    admin can tell 'the helpdesk ignored my email' from 'never received it'."""
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_sender(session, org_id, "stranger@elsewhere.test")
    assert result.user_id is None
    assert "unknown" in result.reason.lower()


async def test_a_user_in_another_org_does_not_resolve(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    await _seed_user(api_env, org_b, email="alice@acme.test")
    async with org_session(org_a, engine=api_env.app_engine) as session:
        result = await resolve_sender(session, org_a, "alice@acme.test")
    assert result.user_id is None


async def test_an_ambiguous_sender_does_not_resolve(api_env) -> None:
    """Spec §5.1: `users` is unique on (org_id, external_subject), NOT on
    email, so two users in one org can legitimately share an address.
    Picking one would attribute a person's ticket to a colleague, and the
    resulting ticket would look entirely normal."""
    org_id = await api_env.create_org()
    await _seed_user(api_env, org_id, email="shared@acme.test")
    await _seed_user(api_env, org_id, email="shared@acme.test")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_sender(session, org_id, "shared@acme.test")
    assert result.user_id is None
    assert "ambiguous" in result.reason.lower()


def test_a_token_is_extracted_from_a_plus_address() -> None:
    assert extract_reply_token(["helpdesk+t-AbC123@acme.test"]) == "AbC123"


def test_a_token_is_found_on_any_recipient_header() -> None:
    assert extract_reply_token(["nobody@acme.test", "helpdesk+t-XYZ@acme.test"]) == "XYZ"


def test_a_plain_address_yields_no_token() -> None:
    assert extract_reply_token(["helpdesk@acme.test"]) is None


def test_a_plus_address_that_is_not_a_token_yields_none() -> None:
    assert extract_reply_token(["helpdesk+newsletter@acme.test"]) is None


def test_token_extraction_is_case_preserving() -> None:
    """token_urlsafe is case-SIGNIFICANT: lower-casing it would collapse the
    keyspace and could match the wrong ticket."""
    assert extract_reply_token(["helpdesk+t-AbCd@acme.test"]) == "AbCd"


def test_a_minted_token_is_url_safe_and_long() -> None:
    token = mint_email_token()
    assert len(token) == 43  # secrets.token_urlsafe(32)
    assert token.isascii() and "@" not in token and "+" not in token


async def test_a_token_resolves_to_its_ticket_and_only_within_the_org(api_env, a_ticket) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    ticket = await a_ticket(org_a)
    token = mint_email_token()

    async with org_session(org_a, engine=api_env.app_engine) as session:
        found = await session.get(type(ticket), ticket.id)
        found.email_token = token
        session.add(found)

    async with org_session(org_a, engine=api_env.app_engine) as session:
        assert (await resolve_ticket_by_token(session, org_a, token)).id == ticket.id
    async with org_session(org_b, engine=api_env.app_engine) as session:
        assert await resolve_ticket_by_token(session, org_b, token) is None


async def test_an_unknown_token_resolves_to_none(api_env) -> None:
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert await resolve_ticket_by_token(session, org_id, "not-a-real-token") is None
```

`a_ticket` is the existing portal test helper in `tests/conftest.py` (the `ATicket` dataclass near line 235). Read its signature and adjust these two tests to match — if it does not take an `org_id`, seed the ticket via `service_desk.tickets.create_ticket` instead.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_identity.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `backend/app/modules/email_intake/identity.py`:

```python
"""Sender resolution and the reply token (spec §5, §0.2).

TWO INDEPENDENT CHECKS, AND BOTH MUST PASS. This module supplies both
halves, and neither is sufficient alone:

- `From:` is a trivially forgeable string. It is checked because an
  unrecognised sender must never create or comment on anything -- not
  because it proves identity.
- The token is NOT a bearer credential. It appears in the headers of every
  message in the thread, is forwarded wherever the requester forwards the
  thread, and outlives their employment. It identifies the TICKET. Whoever
  resolves a ticket by token must ALSO check that the sender is that
  ticket's requester -- see `intake.py`, which is the only caller that may
  put the two together.
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.modules.service_desk.models import Ticket

# `+t-` then the urlsafe-base64 alphabet. Anchored at the end of the local
# part so `helpdesk+t-ABC+extra@...` cannot smuggle a second segment.
_TOKEN_RE = re.compile(r"\+t-([A-Za-z0-9_-]{20,64})@")


@dataclass(frozen=True)
class SenderResolution:
    """`user_id is None` means QUARANTINE. `reason` is recorded on the
    `inbound_message` row so an admin can see what arrived and why it was
    refused -- silently dropping mail makes 'the helpdesk ignored my email'
    indistinguishable from 'the helpdesk never received it' (spec §5.1)."""

    user_id: UUID | None
    reason: str


async def resolve_sender(
    session: AsyncSession, org_id: UUID, from_address: str
) -> SenderResolution:
    address = from_address.strip().lower()
    if not address:
        return SenderResolution(None, "unknown sender: empty From address")

    # Uses ix_users_org_lower_email (migration 0032). LIMIT 2, not LIMIT 1:
    # the second row is what makes ambiguity detectable at all.
    matches = list(
        (
            await session.execute(
                select(User.id)
                .where(User.org_id == org_id, func.lower(User.email) == address)
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not matches:
        return SenderResolution(None, f"unknown sender: {address}")
    if len(matches) > 1:
        # NEVER guess which human sent it (spec §5.1). Two users in one org
        # may legitimately share an address.
        return SenderResolution(None, f"ambiguous sender: {address} matches multiple users")
    return SenderResolution(matches[0], "")


def extract_reply_token(recipients: Sequence[str]) -> str | None:
    """Find a `+t-<token>` in any recipient address (To, Cc, Delivered-To).

    Case is PRESERVED: `secrets.token_urlsafe` output is case-significant,
    and lower-casing would collapse the keyspace.
    """
    for recipient in recipients:
        match = _TOKEN_RE.search(recipient)
        if match:
            return match.group(1)
    return None


async def resolve_ticket_by_token(
    session: AsyncSession, org_id: UUID, token: str
) -> Ticket | None:
    """Resolve a token to its ticket, ORG-SCOPED (invariant #5).

    Returns the ticket, NOT an authorisation. The caller must still check
    the sender against `ticket.requester_id`.
    """
    return (
        await session.execute(
            select(Ticket).where(Ticket.org_id == org_id, Ticket.email_token == token)
        )
    ).scalar_one_or_none()


def mint_email_token() -> str:
    """256 bits, 43 urlsafe characters -- matches `ticket.email_token`'s
    String(43)."""
    return secrets.token_urlsafe(32)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_identity.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the ambiguity test can fail**

Temporarily change `.limit(2)` to `.limit(1)` and drop the `len(matches) > 1` branch. Run `test_an_ambiguous_sender_does_not_resolve`. **Watch it go red** — it should resolve to one of the two users. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/identity.py backend/tests/test_email_intake_identity.py
git commit -m "feat(email-intake): sender and reply-token resolution"
```

---

## Task 5: The disposition engine — §8's table, executed

**Files:**
- Create: `backend/app/modules/email_intake/intake.py`
- Test: `backend/tests/test_email_intake_intake.py`

**Interfaces:**
- Consumes: `ParsedMessage` (3), `resolve_sender`/`extract_reply_token`/`resolve_ticket_by_token` (4), `EmailMailbox`/`InboundMessage` (1), and the ticket core's `tickets.create_ticket`, `threads.add_comment`, `threads.add_attachment`, `transitions.change_status`, `lifecycle.can_transition`.
- Produces:
  - `async def process_message(session, org_id, mailbox: EmailMailbox, parsed: ParsedMessage, *, store: ObjectStore) -> InboundMessage`
  - `def reopen_target(ticket_type: str, status: str) -> str | None`
  - `class AlreadyProcessed(Exception)` — raised when the unique constraint collides. **A normal outcome, not an error** (spec §4 rule 3).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_intake.py`. These are the module's load-bearing tests — every one of them corresponds to a named requirement in spec §11:

```python
"""The disposition engine: spec §8's table, executed, plus §4's idempotency.

Every assertion here is on STORED STATE -- an `inbound_message` row, a
`ticket` row, a `ticket_comment` row -- never on whether a function was
called. A test that asserts on the call passes against an implementation
that calls the right thing and stores the wrong thing.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.tenancy import org_session
from app.modules.email_intake.identity import mint_email_token
from app.modules.email_intake.intake import AlreadyProcessed, process_message, reopen_target
from app.modules.email_intake.models import InboundMessage
from app.modules.service_desk.models import Ticket, TicketComment

# `_parsed(...)` builds a ParsedMessage directly rather than round-tripping
# through raw bytes: Task 3 already tests parsing, and constructing the
# dataclass keeps these tests about DISPOSITION.


def test_reopen_target_covers_every_reopenable_status() -> None:
    """Spec correction C2: `fulfilled -> in_progress` is the request-type
    reopen edge §8's parenthetical omits. Every edge below is one
    `lifecycle.py` actually defines -- assert against `can_transition`, not
    against a hardcoded list, so this cannot drift from the state machine."""
    from app.modules.service_desk.lifecycle import can_transition

    for ticket_type, status, expected in [
        ("incident", "resolved", "open"),
        ("incident", "closed", "open"),
        ("request", "fulfilled", "in_progress"),
        ("request", "closed", "in_progress"),
    ]:
        assert reopen_target(ticket_type, status) == expected
        assert can_transition(ticket_type, status, expected), (ticket_type, status, expected)

    # A live ticket needs no reopen.
    assert reopen_target("incident", "open") is None
    assert reopen_target("request", "in_progress") is None
    # Truly final states are never reopened by an inbound email.
    assert reopen_target("incident", "cancelled") is None
    assert reopen_target("request", "rejected") is None


async def test_a_known_sender_with_no_token_creates_a_ticket(email_env) -> None:
    """Spec §8 row 1."""
    parsed = email_env.parsed(sender="alice@acme.test", subject="Printer broken")
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
        assert row.disposition == "created"
        assert row.sender_user_id == email_env.alice_id

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        ticket = (await session.execute(select(Ticket))).scalar_one()
    assert ticket.origin == "email"
    assert ticket.requester_id == email_env.alice_id
    assert ticket.subject == "Printer broken"
    assert ticket.category_id == email_env.mailbox.default_category_id


async def test_processing_the_same_message_id_twice_creates_one_ticket(email_env) -> None:
    """Spec §11.1 and §4 rule 1. THIS is the idempotency mechanism; it must
    fail if the unique constraint is dropped."""
    parsed = email_env.parsed(sender="alice@acme.test", message_id="<dup@acme.test>")

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        await process_message(session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store)

    with pytest.raises(AlreadyProcessed):
        async with org_session(email_env.org_id, engine=email_env.engine) as session:
            await process_message(
                session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
            )

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        assert len((await session.execute(select(Ticket))).scalars().all()) == 1
        assert len((await session.execute(select(InboundMessage))).scalars().all()) == 1


async def test_a_valid_token_from_the_requester_adds_a_public_comment(email_env) -> None:
    """Spec §8 row 2 and §11.8: ALWAYS public. An internal note is not
    expressible from email, by construction."""
    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="still broken"
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
        assert row.disposition == "commented"
        assert row.ticket_id == ticket_id

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        comment = (
            await session.execute(select(TicketComment).where(TicketComment.ticket_id == ticket_id))
        ).scalar_one()
    assert comment.visibility == "public"
    assert comment.author_id == email_env.alice_id
    assert "still broken" in comment.body


async def test_a_valid_token_from_the_wrong_sender_is_quarantined(email_env) -> None:
    """Spec §0.2's CENTRAL CLAIM and §11.4. The token is not authority: it
    is in the headers of every message in the thread and outlives the
    requester's employment. Asserted on the OUTCOME, not the parsing."""
    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    parsed = email_env.parsed(
        sender="bob@acme.test", to=f"helpdesk+t-{token}@acme.test", body="let me in"
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "quarantined"
    assert row.ticket_id is None

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        comments = (
            (await session.execute(select(TicketComment).where(TicketComment.ticket_id == ticket_id)))
            .scalars()
            .all()
        )
    assert comments == [], "bob's mail must not reach alice's ticket"
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        assert len((await session.execute(select(Ticket))).scalars().all()) == 1, (
            "and it must not become a new ticket either"
        )


async def test_an_unknown_sender_is_quarantined_with_no_ticket(email_env) -> None:
    """Spec §11.3."""
    parsed = email_env.parsed(sender="stranger@elsewhere.test")
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "quarantined"
    assert "unknown" in row.reason.lower()
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.parametrize(
    ("ticket_type", "final_status", "expected_status"),
    [
        ("incident", "resolved", "open"),
        ("incident", "closed", "open"),
        ("request", "fulfilled", "in_progress"),
        ("request", "closed", "in_progress"),
    ],
)
async def test_a_reply_within_the_window_reopens_and_comments(
    email_env, ticket_type: str, final_status: str, expected_status: str
) -> None:
    """Spec §11.7 and correction C1. `closed` is in TERMINAL_STATUSES, so
    `add_comment` REJECTS it -- the reopen must happen FIRST. If the
    implementation comments before reopening, the two `closed` cases raise."""
    ticket_id, token = await email_env.a_ticket_for(
        "alice@acme.test", ticket_type=ticket_type, status=final_status
    )
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="it came back"
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "reopened"
    assert row.ticket_id == ticket_id

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
        comments = (
            (await session.execute(select(TicketComment).where(TicketComment.ticket_id == ticket_id)))
            .scalars()
            .all()
        )
    assert ticket.status == expected_status
    assert len(comments) == 1, "the reply must land on the ticket, not just reopen it"


async def test_a_reply_beyond_the_window_creates_a_new_ticket_citing_the_old(email_env) -> None:
    """Spec §8 row 4 and §11.7's other direction. Text, not a link row --
    parent/child linking is explicitly NOT sequenced (service-desk §14)."""
    old_id, token = await email_env.a_ticket_for(
        "alice@acme.test",
        status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=400),
    )
    old_number = await email_env.ticket_number(old_id)
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="same thing again"
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert row.ticket_id != old_id

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        new_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == row.ticket_id))
        ).scalar_one()
        old_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == old_id))
        ).scalar_one()
    assert old_number in new_ticket.description
    assert old_ticket.status == "closed", "the stale ticket must NOT be resurrected"


async def test_the_window_boundary(email_env) -> None:
    """Just inside reopens; just outside does not. The boundary is where an
    off-by-one lives, and both sides look correct in isolation."""
    window = email_env.mailbox.reopen_window_days
    inside_id, inside_token = await email_env.a_ticket_for(
        "alice@acme.test",
        status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=window - 1),
    )
    outside_id, outside_token = await email_env.a_ticket_for(
        "alice@acme.test",
        status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=window + 1),
    )

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        inside_row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(
                sender="alice@acme.test",
                to=f"helpdesk+t-{inside_token}@acme.test",
                message_id="<in@acme.test>",
            ),
            store=email_env.store,
        )
    assert inside_row.disposition == "reopened"
    assert inside_row.ticket_id == inside_id

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        outside_row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(
                sender="alice@acme.test",
                to=f"helpdesk+t-{outside_token}@acme.test",
                message_id="<out@acme.test>",
            ),
            store=email_env.store,
        )
    assert outside_row.disposition == "created"
    assert outside_row.ticket_id != outside_id


async def test_a_refused_loop_message_gets_a_row_and_no_ticket(email_env) -> None:
    """Spec §2.2: EVERY fetched message gets a row. Nothing is silently
    discarded -- a mailbox that quietly eats mail is indistinguishable from
    one nobody is emailing."""
    parsed = email_env.parsed(sender="alice@acme.test", refusal="refused_loop", refusal_reason="auto-submitted")
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "refused_loop"
    assert row.reason == "auto-submitted"
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


async def test_an_oversized_message_still_delivers_its_text(email_env) -> None:
    """Spec §7: the person is not left silent. Disposition records the
    refusal; the words still reach the ticket."""
    parsed = email_env.parsed(
        sender="alice@acme.test",
        body="the actual words",
        refusal="refused_parse",
        refusal_reason="message exceeds size limit",
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "refused_parse"
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        ticket = (await session.execute(select(Ticket))).scalar_one()
    assert "the actual words" in ticket.description


async def test_attachments_land_on_the_ticket(email_env) -> None:
    from app.modules.email_intake.parsing import ParsedAttachment
    from app.modules.service_desk.models import TicketAttachment

    parsed = email_env.parsed(
        sender="alice@acme.test",
        attachments=(ParsedAttachment(filename="shot.png", content=b"\x89PNG\r\n"),),
    )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        attachment = (
            await session.execute(
                select(TicketAttachment).where(TicketAttachment.ticket_id == row.ticket_id)
            )
        ).scalar_one()
    assert attachment.filename == "shot.png"
    assert attachment.uploaded_by == email_env.alice_id
```

Build an `email_env` fixture in the same file: it creates an org, seeds `alice@acme.test` and `bob@acme.test`, creates a category and a mailbox, exposes `api_env.app_engine` as `.engine` and an `ObjectStore` as `.store`, and provides `parsed(**overrides) -> ParsedMessage`, `a_ticket_for(email, *, ticket_type="incident", status=None, closed_at=None) -> tuple[UUID, str]` (creating the ticket via `service_desk.tickets.create_ticket`, driving it to `status` via `transitions.change_status`, and minting + storing an `email_token`), and `ticket_number(ticket_id) -> str`. Read `tests/test_portal_tickets.py` for how that module builds the same shape and copy it — do not invent a second convention.

`closed_at` needs a real column to key the window on. Check `Ticket` for `resolved_at`/`closed_at`; if neither exists, use `updated_at`, and record that choice in `intake.py`'s docstring and in the Task 10 spec amendment — spec §0.3 says "beyond the window" without naming the timestamp, and which timestamp it is materially changes behaviour.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_intake.py -v`
Expected: `ModuleNotFoundError: No module named 'app.modules.email_intake.intake'`.

- [ ] **Step 3: Implement**

Create `backend/app/modules/email_intake/intake.py`. The structure the tests above require:

```python
"""The disposition engine -- spec §8's table, executed.

THE `inbound_message` ROW IS WRITTEN IN THE SAME TRANSACTION AS THE TICKET
OR COMMENT IT PRODUCES (spec §4 rule 1). IMAP has no transactions: the
fetch, the ticket write and the \\Seen flag are three operations across two
systems. A redelivered message must collide on
`uq_inbound_message_org_message_id` instead of creating a second ticket.
`AlreadyProcessed` is that collision, and it is a NORMAL OUTCOME -- the
poller marks the message \\Seen and moves on. Treating it as a failure is
how one stuck message blocks a mailbox forever (§4 rule 3).

THIS MODULE ADDS NO TICKET RULE. It calls `tickets.create_ticket`,
`threads.add_comment`, `threads.add_attachment` and
`transitions.change_status`. It defines no status, no transition, and no
second answer to who may do what. A behaviour change belongs in
`app/modules/service_desk/`, where its tests live.

REOPEN HAPPENS BEFORE THE COMMENT. `lifecycle.TERMINAL_STATUSES` contains
`closed`, and `threads.add_comment` rejects a terminal ticket -- commenting
first would raise on every reply to a closed ticket. Spec §8 lists the two
in the other order; the spec is wrong, and `docs/modules/email-intake/
spec.md` §8 now records the correction.
"""
```

Then:

- `reopen_target(ticket_type, status)` — a dict of `{("incident", "resolved"): "open", ("incident", "closed"): "open", ("request", "fulfilled"): "in_progress", ("request", "closed"): "in_progress"}`, returning `None` for anything else. Assert each entry against `lifecycle.can_transition` at import time so the map cannot drift from the state machine.
- `process_message(...)` in this order:
  1. If `parsed.refusal == "refused_loop"` → write the row, return. No ticket, no comment.
  2. `resolve_sender`. `user_id is None` → row with `disposition="quarantined"`, `reason=result.reason`, return.
  3. `extract_reply_token(parsed.recipients)`. If a token resolves to a ticket:
     - `ticket.requester_id != sender_user_id` → `quarantined`, reason "reply token does not belong to this sender". **No ticket, no comment** — this is §0.2's central claim.
     - Else compute `reopen_target`. If the ticket needs no reopen → `change_status` skipped, `add_comment`, disposition `commented`.
     - If it needs one and is **within** the window → `change_status` **then** `add_comment`, disposition `reopened`.
     - If it needs one and is **beyond** the window → fall through to creation, with the old ticket's number cited in the new description.
  4. Creation: `create_ticket(session, org_id, actor_id=sender_user_id, TicketCreate(type="incident", category_id=mailbox.default_category_id, subject=parsed.subject or "(no subject)", description=body, requester_id=sender_user_id), origin="email")`. Note `origin="email"` is already in `ck_ticket_origin_valid` — the ticket core declared it in advance, so no constraint migration is needed. **Do not** reuse the `origin="alert"` null-requester carve-out: an email-created ticket has a real human requester.
  5. Attachments: only when `parsed.refusal is None`. Each through `threads.add_attachment` with `actor_id=sender_user_id`.
  6. Write the `InboundMessage` row **last within the same transaction**, via `pg_insert(...).on_conflict_do_nothing(constraint="uq_inbound_message_org_message_id").returning(id)`; `None` → `raise AlreadyProcessed(...)`, which rolls the whole transaction back including the ticket.
- Truncate `reason` to 200 and `subject` to 512 before insert.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_intake.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the two security tests can fail**

Two separate mutations, each watched going red, then restored:

1. Delete the `ticket.requester_id != sender_user_id` branch so a valid token alone is sufficient. `test_a_valid_token_from_the_wrong_sender_is_quarantined` must go red. This is the module's central security property; a test that has never been observed failing is not evidence for it.
2. Swap the order so `add_comment` runs before `change_status`. The two `closed` cases of `test_a_reply_within_the_window_reopens_and_comments` must go red with `TicketValidationError`. This is correction C1.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/intake.py backend/tests/test_email_intake_intake.py
git commit -m "feat(email-intake): disposition engine with idempotent message records"
```

---

## Task 6: Rate limiting

**Files:**
- Modify: `backend/app/modules/email_intake/intake.py`
- Test: `backend/tests/test_email_intake_intake.py` (append)

**Interfaces:**
- Consumes: `InboundMessage` (Task 1), `process_message` (Task 5).
- Produces: `SENDER_RATE_LIMIT: int`, `SENDER_RATE_WINDOW: timedelta`, `TICKET_RATE_LIMIT: int`, `TICKET_RATE_WINDOW: timedelta`; `process_message` gains the `refused_rate` disposition.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_email_intake_intake.py`:

```python
async def test_a_flood_from_one_sender_is_refused_rate(email_env) -> None:
    """Spec §6: a legitimate human does not send forty replies a minute; a
    loop does. Every refusal still gets a row -- being rate-limited is a
    thing an operator must be able to see."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    dispositions = []
    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(email_env.org_id, engine=email_env.engine) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<flood{i}@acme.test>"),
                store=email_env.store,
            )
            dispositions.append(row.disposition)

    assert dispositions[0] == "created"
    assert dispositions[-1] == "refused_rate"
    assert dispositions.count("refused_rate") >= 2


async def test_the_sender_rate_limit_is_scoped_to_one_sender(email_env) -> None:
    """Bob must not be locked out by Alice's flood -- a global limit would
    let one loop deny the whole helpdesk."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(email_env.org_id, engine=email_env.engine) as session:
            await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<a{i}@acme.test>"),
                store=email_env.store,
            )

    async with org_session(email_env.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(sender="bob@acme.test", message_id="<b1@acme.test>"),
            store=email_env.store,
        )
    assert row.disposition == "created"


async def test_the_rate_limit_is_scoped_to_one_org(email_env, api_env) -> None:
    """Invariant #5: one org's loop must not throttle another's helpdesk."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(email_env.org_id, engine=email_env.engine) as session:
            await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<x{i}@acme.test>"),
                store=email_env.store,
            )

    other = await email_env.another_org_with_alice()
    async with org_session(other.org_id, engine=email_env.engine) as session:
        row = await process_message(
            session, other.org_id, other.mailbox,
            other.parsed(sender="alice@acme.test", message_id="<other1@acme.test>"),
            store=email_env.store,
        )
    assert row.disposition == "created"


async def test_a_flood_of_replies_to_one_ticket_is_refused_rate(email_env) -> None:
    """Spec §6's second limit: the per-ticket inbound comment cap."""
    from app.modules.email_intake.intake import TICKET_RATE_LIMIT

    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    dispositions = []
    for i in range(TICKET_RATE_LIMIT + 2):
        async with org_session(email_env.org_id, engine=email_env.engine) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(
                    sender="alice@acme.test",
                    to=f"helpdesk+t-{token}@acme.test",
                    message_id=f"<reply{i}@acme.test>",
                ),
                store=email_env.store,
            )
            dispositions.append(row.disposition)
    assert dispositions[-1] == "refused_rate"
```

Set `SENDER_RATE_LIMIT` low enough (e.g. 10) that these tests stay fast. Add `another_org_with_alice()` to the `email_env` fixture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_intake.py -k rate -v`
Expected: `ImportError: cannot import name 'SENDER_RATE_LIMIT'`.

- [ ] **Step 3: Implement**

Add to `intake.py`:

```python
# Spec §6. The failure these prevent: we notify a requester, their
# out-of-office auto-replies, the reply becomes a comment, the comment
# notifies them again -- and the two systems generate mail until someone
# notices. Mail loops are fast and expensive, and the header checks in
# `parsing.py` catch only auto-repliers that announce themselves.
#
# Counted from `inbound_message`, NOT from an in-process counter: the poller
# runs in a Celery worker, several may run at once, and a per-process count
# would reset on every restart and be wrong under concurrency.
SENDER_RATE_LIMIT = 10
SENDER_RATE_WINDOW = timedelta(minutes=1)
TICKET_RATE_LIMIT = 20
TICKET_RATE_WINDOW = timedelta(hours=1)
```

and a `_rate_limited(session, org_id, from_address, ticket_id) -> str | None` helper returning a reason string, called immediately after sender resolution succeeds (a rate-limited message must still be attributed to its sender on the row) and before any ticket write. Both counts carry an explicit `org_id` predicate — invariant #5 is not delegated to RLS alone in a query this module owns.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_intake.py -v`
Expected: all PASS, including Task 5's.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/email_intake/intake.py backend/tests/test_email_intake_intake.py
git commit -m "feat(email-intake): per-sender and per-ticket inbound rate limits"
```

---

## Task 7: The IMAP transport and the poller

**Files:**
- Create: `backend/app/modules/email_intake/imap.py`
- Create: `backend/app/modules/email_intake/poller.py`
- Modify: `backend/app/worker.py` (beat entry + task, near the `notification-*` entries at line ~1044)
- Test: `backend/tests/test_email_intake_poller.py`

**Interfaces:**
- Consumes: `EmailMailbox` (1), `parse_message` (3), `process_message`/`AlreadyProcessed` (5), `VaultService`.
- Produces:
  - `class ImapClient(Protocol)` with `fetch_unseen(...) -> list[tuple[str, bytes]]` and `mark_seen(uid: str) -> None`
  - `def imap_client_factory(...) -> ImapClient` (the real `imaplib.IMAP4_SSL` implementation)
  - `async def poll_mailbox(org_id, mailbox_id, *, engine, vault, store, client_factory=imap_client_factory) -> dict[str, int]`
  - `async def poll_all_mailboxes(*, engine, admin_engine, vault, store) -> int`
  - `STUCK_POLL_CLAIM_AFTER: timedelta`, `IMAP_TIMEOUT_S: float`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_poller.py`. A `FakeImapClient` — a list of `(uid, raw_bytes)` plus a `seen: set[str]` — makes every test run with no mail server:

```python
"""The poller: claim, fetch, process, \\Seen-after-commit, error isolation.

A FakeImapClient stands in for imaplib. The point of these tests is the
ORDER of operations and the failure behaviour, neither of which needs a real
mail server -- and a test that needed one would not run in CI.
"""


class FakeImapClient:
    def __init__(self, messages, *, fail_on_connect=False, fail_on_mark=False):
        self.messages = list(messages)
        self.seen: set[str] = set()
        self.fail_on_connect = fail_on_connect
        self.fail_on_mark = fail_on_mark
        if fail_on_connect:
            raise OSError("imap: connection refused for user 'helpdesk' password 'hunter2'")

    def fetch_unseen(self):
        return [(uid, raw) for uid, raw in self.messages if uid not in self.seen]

    def mark_seen(self, uid):
        if self.fail_on_mark:
            raise OSError("imap: STORE failed")
        self.seen.add(uid)

    def close(self):
        pass


async def test_a_polled_message_becomes_a_ticket_and_is_marked_seen(poll_env) -> None:
    client = FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))])
    result = await poll_env.poll(client)
    assert result["processed"] == 1
    assert client.seen == {"1"}
    assert await poll_env.ticket_count() == 1


async def test_seen_is_set_only_after_the_commit(poll_env) -> None:
    """Spec §4 rule 2. If \\Seen were set first, a crash before the commit
    would lose the message permanently -- unread is recoverable, read is
    not."""
    observed: list[int] = []

    class ObservingClient(FakeImapClient):
        def mark_seen(self, uid):
            # How many tickets exist AT THE MOMENT \Seen is set? If the
            # commit already happened, 1. If \Seen came first, 0.
            observed.append(poll_env.ticket_count_sync())
            super().mark_seen(uid)

    await poll_env.poll(ObservingClient([("1", poll_env.raw(sender="alice@acme.test"))]))
    assert observed == [1], "the ticket must be committed BEFORE \\Seen is set"


async def test_a_redelivered_message_is_marked_seen_and_not_duplicated(poll_env) -> None:
    """Spec §4 rule 3 and §11.2: the unique-violation path is a NORMAL
    outcome. Treating it as a failure is how one stuck message blocks a
    mailbox forever."""
    raw = poll_env.raw(sender="alice@acme.test", message_id="<same@acme.test>")
    await poll_env.poll(FakeImapClient([("1", raw)]))
    # The same message comes back with a new UID -- a crash between the
    # commit and the flag, exactly as §11.2 describes.
    client = FakeImapClient([("2", raw)])
    result = await poll_env.poll(client)
    assert client.seen == {"2"}, "a redelivery must still be flagged, or it repeats forever"
    assert result["already_processed"] == 1
    assert await poll_env.ticket_count() == 1


async def test_one_bad_message_does_not_stop_the_rest(poll_env) -> None:
    """Spec §7, §11.10."""
    client = FakeImapClient(
        [
            ("1", b"\x00\xff garbage"),
            ("2", poll_env.raw(sender="alice@acme.test", message_id="<good@acme.test>")),
        ]
    )
    await poll_env.poll(client)
    assert client.seen == {"1", "2"}
    assert await poll_env.ticket_count() == 1


async def test_a_connection_failure_records_a_sanitized_last_error(poll_env) -> None:
    """Invariant #3 and spec §2.1: `last_error` is a bounded, sanitized
    excerpt -- never a raw server response, never the password."""
    await poll_env.poll_expecting_failure()
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_error
    assert len(mailbox.last_error) <= 500
    assert "hunter2" not in mailbox.last_error
    assert mailbox.claimed_at is None, "a failed poll must release its claim"


async def test_a_failing_mailbox_does_not_stall_the_others(poll_env) -> None:
    """Spec §4: a mailbox that is down must not stall the others."""
    result = await poll_env.poll_all_with_one_broken()
    assert result >= 1, "the healthy mailbox must still have been polled"
    assert await poll_env.ticket_count() == 1


async def test_two_concurrent_polls_claim_the_mailbox_once(poll_env) -> None:
    """Spec §4: FOR UPDATE SKIP LOCKED, so two workers never poll one
    mailbox concurrently and no message is processed twice."""
    import asyncio

    raw = poll_env.raw(sender="alice@acme.test", message_id="<race@acme.test>")
    results = await asyncio.gather(
        poll_env.poll(FakeImapClient([("1", raw)])),
        poll_env.poll(FakeImapClient([("1", raw)])),
    )
    assert sum(r.get("skipped_claimed", 0) for r in results) == 1
    assert await poll_env.ticket_count() == 1


async def test_a_stuck_claim_is_released_and_repolled(poll_env) -> None:
    """A worker that dies mid-poll leaves claimed_at set forever, and the
    mailbox is never polled again -- silently."""
    from app.modules.email_intake.poller import STUCK_POLL_CLAIM_AFTER

    await poll_env.set_claimed_at(datetime.now(UTC) - STUCK_POLL_CLAIM_AFTER * 2)
    client = FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))])
    result = await poll_env.poll(client)
    assert result["processed"] == 1


async def test_an_inactive_mailbox_is_not_polled(poll_env) -> None:
    await poll_env.deactivate()
    result = await poll_env.poll_all()
    assert result == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_poller.py -v`
Expected: `ModuleNotFoundError: No module named 'app.modules.email_intake.imap'`.

- [ ] **Step 3: Implement the transport**

`backend/app/modules/email_intake/imap.py` — a `Protocol` plus one `imaplib.IMAP4_SSL` implementation. Every call blocking, invoked from `poller.py` through `asyncio.to_thread` (a blocking `imaplib` call inline stalls the agent gateway's WebSockets on the same event loop). `IMAP_TIMEOUT_S = 30.0` passed to the `IMAP4_SSL` constructor. `use_ssl=False` selects `imaplib.IMAP4` — for a LAN relay, matching the shape `delivery.py` already accepts for SMTP. Search `UNSEEN`, `FETCH (RFC822)`, and `STORE +FLAGS \\Seen` for `mark_seen`. **This module opens no listening port** (spec §0.1, invariant #2's posture).

- [ ] **Step 4: Implement the poller**

`backend/app/modules/email_intake/poller.py`:

- `poll_mailbox` claims the mailbox in **one short transaction**: release stuck claims (`claimed_at < now - STUCK_POLL_CLAIM_AFTER`) and then `SELECT ... WHERE id = :id AND active AND claimed_at IS NULL FOR UPDATE SKIP LOCKED`, stamp `claimed_at`, commit. Copy `worker.py:deliver_pending`'s structure.
- Check the credential out **inside** the worker, hold it for one poll, never assign it to the mailbox row or log it. Every checkout writes an audit event — this is the vault's existing behaviour and, per spec §3, is intentional rather than noise to be discovered later.
- The IMAP conversation happens **outside** the claim transaction. Holding a database connection open for the length of someone else's network timeout is how a handful of slow mailboxes exhausts the pool.
- Per message: `parse_message` → `org_session` → `process_message` → **commit** → `mark_seen`. `AlreadyProcessed` counts as `already_processed` and **still marks `\Seen`**. Any other exception is caught per-message, recorded, and the loop continues.
- On completion (success or failure): a final short transaction stamps `last_polled_at`, sets `last_error` (sanitized to 500 chars, via a `_sanitize_excerpt` that copies `delivery.py`'s), and clears `claimed_at`. **`claimed_at` is cleared on the failure path too** — a mailbox whose poll throws must not stay claimed forever.
- `poll_all_mailboxes` iterates orgs (admin engine, as `worker.py`'s other sweeps do), then mailboxes, wrapping each in `try/except` so one failure cannot stall the rest.

Then in `backend/app/worker.py`, alongside the `notification-*` entries:

```python
        "email-intake-poll": {
            "task": "app.worker.email_intake_poll",
            "schedule": 120.0,
        },
```

and the task itself, following the shape of `notification_deliver_pending` at line ~1117.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_poller.py -v`
Expected: all PASS.

- [ ] **Step 6: Prove the ordering test can fail**

Move `mark_seen` to before the commit and run `test_seen_is_set_only_after_the_commit`. **Watch it go red** with `observed == [0]`. Restore. This is spec §4 rule 2 and it is invisible to every other test in the file.

- [ ] **Step 7: Commit**

```bash
git add backend/app/modules/email_intake/imap.py backend/app/modules/email_intake/poller.py \
        backend/app/worker.py backend/tests/test_email_intake_poller.py
git commit -m "feat(email-intake): IMAP poller with claim, idempotent processing, error isolation"
```

---

## Task 8: Reply-To on outbound ticket mail (spec correction C3)

**Files:**
- Modify: `backend/app/modules/notification/delivery.py` (`_send_smtp_sync` ~line 496, `_send_email` ~line 534)
- Test: `backend/tests/test_email_intake_reply_to.py`

**Interfaces:**
- Consumes: `mint_email_token` (4), `EmailMailbox` (1), `ticket.email_token` (1).
- Produces: `_send_smtp_sync(..., reply_to: str | None = None)`; `async def reply_to_for_ticket(session, org_id, ticket_id) -> str | None` in `app/modules/email_intake/identity.py`.

**Why this task exists:** without it, nothing ever mints an `email_token` and no requester ever receives an address they can reply to. Every reply-path test in Tasks 4–7 constructs its own token, so the entire feature would be dead code with a fully green suite. See correction C3.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_reply_to.py`:

```python
"""Outbound ticket mail carries a Reply-To that this module can resolve.

Spec §5.2. Without these, `email_token` is never minted in production and
the whole reply path is unreachable -- while every other test passes,
because each constructs its own token.
"""


async def test_an_email_notification_for_a_ticket_mints_a_token(reply_env) -> None:
    ticket_id = await reply_env.a_ticket()
    assert await reply_env.token_for(ticket_id) is None
    await reply_env.send_ticket_notification(ticket_id)
    assert await reply_env.token_for(ticket_id) is not None


async def test_the_reply_to_header_carries_the_token_at_the_mailbox_address(reply_env) -> None:
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    token = await reply_env.token_for(ticket_id)
    assert reply_env.last_message["Reply-To"] == f"helpdesk+t-{token}@acme.test"


async def test_the_token_is_stable_across_notifications(reply_env) -> None:
    """It is in the headers of every message in the thread; a second one
    would orphan the first."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    first = await reply_env.token_for(ticket_id)
    await reply_env.send_ticket_notification(ticket_id)
    assert await reply_env.token_for(ticket_id) == first


async def test_no_mailbox_means_no_reply_to_and_no_token(reply_env) -> None:
    """Email intake is per-org configuration. An org that has not configured
    a mailbox must not receive a Reply-To pointing at an address nobody
    reads -- a reply there vanishes silently."""
    await reply_env.remove_mailbox()
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


async def test_an_inactive_mailbox_gets_no_reply_to(reply_env) -> None:
    await reply_env.deactivate_mailbox()
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    assert reply_env.last_message.get("Reply-To") is None


async def test_a_non_ticket_notification_gets_no_reply_to(reply_env) -> None:
    """Only ticket mail is repliable. A Reply-To on anything else invites a
    reply that resolves to no ticket."""
    await reply_env.send_non_ticket_notification()
    assert reply_env.last_message.get("Reply-To") is None


async def test_the_reply_to_round_trips_through_extract_reply_token(reply_env) -> None:
    """The end-to-end claim: what we SEND is what `identity.py` can PARSE.
    Two independently-correct halves that disagree on format is exactly the
    defect a per-half test cannot see."""
    from app.modules.email_intake.identity import extract_reply_token

    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    header = reply_env.last_message["Reply-To"]
    assert extract_reply_token([header]) == await reply_env.token_for(ticket_id)
```

`reply_env` monkeypatches `delivery._send_smtp_sync` with a recorder capturing the `EmailMessage` (or its kwargs) as `last_message`. Read the existing `tests/test_notification_delivery.py` for how that module already fakes SMTP and reuse it rather than inventing a second approach.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_reply_to.py -v`
Expected: FAIL — `Reply-To` is `None` and `email_token` stays `None`.

- [ ] **Step 3: Implement**

Add to `backend/app/modules/email_intake/identity.py`:

```python
async def reply_to_for_ticket(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> str | None:
    """The `Reply-To` for outbound mail about this ticket, or None.

    Minting the token HERE, lazily, is deliberate: a token is only useful
    once it has been published to a requester, and a ticket that is never
    emailed about never needs one. `secrets.token_urlsafe` at ticket-create
    time would put a permanent 256-bit secret on every ticket the platform
    has ever created, for nothing.

    Returns None -- and mints NOTHING -- when the org has no active mailbox.
    A Reply-To pointing at an address nobody polls is worse than none: the
    requester's reply vanishes silently.
    """
```

It selects the org's active `EmailMailbox` (returning `None` if there is none), loads the ticket, mints and stores `email_token` if it is `NULL`, and returns `f"{local}+t-{token}@{domain}"` split from `mailbox.address` on the last `@`. If the local part already contains a `+`, that is a plus-address being used as the mailbox address and this module cannot nest a second tag — return `None` and record the reason in the docstring.

Then in `delivery.py`: add `reply_to: str | None = None` to `_send_smtp_sync`, setting `message["Reply-To"] = reply_to` only when it is not `None`; and in `_send_email`, when the delivery's event subject is a ticket, call `reply_to_for_ticket` and pass the result through.

Read `_send_email` and the `NotificationDelivery`/`DomainEvent` shape first to find how the subject type and id are available — `domain_events` carries `subject_type`/`subject_id` (`app/core/events.py`), and `subject_type == "ticket"` is the condition.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_reply_to.py tests/test_notification_delivery.py -v`
Expected: all PASS. The notification suite must stay green — this task adds an optional parameter and must change nothing about existing sends.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/notification/delivery.py \
        backend/app/modules/email_intake/identity.py \
        backend/tests/test_email_intake_reply_to.py
git commit -m "feat(email-intake): publish a per-ticket Reply-To on outbound ticket mail"
```

---

## Task 9: API surface

**Files:**
- Create: `backend/app/modules/email_intake/api.py`
- Modify: `backend/app/modules/email_intake/__init__.py`, `backend/app/main.py`
- Test: `backend/tests/test_email_intake_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces: 6 routes under `/api/v1/email-intake`, each `require_permission`-gated.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_intake_api.py`:

```python
"""API surface, RBAC completeness, and the no-credential guarantee (spec §10)."""

from app.core.rbac import route_permission

EMAIL_INTAKE_PREFIX = "/api/v1/email-intake"


def _routes(app):
    return [
        r
        for r in app.router.routes
        if getattr(r, "path", "").startswith(EMAIL_INTAKE_PREFIX)
    ]


async def test_every_route_carries_a_permission(api_env) -> None:
    """Invariant #6, DERIVED from the router rather than a hand-maintained
    list -- a route added later without require_permission fails here.

    NOTE: iterate `app.router.routes`, NOT `app.routes`. In FastAPI 0.139.2
    `app.routes` does not contain `include_router`'d routes, so the obvious
    version of this test passes vacuously over an empty list."""
    routes = _routes(api_env.app)
    assert len(routes) >= 6, f"expected the email-intake routes, found {len(routes)}"
    for route in routes:
        assert route_permission(route) is not None, f"{route.path} has no permission"


async def test_write_routes_require_the_admin_permission(api_env) -> None:
    for route in _routes(api_env.app):
        methods = getattr(route, "methods", set())
        if methods & {"POST", "PATCH", "DELETE"}:
            assert route_permission(route) == "email_intake.admin", route.path


async def test_a_reader_cannot_create_a_mailbox(api_env) -> None:
    """Server-side, not UI-side (invariant #6)."""
    org_id = await api_env.create_org()
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes", json={}, headers=headers
    )
    assert response.status_code == 403


async def test_no_response_contains_the_password_or_a_credential_id(api_env) -> None:
    """Spec §10 and §11.12, invariant #3. Asserted over the RAW BODY, so a
    credential leaking through a nested or newly-added field is caught."""
    env = await _seed_mailbox(api_env, password="hunter2")
    for path in (
        f"{EMAIL_INTAKE_PREFIX}/mailboxes",
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}",
        f"{EMAIL_INTAKE_PREFIX}/messages",
    ):
        response = await api_env.client.get(path, headers=env.admin_headers)
        assert response.status_code == 200, path
        body = response.text
        assert "hunter2" not in body, path
        assert str(env.credential_id) not in body, path
        assert "credential_id" not in body, path


async def test_a_mailbox_from_another_org_is_404(api_env) -> None:
    """Never 403 -- distinguishing 'exists elsewhere' from 'does not exist'
    is an org-membership oracle."""
    env = await _seed_mailbox(api_env)
    other_org = await api_env.create_org("other")
    other_sub = await api_env.create_user(other_org, "org-admin")
    other_headers = await api_env.headers(other_org, other_sub)
    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}", headers=other_headers
    )
    assert response.status_code == 404


async def test_the_message_log_filters_and_reports_the_quarantined_count(api_env) -> None:
    """Spec §10: quarantined mail is mail a human expected to become a
    ticket and which did not. It is invisible unless surfaced."""
    env = await _seed_messages(api_env)
    response = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages", headers=env.admin_headers
    )
    body = response.json()
    assert body["quarantined_count"] == 2

    filtered = await api_env.client.get(
        f"{EMAIL_INTAKE_PREFIX}/messages?disposition=quarantined", headers=env.admin_headers
    )
    assert {m["disposition"] for m in filtered.json()["messages"]} == {"quarantined"}


async def test_the_test_route_never_returns_a_raw_server_error(api_env) -> None:
    """Spec §10: it validates credentials by connecting and listing the
    folder, and returns neither the password nor a raw server response."""
    env = await _seed_mailbox(api_env, password="hunter2", host="127.0.0.1", port=1)
    response = await api_env.client.post(
        f"{EMAIL_INTAKE_PREFIX}/mailboxes/{env.mailbox_id}/test", headers=env.admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "hunter2" not in response.text
    assert len(body["detail"]) <= 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_email_intake_api.py -v`
Expected: FAIL — the routes do not exist, so `_routes(...)` is empty and the first assertion trips.

- [ ] **Step 3: Implement**

`backend/app/modules/email_intake/api.py` — six routes, exactly as spec §10 tabulates, literal segments before `{id}` parameters:

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/email-intake/mailboxes` | `email_intake.read` |
| POST | `/email-intake/mailboxes` | `email_intake.admin` |
| GET | `/email-intake/mailboxes/{mailbox_id}` | `email_intake.read` |
| PATCH | `/email-intake/mailboxes/{mailbox_id}` | `email_intake.admin` |
| DELETE | `/email-intake/mailboxes/{mailbox_id}` | `email_intake.admin` |
| POST | `/email-intake/mailboxes/{mailbox_id}/test` | `email_intake.admin` |
| GET | `/email-intake/messages` | `email_intake.read` |

(Seven route registrations; §10's table collapses GET/PATCH/DELETE on `{id}` into one row.)

Handlers stay thin — resolve the principal, call the Task 2/5 service, map `MailboxNotFoundError → 404` and `MailboxValidationError → 400`. Business logic does not live in route handlers. `/test` connects via the Task 7 client, lists the folder, and returns `{"ok": bool, "detail": str}` with `detail` sanitized to 200 characters; it is audited and rate-limited per org, and returns neither the password nor a raw server error.

`backend/app/modules/email_intake/__init__.py`:

```python
"""Email intake module registration."""

from fastapi import FastAPI

from app.modules.email_intake.api import router


def register(app: FastAPI) -> None:
    app.include_router(router, prefix="/api/v1")
```

Then wire it in `app/main.py` next to the other `register(app)` calls — read how `app.modules.portal` is registered and copy that exactly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_email_intake_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the RBAC test can fail**

Temporarily remove `require_permission` from one route and run `test_every_route_carries_a_permission`. **Watch it go red.** If it stays green, the test is iterating an empty list — fix the test, not the route. Restore.

- [ ] **Step 6: Run the full backend suite**

Run: `cd backend && pytest -q && mypy app && ruff check app tests`
Expected: all green. Fix anything this module broke before moving on.

- [ ] **Step 7: Commit**

```bash
git add backend/app/modules/email_intake/ backend/app/main.py backend/tests/test_email_intake_api.py
git commit -m "feat(email-intake): admin API for mailboxes and the inbound message log"
```

---

## Task 10: Admin UI, docs, and spec amendments

**Files:**
- Create: `frontend/src/modules/email-intake/{api.ts,types.ts,MailboxList.tsx,MailboxForm.tsx,MessageLog.tsx}`
- Modify: the app's route table and navigation (grep for where `portal` or `notification` registers its routes)
- Modify: `docs/modules/email-intake/spec.md`
- Modify: `deploy/.env.example` if the poll interval becomes configurable
- Test: `frontend/src/modules/email-intake/__tests__/`

- [ ] **Step 1: Write the failing frontend tests**

Cover three behaviours, each asserting on rendered output:

1. The mailbox form **never renders a stored password back**. It has a "password" input for setting a new one, and shows "configured" / "not configured" from `has_credential` — a form that round-trips a secret through the browser is invariant #3 in a different coat.
2. The message log shows the **quarantined count prominently** and it comes from the API response, not from counting the current page — a page-local count reads "3 quarantined" when there are 300.
3. Filtering by disposition issues a request with `?disposition=…` rather than filtering client-side.

Do not write a test that asserts `Object.keys()` over a fixture defined in the same file. That test cannot fail.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npm test -- email-intake`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Implement the frontend**

TanStack Query for server state, no `any`, strict TypeScript, small focused components. Read `frontend/src/modules/notification/` and match its conventions — this module introduces no new frontend pattern.

- [ ] **Step 4: Run frontend checks**

Run: `cd frontend && npm test && npx tsc --noEmit && npm run lint`
Expected: all green.

- [ ] **Step 5: Amend the spec**

Edit `docs/modules/email-intake/spec.md` so the spec and the code agree. Each amendment says what changed and why — a spec that silently drifts is worse than one that is wrong in a documented way:

- **§8** — reopen happens **before** the comment, because `closed` is in `lifecycle.TERMINAL_STATUSES` and `add_comment` rejects it (correction C1).
- **§8** — add `fulfilled -> in_progress` to the reopen edges for request-type tickets (correction C2).
- **§5.2 / §1** — state plainly that publishing the Reply-To is a change to `notification/delivery.py`, and that "share no code" describes config and tables, not this header (correction C3). Note that no Reply-To is emitted when the org has no active mailbox.
- **§2.1** — add `claimed_at` to the column list; §4's `FOR UPDATE SKIP LOCKED` claim cannot be implemented without it.
- **§0.3** — name the timestamp the reopen window is measured from, whichever Task 5 used.
- **§6** — record the chosen `SENDER_RATE_LIMIT` / `TICKET_RATE_LIMIT` values and their windows.
- **§13** — add whatever the build deferred, with the reason.

- [ ] **Step 6: Full verification**

Run: `cd backend && pytest -q && mypy app && ruff check app tests` and `cd frontend && npm test && npx tsc --noEmit`
Expected: all green. Record the four counts (backend tests, frontend tests, mypy files, lint) — they go in the PR body.

- [ ] **Step 7: End-to-end smoke test against a live database**

Not a unit test — a script under the scratchpad directory that drives the real services against a real Postgres:

1. Create an org, two users, a category, and a mailbox.
2. Feed a `FakeImapClient` a genuine RFC822 message from user A → assert a ticket exists with `origin='email'` and the right requester.
3. Send a notification for that ticket → assert a `Reply-To` was emitted and read the token out of the header.
4. Feed a reply **to that header address** from user A → assert a public comment landed.
5. Feed the same reply from user B → assert quarantined, and that **no** comment reached A's ticket.
6. Feed step 4's message again with a new UID → assert one comment, not two.

Every previous sub-project in this module had a defect that only a live end-to-end run found. Run this, fix what it surfaces, and say plainly in the report what it did and did not cover.

- [ ] **Step 8: Commit and push**

```bash
git add frontend/src/modules/email-intake/ docs/modules/email-intake/spec.md
git commit -m "feat(email-intake): admin UI, and spec amendments from the build"
git push -u origin claude/platform-superpower-skills-7fxkgr
```

---

## Self-review

**Spec coverage.** §0.1 IMAP-only → Task 7. §0.2 two independent checks → Tasks 4 and 5 (with the wrong-sender mutation watched red). §0.3 reopen window → Task 5. §2.1/§2.2/§2.3 → Task 1. §3 credentials → Tasks 2 and 7. §4 idempotency → Tasks 5 and 7. §5.1 sender resolution and the `users` index → Tasks 1 and 4. §5.2 the token → Tasks 4 and 8. §6 loop/bounce → Task 3; rate limits → Task 6. §7 MIME → Task 3. §8 the disposition table → Task 5. §9 RBAC/tenancy/audit → Tasks 1, 2, 9. §10 API → Task 9. §11's thirteen tests: 1→T5, 2→T7, 3→T5, 4→T5, 5→T3, 6→T3, 7→T5, 8→T5, 9→T3, 10→T3/T7, 11→T1, 12→T2/T9, 13→T9. §12 migration → Task 1. §13 deferrals → nothing built, confirmed in Task 10.

**Gaps found and closed while reviewing.** The spec never says what happens when a mailbox with processed messages is deleted (`inbound_message.mailbox_id` is a NOT NULL FK) — Task 2 forces the decision. The spec never names the timestamp the reopen window measures from — Task 5 forces it and Task 10 records it. `claimed_at` is required by §4 but absent from §2.1's column list — added in Task 1, recorded in Task 10.

**Naming consistency.** `process_message`, `poll_mailbox`, `resolve_sender`, `extract_reply_token`, `resolve_ticket_by_token`, `mint_email_token`, `reply_to_for_ticket`, `reopen_target` are each defined once and referenced with the same name and signature everywhere. `ParsedMessage`/`ParsedAttachment` are Task 3's and used unchanged in 5 and 7. Table names are `email_mailbox` and `inbound_message` (singular, matching `ticket`/`ticket_queue`) throughout.

**Known risk this plan does not remove.** Tasks 5–7 lean on an `email_env`/`poll_env`/`reply_env` fixture each task builds itself. If a fixture papers over a real integration seam — the way `conftest.py`'s grants masked the portal's missing `ITS_DATABASE_PORTAL_URL` — the suite goes green over a module that cannot run in a real deployment. Task 10 step 7 exists specifically for that, and is not optional.

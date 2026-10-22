# Service Desk (ticket core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Service Desk ticket core — incidents and service requests, queues and assignment, per-category request approval, comments and attachments — as the platform's own ticketing system.

**Architecture:** Control-plane only, no agent capability and no worker. One `ticket` table with a `type` discriminator (`incident` | `request`) whose two lifecycles are enforced by a single shared state machine plus a DB check constraint. Business logic lives in services; route handlers translate errors and nothing more. Every query is org-scoped in addition to RLS.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, PostgreSQL with RLS. Frontend: React + TypeScript (strict), Vite, TanStack Query.

**Spec:** `docs/modules/service-desk/spec.md` — authoritative. Where this plan and the spec disagree, the spec wins and the plan is wrong.

## Global Constraints

- **Migration head is `0022`.** This module's migration is `0023`. Task 1 confirms the head before writing it. Migrations are forward-only — never edit a shipped one.
- **Backend suite is at 1453 passing.** Every task ends green. Gates: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m mypy app/`, `.venv/bin/python -m ruff check app/ tests/`.
- **Environment first, every session:** `pg_ctlcluster 16 main start ; redis-server --daemonize yes`
- **Run gates in the FOREGROUND** as plain blocking commands. The suite takes ~3.5 minutes. Do not background it, sleep-poll, or launch monitors.
- **Use `.venv/bin/python`, never bare `python`.** Run backend commands from `backend/`.
- **Type hints are mandatory**; mypy runs strict. No `any` in TypeScript.
- **Every query carries an explicit `org_id` predicate** in addition to RLS (invariant #5).
- **Every privileged mutation writes an audit event** (invariant #4); no audit on view.
- **`org_id`/`actor_id` always come from `AuthContext`**, never from a request body or path.
- **`backend/tests/test_schema_drift.py` compares `Base.metadata` to the live schema.** Model and migration must agree exactly on nullability, numeric precision/scale, and varchar length. It will fail the build if they drift.
- **`backend/tests/test_rls.py` auto-covers every table with an `org_id` column.** A new table without `ENABLE + FORCE` RLS and a `tenant_isolation` policy fails it.
- Statuses are `str(32)` everywhere they appear (spec §2). `pending_approval` is exactly 16 characters; a column sized to its longest current value has no headroom.

---

## File structure

**Backend** (`backend/app/modules/service_desk/`):

| File | Responsibility |
|---|---|
| `models.py` | ORM models for the eight tables |
| `schema.py` | Pydantic request/response models |
| `errors.py` | `TicketValidationError`, `TicketNotFoundError` (a subclass) |
| `lifecycle.py` | Pure state machine — allowed statuses and transitions per type. No DB, no I/O |
| `numbering.py` | Race-safe ticket-number allocation |
| `refdata.py` | Queues and categories, including the lazy seed |
| `tickets.py` | Create, get, list, update |
| `transitions.py` | Status changes and assignment |
| `approvals.py` | Request approval and rejection |
| `threads.py` | Comments and attachments |
| `api.py` | Routes, registration, error translation |
| `adapters.py` | `ServiceDeskChangeTicketAdapter` for change management's port |

**Frontend** (`frontend/src/modules/service-desk/`): `types.ts`, `api.ts`, `serviceDeskState.ts`, `TicketListPage.tsx`, `TicketDetailPage.tsx`, `MyWorkPage.tsx`, `ServiceDeskAdminPage.tsx`, `ServiceDeskNav.tsx`, `__tests__/serviceDesk.test.tsx`.

Split rationale: `lifecycle.py` and `numbering.py` are separated from `tickets.py` because both are pure, heavily-tested logic that other tasks consume — and because a second copy of "what state can this be in" is exactly what produced two Critical double-counts in the Budget module. `threads.py` holds comments and attachments together because they share a parent and an authorization rule.

---

## Task 1: Migration 0023 — tables, RLS, RBAC

**Files:**
- Create: `backend/migrations/versions/0023_service_desk.py`
- Modify: `backend/app/core/rbac.py` (add the five permission codes)
- Modify: `backend/tests/test_rbac.py` (assert the five codes exist)
- Test: `backend/tests/test_service_desk_migration.py`

**Interfaces:**
- Produces: tables `ticket_queue`, `ticket_category`, `ticket_number_counter`, `ticket`, `ticket_comment`, `ticket_attachment`, `ticket_approval`, `ticket_status_history`; permission codes `ticket.read`, `ticket.write`, `ticket.assign`, `ticket.approve`, `ticket.admin`.

**Critical:** the RBAC codes must land in `app/core/rbac.py` and `tests/test_rbac.py` **in this task**. Seeding permissions in the migration without registering them leaves the suite red for every later task, which makes every later task's "gates green" claim meaningless.

- [ ] **Step 1: Confirm the migration head**

Run: `cd backend && .venv/bin/python -m alembic heads`
Expected: `0022 (head)`. If it is not `0022`, stop and re-derive the revision number — do not guess.

- [ ] **Step 2: Write the failing migration test**

```python
# backend/tests/test_service_desk_migration.py
"""Service Desk migration (docs/modules/service-desk/spec.md §2, §8.2)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_TABLES = [
    "ticket_queue",
    "ticket_category",
    "ticket_number_counter",
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_approval",
    "ticket_status_history",
]

_PERMISSIONS = [
    "ticket.read",
    "ticket.write",
    "ticket.assign",
    "ticket.approve",
    "ticket.admin",
]


async def test_all_tables_exist_with_forced_rls(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        for table in _TABLES:
            row = (
                await conn.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                        " JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'public' AND c.relname = :t"
                    ),
                    {"t": table},
                )
            ).one_or_none()
            assert row is not None, f"{table} does not exist"
            assert row == (True, True), f"{table} RLS not enabled+forced: {row}"


async def test_permissions_seeded(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text("SELECT code FROM permissions WHERE code LIKE 'ticket.%'")
            )
        }
    assert codes == set(_PERMISSIONS)


async def test_technician_holds_read_write_assign_only(admin_engine: AsyncEngine) -> None:
    """Spec §0.6: the inverse of Budget. Service Desk is a technician's primary
    tool, so technician gets read/write/assign -- and deliberately NOT approve
    (a requester must not authorize their own request) or admin."""
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text(
                    "SELECT p.code FROM permissions p"
                    " JOIN role_permissions rp ON rp.permission_id = p.id"
                    " JOIN roles r ON r.id = rp.role_id"
                    " WHERE r.name = 'technician' AND p.code LIKE 'ticket.%'"
                )
            )
        }
    assert codes == {"ticket.read", "ticket.write", "ticket.assign"}


async def test_auditor_holds_read_only(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        codes = {
            r.code
            for r in await conn.execute(
                text(
                    "SELECT p.code FROM permissions p"
                    " JOIN role_permissions rp ON rp.permission_id = p.id"
                    " JOIN roles r ON r.id = rp.role_id"
                    " WHERE r.name = 'auditor' AND p.code LIKE 'ticket.%'"
                )
            )
        }
    assert codes == {"ticket.read"}
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_service_desk_migration.py -q`
Expected: FAIL — `ticket_queue does not exist`.

- [ ] **Step 4: Write the migration**

Follow `backend/migrations/versions/0020_budget.py` exactly for style — `_rls(table)` helper, `PERMISSIONS` list, `ROLE_GRANTS` dict, and the same permission/role seeding loop at the bottom. Copy that structure rather than inventing one.

Key details that must be right:

```python
revision: str = "0023"
down_revision: str | None = "0022"

PERMISSIONS = [
    ("ticket.read", "ticket", "read"),
    ("ticket.write", "ticket", "write"),
    ("ticket.assign", "ticket", "assign"),
    ("ticket.approve", "ticket", "approve"),
    ("ticket.admin", "ticket", "admin"),
]
_ALL = [c for c, _, _ in PERMISSIONS]
# Spec §0.6: the INVERSE of Budget. This is a technician's primary tool, so
# technician holds read/write/assign. `approve` is withheld deliberately -- §4
# forbids a requester approving their own request, and that separation is
# meaningless if every technician holds both halves.
ROLE_GRANTS = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["ticket.read", "ticket.write", "ticket.assign"],
    "auditor": ["ticket.read"],
    "read-only": ["ticket.read"],
}
```

Column shapes that the schema-drift test will enforce against the models in Task 2 — get them right here and copy them verbatim there:

- `ticket.status`, `ticket_status_history.from_status`, `ticket_status_history.to_status`: `varchar(32)`
- `ticket.type`, `ticket.priority`, `ticket.origin`, `ticket_category.type`, `ticket_comment.author_type`, `ticket_comment.visibility`, `ticket_approval.state`, `ticket_status_history.actor_type`: `varchar(16)`
- `ticket.number`: `varchar(24)`
- `*.key`: `varchar(64)`; `*.name`, `ticket.subject`, `ticket_attachment.filename`: `varchar(255)`
- `ticket.requester_id`, `ticket.created_by`: **nullable** (spec §2)
- `ticket_comment.author_id`, `ticket_status_history.actor_id`, `ticket_approval.decided_by`: nullable

Check constraints, each with the spec reason in a comment:

```python
op.execute(
    "ALTER TABLE ticket ADD CONSTRAINT ck_ticket_status_matches_type CHECK ("
    " (type = 'incident' AND status IN"
    "   ('new','open','pending','resolved','closed','cancelled'))"
    " OR (type = 'request' AND status IN"
    "   ('new','pending_approval','in_progress','fulfilled','closed',"
    "    'rejected','cancelled')))"
)
# Spec §2: a machine-origin ticket has no human requester or author, and
# inventing a synthetic user to satisfy NOT NULL would put a fake person in the
# audit trail. Every other origin must keep its human attribution.
op.execute(
    "ALTER TABLE ticket ADD CONSTRAINT ck_ticket_human_attribution CHECK ("
    " origin = 'alert' OR (requester_id IS NOT NULL AND created_by IS NOT NULL))"
)
# Spec §2: requires_approval is meaningful only for requests.
op.execute(
    "ALTER TABLE ticket_category ADD CONSTRAINT ck_category_approval_is_request_only"
    " CHECK (type = 'request' OR requires_approval = false)"
)
# Spec §2: a machine comment is never attributed to a person.
op.execute(
    "ALTER TABLE ticket_comment ADD CONSTRAINT ck_comment_author CHECK ("
    " (author_type = 'user' AND author_id IS NOT NULL)"
    " OR (author_type = 'system' AND author_id IS NULL))"
)
```

Unique constraints: `(org_id, key)` on `ticket_queue` and `ticket_category`; `(org_id, number)` on `ticket`; `(org_id, ticket_id)` on `ticket_approval`.

Indexes on `ticket`: `(org_id, status)`, `(org_id, queue_id, status)`, `(org_id, assigned_to, status)`, `(org_id, requester_id)`, `(org_id, device_id)`.

`ticket_number_counter` has `org_id` as its primary key (no surrogate `id`) and still needs RLS — it carries `org_id`, so `test_rls.py` will demand it.

`downgrade()` drops the tables in FK-dependency order and deletes the seeded permissions.

- [ ] **Step 5: Register the permission codes**

In `backend/app/core/rbac.py`, add to the permission-description dict, next to the budget entries:

```python
    "ticket.read": "View tickets, comments, attachments, queues, and categories",
    "ticket.write": (
        "Create tickets, comment, edit ticket fields, and upload attachments"
    ),
    "ticket.assign": "Assign or reassign a ticket, and move it between queues",
    "ticket.approve": "Approve or reject a service request",
    "ticket.admin": "Manage ticket queues and categories",
```

In `backend/tests/test_rbac.py`, add the five codes to whatever list that test asserts against.

- [ ] **Step 6: Run migrations and the full suite**

Run: `.venv/bin/python -m alembic upgrade head && .venv/bin/python -m pytest tests/ -q`
Expected: the four new tests pass; total 1457 passed. `test_rls.py` and `test_schema_drift.py` must both still pass — `test_schema_drift` will fail if the models do not exist yet, so if it complains about missing tables in `Base.metadata`, that is Task 2's job and this task's migration test should still be green.

If `test_schema_drift.py` fails here because the new tables are not in `Base.metadata`, note it and proceed — Task 2 closes it. Do not weaken the drift test to accommodate the gap.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0023_service_desk.py backend/app/core/rbac.py \
        backend/tests/test_rbac.py backend/tests/test_service_desk_migration.py
git commit -m "feat(service-desk): migration 0023 — tables, RLS, RBAC"
```

---

## Task 2: ORM models

**Files:**
- Create: `backend/app/modules/service_desk/__init__.py`, `backend/app/modules/service_desk/models.py`
- Modify: `backend/app/models/__init__.py` (re-export the new models)
- Test: covered by `backend/tests/test_schema_drift.py` (already exists)

**Interfaces:**
- Consumes: the tables from Task 1.
- Produces: `TicketQueue`, `TicketCategory`, `TicketNumberCounter`, `Ticket`, `TicketComment`, `TicketAttachment`, `TicketApproval`, `TicketStatusHistory`.

- [ ] **Step 1: Write the models**

Follow `backend/app/modules/budget/models.py` for style: `uuid7_pk()`, `Mapped[...]` annotations, `__table_args__` carrying the indexes and unique constraints with the same names the migration used.

Every column's type, length, and nullability must match migration 0023 **exactly** — `test_schema_drift.py` compares them and will fail on any divergence. `Base.type_annotation_map` maps `datetime` to `DateTime(timezone=True)`, so a bare `Mapped[datetime]` is correct for `timestamptz` columns.

- [ ] **Step 2: Re-export**

Add the eight models to `backend/app/models/__init__.py` alongside the budget ones.

- [ ] **Step 3: Run the drift test**

Run: `.venv/bin/python -m pytest tests/test_schema_drift.py -q`
Expected: PASS. Any failure names the exact column and the exact mismatch — fix the model or the migration to agree, and if the migration is wrong, fix it *in 0023* since it has not shipped yet.

- [ ] **Step 4: Gates and commit**

```bash
git add backend/app/modules/service_desk/ backend/app/models/__init__.py
git commit -m "feat(service-desk): ORM models"
```

---

## Task 3: Schemas and typed errors

**Files:**
- Create: `backend/app/modules/service_desk/errors.py`, `backend/app/modules/service_desk/schema.py`
- Test: `backend/tests/test_service_desk_schema.py`

**Interfaces:**
- Produces:
  - `TicketValidationError(Exception)`, `TicketNotFoundError(TicketValidationError)` — the subclass relationship is load-bearing: `api.py` maps not-found to 404 by `isinstance` with the subclass checked **first**.
  - `TicketType = Literal["incident", "request"]`, `TicketPriority = Literal["low","normal","high","urgent"]`, `TicketOrigin = Literal["technician","alert","portal","email"]`, `CommentVisibility = Literal["public","internal"]`.
  - `QueueCreate`, `QueueUpdate`, `QueueOut`, `CategoryCreate`, `CategoryUpdate`, `CategoryOut`, `TicketCreate`, `TicketUpdate`, `TicketOut`, `TicketStatusChange`, `TicketAssign`, `ApprovalDecision`, `CommentCreate`, `CommentOut`, `AttachmentOut`, `StatusHistoryOut`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_service_desk_schema.py
import pytest
from pydantic import ValidationError

from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.schema import TicketCreate, TicketUpdate


def test_not_found_is_a_validation_error_subclass() -> None:
    """api.py maps by isinstance with the subclass checked FIRST; if this
    relationship breaks, every 404 silently becomes a 400."""
    assert issubclass(TicketNotFoundError, TicketValidationError)


def test_ticket_create_requires_subject_and_category() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="incident")


def test_ticket_create_defaults_priority_to_normal() -> None:
    from uuid import uuid4

    t = TicketCreate(type="incident", category_id=uuid4(), subject="Printer on fire")
    assert t.priority == "normal"
    assert t.description == ""


def test_ticket_update_distinguishes_omitted_from_explicit_null() -> None:
    """The service layer decides by key PRESENCE, so the route must be able to
    pass model_dump(exclude_unset=True) and have omitted mean 'do not touch'."""
    from uuid import uuid4

    omitted = TicketUpdate().model_dump(exclude_unset=True)
    assert "assigned_to" not in omitted

    cleared = TicketUpdate(assigned_to=None).model_dump(exclude_unset=True)
    assert cleared == {"assigned_to": None}

    set_to = uuid4()
    assigned = TicketUpdate(assigned_to=set_to).model_dump(exclude_unset=True)
    assert assigned == {"assigned_to": set_to}
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/test_service_desk_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.service_desk.errors`.

- [ ] **Step 3: Write `errors.py`**

```python
"""Typed errors for the Service Desk module.

`TicketNotFoundError` subclasses `TicketValidationError` so a single
`except TicketValidationError` in a service catches both, while `api.py`
distinguishes them by `isinstance` with the SUBCLASS CHECKED FIRST to map
404 vs 400. Never route on exception-message text.
"""


class TicketValidationError(Exception):
    """A request the service layer rejects -- becomes HTTP 400."""


class TicketNotFoundError(TicketValidationError):
    """A resource that does not exist under this org -- becomes HTTP 404.

    A cross-org id and a nonexistent id are deliberately indistinguishable
    (invariant #5): both raise this.
    """
```

- [ ] **Step 4: Write `schema.py`**

Follow `backend/app/modules/budget/schema.py` for style. Notes that matter:

- `TicketOut` carries **no `org_id` field** (invariant #5).
- `TicketOut` exposes `has_attachments: bool` and never an object key.
- Every `*Update` model has all-optional fields so `exclude_unset` is meaningful.
- `status` is **absent** from `TicketUpdate`: status moves only through the dedicated transition endpoint, never as a side effect of a generic PATCH. Same reasoning as Budget's `ExpenseUpdate` excluding `status`.

- [ ] **Step 5: Run tests, gates, commit**

```bash
git add backend/app/modules/service_desk/errors.py \
        backend/app/modules/service_desk/schema.py \
        backend/tests/test_service_desk_schema.py
git commit -m "feat(service-desk): schemas and typed errors"
```

---

## Task 4: `lifecycle.py` — the state machine

**Files:**
- Create: `backend/app/modules/service_desk/lifecycle.py`
- Test: `backend/tests/test_service_desk_lifecycle.py`

**Interfaces:**
- Produces:
  - `INCIDENT_STATUSES: frozenset[str]`, `REQUEST_STATUSES: frozenset[str]`
  - `TERMINAL_STATUSES: frozenset[str]` — `{"closed", "cancelled", "rejected"}`
  - `initial_status(ticket_type: str, *, requires_approval: bool) -> str`
  - `can_transition(ticket_type: str, from_status: str, to_status: str) -> bool`
  - `assert_transition(ticket_type: str, from_status: str, to_status: str) -> None` — raises `TicketValidationError` naming both statuses

This is pure logic: no database, no session, no I/O. Every other task imports from here. **There must be no second implementation of "what state can this be in" anywhere in the module** — the Budget module produced two Critical double-counts precisely because two files each implemented the same guard and disagreed.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_service_desk_lifecycle.py
"""The ticket state machine (docs/modules/service-desk/spec.md §3).

Asserts the INVALID transitions as hard as the valid ones. A state machine
tested only on its happy path is unproven: the whole point is what it refuses.
"""

import pytest

from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import (
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
    initial_status,
)


def test_incident_starts_new_and_walks_to_closed() -> None:
    assert initial_status("incident", requires_approval=False) == "new"
    for a, b in [
        ("new", "open"),
        ("open", "pending"),
        ("pending", "open"),
        ("open", "resolved"),
        ("resolved", "closed"),
    ]:
        assert can_transition("incident", a, b), f"{a} -> {b} should be allowed"


def test_request_skips_approval_when_category_requires_none() -> None:
    assert initial_status("request", requires_approval=False) == "in_progress"
    assert initial_status("request", requires_approval=True) == "pending_approval"


def test_request_walks_approval_to_closed() -> None:
    for a, b in [
        ("pending_approval", "in_progress"),
        ("pending_approval", "rejected"),
        ("in_progress", "fulfilled"),
        ("fulfilled", "closed"),
    ]:
        assert can_transition("request", a, b), f"{a} -> {b} should be allowed"


def test_reopen_is_allowed_from_resolved_and_closed() -> None:
    assert can_transition("incident", "resolved", "open")
    assert can_transition("incident", "closed", "open")
    assert can_transition("request", "fulfilled", "in_progress")
    assert can_transition("request", "closed", "in_progress")


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES - {"closed"}))
def test_no_transition_leaves_cancelled_or_rejected(terminal: str) -> None:
    """`closed` is deliberately reopenable; `cancelled` and `rejected` are not.
    A cancelled ticket that can be revived is a ticket whose history lies."""
    ticket_type = "request" if terminal == "rejected" else "incident"
    for target in ("new", "open", "in_progress", "resolved", "closed"):
        assert not can_transition(ticket_type, terminal, target)


def test_incident_cannot_use_request_statuses() -> None:
    assert not can_transition("incident", "new", "pending_approval")
    assert not can_transition("incident", "new", "in_progress")
    assert not can_transition("request", "new", "resolved")
    assert not can_transition("request", "in_progress", "pending")


def test_cannot_skip_states() -> None:
    assert not can_transition("incident", "new", "closed")
    assert not can_transition("incident", "new", "resolved")
    assert not can_transition("request", "pending_approval", "fulfilled")


def test_assert_transition_names_both_statuses() -> None:
    with pytest.raises(TicketValidationError) as exc:
        assert_transition("incident", "new", "closed")
    assert "new" in str(exc.value) and "closed" in str(exc.value)


def test_cancel_is_reachable_from_every_non_terminal_state() -> None:
    for status in ("new", "open", "pending", "resolved"):
        assert can_transition("incident", status, "cancelled")
    for status in ("new", "pending_approval", "in_progress", "fulfilled"):
        assert can_transition("request", status, "cancelled")
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/test_service_desk_lifecycle.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Model the transitions as an explicit `dict[str, dict[str, frozenset[str]]]` keyed by ticket type then from-status. Explicit beats clever: a reader must be able to see the whole machine at once, and the check constraint in migration 0023 has to be kept in agreement with it by eye.

- [ ] **Step 4: Verify the machine agrees with the DB constraint**

Run: `.venv/bin/python -c "from app.modules.service_desk.lifecycle import INCIDENT_STATUSES, REQUEST_STATUSES; print(sorted(INCIDENT_STATUSES)); print(sorted(REQUEST_STATUSES))"`
Compare against `ck_ticket_status_matches_type` in migration 0023. They must list the same statuses. If they differ, one of them is wrong — fix it now, because the mismatch surfaces later as an `IntegrityError` 500 on a transition the service layer thought was legal.

- [ ] **Step 5: Gates and commit**

```bash
git add backend/app/modules/service_desk/lifecycle.py \
        backend/tests/test_service_desk_lifecycle.py
git commit -m "feat(service-desk): ticket state machine"
```

---

## Task 5: `numbering.py` — race-safe ticket numbers

**Files:**
- Create: `backend/app/modules/service_desk/numbering.py`
- Test: `backend/tests/test_service_desk_numbering.py`

**Interfaces:**
- Produces: `async def next_number(session: AsyncSession, org_id: UUID, ticket_type: str) -> str` returning `"INC-1042"` / `"REQ-233"`.

**This is the task most likely to be got subtly wrong.** A read-then-write produces duplicate user-visible ticket numbers under concurrency — permanent, and visible to customers. The platform hardening PR swept this exact class out of five modules; do not reintroduce it here.

- [ ] **Step 1: Write the failing test, including the concurrency case**

```python
# backend/tests/test_service_desk_numbering.py
"""Ticket-number allocation (docs/modules/service-desk/spec.md §2.1).

The concurrency test is the point of this file. A check-then-increment
implementation passes every sequential test here and produces duplicate
numbers in production.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import TicketNumberCounter
from app.modules.service_desk.numbering import next_number


async def test_numbers_start_at_one_and_increment(api_env) -> None:
    org_id = await api_env.create_org("sd-num")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_id, "incident") == "INC-1"
        assert await next_number(s, org_id, "incident") == "INC-2"
        await s.commit()


async def test_incident_and_request_sequences_are_independent(api_env) -> None:
    org_id = await api_env.create_org("sd-num-split")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_id, "incident") == "INC-1"
        assert await next_number(s, org_id, "request") == "REQ-1"
        assert await next_number(s, org_id, "incident") == "INC-2"
        await s.commit()


async def test_sequences_are_per_org(api_env) -> None:
    org_a = await api_env.create_org("sd-num-a")
    org_b = await api_env.create_org("sd-num-b")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        assert await next_number(s, org_a, "incident") == "INC-1"
        assert await next_number(s, org_b, "incident") == "INC-1"
        await s.commit()


async def test_concurrent_allocation_never_duplicates(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two sessions allocating at the same instant must get different numbers.

    Deterministic interleave, not a hopeful race: session A's counter row is
    created and committed first, then both sessions allocate against it in
    overlapping transactions. A read-then-write implementation returns INC-1
    twice here; the atomic UPDATE ... RETURNING serializes on the row lock and
    returns INC-1 and INC-2.
    """
    org_id = await api_env.create_org("sd-num-race")

    async with AsyncSession(admin_engine, expire_on_commit=False) as warmup:
        await next_number(warmup, org_id, "incident")  # creates the counter row
        await warmup.commit()

    async with (
        AsyncSession(admin_engine, expire_on_commit=False) as sa,
        AsyncSession(admin_engine, expire_on_commit=False) as sb,
    ):
        a = await next_number(sa, org_id, "incident")
        await sa.commit()
        b = await next_number(sb, org_id, "incident")
        await sb.commit()

    assert a != b, f"duplicate ticket number allocated: {a} == {b}"
    assert {a, b} == {"INC-2", "INC-3"}

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        counter = (
            await s.execute(
                select(TicketNumberCounter).where(TicketNumberCounter.org_id == org_id)
            )
        ).scalar_one()
        assert counter.next_incident == 4
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/test_service_desk_numbering.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement with a single atomic statement**

```python
"""Race-safe ticket-number allocation (spec §2.1).

The number is user-visible and permanent, so a duplicate is not a transient
error -- it is two tickets that can never be told apart again. Allocation is
therefore a SINGLE atomic `UPDATE ... RETURNING`, never a SELECT followed by
an UPDATE: between those two statements a concurrent caller reads the same
value and both allocate the same number.

The counter row is created lazily with `on_conflict_do_nothing` so two
concurrent first-ever calls in one org cannot both insert (the same shape
`fx.get_settings` and `refdata.seed_default_categories` use).

Gaps are acceptable: a rolled-back transaction consumes its number and does
not return it. Reusing numbers would be far worse than skipping them.
"""

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TicketNumberCounter
from app.modules.service_desk.errors import TicketValidationError

_PREFIX = {"incident": "INC", "request": "REQ"}
_COLUMN = {
    "incident": TicketNumberCounter.next_incident,
    "request": TicketNumberCounter.next_request,
}


async def next_number(session: AsyncSession, org_id: UUID, ticket_type: str) -> str:
    if ticket_type not in _PREFIX:
        raise TicketValidationError(f"unknown ticket type: {ticket_type}")

    await session.execute(
        pg_insert(TicketNumberCounter)
        .values(org_id=org_id)
        .on_conflict_do_nothing(index_elements=["org_id"])
    )

    column = _COLUMN[ticket_type]
    allocated = (
        await session.execute(
            update(TicketNumberCounter)
            .where(TicketNumberCounter.org_id == org_id)
            .values({column: column + 1})
            .returning(column)
        )
    ).scalar_one()

    return f"{_PREFIX[ticket_type]}-{allocated - 1}"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_service_desk_numbering.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Prove the concurrency test is load-bearing**

Temporarily replace the atomic `UPDATE ... RETURNING` with a read-then-write:

```python
    row = (await session.execute(
        select(TicketNumberCounter).where(TicketNumberCounter.org_id == org_id)
    )).scalar_one()
    allocated = getattr(row, "next_incident" if ticket_type == "incident" else "next_request")
    setattr(row, "next_incident" if ticket_type == "incident" else "next_request", allocated + 1)
    await session.flush()
    return f"{_PREFIX[ticket_type]}-{allocated}"
```

Run the concurrency test. Expected: FAIL. Restore the atomic version exactly, confirm `git diff` on `numbering.py` shows only your intended implementation, and re-run to PASS. **Report both observations** — a concurrency test that would pass against the broken implementation is worth nothing.

- [ ] **Step 6: Gates and commit**

```bash
git add backend/app/modules/service_desk/numbering.py \
        backend/tests/test_service_desk_numbering.py
git commit -m "feat(service-desk): race-safe ticket numbering"
```

---

## Task 6: `refdata.py` — queues and categories

**Files:**
- Create: `backend/app/modules/service_desk/refdata.py`
- Test: `backend/tests/test_service_desk_refdata.py`

**Interfaces:**
- Produces:
  - `DEFAULT_QUEUES: tuple[tuple[str, str], ...]` — `(("helpdesk","Helpdesk"), ("infrastructure","Infrastructure"), ("security","Security"))`
  - `async def ensure_default_queues(session, org_id, actor_id) -> list[TicketQueue]`
  - `async def list_queues(session, org_id, *, include_inactive: bool = False) -> list[TicketQueue]`
  - `async def create_queue(session, org_id, actor_id, *, key, name, description="") -> TicketQueue`
  - `async def update_queue(session, org_id, actor_id, queue_id, fields: dict) -> TicketQueue`
  - `async def resolve_queue(session, org_id, queue_id) -> TicketQueue`
  - the same five for categories, plus `async def resolve_category(session, org_id, category_id) -> TicketCategory`

- [ ] **Step 1: Write the failing tests**

Cover: the lazy seed creates exactly three queues and is idempotent; the seed emits exactly one `ticket.queues_seeded` audit event with `actor_type="system"` and `actor_id=None`, and emits nothing at all on a second call; a duplicate `key` raises `TicketValidationError` rather than surfacing an `IntegrityError`; a concurrent duplicate does the same (deterministic interleave — intercept the pre-check SELECT, have a second session commit the competing row inside the window, following `tests/test_budget_rates.py`'s concurrent-duplicate test); `resolve_queue`/`resolve_category` raise `TicketNotFoundError` for a cross-org id; a category with `type="incident"` and `requires_approval=True` is rejected as a typed 400; `list_*` excludes inactive by default and includes it with `include_inactive=True`, ordered by `sort_order, name` with an explicit `ORDER BY`.

- [ ] **Step 2: Run to confirm failure, then implement**

Follow `backend/app/modules/budget/refdata.py` closely — it solved every one of these problems already, including the audited system-actor lazy seed and the `on_conflict_do_nothing` duplicate handling. Read it before writing.

The seed's audit event is **one** `ticket.queues_seeded` with `actor_type="system"` and `actor_id=None`, naming the keys it actually inserted, and **nothing** when it inserts nothing. Per-row user-attributed events on a lazy seed were a Budget defect: a read-only user opening a page became, in the immutable audit log, the creator of the org's reference data.

- [ ] **Step 3: Gates and commit**

```bash
git commit -m "feat(service-desk): queues and categories"
```

---

## Task 7: `tickets.py` — create, get, list, update

**Files:**
- Create: `backend/app/modules/service_desk/tickets.py`
- Test: `backend/tests/test_service_desk_tickets.py`

**Interfaces:**
- Consumes: `lifecycle.initial_status`, `numbering.next_number`, `refdata.resolve_queue`, `refdata.resolve_category`.
- Produces:
  - `async def create_ticket(session, org_id, actor_id, data: TicketCreate, *, origin: str = "technician", source_alert_id: UUID | None = None) -> Ticket`
  - `async def get_ticket(session, org_id, ticket_id) -> Ticket`
  - `async def list_tickets(session, org_id, *, type=None, status=None, queue_id=None, assigned_to=None, requester_id=None, device_id=None, priority=None, limit=50, offset=0) -> list[Ticket]`
  - `async def update_ticket(session, org_id, actor_id, ticket_id, fields: dict) -> Ticket`
  - `TICKET_EDITABLE_FIELDS: tuple[str, ...]`, `NON_NULLABLE_TICKET_FIELDS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

Cover:
- Creation resolves category and queue **org-scoped**; a cross-org `category_id`, `queue_id`, `device_id`, `requester_id`, or `assigned_to` raises `TicketNotFoundError`.
- Queue defaults from `ticket_category.default_queue_id`; an explicit `queue_id` overrides it; when neither resolves, creation raises `TicketValidationError` rather than landing the ticket arbitrarily.
- The initial status comes from `lifecycle.initial_status` — an incident starts `new`; a request whose category requires approval starts `pending_approval`; one whose category does not starts `in_progress`.
- Creation writes a `ticket_status_history` row with `from_status=None`.
- Creation with `origin="alert"` succeeds with `requester_id=None` and `created_by=None` (the Phase 15 contract, spec §7.2) — and creation with any other origin and a null requester is rejected.
- `update_ticket` decides by key **presence**: an omitted key leaves the field untouched, an explicit `None` on a nullable field clears it, and an explicit `None` on a non-nullable field is rejected **before** any `setattr`, so the ORM object is never left half-mutated.
- `status` is not editable through `update_ticket` — passing it is ignored or rejected, never applied.
- A terminal ticket rejects edits.
- Audit: `ticket.created` once per create, `ticket.updated` once per update with **field names only** in metadata, never the subject or description bodies.
- Tenancy: load-bearing cross-org test on a raw `AsyncSession(admin_engine)`, with org B's ticket seeded **inside** the test, mutation-verified.

- [ ] **Step 2: Implement**

Follow `backend/app/modules/budget/expenses.py` for the multi-pass update shape: reject nulls for non-nullable fields first, resolve cross-references second, apply `setattr` last. Read that file's module docstring — it documents why the ordering matters.

- [ ] **Step 3: Mutation-verify the tenancy test**

Delete `Ticket.org_id == org_id` from the list query, run **that one test in isolation**, confirm it fails with a real assertion about org B's data, restore exactly, confirm `git diff` is empty, confirm it passes. Report the verbatim failure output.

- [ ] **Step 4: Gates and commit**

```bash
git commit -m "feat(service-desk): ticket create, read, list, update"
```

---

## Task 8: `transitions.py` — status changes and assignment

**Files:**
- Create: `backend/app/modules/service_desk/transitions.py`
- Test: `backend/tests/test_service_desk_transitions.py`

**Interfaces:**
- Consumes: `lifecycle.assert_transition`, `tickets.get_ticket`.
- Produces:
  - `async def change_status(session, org_id, ticket_id, *, to_status: str, actor_id: UUID | None, actor_type: str = "user", reason: str = "") -> Ticket`
  - `async def assign_ticket(session, org_id, actor_id, ticket_id, *, assigned_to: UUID | None, queue_id: UUID | None = None) -> Ticket`

- [ ] **Step 1: Write the failing tests**

Cover:
- Every valid transition for both types sets `status` and writes exactly one `ticket_status_history` row with the correct `from_status`.
- Every **invalid** transition raises `TicketValidationError` and leaves the ticket completely unchanged — re-read it from a fresh session and assert the status did not move. (Use `session.expire()` before the re-select, or a second session: a plain re-select returns the identity-mapped object and proves nothing. Budget lost a review round to exactly this.)
- Moving to `resolved` stamps `resolved_at`; moving to `closed` stamps `closed_at`; reopening clears the later stamp.
- `actor_type="system"` with `actor_id=None` is recorded as such — the Phase 15 path.
- `assign_ticket` requires the assignee to resolve org-scoped, and can both set and clear (`assigned_to=None`).
- Audit: `ticket.status_changed` and `ticket.assigned`, once each.

- [ ] **Step 2: Implement, then Step 3: gates and commit**

```bash
git commit -m "feat(service-desk): status transitions and assignment"
```

---

## Task 9: `approvals.py` — request approval

**Files:**
- Create: `backend/app/modules/service_desk/approvals.py`
- Test: `backend/tests/test_service_desk_approvals.py`

**Interfaces:**
- Consumes: `transitions.change_status`, `tickets.get_ticket`.
- Produces:
  - `async def decide_approval(session, org_id, actor_id, ticket_id, *, approve: bool, reason: str = "") -> TicketApproval`

- [ ] **Step 1: Write the failing tests**

The one that matters most:

```python
async def test_requester_cannot_approve_their_own_request(api_env) -> None:
    """Spec §4. This is the entire reason `ticket.approve` is separate from
    `ticket.write` -- if the requester can self-approve while holding the
    permission, the separation is decorative."""
    ...
    with pytest.raises(TicketValidationError) as exc:
        await decide_approval(session, org_id, requester_id, ticket.id, approve=True)
    assert "own request" in str(exc.value).lower()

    # And the ticket did not move.
    async with AsyncSession(admin_engine, expire_on_commit=False) as fresh:
        after = (await fresh.execute(
            select(Ticket).where(Ticket.id == ticket.id)
        )).scalar_one()
        assert after.status == "pending_approval"
```

Also cover: approving moves `pending_approval → in_progress` and stamps the `ticket_approval` row; rejecting moves to `rejected` and **requires a non-empty reason**; deciding twice raises; deciding an incident raises; deciding a request whose category needs no approval raises; both decisions are audited (`ticket.approved` / `ticket.rejected`) and write status history.

- [ ] **Step 2: Implement, then Step 3: gates and commit**

```bash
git commit -m "feat(service-desk): request approval and rejection"
```

---

## Task 10: `threads.py` — comments and attachments

**Files:**
- Create: `backend/app/modules/service_desk/threads.py`
- Create: `backend/app/core/attachments.py` (the shared filename sanitizer)
- Modify: `backend/app/core/storage.py` (add `put_ticket_attachment` / `get_ticket_attachment` / `delete_ticket_attachment`)
- Modify: `backend/app/modules/budget/api.py` (import the sanitizer from core instead of defining it)
- Move: Budget's sanitizer unit tests to `backend/tests/test_core_attachments.py`
- Test: `backend/tests/test_service_desk_threads.py`

**Pre-flight decision (supersedes the "copy the approach" instruction below):**
the sanitizer is **extracted to `app/core/attachments.py`** and consumed by both
Budget and Service Desk — not copied. Two verbatim copies of security-critical
logic is the shape that produced this codebase's worst defect (two
implementations of one guard, 160 lines apart, silently disagreeing), and
`app/core/csv_export.py` and `app/core/http.py` are the established precedent
for shared helpers.

The extraction is mechanical and must not change behavior: `re.fullmatch` and
`re.ASCII` both stay, and the comments explaining *why* each is load-bearing
move with the code. Budget's existing sanitizer tests move to
`tests/test_core_attachments.py` unchanged and must still pass — if any
assertion needs editing to go green, the extraction changed behavior and is
wrong. Run `pytest tests/test_budget_api.py tests/test_core_attachments.py -q`
after the move and report both results.

**Interfaces:**
- Produces:
  - `async def add_comment(session, org_id, ticket_id, *, body: str, author_id: UUID | None, author_type: str = "user", visibility: str = "public") -> TicketComment`
  - `async def list_comments(session, org_id, ticket_id, *, include_internal: bool = True) -> list[TicketComment]`
  - `async def add_attachment(session, org_id, actor_id, ticket_id, *, filename: str, data: bytes, store) -> TicketAttachment`
  - `async def get_attachment(session, org_id, attachment_id) -> TicketAttachment`
  - `def sanitize_attachment_filename(filename: str) -> str`

**Read `backend/app/modules/budget/api.py`'s attachment section before starting.** It carries the hardened implementation and the reasons, both of which cost two review rounds to get right. Copy the approach; do not re-derive it.

- [ ] **Step 1: Write the failing tests, including the sanitizer units**

The sanitizer must be tested **directly**, not through the API client:

```python
def test_sanitize_rejects_a_trailing_newline() -> None:
    """Python's `$` matches immediately BEFORE a trailing newline, so a
    `^...$` allow-list accepts "a.pdf\\n" -- which then reaches
    Content-Disposition and kills every download with h11's
    `LocalProtocolError`. `re.fullmatch` is what closes it.

    This is unit-tested rather than driven through the API because the
    in-process ASGI harness does not run h11: an HTTP-level test returns 200
    here and the bug only appears under uvicorn.
    """
    for bad in ("a.pdf\n", "a.pdf\r", "a.pdf\r\n"):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitize_rejects_non_ascii() -> None:
    """Bare `\\w` is Unicode-wide; Starlette encodes headers as latin-1, so an
    ordinary non-Latin filename uploads fine and then makes every download a
    500 for the whole org. `re.ASCII` gives the user a fixable 400 instead."""
    for bad in ("発票.pdf", "счёт.pdf"):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitize_rejects_paths_traversal_and_empty() -> None:
    for bad in ("", "..", "../x.pdf", "a/b.pdf", "C:\\evil\\x.pdf", "x" * 256):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitized_value_is_header_safe() -> None:
    for good in ("invoice.pdf", "INV-2026 03.pdf", "a_b-c.1.pdf"):
        f'attachment; filename="{sanitize_attachment_filename(good)}"'.encode("latin-1")
```

Also cover: the object key is **entirely server-derived** (`{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}`) with no client-supplied segment; `get_ticket_attachment` asserts the key starts with `{org_id}/service-desk/`; comments are append-only; a `system` comment has a NULL `author_id`; `include_internal=False` excludes internal comments (the portal's future need); adding a comment to a terminal ticket is rejected; audit fires once for `ticket.comment_added` and once for `ticket.attachment_uploaded`.

- [ ] **Step 2: Implement**

The regex is `re.compile(r"[\w.\- ]+", re.ASCII)` used with `.fullmatch()`. Both flags are load-bearing and their reasons are in the tests above — do not "simplify" either.

Upload size is bounded via `app/core/http.py`'s existing helpers. Pick a cap suited to screenshots and logs (10 MiB is reasonable) and state the reasoning in a comment.

- [ ] **Step 3: Gates and commit**

```bash
git commit -m "feat(service-desk): comments and attachments"
```

---

## Task 11: API routes, registration, and the change-management adapter

**Files:**
- Create: `backend/app/modules/service_desk/api.py`, `backend/app/modules/service_desk/adapters.py`
- Modify: `backend/app/modules/__init__.py` (add to `register_all`), `backend/app/modules/service_desk/__init__.py` (`register(app)`)
- Modify: `docs/modules/change-mgmt/spec.md` §5 (record that the port now has a real backend), `deploy/README.md` (add a Service Desk section)
- Test: `backend/tests/test_service_desk_api.py`, `backend/tests/test_service_desk_adapter.py`

**Interfaces:**
- Consumes: every service from Tasks 6–10.
- Produces: the routes in spec §9; `ServiceDeskChangeTicketAdapter` implementing change management's `ChangeTicketPort`.

- [ ] **Step 1: Write the failing API tests**

Required, beyond ordinary route coverage:

- **RBAC completeness derived from the router**, so a route added later without a permission test fails the suite:

```python
from app.modules.service_desk.api import router

declared = {(m, r.path) for r in router.routes for m in r.methods if m != "HEAD"}
covered = {(m, template) for m, template, _, _ in ROUTES}
assert declared == covered, f"route(s) not RBAC-tested: {declared - covered}"
```

- **Technician access asserted positively**: technician *can* read, write, and assign; technician *cannot* approve or administer. This is the inverse of Budget's defining assertion and equally load-bearing.
- **Route ordering**: a test proving `/service-desk/attachments/{id}` and every static segment resolve before `{ticket_id}` captures them.
- **Audit split**: a list call writes nothing; an attachment download writes exactly one event; a status change writes exactly one.
- **Error mapping**: `TicketNotFoundError` → 404, every other `TicketValidationError` → 400, and no path returns 500. Include the approval, transition, and attachment-sanitizer paths explicitly — those are the branches that regress silently if someone narrows an `except`.

- [ ] **Step 2: Implement `api.py`**

Copy `backend/app/modules/subscriptions/api.py`'s `_http_error` shape **exactly** — `isinstance`, subclass checked first, never message-string matching.

`PATCH` handlers pass `model_dump(exclude_unset=True)` to the service. Without it, Task 7's omitted-vs-null distinction silently collapses and every PATCH clears unmentioned nullable fields.

- [ ] **Step 3: Implement the change-management adapter**

```python
"""Real backend for change management's `ChangeTicketPort` (spec §7.1).

Phase 9 shipped that port with a `NullChangeTicketAdapter` that records intent
and performs no network call, explicitly awaiting a backend. This is it.

Additive by design: change management's reviewed core is not modified, and the
null adapter remains the default until an operator sets
ITS_CHANGE_TICKET_BACKEND, so an existing deployment's behavior does not change
underneath it.
"""
```

The adapter opens an `incident`-type ticket in the org's default queue. Test it against the port's protocol, and test that the null adapter is still the default.

- [ ] **Step 4: Amend the change-management spec**

Now — and only now, because the code exists — update `docs/modules/change-mgmt/spec.md` §5 to record that the port has a real Service Desk backend and that Odoo is no longer the intended target. The service-desk spec §13 explicitly deferred this edit to this commit.

- [ ] **Step 5: Deploy notes**

Add a `### Service Desk` section to `deploy/README.md`, following the house style of the ten module sections already there: control-plane only, no worker, the four-plus-one permission model with technician's grants called out, the attachment object-storage prefix, audit-on-view posture, and the note that notification/portal/email/SLA are separate sub-projects not present in this build.

- [ ] **Step 6: Gates and commit**

```bash
git commit -m "feat(service-desk): API routes, registration, change-mgmt adapter"
```

---

## Task 12: Frontend

**Files:**
- Create: `frontend/src/modules/service-desk/{types.ts,api.ts,serviceDeskState.ts,ServiceDeskNav.tsx,TicketListPage.tsx,TicketDetailPage.tsx,MyWorkPage.tsx,ServiceDeskAdminPage.tsx}`
- Create: `frontend/src/modules/service-desk/__tests__/serviceDesk.test.tsx`
- Modify: `frontend/src/App.tsx` (routes + a `ticket.read`-gated nav entry), `frontend/src/lib/api.ts` (client methods)

**Interfaces:**
- Consumes: the routes from Task 11. **`backend/app/modules/service_desk/api.py` and `schema.py` are the contract** — read the actual routes and response shapes. The TypeScript types are hand-written, so a wrong field name typechecks cleanly and fails at runtime.

- [ ] **Step 1: Write the failing tests**

Required:
- **Every page is reachable from navigation**, with tests asserting both the links' presence and their absence without `ticket.read`. Phase 12 shipped two routed-but-unlinked pages; do not repeat it.
- Each mutation's wiring asserted with **exact call arguments** — create, comment, status change, assign, approve, reject.
- `ServiceDeskNav` is **one shared component**, not copied per page. Budget shipped five verbatim copies and had to have them extracted.

- [ ] **Step 2: Implement**

Follow `frontend/src/modules/budget/` for structure and conventions: strict TypeScript with no `any`, no `as unknown as`, no `@ts-ignore`; badge variant maps using only vocabulary the backend emits with a `?? "muted"` fallback and a comment citing the source; every mutation hook's `invalidateQueries` keys matching keys actually registered.

Every hook defined must be reachable from the UI or deleted. Budget shipped nine unwired hooks — including the module's headline action — and needed a fix round.

- [ ] **Step 3: Gates**

Run from `frontend/`: `npm test`, `npm run typecheck`, `npm run build`. All three clean, foreground.

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(service-desk): ticket list, detail, my work, admin"
```

---

## Self-review

**Spec coverage:** §2 data model → Tasks 1–2. §2.1 numbering → Task 5. §3 lifecycle → Task 4, enforced in Task 8. §4 approval → Task 9. §5 queues/assignment → Tasks 6, 8. §6 comments/attachments → Task 10. §7.1 change-mgmt adapter → Task 11. §7.2 Phase 15 reservation → Task 1 (columns) and Task 7 (`origin="alert"` creation path). §7.3 device link → Tasks 1, 7, 12. §8.1 tenancy → every service task, mutation-verified in Task 7. §8.2 RBAC → Task 1 (seed) and Task 11 (enforcement). §8.3 audit → each service task, split asserted in Task 11. §9 API → Task 11. §10 no worker → nothing to build. §11 frontend → Task 12. §12 testing → distributed, with the eight required tests placed in the tasks that own them. §13 doc changes → done in the spec commit, except change-mgmt §5 which is Task 11 Step 4.

**Gap found and closed during review:** spec §12 requires an audit-split test and a technician-positive test; both were unassigned until Task 11's step 1 was expanded to name them.

**Type consistency:** `next_number` returns `str` and is consumed only by Task 7. `initial_status(ticket_type, *, requires_approval)` and `assert_transition(ticket_type, from_status, to_status)` are used with those exact signatures in Tasks 7 and 8. `change_status(..., actor_type="user")` is called by Task 9's approval path with the default and by the Phase-15-reserved path with `"system"`. `sanitize_attachment_filename` is defined in Task 10 and used only there and in Task 11's error-mapping test.

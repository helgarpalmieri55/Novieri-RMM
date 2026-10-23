# SLA Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track whether each ticket met its response and resolution promises, measured on a business calendar, and act when one is missed.

**Architecture:** A policy keyed on `(queue, priority)` selects targets and a calendar. Two pure functions do all working-time arithmetic; everything else is built on them. Deadlines are **materialised** onto a `ticket_sla` row so the breach sweep is an indexed comparison rather than a per-ticket computation. A Celery beat task detects breaches, escalates, and auto-closes. Breach publishes a domain event; the notification module delivers it.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Postgres RLS, React + TypeScript. Stdlib `zoneinfo` for timezones — **no new dependency**.

## Global Constraints

Every task's requirements implicitly include this section.

- **Authority is `docs/modules/sla/spec.md`.** Where this plan and the spec disagree, **the spec wins** — in the previous sub-project this plan's author was wrong 13 times and twice contradicted the spec while the spec was right. Verify every signature against the real code before calling it, and record any deviation in your report.
- **Migration `0033`.** Head is `0032` (`0032_email_intake.py`). `down_revision` is the bare string `"0032"`.
- **`pytest` is at `backend/.venv/bin/pytest`.** Bare `pytest` and `python -m pytest` are NOT installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`.
- **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`) and carry on. Do not background a long test run and wait on it.
- **Baseline: 2412 backend tests, 236 frontend.** Do not regress them. `npm run lint` does not exist in this repo; `npx tsc --noEmit` is the only frontend static check.
- **Tenancy GUC is `app.current_org`.** Every new table gets `ENABLE + FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy with both `USING` and `WITH CHECK`. **Note:** for an `ALL`-command policy Postgres reuses `USING` as `WITH CHECK` when the latter is omitted, so deleting `WITH CHECK` falsifies nothing. The mutations that actually kill these tests are **`WITH CHECK (true)`** for an insert test and **`USING (true)`** for a read test. Use those.
- **RLS masks application predicates.** A cross-org test that goes through `org_session` passes even with the service's own `org_id` predicate deleted, because RLS rejects first. To pin an application-layer predicate, exercise it on the **admin engine** where RLS is not the thing rejecting. This defect shipped four times in the previous sub-project.
- **Real table names:** `ticket`, `ticket_queue`, `ticket_category`, `ticket_comment` are SINGULAR. `users`, `organizations`, `audit_events`, `domain_events`, `permissions`, `role_permissions`, `roles` are PLURAL. Grep `__tablename__`; do not guess.
- **`permissions` is `(code, module, action)`** — there is **no** `resource` and **no** `description` column.
- **`AuditEvent`'s metadata attribute is `meta`** (mapped to column `metadata`), not `metadata_json`.
- **Modules register in `app/modules/__init__.py`**, not `app/main.py` — `main.py` has no `register(app)` calls.
- **FastAPI 0.139.2: `app.routes` IS `app.router.routes`** (the same list), and **neither contains `include_router`'d routes** — only a `_IncludedRouter` with `path=None`. A route-completeness test must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. `backend/tests/test_email_intake_api.py` has a working flattener — copy it.
- Tests use `api_env` with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`.
- Async throughout, full type hints, business logic in services not route handlers, `mypy` and `ruff` clean.

## Ticket-core signatures you will call (verified against the code)

```python
create_ticket(session, org_id, actor_id: UUID | None, data: TicketCreate, *,
              origin: TicketOrigin = "technician", source_alert_id: UUID | None = None) -> Ticket
update_ticket(session, org_id, actor_id: UUID, ticket_id: UUID,
              fields: dict[str, Any]) -> Ticket          # Task 8 changes this signature
change_status(session, org_id, ticket_id, *, to_status: str, actor_id: UUID | None,
              actor_type: Literal["user", "system"] = "user", reason: str = "") -> Ticket
add_comment(session, org_id, ticket_id, *, body: str, author_id: UUID | None,
            author_type: Literal["user", "system"] = "user", visibility: str = "public") -> TicketComment
audit_service.record(session, *, org_id, actor_type, actor_id, action,
                     target_type="", target_id="", metadata=None) -> AuditEvent
publish(session, *, org_id, event_type, subject_type, subject_id, payload, actor_user_id) -> None
```

`TicketCreate.subject` is bounded at **255** and `description` at **20000** by Pydantic — not just by the column. `tests/conftest.py` provides `a_ticket_for(org_id, who) -> ATicket` (admin engine, `.id` / `.requester_id`).

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/migrations/versions/0033_sla.py` | Six tables, RLS, two permissions, one event type |
| `backend/app/modules/sla/__init__.py` | `register(app)` |
| `backend/app/modules/sla/models.py` | The six ORM models |
| `backend/app/modules/sla/errors.py` | `SlaValidationError`, `SlaNotFoundError`, `UnsatisfiableCalendar` |
| `backend/app/modules/sla/calendar.py` | **Pure** working-time arithmetic. No DB. |
| `backend/app/modules/sla/calendars.py` | Calendar/window/holiday CRUD + audit |
| `backend/app/modules/sla/policies.py` | Policy CRUD + `resolve_policy` |
| `backend/app/modules/sla/tracking.py` | `ticket_sla` lifecycle: create, recompute, pause, met |
| `backend/app/modules/sla/breach.py` | Breach marking + escalation |
| `backend/app/modules/sla/sweep.py` | The beat task body: breach pass + auto-close pass |
| `backend/app/modules/sla/schema.py` | Pydantic request/response models |
| `backend/app/modules/sla/api.py` | Routes, each `require_permission`-gated |

**Modified (backend):** `app/core/rbac.py` (2 codes), `app/core/events.py` (`ticket.sla_breached`), `app/modules/__init__.py`, `app/worker.py` (beat + task), `app/modules/service_desk/tickets.py` (Task 8), `app/models/__init__.py`.

**Created (frontend):** `frontend/src/modules/sla/` — `api.ts`, `types.ts`, `CalendarList.tsx`, `CalendarEditor.tsx`, `PolicyMatrix.tsx`, `TicketSlaPanel.tsx`.

---

## Task 1: Migration 0033 — six tables, RLS, permissions, event type

**Files:**
- Create: `backend/migrations/versions/0033_sla.py`, `backend/app/modules/sla/__init__.py` (empty for now), `backend/app/modules/sla/models.py`
- Modify: `backend/app/core/rbac.py`, `backend/app/core/events.py`, `backend/app/models/__init__.py`
- Test: `backend/tests/test_sla_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SlaCalendar`, `SlaCalendarWindow`, `SlaCalendarHoliday`, `SlaPolicy`, `TicketSla`, `SlaOrgSettings`; permissions `sla.read` / `sla.admin`; event type `ticket.sla_breached`; revision `"0033"`. Also the seed helpers later tasks import: `_seed_calendar(api_env, org_id, **kw) -> UUID`, `_seed_policy(api_env, org_id, **kw) -> UUID`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_sla_migration.py`. Cover, at minimum:

```python
"""Migration 0033: shape, RLS, permissions (spec §2, §8, §11).

The RLS tests are BEHAVIOURAL -- they insert two orgs' rows through the
RLS-constrained app engine and assert on what each org sees. A test asserting
`relrowsecurity` is true passes against a policy whose predicate was deleted.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.modules.sla.models import SlaCalendar, SlaPolicy


@pytest.mark.parametrize(
    "table",
    [
        "sla_calendar",
        "sla_calendar_window",
        "sla_calendar_holiday",
        "sla_policy",
        "ticket_sla",
        "sla_org_settings",
    ],
)
async def test_rls_is_enabled_and_forced(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_a_calendar_is_invisible_to_another_org(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor = await _seed_user(api_env, org_a)
    async with org_session(org_a, engine=api_env.app_engine) as session:
        session.add(
            SlaCalendar(
                org_id=org_a, name="UK business", timezone="Europe/London", created_by=actor
            )
        )
    async with org_session(org_a, engine=api_env.app_engine) as session:
        assert len((await session.execute(select(SlaCalendar))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine) as session:
        assert (await session.execute(select(SlaCalendar))).scalars().all() == []


async def test_a_calendar_cannot_be_inserted_under_another_orgs_id(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor = await _seed_user(api_env, org_a)
    with pytest.raises(ProgrammingError):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            session.add(
                SlaCalendar(
                    org_id=org_b, name="theirs", timezone="Europe/London", created_by=actor
                )
            )


async def test_a_zero_target_is_rejected(api_env) -> None:
    """Spec §2.4: a zero target would be breached at creation, which is never
    what anyone means."""
    org_id = await api_env.create_org()
    actor = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    with pytest.raises(ProgrammingError):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            session.add(
                SlaPolicy(
                    org_id=org_id,
                    queue_id=None,
                    priority=None,
                    calendar_id=calendar_id,
                    response_target_minutes=0,
                    resolution_target_minutes=60,
                    created_by=actor,
                )
            )


async def test_a_window_with_start_after_end_is_rejected(api_env) -> None:
    """Spec §2.2: a window spanning midnight is two windows on adjacent days;
    the clock does not handle wrapping and must not be asked to."""
    # ... insert SlaCalendarWindow(weekday=0, start_time=time(17,0), end_time=time(9,0))
    # and assert ProgrammingError.


async def test_one_sla_row_per_ticket(api_env, a_ticket_for) -> None:
    """Spec §2.5: `ticket_id` is UNIQUE. Multiple concurrent SLAs are §13."""
    # ... insert two TicketSla rows for one ticket, assert IntegrityError/ProgrammingError.


@pytest.mark.parametrize("code", ["sla.read", "sla.admin"])
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


async def test_the_breach_event_type_is_registered() -> None:
    from app.core.events import EVENT_TYPES

    assert "ticket.sla_breached" in EVENT_TYPES
```

Then add the seed helpers at the bottom — later tasks import them, so their signatures matter:

```python
async def _seed_user(api_env, org_id: UUID, email: str | None = None) -> UUID: ...
async def _seed_calendar(api_env, org_id: UUID, *, timezone: str = "Europe/London",
                         windows: list[tuple[int, time, time]] | None = None,
                         holidays: list[date] | None = None) -> UUID: ...
async def _seed_policy(api_env, org_id: UUID, *, calendar_id: UUID,
                       queue_id: UUID | None = None, priority: str | None = None,
                       response_minutes: int = 60, resolution_minutes: int = 480,
                       escalate_priority: bool = False,
                       escalate_to_queue_id: UUID | None = None) -> UUID: ...
```

`_seed_calendar` defaults to Mon–Fri 09:00–17:00 when `windows` is None. **Document any side effect it has** (e.g. if it must create a user for `created_by`, say so in the docstring and use an obviously non-colliding email) — a hidden extra row in `users` broke sender-resolution tests in the previous sub-project.

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_sla_migration.py -v`
Expected: `ModuleNotFoundError: No module named 'app.modules.sla'`.

- [ ] **Step 3: Write the models**

`backend/app/modules/sla/models.py`, mirroring `app/modules/email_intake/models.py`'s header style (`Base`, `uuid7_pk`, a local `_utcnow`). Six classes exactly as spec §2.1–2.6 describes. Constraints that must exist:

- `sla_calendar`: unique `(org_id, name)`.
- `sla_calendar_window`: CHECK `start_time < end_time`.
- `sla_calendar_holiday`: unique `(calendar_id, holiday_date)`.
- `sla_policy`: unique `(org_id, queue_id, priority)`; CHECK both targets `> 0`; CHECK `priority IN ('low','normal','high','urgent') OR priority IS NULL`.
- `ticket_sla`: **unique `ticket_id`**; CHECK `policy_source IN ('initial','retriage','escalation')`.
- `sla_org_settings`: unique `org_id`; CHECK `auto_close_after_days > 0`.

**A NULL in a Postgres unique constraint does not collide.** `(org_id, NULL, NULL)` can therefore be inserted twice, which would give an org two conflicting fallback policies. Add partial unique indexes covering the wildcard combinations so each of the four resolution levels is unique per org. Write a test for it.

- [ ] **Step 4: Write the migration**

`backend/migrations/versions/0033_sla.py`. `revision = "0033"`, `down_revision = "0032"`. Read `0032_email_intake.py` for the exact permission-insert SQL and the standard RLS block, and copy them rather than trusting this plan's recollection.

Indexes required by §2.5, because they are what make the sweep an index scan:

```python
op.execute(
    "CREATE INDEX ix_ticket_sla_org_response_due ON ticket_sla (org_id, response_due_at) "
    "WHERE response_breached_at IS NULL AND response_met_at IS NULL"
)
op.execute(
    "CREATE INDEX ix_ticket_sla_org_resolution_due ON ticket_sla (org_id, resolution_due_at) "
    "WHERE resolution_breached_at IS NULL AND resolution_met_at IS NULL"
)
```

Add the two permission codes to `PERMISSIONS` in `app/core/rbac.py`, and `"ticket.sla_breached"` to `EVENT_TYPES` in `app/core/events.py` with a comment saying which module publishes it.

- [ ] **Step 5: Run tests**

Run: `cd backend && .venv/bin/pytest tests/test_sla_migration.py tests/test_schema_drift.py tests/test_rls.py tests/test_rbac.py -v`
Expected: all PASS. If `test_rbac.py::test_permission_registry_complete` fails, it pins the exact permission-code set — adding the two codes to its literal is the correct fix.

- [ ] **Step 6: Prove the tenancy tests can fail**

Set the policy to `WITH CHECK (true)`, rebuild the test DB, run the insert test — **watch it go red**. Then set `USING (true)` and watch the visibility test go red. Restore both. Paste both outputs in your report. Deleting the `WITH CHECK` clause entirely proves nothing (see Global Constraints).

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0033_sla.py backend/app/modules/sla/ \
        backend/app/core/rbac.py backend/app/core/events.py \
        backend/app/models/__init__.py backend/tests/test_sla_migration.py
git commit -m "feat(sla): migration 0033 -- calendars, policies, ticket SLA state"
```

---

## Task 2: The working-time clock

**This is the hardest task in the plan and the one most likely to ship a silent bug.** It is pure, has no database, and is cheap to test exhaustively — so test it exhaustively.

**Files:**
- Create: `backend/app/modules/sla/calendar.py`, `backend/app/modules/sla/errors.py`
- Test: `backend/tests/test_sla_calendar.py`

**Interfaces:**
- Consumes: nothing (stdlib `zoneinfo`, `datetime` only).
- Produces:
  - `@dataclass(frozen=True) CalendarSpec(timezone: str, windows: tuple[tuple[int, time, time], ...], holidays: frozenset[date])`
  - `def working_minutes_between(cal: CalendarSpec, start: datetime, end: datetime) -> int`
  - `def add_working_minutes(cal: CalendarSpec, start: datetime, minutes: int) -> datetime`
  - `class UnsatisfiableCalendar(Exception)`
  - `MAX_LOOKAHEAD_DAYS: int`

`CalendarSpec` is a plain value object so this module never touches the ORM. Task 3 builds one from rows.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_sla_calendar.py`. These are pure and synchronous — no `api_env`, no database, no `@pytest.mark.asyncio`.

```python
"""Working-time arithmetic (spec §4, §10.1-10.3).

Pure. Every test states a wall-clock expectation in the calendar's LOCAL time,
because a UTC-only assertion passes against an implementation that is an hour
wrong for half the year.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.modules.sla.calendar import (
    CalendarSpec,
    UnsatisfiableCalendar,
    add_working_minutes,
    working_minutes_between,
)

LONDON = "Europe/London"
# Mon-Fri 09:00-17:00, with a 12:00-13:00 lunch break expressed as two windows.
BUSINESS = CalendarSpec(
    timezone=LONDON,
    windows=tuple(
        (wd, s, e)
        for wd in range(5)
        for s, e in [(time(9, 0), time(12, 0)), (time(13, 0), time(17, 0))]
    ),
    holidays=frozenset(),
)


def _local(y, m, d, hh, mm, tz=LONDON) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def test_within_one_window() -> None:
    start = _local(2026, 3, 2, 9, 30)   # Monday
    end = _local(2026, 3, 2, 11, 30)
    assert working_minutes_between(BUSINESS, start, end) == 120


def test_spanning_the_lunch_break() -> None:
    """11:00 to 14:00 is three wall-clock hours but two working hours."""
    assert working_minutes_between(BUSINESS, _local(2026, 3, 2, 11, 0),
                                   _local(2026, 3, 2, 14, 0)) == 120


def test_spanning_a_weekend() -> None:
    """Friday 16:00 to Monday 10:00 = 1h Friday + 1h Monday."""
    assert working_minutes_between(BUSINESS, _local(2026, 3, 6, 16, 0),
                                   _local(2026, 3, 9, 10, 0)) == 120


def test_spanning_a_holiday() -> None:
    cal = CalendarSpec(BUSINESS.timezone, BUSINESS.windows, frozenset({date(2026, 3, 3)}))
    # Monday 16:00 -> Wednesday 10:00, with Tuesday a holiday.
    assert working_minutes_between(cal, _local(2026, 3, 2, 16, 0),
                                   _local(2026, 3, 4, 10, 0)) == 120


def test_starting_outside_working_hours() -> None:
    """A ticket filed at 03:00 accrues nothing until 09:00."""
    assert working_minutes_between(BUSINESS, _local(2026, 3, 2, 3, 0),
                                   _local(2026, 3, 2, 10, 0)) == 60


def test_start_after_end_is_zero_not_negative() -> None:
    assert working_minutes_between(BUSINESS, _local(2026, 3, 2, 11, 0),
                                   _local(2026, 3, 2, 9, 0)) == 0


def test_add_lands_on_the_next_working_moment() -> None:
    """Filed 16:00 Friday, 4 working hours -> Monday 12:00 LOCAL
    (1h Fri 16-17, then Mon 09-12 with lunch not yet reached)."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 6, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 3, 9, 12, 0)


def test_add_from_outside_working_hours_starts_at_the_next_window() -> None:
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 3, 0), 60)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.hour, local.minute) == (10, 0)


def test_add_across_spring_forward() -> None:
    """Spec §10.2. Europe/London springs forward 01:00->02:00 on 2026-03-29
    (a Sunday). A target starting Friday and landing Monday must be correct in
    LOCAL wall-clock time -- the naive implementation adds a timedelta to a UTC
    instant and lands an hour off."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 27, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (3, 30, 12, 0)


def test_add_across_autumn_back() -> None:
    """Europe/London falls back 02:00->01:00 on 2026-10-25 (a Sunday)."""
    due = add_working_minutes(BUSINESS, _local(2026, 10, 23, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (10, 26, 12, 0)


def test_between_across_dst_counts_working_minutes_not_elapsed() -> None:
    """The weekend containing a DST shift still contributes only the working
    windows either side of it."""
    assert working_minutes_between(BUSINESS, _local(2026, 3, 27, 16, 0),
                                   _local(2026, 3, 30, 10, 0)) == 120


def test_an_empty_calendar_raises_rather_than_looping() -> None:
    """Spec §4, §10.3: a calendar with no windows is otherwise a way to hang
    the sweep."""
    empty = CalendarSpec(LONDON, (), frozenset())
    with pytest.raises(UnsatisfiableCalendar):
        add_working_minutes(empty, _local(2026, 3, 2, 9, 0), 60)


def test_a_target_longer_than_a_working_week_still_resolves() -> None:
    """40 working hours on a 40-hour calendar lands a week later, not never."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 9, 0), 40 * 60)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour) == (3, 9, 9)


def test_add_zero_minutes_returns_the_next_working_moment() -> None:
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 3, 0), 0)
    assert due.astimezone(ZoneInfo(LONDON)).hour == 9


def test_a_holiday_covering_every_remaining_day_raises() -> None:
    """Bounded iteration, not an infinite search."""
    holidays = frozenset(date(2026, 3, d) for d in range(2, 32))
    cal = CalendarSpec(LONDON, BUSINESS.windows, holidays)
    # far enough that MAX_LOOKAHEAD_DAYS is exceeded
    with pytest.raises(UnsatisfiableCalendar):
        add_working_minutes(cal, _local(2026, 3, 2, 9, 0), 60 * 200)
```

**Verify the DST dates against `zoneinfo` before trusting them** — run a one-liner and confirm 2026-03-29 and 2026-10-25 are the actual Europe/London transitions. If they differ, fix the test dates, not the assertions' intent.

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_sla_calendar.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `errors.py` (`SlaValidationError`, `SlaNotFoundError(SlaValidationError)`, `UnsatisfiableCalendar(SlaValidationError)`) and `calendar.py`.

Implementation requirements:

- **Compute in local time.** Convert `start` to the calendar's zone, iterate day by day building that day's windows as local `datetime`s, and convert to UTC only at the boundaries. Never add a `timedelta` to a UTC instant and call it a working hour.
- `MAX_LOOKAHEAD_DAYS = 366`. `add_working_minutes` raises `UnsatisfiableCalendar` once it passes that without consuming the budget — naming the calendar's problem, not "loop limit exceeded".
- Windows for a weekday are sorted and treated as half-open `[start, end)`, so a boundary minute is not double-counted.
- A holiday removes the whole date's windows.
- Both functions accept and return timezone-aware UTC datetimes; assert that on entry rather than guessing about naive input.

- [ ] **Step 4: Run tests**

Run: `cd backend && .venv/bin/pytest tests/test_sla_calendar.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the DST tests can fail**

Replace the local-time walk with naive UTC arithmetic (`start + timedelta(minutes=...)` clipped to windows). **Watch `test_add_across_spring_forward` go red.** Restore. If it stays green, the test is asserting in UTC and cannot see the bug — fix the test.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/sla/calendar.py backend/app/modules/sla/errors.py \
        backend/tests/test_sla_calendar.py
git commit -m "feat(sla): working-time arithmetic over business calendars"
```

---

## Task 3: Calendar CRUD

**Files:** Create `backend/app/modules/sla/calendars.py`; Test `backend/tests/test_sla_calendars.py`

**Interfaces:**
- Consumes: `SlaCalendar`/`SlaCalendarWindow`/`SlaCalendarHoliday` (T1), `CalendarSpec` (T2).
- Produces: `create_calendar`, `update_calendar`, `delete_calendar`, `get_calendar`, `list_calendars`, `replace_windows`, `add_holiday`, `remove_holiday`, and **`load_calendar_spec(session, org_id, calendar_id) -> CalendarSpec`** — the bridge from rows to Task 2's value object, used by every later task.

Requirements:
- **Validate the timezone against `zoneinfo` at write time** (spec §2.1). A calendar whose timezone does not resolve is rejected here, never at sweep time — a bad timezone discovered by the sweep breaks every ticket on that calendar at once. Test with `"Not/AZone"`.
- `replace_windows` replaces a calendar's whole window set in one transaction (spec §9): validate no overlaps within a weekday, and reject `start >= end`.
- Deleting a calendar referenced by an **active** policy raises `SlaValidationError` naming the policies (spec §9).
- Audit create/update/delete (invariant #4).
- Create is `ON CONFLICT DO NOTHING` + `RETURNING id` on `(org_id, name)`, raising the same typed error a pre-check would — **not** check-then-insert. This project has run two sweeps to remove that bug class.
- Every query carries an explicit `org_id` predicate, and **at least one test must pin that predicate on the admin engine** (see Global Constraints).

Commit: `feat(sla): calendar configuration with windows and holidays`

---

## Task 4: Policy CRUD and resolution

**Files:** Create `backend/app/modules/sla/policies.py`; Test `backend/tests/test_sla_policies.py`

**Interfaces:**
- Consumes: `SlaPolicy` (T1).
- Produces: `create_policy`, `update_policy`, `delete_policy`, `get_policy`, `list_policies`, and **`resolve_policy(session, org_id, *, queue_id, priority) -> SlaPolicy | None`**.

`resolve_policy` implements spec §3's order exactly:

1. `(queue_id, priority)` 2. `(queue_id, NULL)` 3. `(NULL, priority)` 4. `(NULL, NULL)`

Only `active` policies. **Returns `None` when nothing matches** — never a fabricated default.

Tests must cover all four levels *and* the no-match case, and must prove the **order** matters: with both a `(queue, priority)` and a `(queue, NULL)` policy present, the specific one wins. A test that only ever has one candidate cannot detect a reversed order.

Deleting a policy leaves existing `ticket_sla` rows intact (spec §9) — their `policy_id` records what was promised. Add a test.

Commit: `feat(sla): policy CRUD and (queue, priority) resolution`

---

## Task 5: `ticket_sla` lifecycle — create and recompute

**Files:** Create `backend/app/modules/sla/tracking.py`; Test `backend/tests/test_sla_tracking.py`

**Interfaces:**
- Consumes: `resolve_policy` (T4), `load_calendar_spec` (T3), `add_working_minutes` (T2), `TicketSla` (T1).
- Produces:
  - `async def start_tracking(session, org_id, ticket) -> TicketSla | None`
  - `async def recompute_for_retriage(session, org_id, ticket) -> TicketSla | None`
  - `POLICY_SOURCES = ("initial", "retriage", "escalation")`

`start_tracking` resolves a policy; returns `None` and creates **no row** if none matches (spec §3). Otherwise it materialises both deadlines from the ticket's `created_at` and writes `policy_source="initial"`.

`recompute_for_retriage` re-resolves and recomputes **from `ticket.created_at`**, not from now (spec §0.3), writing `policy_source="retriage"`. A deadline already met or breached is **not** rewritten.

Tests must include spec §10.5: a ticket filed `normal` and re-triaged to `urgent` past the new target comes out **already breached** — that is the intended, documented behaviour, not a bug to soften.

Commit: `feat(sla): materialise deadlines at creation and on re-triage`

---

## Task 6: The met-detection hooks

**Files:** Modify `backend/app/modules/sla/tracking.py`; Test `backend/tests/test_sla_met.py`

**Interfaces:**
- Produces: `async def note_public_comment(session, org_id, ticket_id, *, author_id) -> None`, `async def note_resolved(session, org_id, ticket_id) -> None`.

Per spec §0.6:
- `note_public_comment` stamps `response_met_at` **only** when the author is not the ticket's requester and `response_met_at` is NULL. An internal note must never reach this function — wire it only from the public-comment path.
- `note_resolved` stamps `resolution_met_at`, **and** `response_met_at` if still NULL (a ticket resolved with no reply cannot owe a response forever).

Tests (spec §10.9, §10.10) must assert on **stored state** and must include the negative cases: an internal note, an assignment, and a status change out of `new` each leave `response_met_at` NULL. Those three negatives are the whole point of §0.6 — without them the test passes against an implementation that stops the clock on any activity.

Wire the hooks where the ticket core already publishes comment and status events; do not duplicate the ticket core's logic.

Commit: `feat(sla): stop the response clock on real customer contact`

---

## Task 7: Pause and resume

**Files:** Modify `backend/app/modules/sla/tracking.py`; Test `backend/tests/test_sla_pause.py`

**Interfaces:**
- Produces: `async def pause(session, org_id, ticket_id) -> None`, `async def resume(session, org_id, ticket_id) -> None`, `PAUSING_STATUSES = frozenset({"pending", "pending_approval"})`.

Entering either status stamps `paused_at`; leaving adds `working_minutes_between(paused_at, now)` to both **outstanding** deadlines and clears it.

**Pause is measured in working time** (spec §5). The load-bearing test: a pause from Friday 16:00 to Monday 09:00 on a business-hours calendar extends the deadline by **60 minutes** (the Friday hour), not by 65 hours of wall clock. Prove it by mutating the implementation to use wall-clock elapsed and watching the test go red.

A met deadline is never extended. A double pause (already paused) is a no-op, not a second stamp — test it.

Commit: `feat(sla): pause the clock on pending, in working time`

---

## Task 8: `update_ticket` gains the system-actor path

**Files:** Modify `backend/app/modules/service_desk/tickets.py:539-620`; Test `backend/tests/test_service_desk_tickets.py` (append)

**This is a ticket-core change and spec §1 states it explicitly.** It is its own task so a reviewer can gate it separately from the SLA module.

`update_ticket` currently takes `actor_id: UUID` positionally and hardcodes `actor_type="user"` in its audit call. Breach escalation is system-originated and has no human actor.

Change it to mirror `change_status`, which already has this exactly:

```python
async def update_ticket(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    ticket_id: UUID,
    fields: dict[str, Any],
    *,
    actor_type: Literal["user", "system"] = "user",
) -> Ticket:
```

with the same validation pairing `change_status` enforces at `transitions.py:191-196`:

```python
if actor_type not in ("user", "system"):
    raise TicketValidationError(f"unknown actor_type: {actor_type!r}")
if actor_type == "system" and actor_id is not None:
    raise TicketValidationError("actor_type='system' updates must not carry an actor_id")
if actor_type == "user" and actor_id is None:
    raise TicketValidationError("actor_type='user' updates require an actor_id")
```

and pass `actor_type` through to `audit_service.record`.

Existing callers pass `actor_id` positionally and get `actor_type="user"` by default, so they are unchanged — **verify that by running the full ticket-core suite**, not by inspection.

Tests: a system update is audited with `actor_type="system"` and `actor_id=None`; the three validation errors each raise; an ordinary user update is unchanged.

Commit: `feat(service-desk): allow system-actor ticket updates, mirroring change_status`

---

## Task 9: Breach and escalation

**Files:** Create `backend/app/modules/sla/breach.py`; Test `backend/tests/test_sla_breach.py`

**Interfaces:**
- Consumes: `update_ticket` with `actor_type="system"` (T8) for the **priority** half, `transitions.assign_ticket` with `actor_type="system"` for the **queue** half, `publish` (kernel), `TicketSla` (T1).

**Read spec §6.0 before writing this task.** The two escalation halves use two
different ticket-core services and produce two different audit actions.
`queue_id` is deliberately absent from `TICKET_EDITABLE_FIELDS` and passing it
to `update_ticket` raises — `transitions.assign_ticket` is the queue-move
service. Both take `actor_id=None` positionally with keyword-only
`actor_type="system"`.
- Produces: `async def mark_breached(session, org_id, ticket_sla, *, target: Literal["response","resolution"]) -> None`, `PRIORITY_LADDER = ("low", "normal", "high", "urgent")`.

`mark_breached` stamps the breach column, publishes `ticket.sla_breached` to the outbox (spec §6 — this module sends no mail), and applies escalation when the policy enables it.

**The cascade guard is the point of this task (spec §0.4).** An escalation:
- raises priority one step along `PRIORITY_LADDER` (`urgent` does not escalate) and/or moves queue,
- writes `policy_source="escalation"`,
- sets `escalated_at` so it happens **once per ticket**, not once per sweep,
- and **does not recompute deadlines** — neither for the priority bump **nor for the queue move**. A queue move would otherwise re-resolve under §3 and pick up the escalation queue's policy, which is the same cascade through a second door.

Required tests:
- **§10.6, the loop guard:** escalating a breached ticket leaves `response_due_at`/`resolution_due_at` **unchanged**. Mutate the code to recompute after escalation and watch this go red — without this test the cascade is what ships.
- **§10.7:** running `mark_breached` twice escalates once.
- A queue-move escalation likewise does not recompute.
- A queue-move escalation **actually moves the queue** — assert the ticket's `queue_id` afterwards, and assert the `ticket.assigned` audit row carries `actor_type="system"`/`actor_id=None`. (Task 8's review found the old route through `update_ticket` silently dropped the field; a test that only asserts "no exception raised" would have passed against that.)
- `urgent` does not escalate past the ceiling.
- The breach event is published exactly once, with the ticket, the target, and the policy.
- Escalation is audited with the old and new priority/queue.

Commit: `feat(sla): breach marking, one-shot escalation, and the cascade guard`

---

## Task 10: The sweep and auto-close

**Files:** Create `backend/app/modules/sla/sweep.py`; Modify `backend/app/worker.py`; Test `backend/tests/test_sla_sweep.py`

**Interfaces:**
- Produces: `async def sweep_org(session, org_id) -> dict[str, int]`, `async def sweep_all_orgs(*, engine, admin_engine) -> int`, `SWEEP_INTERVAL_SECONDS = 300.0`.

Two passes per org, in one task:

1. **Breach.** Rows where the due column `<= now()`, the matching `breached_at` and `met_at` are NULL, and `paused_at IS NULL`. These are the conditions the partial indexes from Task 1 cover.
2. **Auto-close.** Tickets in `resolved`/`fulfilled` whose `resolved_at` is older than `sla_org_settings.auto_close_after_days` (default 7) → `change_status(..., to_status="closed", actor_type="system", actor_id=None)`.

Add the beat entry alongside the existing ones near `app/worker.py:1044`:

```python
"sla-sweep": {"task": "app.worker.sla_sweep", "schedule": 300.0},
```

and the task itself, following `notification_deliver_pending`'s shape.

Required tests:
- **§10.11 idempotency:** running the sweep twice produces one breach record, one event, one escalation. Assert on counts, not on absence of error.
- **§10.12:** auto-close respects the per-org setting and uses `actor_type="system"`.
- A **paused** ticket past its due time is **not** breached.
- A ticket whose deadline was **met** is not breached.
- One org's sweep does not touch another's — pinned on the admin engine.
- **§10.13:** editing a calendar recomputes running deadlines and leaves met/breached ones untouched. (`recompute_for_calendar_change` lives here or in `tracking.py`; state which in your report.)
- The beat entry is registered — follow the `test_create_celery_registers_*` precedent other modules ship.

Commit: `feat(sla): breach sweep, auto-close, and the beat entry`

---

## Task 11: API

**Files:** Create `backend/app/modules/sla/api.py`, `schema.py`; Modify `backend/app/modules/sla/__init__.py`, `backend/app/modules/__init__.py`; Test `backend/tests/test_sla_api.py`

Routes exactly as spec §9 tabulates. Note the per-ticket read lives under `/api/v1/tickets/{ticket_id}/sla` and is gated on **`ticket.read`**, not `sla.read` — a technician needs to see a deadline without holding SLA admin rights.

Requirements:
- Handlers stay thin: resolve principal, call service, map errors. `SlaNotFoundError` → 404, `SlaValidationError` → 400. **Let the error escape the `org_session` block** so the transaction rolls back; map it outside. Catching inside leaves partial writes committed.
- **404, never 403**, for another org's calendar or policy.
- RBAC completeness derived from the router — **use the flattener from `backend/tests/test_email_intake_api.py`**, since `app.routes` does not contain `include_router`'d routes. Pin the exact route count; a `>=` assertion hides a missing route.
- Removing `require_permission` from one route must turn the completeness test red. Prove it.
- Register in `app/modules/__init__.py` next to the other modules.

Commit: `feat(sla): admin API for calendars, policies, and settings`

---

## Task 12: Frontend, spec amendments, and the live smoke test

**Files:** Create `frontend/src/modules/sla/{api.ts,types.ts,CalendarList.tsx,CalendarEditor.tsx,PolicyMatrix.tsx,TicketSlaPanel.tsx}` and tests; Modify `docs/modules/sla/spec.md`

**Frontend** per spec §9.1, matching `frontend/src/modules/notification/`'s conventions. Three behaviours must be tested on **rendered output**:

1. A calendar's **timezone is visible on every view of it** — a working window is meaningless without it, and a reader who assumes their own timezone misreads every row.
2. The policy matrix renders a wildcard as **"any"**, not blank, so an unset cell is distinguishable from one that deliberately matches everything.
3. A **paused clock is visibly paused** — a countdown that silently stops moving reads as a bug and prompts nobody to act.

Breach state is never computed in the browser (spec §9.1).

**Do not write a test that asserts `Object.keys()` over a fixture defined in the same file.** That test cannot fail; this project has shipped one.

**Spec amendments.** Record every decision the build forced, with its reason. At minimum: the partial unique indexes for wildcard policies (Task 1), whichever module owns `recompute_for_calendar_change` (Task 10), and anything else you deviated on.

**The live smoke test** — a script under the scratchpad directory driving real services against a real Postgres, not mocks:

1. Create an org, a calendar (Mon–Fri 09:00–17:00, `Europe/London`), a queue, a `(queue, high)` policy with a 4-working-hour resolution target and escalation enabled.
2. Create a ticket at a known time **outside** working hours → assert `resolution_due_at` lands at the correct **local** wall-clock moment.
3. Move it to `pending` over a weekend, then back → assert the deadline moved by the working minutes only.
4. Wind the clock past the deadline and run the sweep → assert exactly one breach, one `ticket.sla_breached` event, one escalation.
5. Run the sweep **again** → assert nothing changed.
6. Resolve the ticket, wind past `auto_close_after_days`, sweep → assert it closed with `actor_type="system"`.

Steps 2 and 4 matter most: they are the only end-to-end check that the clock the API reports and the clock the sweep enforces are the same clock. **Report plainly what it did and did not cover** — if a step could not be driven end to end, say so rather than substituting a mock.

**Verify:** backend full suite, `mypy app`, `ruff check app tests`, frontend `npm test` + `npx tsc --noEmit`. Record the counts.

Commit and push; do **not** open a pull request.

---

## Self-review

**Spec coverage.** §0.1 calendars → T2/T3. §0.2 `(queue, priority)` → T4. §0.3 recompute-from-start → T5. §0.4 the cascade guard → T9 (with the named loop-guard test). §0.5 event-not-mail → T9. §0.6 response clock → T6. §2.1–2.6 → T1. §3 resolution → T4. §4 clock → T2. §5 pause → T7. §6 breach/escalation → T9. §7 sweep and auto-close → T10. §8 RBAC/tenancy/audit → T1, T3, T4, T11. §9 API → T11; §9.1 frontend → T12. §10's fifteen tests: 1→T2, 2→T2, 3→T2, 4→T4, 5→T5, 6→T9, 7→T9, 8→T7, 9→T6, 10→T6, 11→T10, 12→T10, 13→T10, 14→T1/T3/T4, 15→T11. §11 migration → T1. §12 worker → T10. §13 deferrals → nothing built.

**Gaps found and closed while reviewing.** The spec's unique constraint on `(org_id, queue_id, priority)` does not prevent duplicate wildcard policies, because NULLs do not collide in a Postgres unique constraint — an org could hold two conflicting fallbacks. Task 1 now requires partial unique indexes and a test. Task 8 was also promoted from a footnote in spec §1 to its own task, because it modifies the ticket core and deserves an independent reviewer gate.

**Naming consistency.** `working_minutes_between`, `add_working_minutes`, `CalendarSpec`, `load_calendar_spec`, `resolve_policy`, `start_tracking`, `recompute_for_retriage`, `note_public_comment`, `note_resolved`, `pause`, `resume`, `mark_breached`, `sweep_org`, `sweep_all_orgs` are each defined once and referenced identically throughout. Table names are `sla_calendar`, `sla_calendar_window`, `sla_calendar_holiday`, `sla_policy`, `ticket_sla`, `sla_org_settings` everywhere.

**Known risk this plan does not remove.** Tasks 5–10 each build their own fixtures over a six-table model. If a fixture papers over a real seam — the way a test harness supplying grants that `deploy/` did not masked a Critical twice in this project — the suite goes green over a module that cannot run in production. Task 12's smoke test exists for that and is not optional.

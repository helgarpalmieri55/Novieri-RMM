"""Migration 0033: shape, RLS, permissions (spec §2, §8, §11).

The RLS tests are BEHAVIOURAL -- they insert two orgs' rows through the
RLS-constrained app engine and assert on what each org sees. A test asserting
`relrowsecurity` is true passes against a policy whose predicate was deleted.

NOTE ON THE MODEL IMPORT. The SLA models are imported from `app.models` (the
aggregator), not `app.modules.sla.models` directly, mirroring
`test_email_intake_migration.py`'s documented reasoning: `sla/models.py`
itself does `from app.models.base import Base, uuid7_pk`. If this file
imported that submodule path directly before anything had imported the
`app.models` package as a whole -- which is exactly what happens when this
file is the FIRST one pytest collects, as in this task's own verification
command -- Python would start executing `app/models/__init__.py` (needed to
reach `app.models.base`), which itself imports `app.modules.sla.models` to
register the new tables on `Base.metadata`, and re-enter the
still-executing `sla/models` module -- raising a genuine circular
`ImportError`. Importing via `app.models` avoids it entirely.

NOTE ON EXCEPTION CLASSES. The brief's own draft asserted `ProgrammingError`
for the CHECK-constraint tests (zero target, inverted window) and left the
unique-`ticket_id` test's exception unstated. Verified against this
codebase's actual asyncpg/SQLAlchemy classification (see
`test_email_intake_migration.py`, `test_budget_budgets.py`, etc.): a
CHECK/UNIQUE constraint violation surfaces as `IntegrityError`; only an RLS
policy violation (`WITH CHECK`) surfaces as `ProgrammingError`. Both are
used below, matched to what each row actually violates.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    SlaCalendar,
    SlaCalendarHoliday,
    SlaCalendarWindow,
    SlaOrgSettings,
    SlaPolicy,
    TicketSla,
)


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
                    "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    # Without the namespace filter this `.one()` would raise on
                    # a same-named relation in any other schema -- a failure
                    # that looks like an RLS defect but is not one.
                    "WHERE n.nspname = 'public' AND c.relname = :t"
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
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            SlaCalendar(
                org_id=org_a, name="UK business", timezone="Europe/London", created_by=actor
            )
        )
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert len((await session.execute(select(SlaCalendar))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(SlaCalendar))).scalars().all() == []


async def test_a_calendar_cannot_be_inserted_under_another_orgs_id(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor = await _seed_user(api_env, org_a)
    with pytest.raises(ProgrammingError):
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                SlaCalendar(
                    org_id=org_b, name="theirs", timezone="Europe/London", created_by=actor
                )
            )


# ---------------------------------------------------------------------------
# Behavioural cross-org isolation, ONE TEST PER TABLE (spec §8, §10.14).
#
# `sla_calendar` has its own hand-written pair above; these five cover the
# rest. The pair matters: `USING` (read) and `WITH CHECK` (write) are separate
# predicates on the same policy, and a mutation to one does not fail the other's
# test. Catalogue assertions like `test_rls_is_enabled_and_forced` above and
# `test_rls.py::test_every_org_scoped_table_has_forced_rls_policy` all pass
# against a policy whose predicate has been replaced with `true` -- verified by
# executing exactly that mutation, which turns SIX tests in this module red and
# nothing else in the repo.
# ---------------------------------------------------------------------------

_CROSS_ORG_TABLES = [
    "sla_calendar_window",
    "sla_calendar_holiday",
    "sla_policy",
    "ticket_sla",
    "sla_org_settings",
]


@pytest.mark.parametrize("table", _CROSS_ORG_TABLES)
async def test_a_row_is_invisible_to_another_org(api_env, a_ticket_for, table: str) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    model, build_row = _ROW_BUILDERS[table]
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(await build_row(api_env, a_ticket_for, org_a, org_a))
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert len((await session.execute(select(model))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(model))).scalars().all() == []


@pytest.mark.parametrize("table", _CROSS_ORG_TABLES)
async def test_a_row_cannot_be_inserted_under_another_orgs_id(
    api_env, a_ticket_for, table: str
) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    _, build_row = _ROW_BUILDERS[table]
    # Every FK this row carries is satisfied (Postgres FK checks bypass RLS),
    # so `org_id` is the only thing the write can be rejected for.
    row = await build_row(api_env, a_ticket_for, org_a, org_b)
    with pytest.raises(ProgrammingError):
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(row)


async def test_a_zero_target_is_rejected(api_env) -> None:
    """Spec §2.4: a zero target would be breached at creation, which is never
    what anyone means."""
    org_id = await api_env.create_org()
    actor = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
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
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                SlaCalendarWindow(
                    org_id=org_id,
                    calendar_id=calendar_id,
                    weekday=0,
                    start_time=time(17, 0),
                    end_time=time(9, 0),
                )
            )


async def test_a_window_ending_at_midnight_is_legal(api_env) -> None:
    """Migration 0035 (Task 3 review). `ck_sla_calendar_window_start_before_end`
    was originally a plain `start_time < end_time` -- Postgres `TIME` has no
    "24:00" representation, so `end_time = '00:00:00'` could never satisfy
    that CHECK for ANY `start_time`, making it physically impossible to store
    the one window shape `app/modules/sla/calendar.py`'s `CalendarSpec`
    explicitly treats as legal: the midnight-end-of-day sentinel a night
    shift's second half or a 24/7 calendar needs (spec §4). The constraint is
    now `start_time < end_time OR end_time = '00:00:00'`.

    THIS is the real pin for migration 0035, not `test_schema_drift.py`.
    `test_schema_drift.py::test_metadata_matches_live_schema` passes
    VACUOUSLY with respect to this change -- its own docstring excludes CHECK
    constraints from the comparison by design, so it would pass identically
    even if `models.py`'s constraint text disagreed with the live database.
    Only this behavioural test -- which moves from a pre-0035 `IntegrityError`
    to a stored row -- actually exercises the new CHECK (Task 3 review,
    execution check 1)."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            SlaCalendarWindow(
                org_id=org_id,
                calendar_id=calendar_id,
                weekday=0,
                start_time=time(18, 0),
                end_time=time(0, 0),
            )
        )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                select(SlaCalendarWindow).where(SlaCalendarWindow.calendar_id == calendar_id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].end_time == time(0, 0)


async def test_one_sla_row_per_ticket(api_env, a_ticket_for) -> None:
    """Spec §2.5: `ticket_id` is UNIQUE. Multiple concurrent SLAs are §13."""
    org_id = await api_env.create_org()
    ticket = await a_ticket_for(org_id, "sla-dup")
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)
    now = datetime.now(UTC)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            TicketSla(
                org_id=org_id,
                ticket_id=ticket.id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=now,
                resolution_due_at=now,
                policy_source="initial",
            )
        )

    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                TicketSla(
                    org_id=org_id,
                    ticket_id=ticket.id,
                    policy_id=policy_id,
                    calendar_id=calendar_id,
                    response_due_at=now,
                    resolution_due_at=now,
                    policy_source="initial",
                )
            )


@pytest.mark.parametrize(
    ("index_name", "scoped_to_a_queue", "priority"),
    [
        ("uq_sla_policy_client_queue_and_priority", True, "high"),
        ("uq_sla_policy_client_queue_wildcard_priority", True, None),
        ("uq_sla_policy_client_wildcard_queue_priority", False, "urgent"),
        ("uq_sla_policy_client_wildcard_both", False, None),
    ],
)
async def test_a_duplicate_policy_is_rejected_at_every_resolution_level(
    api_env, index_name: str, scoped_to_a_queue: bool, priority: str | None
) -> None:
    """Spec §2.4/§3: NULLs do not collide in a Postgres unique constraint, so
    a plain UNIQUE(org_id, queue_id, priority) would let an org hold two
    conflicting `(NULL, NULL)` fallback policies (or two conflicting
    queue-only / priority-only ones) -- resolution would then pick one
    arbitrarily. Four partial unique indexes, one per §3 resolution level,
    close that; ONE CASE PER INDEX, because a suite testing only the
    all-wildcard level stays green when any of the other three is dropped.

    `index_name` is not asserted on -- it names which index each case is
    responsible for, so a failure says which one went missing."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id) if scoped_to_a_queue else None
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=priority
    )

    with pytest.raises(IntegrityError):
        await _seed_policy(
            api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=priority
        )


@pytest.mark.parametrize(
    ("index_name", "scoped_to_a_queue", "priority"),
    [
        ("uq_sla_policy_client_queue_and_priority", True, "high"),
        ("uq_sla_policy_client_queue_wildcard_priority", True, None),
        ("uq_sla_policy_client_wildcard_queue_priority", False, "urgent"),
        ("uq_sla_policy_client_wildcard_both", False, None),
    ],
)
async def test_an_inactive_duplicate_policy_is_allowed_at_every_resolution_level(
    api_env, index_name: str, scoped_to_a_queue: bool, priority: str | None
) -> None:
    """Migration 0036 (Task 4 review F2). Each of the four partial unique
    indexes now ends `AND active`, so uniqueness holds among ACTIVE policies
    at a level -- retired policies at the same level are history §3 never
    looks at.

    THIS is the DB-level pin for 0036, the way
    `test_a_window_ending_at_midnight_is_legal` is for 0035:
    `test_schema_drift.py` does not compare indexes at all, so nothing else
    in the repo would notice if 0036 were reverted. The test above is its
    other half -- together they say "one active policy per level, any number
    of retired ones", and dropping `AND active` from any single index turns
    exactly that index's case here red.

    ONE CASE PER INDEX for the same reason as the test above: a suite testing
    only the all-wildcard level stays green when the other three regress.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id) if scoped_to_a_queue else None

    active_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=priority
    )
    # Two RETIRED policies at the same level, alongside the active one.
    first_retired = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=priority, active=False
    )
    second_retired = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=priority, active=False
    )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (await session.execute(select(SlaPolicy))).scalars().all()
    assert {r.id for r in rows} == {active_id, first_retired, second_retired}
    assert sum(1 for r in rows if r.active) == 1


@pytest.mark.parametrize("weekday", [-1, 7, 99])
async def test_a_weekday_outside_monday_to_sunday_is_rejected(api_env, weekday: int) -> None:
    """Migration 0034. Spec §2.2 is "int 0-6, Monday=0", but nothing enforced
    it: `weekday = 99` committed cleanly. Python's `date.weekday()` only ever
    returns 0-6, so an ISO Sunday-is-7 typo produced a window that silently
    never applied -- the calendar quietly lost a working day, deadlines landed
    later than the org configured, and nothing errored anywhere."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                SlaCalendarWindow(
                    org_id=org_id,
                    calendar_id=calendar_id,
                    weekday=weekday,
                    start_time=time(9, 0),
                    end_time=time(17, 0),
                )
            )


@pytest.mark.parametrize("role", ["org-admin", "super-admin"])
@pytest.mark.parametrize("code", ["sla.read", "sla.admin"])
async def test_permission_is_seeded_to_the_admin_roles(admin_engine, code: str, role: str) -> None:
    """Spec §8: both codes seeded to `super-admin` AND `org-admin`.
    `scalar_one_or_none` fails both ways -- `None` if the grant is missing,
    `MultipleResultsFound` if the migration double-granted it."""
    async with AsyncSession(admin_engine) as session:
        granted = (
            await session.execute(
                text(
                    "SELECT 1 FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = :r AND p.code = :c"
                ),
                {"c": code, "r": role},
            )
        ).scalar_one_or_none()
    assert granted == 1


async def test_the_breach_event_type_is_registered() -> None:
    from app.core.events import EVENT_TYPES

    assert "ticket.sla_breached" in EVENT_TYPES


# ---------------------------------------------------------------------------
# Seed helpers -- later tasks import these, so their signatures matter.
# ---------------------------------------------------------------------------


async def _seed_user(api_env, org_id: UUID, email: str | None = None) -> UUID:
    """Creates a fresh `users` row via the admin (RLS-bypassing) engine, for
    use as `created_by`. Side effect: inserts exactly one row into `users`
    per call -- a hidden extra row broke sender-resolution tests in the
    email-intake sub-project, so this uses an obviously non-colliding,
    caller-independent email by default."""
    from app.models import User

    email = email or f"sla-actor-{uuid4().hex[:10]}@test.local"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=f"sla-{uuid4().hex[:10]}", email=email)
        session.add(user)
        await session.commit()
        return user.id


async def _seed_queue(api_env, org_id: UUID) -> UUID:
    """Creates a fresh `ticket_queue` row via the admin (RLS-bypassing)
    engine, for use as a policy's `queue_id`.

    Side effects: inserts exactly one `ticket_queue` row per call, AND calls
    `_seed_user` for `created_by`, which inserts one extra `users` row per
    call (see `_seed_user`'s docstring). Nothing else -- no category, no
    ticket.
    """
    from app.models import TicketQueue

    actor = await _seed_user(
        api_env, org_id, email=f"sla-queue-actor-{uuid4().hex[:10]}@test.local"
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = TicketQueue(
            org_id=org_id,
            key=f"sla-q-{uuid4().hex[:10]}",
            name=f"queue-{uuid4().hex[:6]}",
            created_by=actor,
        )
        session.add(queue)
        await session.commit()
        return queue.id


async def _seed_calendar(
    api_env,
    org_id: UUID,
    *,
    name: str | None = None,
    timezone: str = "Europe/London",
    active: bool = True,
    windows: list[tuple[int, time, time]] | None = None,
    holidays: list[date] | None = None,
) -> UUID:
    """Seeds a calendar with working windows and (optionally) holidays, via
    the RLS-constrained app engine under `org_id`'s tenant context.

    `name` defaults to a `uuid4`-suffixed unique name when None; pass an
    explicit one to set up a `uq_sla_calendar_org_name` conflict (Task 3).
    `active` defaults to True; pass `False` for a calendar an active policy
    must not be allowed to reference (Task 3).

    `windows` defaults to Mon-Fri 09:00-17:00 when None; pass `[]` for a
    calendar with no windows at all.

    Side effect: calls `_seed_user` for `created_by`, which inserts one extra
    `users` row per call (see `_seed_user`'s docstring). Beyond that this
    helper writes exactly one `sla_calendar` row, one `sla_calendar_window`
    row per entry in `windows`, and one `sla_calendar_holiday` row per entry
    in `holidays`.
    """
    actor = await _seed_user(
        api_env, org_id, email=f"sla-calendar-actor-{uuid4().hex[:10]}@test.local"
    )
    if windows is None:
        windows = [(d, time(9, 0), time(17, 0)) for d in range(5)]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        calendar = SlaCalendar(
            org_id=org_id,
            name=name if name is not None else f"calendar-{uuid4().hex[:10]}",
            timezone=timezone,
            active=active,
            created_by=actor,
        )
        session.add(calendar)
        await session.flush()
        for weekday, start, end in windows:
            session.add(
                SlaCalendarWindow(
                    org_id=org_id,
                    calendar_id=calendar.id,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                )
            )
        for holiday_date in holidays or []:
            session.add(
                SlaCalendarHoliday(
                    org_id=org_id,
                    calendar_id=calendar.id,
                    holiday_date=holiday_date,
                    name=f"holiday-{holiday_date.isoformat()}",
                )
            )
        calendar_id = calendar.id
    return calendar_id


async def _seed_policy(
    api_env,
    org_id: UUID,
    *,
    calendar_id: UUID,
    queue_id: UUID | None = None,
    priority: str | None = None,
    response_minutes: int = 60,
    resolution_minutes: int = 480,
    escalate_priority: bool = False,
    escalate_to_queue_id: UUID | None = None,
    active: bool = True,
) -> UUID:
    """Seeds one SLA policy via the RLS-constrained app engine under
    `org_id`'s tenant context.

    `active` defaults to True; pass `False` for a policy that resolution must
    skip (Task 4) or that must not block deleting its calendar (Task 3).

    Side effect: calls `_seed_user` for `created_by`, which inserts one extra
    `users` row per call (see `_seed_user`'s docstring). Beyond that this
    helper writes exactly one `sla_policy` row -- it does NOT seed a calendar
    or a queue, so `calendar_id`/`queue_id` must already exist.
    """
    actor = await _seed_user(
        api_env, org_id, email=f"sla-policy-actor-{uuid4().hex[:10]}@test.local"
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        policy = SlaPolicy(
            org_id=org_id,
            queue_id=queue_id,
            priority=priority,
            calendar_id=calendar_id,
            response_target_minutes=response_minutes,
            resolution_target_minutes=resolution_minutes,
            escalate_priority=escalate_priority,
            escalate_to_queue_id=escalate_to_queue_id,
            active=active,
            created_by=actor,
        )
        session.add(policy)
        await session.flush()
        policy_id = policy.id
    return policy_id


# ---------------------------------------------------------------------------
# Row builders for the per-table cross-org tests above.
#
# Each returns an UNSAVED instance carrying `row_org` as its `org_id`, having
# already seeded whatever it needs to reference under `seed_org`. Splitting the
# two orgs is what lets one builder serve both the read test (`row_org ==
# seed_org`) and the WITH CHECK test (`row_org` is the OTHER org, every FK
# still satisfied, so `org_id` is the only thing left to reject).
# ---------------------------------------------------------------------------


async def _build_window(api_env, a_ticket_for, seed_org: UUID, row_org: UUID):
    calendar_id = await _seed_calendar(api_env, seed_org, windows=[])
    return SlaCalendarWindow(
        org_id=row_org,
        calendar_id=calendar_id,
        weekday=0,
        start_time=time(9, 0),
        end_time=time(17, 0),
    )


async def _build_holiday(api_env, a_ticket_for, seed_org: UUID, row_org: UUID):
    calendar_id = await _seed_calendar(api_env, seed_org, windows=[])
    return SlaCalendarHoliday(
        org_id=row_org,
        calendar_id=calendar_id,
        holiday_date=date(2026, 12, 25),
        name="Christmas Day",
    )


async def _build_policy(api_env, a_ticket_for, seed_org: UUID, row_org: UUID):
    calendar_id = await _seed_calendar(api_env, seed_org, windows=[])
    actor = await _seed_user(api_env, seed_org)
    return SlaPolicy(
        org_id=row_org,
        queue_id=None,
        priority=None,
        calendar_id=calendar_id,
        response_target_minutes=60,
        resolution_target_minutes=480,
        created_by=actor,
    )


async def _build_ticket_sla(api_env, a_ticket_for, seed_org: UUID, row_org: UUID):
    ticket = await a_ticket_for(seed_org, "sla-tenancy")
    calendar_id = await _seed_calendar(api_env, seed_org, windows=[])
    policy_id = await _seed_policy(api_env, seed_org, calendar_id=calendar_id)
    now = datetime.now(UTC)
    return TicketSla(
        org_id=row_org,
        ticket_id=ticket.id,
        policy_id=policy_id,
        calendar_id=calendar_id,
        response_due_at=now,
        resolution_due_at=now,
        policy_source="initial",
    )


async def _build_org_settings(api_env, a_ticket_for, seed_org: UUID, row_org: UUID):
    actor = await _seed_user(api_env, seed_org)
    return SlaOrgSettings(org_id=row_org, auto_close_after_days=7, created_by=actor)


_ROW_BUILDERS: dict[str, tuple[type, Callable[..., Awaitable[Any]]]] = {
    "sla_calendar_window": (SlaCalendarWindow, _build_window),
    "sla_calendar_holiday": (SlaCalendarHoliday, _build_holiday),
    "sla_policy": (SlaPolicy, _build_policy),
    "ticket_sla": (TicketSla, _build_ticket_sla),
    "sla_org_settings": (SlaOrgSettings, _build_org_settings),
}

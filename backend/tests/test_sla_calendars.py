"""Calendar CRUD: windows, holidays, and the bridge to the pure clock
(docs/modules/sla/spec.md §2.1-2.3, §9; Task 3).

Mirrors `test_email_intake_mailboxes.py`/`test_budget_budgets.py`'s shape:
behavioural org-scoping tests, a real two-session race for the ON CONFLICT
create path, and a `load_calendar_spec` integration check against Task 2's
pure clock (`app/modules/sla/calendar.py`).

ADMIN-ENGINE-PINNED TESTS. `org_session` runs through `app_user`, which is
RLS-constrained -- a cross-org test through it alone passes even if this
module's own `org_id` predicate is deleted, because RLS rejects first. The
tests in the "tenancy -- load-bearing" section below use a raw
`AsyncSession(admin_engine)` (its_admin is BYPASSRLS,
`tests/conftest.py`), so the SERVICE FUNCTION's predicate is the sole guard
under test.
"""

import asyncio
import re
from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent
from app.modules.sla.calendar import CalendarSpec, working_minutes_between
from app.modules.sla.calendars import (
    add_holiday,
    create_calendar,
    delete_calendar,
    get_calendar,
    list_calendars,
    load_calendar_spec,
    remove_holiday,
    replace_windows,
    update_calendar,
)
from app.modules.sla.errors import SlaNotFoundError, SlaValidationError
from app.modules.sla.models import (
    SlaCalendar,
    SlaCalendarHoliday,
    SlaCalendarWindow,
    SlaPolicy,
    TicketSla,
)
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user

LONDON = "Europe/London"

# Task 3 review F1: every one of these is a name `zoneinfo.ZoneInfo` will not
# resolve, and every one must become a typed `SlaValidationError`, never a raw
# 500. `_validate_timezone` originally caught only `ZoneInfoNotFoundError` (a
# `KeyError` subclass); it missed the plain `ValueError` `zoneinfo` raises for
# a malformed key -- `''`, an absolute path, and a `..`-containing,
# path-traversal-shaped name are all `ValueError`, not `ZoneInfoNotFoundError`
# -- and both existing tests used only `"Not/AZone"`, holding the exact
# parameter the defect varies (which exception class is raised) constant.
_BAD_TIMEZONES = [
    "Not/AZone",  # ZoneInfoNotFoundError: well-formed, unknown
    "",  # ValueError: empty key
    "/etc/localtime",  # ValueError: absolute path
    "Europe/../../etc/passwd",  # ValueError: path-traversal-shaped
    "UTC ",  # ZoneInfoNotFoundError: trailing space, unknown
]


async def _audit(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# create_calendar
# ---------------------------------------------------------------------------


async def test_create_calendar_stores_the_row_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        calendar = await create_calendar(
            session, org_id, actor_id, name="UK Business", timezone=LONDON
        )

    assert calendar.name == "UK Business"
    assert calendar.timezone == LONDON
    assert calendar.active is True

    events = await _audit(api_env, org_id)
    assert [(e.action, e.target_id) for e in events] == [
        ("sla.calendar_created", str(calendar.id))
    ]


@pytest.mark.parametrize("bad_tz", _BAD_TIMEZONES)
async def test_create_calendar_rejects_an_invalid_timezone(api_env, bad_tz: str) -> None:
    """Spec §2.1: every string in `_BAD_TIMEZONES` must become a typed
    `SlaValidationError`, never a raw 500, and the row must never be
    written (Task 3 review F1)."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)

    with pytest.raises(SlaValidationError, match=re.escape(repr(bad_tz))):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await create_calendar(session, org_id, actor_id, name="Bad TZ", timezone=bad_tz)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(SlaCalendar))).scalars().all() == []


async def test_create_calendar_rejects_a_non_string_timezone(api_env) -> None:
    """`zoneinfo.ZoneInfo` raises `TypeError` -- not `ValueError` or
    `ZoneInfoNotFoundError` -- for a non-string key. `create_calendar`'s
    `timezone: str` parameter is enforced only by mypy, not at runtime, so a
    caller that loses type validation upstream must still get a typed 400,
    not a raw 500 (Task 3 review F1: "reject anything ZoneInfo will not
    accept")."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)

    with pytest.raises(SlaValidationError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await create_calendar(
                session, org_id, actor_id, name="Bad TZ", timezone=None  # type: ignore[arg-type]
            )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(SlaCalendar))).scalars().all() == []


async def test_create_calendar_duplicate_name_is_a_typed_error_not_an_integrity_error(
    api_env,
) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_calendar(session, org_id, actor_id, name="Ops", timezone=LONDON)

    with pytest.raises(SlaValidationError, match="already exists"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await create_calendar(session, org_id, actor_id, name="Ops", timezone="UTC")


async def test_the_same_name_is_allowed_in_a_different_org(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    actor_b = await _seed_user(api_env, org_b)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_calendar(session, org_a, actor_a, name="Ops", timezone=LONDON)
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_calendar(session, org_b, actor_b, name="Ops", timezone=LONDON)


async def test_concurrent_creates_of_the_same_name_yield_one_row(api_env) -> None:
    """The real race, two sessions, not a simulated one (see module docstring
    and CLAUDE.md's TOCTOU discipline). `create_calendar` has no vault or
    other secondary ON CONFLICT to warm up first (unlike
    `email_intake.mailboxes`), so the barrier alone is enough to make the two
    attempts genuinely collide on `uq_sla_calendar_org_name`."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    barrier = asyncio.Barrier(2)

    async def attempt() -> bool:
        try:
            async with org_session(
                org_id, engine=api_env.app_engine, clients=ClientScope.all()
            ) as session:
                # Take the connection and open the transaction BEFORE the
                # rendezvous so neither attempt pays connection latency after
                # the barrier releases.
                await session.execute(select(SlaCalendar.id))
                await asyncio.wait_for(barrier.wait(), timeout=30)
                await create_calendar(session, org_id, actor_id, name="Race", timezone=LONDON)
            return True
        except SlaValidationError:
            return False

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in results), results
    # Exactly one winner and one loser -- not two winners, not two losers.
    assert sorted(results) == [False, True], results

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(select(SlaCalendar).where(SlaCalendar.name == "Race"))
        ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# get_calendar / list_calendars
# ---------------------------------------------------------------------------


async def test_get_calendar_raises_not_found_for_a_missing_id(api_env) -> None:
    org_id = await api_env.create_org()
    with pytest.raises(SlaNotFoundError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await get_calendar(session, org_id, uuid4())


async def test_list_calendars_is_sorted_by_name_and_shows_only_this_orgs_rows_under_rls(
    api_env,
) -> None:
    org_id = await api_env.create_org()
    other_org = await api_env.create_org("other")
    await _seed_calendar(api_env, org_id, name="Zebra")
    await _seed_calendar(api_env, org_id, name="Alpha")
    await _seed_calendar(api_env, other_org, name="Not Mine")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        calendars = await list_calendars(session, org_id)

    assert [c.name for c in calendars] == ["Alpha", "Zebra"]


# ---------------------------------------------------------------------------
# update_calendar
# ---------------------------------------------------------------------------


async def test_update_calendar_applies_supplied_fields_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, name="Ops")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        updated = await update_calendar(
            session, org_id, actor_id, calendar_id, {"active": False}
        )

    assert updated.active is False
    assert updated.name == "Ops"  # untouched

    events = await _audit(api_env, org_id)
    assert events[-1].action == "sla.calendar_updated"
    assert events[-1].meta == {"changed": ["active"]}


@pytest.mark.parametrize("bad_tz", _BAD_TIMEZONES)
async def test_update_calendar_rejects_an_invalid_timezone(api_env, bad_tz: str) -> None:
    """Same table as `test_create_calendar_rejects_an_invalid_timezone`
    (Task 3 review F1) -- `update_calendar` casts to `str()` before
    validating, so every string case reaches `_validate_timezone` exactly as
    `create_calendar` does. (`None` is deliberately excluded here: supplying
    `timezone: None` in `fields` means "no change" and is rejected earlier,
    with its own typed "cannot be null" error -- see
    `test_update_calendar_rejects_null_for_a_non_nullable_field`.)"""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    with pytest.raises(SlaValidationError, match=re.escape(repr(bad_tz))):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await update_calendar(session, org_id, actor_id, calendar_id, {"timezone": bad_tz})

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        calendar = await get_calendar(session, org_id, calendar_id)
    assert calendar.timezone == LONDON  # unchanged


async def test_update_calendar_rename_to_an_existing_name_is_a_typed_error(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    await _seed_calendar(api_env, org_id, name="Taken")
    calendar_id = await _seed_calendar(api_env, org_id, name="Mine")

    with pytest.raises(SlaValidationError, match="already exists") as excinfo:
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await update_calendar(session, org_id, actor_id, calendar_id, {"name": "Taken"})

    # `from exc`, not `from None`: the typed error is a better DIAGNOSIS of the
    # violation, not a replacement for the evidence. A `from None` here also
    # hides which constraint actually fired, which is precisely what made the
    # un-narrowed handler's mistranslation (the test below) undebuggable.
    assert isinstance(excinfo.value.__cause__, IntegrityError)


async def test_update_calendar_re_raises_an_unrelated_integrity_error(api_env) -> None:
    """The fallback narrowing on the RENAME path (Task 11 review F5), the
    counterpart of `test_delete_calendar_blocked_by_a_cross_org_policy_
    reference`'s narrowing on the delete path.

    THE MECHANISM, because it is what makes an unrelated violation reachable
    at all. `session.begin_nested()` calls `Session.flush()` from
    `SessionTransaction._take_snapshot` the instant it opens -- an EXPLICIT
    flush, not autoflush, so it fires even inside `no_autoflush` -- and a flush
    emits the caller's WHOLE unit of work, not just the calendar UPDATE this
    block is about. That flush is inside `update_calendar`'s `try`, so a
    doomed row the caller added earlier surfaces at the very handler written
    for the duplicate-name case.

    THE PARAMETER THIS FIXTURE VARIES is WHICH constraint the violation
    names, while holding `renaming` True. The un-narrowed handler branched on
    `renaming` alone, so with a genuine duplicate name -- the only case the
    suite exercised -- the guarded and unguarded versions are
    indistinguishable. Here the rename is to a name nothing holds, and the
    violation is a duplicate HOLIDAY: mistranslating it would tell an admin
    "a calendar named 'Renamed' already exists in this org", which is not
    merely unhelpful but false, with `from None` erasing the constraint that
    actually fired.

    `no_autoflush` is what places the doomed row at that flush rather than at
    `get_calendar`'s autoflush a few lines earlier; a caller batching writes
    (the reason `no_autoflush` exists) reaches this ordering for real. The
    session is a raw admin one because the failed flush runs in the OUTER
    transaction -- `_take_snapshot` flushes BEFORE the SAVEPOINT is issued --
    which leaves it aborted, and `org_session` would try to commit it on exit.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(
        api_env, org_id, name="Mine", holidays=[date(2026, 12, 25)]
    )

    async with AsyncSession(api_env.admin_engine) as session:
        with session.no_autoflush:
            session.add(
                SlaCalendarHoliday(
                    org_id=org_id,
                    calendar_id=calendar_id,
                    holiday_date=date(2026, 12, 25),  # already taken
                    name="Christmas, again",
                )
            )
            with pytest.raises(IntegrityError) as excinfo:
                await update_calendar(
                    session, org_id, actor_id, calendar_id, {"name": "Renamed"}
                )

    assert "uq_sla_calendar_holiday_cal_date" in str(excinfo.value)
    # Nothing was committed: no rename, no audit row.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await get_calendar(session, org_id, calendar_id)).name == "Mine"
    assert await _audit(api_env, org_id) == []


async def test_update_calendar_rejects_null_for_a_non_nullable_field(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    with pytest.raises(SlaValidationError, match="cannot be null"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await update_calendar(session, org_id, actor_id, calendar_id, {"name": None})


# ---------------------------------------------------------------------------
# delete_calendar
# ---------------------------------------------------------------------------


async def test_delete_calendar_removes_windows_and_holidays(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(
        api_env, org_id, holidays=[date(2026, 12, 25)]
    )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await delete_calendar(session, org_id, actor_id, calendar_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(SlaCalendar))).scalars().all() == []
        assert (await session.execute(select(SlaCalendarWindow))).scalars().all() == []
        assert (await session.execute(select(SlaCalendarHoliday))).scalars().all() == []

    events = await _audit(api_env, org_id)
    assert events[-1].action == "sla.calendar_deleted"


async def test_delete_calendar_blocked_by_an_active_policy_names_it(api_env) -> None:
    """Spec §9: the error names the policies."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id, active=True)

    with pytest.raises(SlaValidationError, match=str(policy_id)):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_calendar(session, org_id, actor_id, calendar_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await get_calendar(session, org_id, calendar_id) is not None


async def test_delete_calendar_blocked_by_an_inactive_only_policy(api_env) -> None:
    """DELIBERATE DECISION (see `calendars.py` module docstring): the FK
    `sla_policy.calendar_id` is RESTRICT and does not distinguish active from
    inactive rows, so an inactive-only reference blocks deletion exactly as
    an active one does -- but with its own typed error naming it, rather
    than surfacing the FK violation as a raw `IntegrityError`."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id, active=False)

    with pytest.raises(SlaValidationError, match=str(policy_id)):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_calendar(session, org_id, actor_id, calendar_id)


async def test_delete_calendar_blocked_by_a_same_org_ticket_sla_reference(
    api_env, a_ticket_for
) -> None:
    """Task 3 review F2: `ticket_sla.calendar_id` is a FOURTH RESTRICT FK
    into `sla_calendar` that the pre-check originally missed, so a completely
    ordinary same-org shape -- exactly what spec §9 itself says survives a
    policy delete ("Deleting a policy leaves existing `ticket_sla` rows
    intact") -- fell through to the generic cross-org fallback and was
    reported with a FALSE, unactionable message ("referenced by another
    record this organization cannot see", when the admin can see this ticket
    perfectly well). The error must instead name the real reason."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    other_calendar_id = await _seed_calendar(api_env, org_id, windows=[], name="Elsewhere")
    policy_id = await _seed_policy(api_env, org_id, calendar_id=other_calendar_id)
    ticket = await a_ticket_for(org_id, "sla-delete")

    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
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
        await session.commit()

    with pytest.raises(SlaValidationError, match=str(ticket.id)) as excinfo:
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_calendar(session, org_id, actor_id, calendar_id)
    # It must NOT be the generic cross-org fallback message -- that message
    # is false for a same-org reference.
    assert "cannot see" not in str(excinfo.value)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await get_calendar(session, org_id, calendar_id) is not None


async def test_delete_calendar_blocked_by_a_cross_org_policy_reference(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The self-found cross-org fallback (Task 3 review, execution check 4):
    a policy in a DIFFERENT org referencing this calendar (the FK has no org
    concept; nothing in ordinary application traffic can produce this row --
    Task 4's `create_policy` resolves `calendar_id` org-scoped) still blocks
    the delete, converted to the same typed `SlaValidationError`, and must
    not name org B's policy id -- it belongs to a tenant this caller cannot
    see."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, name="A's calendar", windows=[])
    queue_b = await _seed_queue(api_env, org_b)
    user_b = await _seed_user(api_env, org_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        policy_b = SlaPolicy(
            org_id=org_b,
            queue_id=queue_b,
            priority=None,
            calendar_id=calendar_a,
            response_target_minutes=60,
            resolution_target_minutes=480,
            created_by=user_b,
        )
        session.add(policy_b)
        await session.commit()
        policy_b_id = str(policy_b.id)

    with pytest.raises(SlaValidationError) as excinfo:
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_calendar(session, org_a, actor_a, calendar_a)
    assert policy_b_id not in str(excinfo.value)
    assert "another record this organization cannot see" in str(excinfo.value)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await get_calendar(session, org_a, calendar_a) is not None


async def test_delete_calendar_raises_not_found_for_a_missing_id(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    with pytest.raises(SlaNotFoundError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_calendar(session, org_id, actor_id, uuid4())


# ---------------------------------------------------------------------------
# replace_windows
# ---------------------------------------------------------------------------


async def test_replace_windows_replaces_the_whole_set(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)  # Mon-Fri 09:00-17:00

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await replace_windows(
            session, org_id, actor_id, calendar_id, [(5, time(10, 0), time(14, 0))]
        )

    assert [(w.weekday, w.start_time, w.end_time) for w in result] == [
        (5, time(10, 0), time(14, 0))
    ]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                select(SlaCalendarWindow).where(SlaCalendarWindow.calendar_id == calendar_id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].weekday == 5


async def test_replace_windows_permits_a_midnight_end_time(api_env) -> None:
    """The resolved decision this task must honour: `end_time = 00:00` is
    LEGAL (midnight-end-of-day) -- not rejected as `start >= end`, and
    actually persistable now that migration 0035 relaxes the DB CHECK."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await replace_windows(
            session,
            org_id,
            actor_id,
            calendar_id,
            [(0, time(18, 0), time(0, 0)), (0, time(0, 0), time(6, 0))],
        )

    assert {(w.start_time, w.end_time) for w in result} == {
        (time(18, 0), time(0, 0)),
        (time(0, 0), time(6, 0)),
    }


async def test_replace_windows_rejects_start_after_end(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])

    with pytest.raises(SlaValidationError, match="start"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await replace_windows(
                session, org_id, actor_id, calendar_id, [(0, time(17, 0), time(9, 0))]
            )


async def test_replace_windows_rejects_overlapping_windows_on_the_same_weekday(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])

    with pytest.raises(SlaValidationError, match="overlap"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await replace_windows(
                session,
                org_id,
                actor_id,
                calendar_id,
                [(0, time(9, 0), time(13, 0)), (0, time(12, 0), time(17, 0))],
            )


async def test_replace_windows_permits_touching_windows(api_env) -> None:
    """[start, end) is half-open: a window ending exactly where the next
    starts is a lunch break, not an overlap."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await replace_windows(
            session,
            org_id,
            actor_id,
            calendar_id,
            [(0, time(9, 0), time(12, 0)), (0, time(12, 0), time(17, 0))],
        )
    assert len(result) == 2


async def test_replace_windows_rejects_an_invalid_weekday(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])

    with pytest.raises(SlaValidationError, match="weekday"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await replace_windows(
                session, org_id, actor_id, calendar_id, [(7, time(9, 0), time(17, 0))]
            )


async def test_replace_windows_validates_before_writing_anything(api_env) -> None:
    """A batch that fails validation must leave the calendar's EXISTING
    windows untouched -- a partial edit can otherwise leave overlapping or
    inverted windows the clock has no defined behaviour for."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)  # Mon-Fri 09:00-17:00

    with pytest.raises(SlaValidationError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await replace_windows(
                session,
                org_id,
                actor_id,
                calendar_id,
                [(0, time(9, 0), time(13, 0)), (0, time(12, 0), time(17, 0))],
            )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                select(SlaCalendarWindow).where(SlaCalendarWindow.calendar_id == calendar_id)
            )
        ).scalars().all()
    assert len(rows) == 5  # the original Mon-Fri set, untouched


# ---------------------------------------------------------------------------
# add_holiday / remove_holiday
# ---------------------------------------------------------------------------


async def test_add_and_remove_holiday(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        holiday = await add_holiday(
            session, org_id, actor_id, calendar_id, holiday_date=date(2026, 12, 25), name="Xmas"
        )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await remove_holiday(session, org_id, actor_id, calendar_id, holiday.id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(SlaCalendarHoliday))).scalars().all() == []

    events = await _audit(api_env, org_id)
    assert [e.action for e in events] == [
        "sla.calendar_holiday_added",
        "sla.calendar_holiday_removed",
    ]


async def test_add_holiday_duplicate_date_is_a_typed_error(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await add_holiday(
            session, org_id, actor_id, calendar_id, holiday_date=date(2026, 12, 25), name="Xmas"
        )

    with pytest.raises(SlaValidationError, match="already has a holiday"):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await add_holiday(
                session,
                org_id,
                actor_id,
                calendar_id,
                holiday_date=date(2026, 12, 25),
                name="Christmas Day",
            )


async def test_remove_holiday_raises_not_found_for_a_missing_id(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    with pytest.raises(SlaNotFoundError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await remove_holiday(session, org_id, actor_id, calendar_id, uuid4())


async def test_remove_holiday_is_scoped_to_its_own_calendar(api_env) -> None:
    """A holiday id that exists but belongs to a DIFFERENT calendar in the
    same org must not be removable through the wrong calendar_id."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_a = await _seed_calendar(api_env, org_id, name="A")
    calendar_b = await _seed_calendar(api_env, org_id, name="B")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        holiday = await add_holiday(
            session, org_id, actor_id, calendar_a, holiday_date=date(2026, 12, 25), name="Xmas"
        )

    with pytest.raises(SlaNotFoundError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await remove_holiday(session, org_id, actor_id, calendar_b, holiday.id)


# ---------------------------------------------------------------------------
# load_calendar_spec -- the bridge to Task 2's pure clock
# ---------------------------------------------------------------------------


async def test_load_calendar_spec_builds_a_spec_the_clock_can_use(api_env) -> None:
    """Task 3 review F6: the original fixture (one window, one weekday, a
    holiday-only assertion) is structurally immune to defects in window
    ordering, multiple windows per weekday, and the `end_time = 00:00`
    sentinel -- a lost window, a duplicated window, or a dropped sentinel
    would ALL still pass it. This shape exercises every one of those, over
    the week of 2026-03-02 (a Monday):

      - Monday: split by a lunch break (09:00-12:30, 13:30-17:00) -> 420 min
      - Tuesday: an evening shift to midnight (18:00-00:00) -> 360 min
      - Wednesday: a night-shift second half plus a day window
        (00:00-06:00, 09:00-17:00) -> 840 min of windows, but it is ALSO one
        of the two holidays -> 0 min, proving the holiday still wins over a
        heavily-windowed day
      - Thursday: a genuine 24-hour day (00:00-00:00) -> 1440 min
      - Friday/Saturday/Sunday: no windows -> 0 min

    Windows are listed here already in `(weekday, start_time)` order, which
    is also what `load_calendar_spec`'s `ORDER BY` produces (F4) -- so the
    round-tripped spec compares equal to a hand-built one, not just
    set-equal.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=[])

    windows = [
        (0, time(9, 0), time(12, 30)),  # Monday morning
        (0, time(13, 30), time(17, 0)),  # Monday afternoon (lunch break split)
        (1, time(18, 0), time(0, 0)),  # Tuesday: evening shift to midnight
        (2, time(0, 0), time(6, 0)),  # Wednesday: night-shift second half
        (2, time(9, 0), time(17, 0)),  # Wednesday: normal day window
        (3, time(0, 0), time(0, 0)),  # Thursday: a genuine 24-hour day
    ]
    # This week's Wednesday, plus an unrelated one:
    holidays = [date(2026, 3, 4), date(2026, 12, 25)]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await replace_windows(session, org_id, actor_id, calendar_id, windows)
        for holiday_date in holidays:
            await add_holiday(
                session,
                org_id,
                actor_id,
                calendar_id,
                holiday_date=holiday_date,
                name=f"holiday-{holiday_date.isoformat()}",
            )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        spec = await load_calendar_spec(session, org_id, calendar_id)

    assert spec.timezone == LONDON
    assert spec.windows == tuple(windows)
    assert spec.holidays == frozenset(holidays)
    hand = CalendarSpec(timezone=LONDON, windows=tuple(windows), holidays=frozenset(holidays))
    assert spec == hand

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(LONDON)

    def _day_minutes(day: int) -> int:
        start = datetime(2026, 3, day, 0, 0, tzinfo=tz).astimezone(UTC)
        end = datetime(2026, 3, day + 1, 0, 0, tzinfo=tz).astimezone(UTC)
        return working_minutes_between(spec, start, end)

    assert _day_minutes(2) == 420  # Monday: lunch break
    assert _day_minutes(3) == 360  # Tuesday: evening shift to midnight
    assert _day_minutes(4) == 0  # Wednesday: holiday, despite 840 min of windows
    assert _day_minutes(5) == 1440  # Thursday: a genuine 24-hour day
    assert _day_minutes(6) == 0  # Friday
    assert _day_minutes(7) == 0  # Saturday
    assert _day_minutes(8) == 0  # Sunday

    week_start = datetime(2026, 3, 2, 0, 0, tzinfo=tz).astimezone(UTC)
    week_end = datetime(2026, 3, 9, 0, 0, tzinfo=tz).astimezone(UTC)
    assert working_minutes_between(spec, week_start, week_end) == 2220


async def test_load_calendar_spec_raises_not_found_for_a_missing_calendar(api_env) -> None:
    org_id = await api_env.create_org()
    with pytest.raises(SlaNotFoundError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await load_calendar_spec(session, org_id, uuid4())


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session (see module
# docstring). RLS masks the app-level predicate through org_session; these
# tests remove RLS from the equation entirely.
# ---------------------------------------------------------------------------


async def test_tenancy_get_calendar_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b, name="Org B's calendar")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaNotFoundError):
            await get_calendar(session, org_a, calendar_b)


async def test_tenancy_delete_calendar_policy_check_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The policy pre-check in `delete_calendar` is deliberately `org_id`
    scoped: it must never NAME another org's policy id to a caller in a
    different org (that would be a cross-tenant existence disclosure -- the
    exact bug class `notification/channels.py`'s module docstring warns
    about). Constructed directly on the BYPASSRLS admin session (the FK
    permits a policy to reference another org's calendar; nothing in
    ordinary application traffic would ever produce this row -- Task 4's
    `create_policy` resolves `calendar_id` org-scoped).

    Proves TWO things at once, both dependent on the `org_id` predicate:
    1. The typed error's message never contains org B's policy id -- the
       org-scoped pre-check legitimately finds nothing, so it falls through
       to the generic fallback that names nobody.
    2. Deletion is refused at all (rather than silently succeeding) --
       because the FK is real and does not check org membership, so the
       fallback in `delete_calendar` must convert the resulting
       `IntegrityError` into the same typed `SlaValidationError` shape.

    If the `org_id` predicate were dropped from the pre-check query, org B's
    policy id would appear in the error message instead -- (1) would fail.
    """
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, name="A's calendar")
    queue_b = await _seed_queue(api_env, org_b)
    user_b = await _seed_user(api_env, org_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        policy_b = SlaPolicy(
            org_id=org_b,
            queue_id=queue_b,
            priority=None,
            calendar_id=calendar_a,
            response_target_minutes=60,
            resolution_target_minutes=480,
            created_by=user_b,
        )
        session.add(policy_b)
        await session.commit()
        policy_b_id = str(policy_b.id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError) as excinfo:
            await delete_calendar(session, org_a, actor_a, calendar_a)
        # The failed flush leaves the session needing a rollback before
        # anything else -- exactly what `org_session`'s `session.begin()`
        # context manager does automatically when the error instead
        # propagates out of that block in real request handling.
        await session.rollback()
    assert policy_b_id not in str(excinfo.value)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_a))
        ).scalar_one_or_none() is not None


async def test_tenancy_delete_calendar_ticket_sla_check_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, a_ticket_for
) -> None:
    """The `ticket_sla` pre-check added for F2 is deliberately `org_id`
    scoped too, mirroring the policy pre-check's own tenancy test directly
    above it: it must never NAME another org's ticket id to a caller in a
    different org. Constructed directly on the BYPASSRLS admin session (the
    FK permits a `ticket_sla` row to reference another org's calendar;
    nothing in ordinary application traffic would ever produce this row)."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, name="A's calendar", windows=[])
    calendar_b = await _seed_calendar(api_env, org_b, name="B's own calendar", windows=[])
    policy_b_id = await _seed_policy(api_env, org_b, calendar_id=calendar_b)
    ticket_b = await a_ticket_for(org_b, "cross-org-ticket-sla")

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            TicketSla(
                org_id=org_b,
                ticket_id=ticket_b.id,
                policy_id=policy_b_id,
                calendar_id=calendar_a,
                response_due_at=now,
                resolution_due_at=now,
                policy_source="initial",
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError) as excinfo:
            await delete_calendar(session, org_a, actor_a, calendar_a)
        await session.rollback()
    assert str(ticket_b.id) not in str(excinfo.value)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_a))
        ).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Task 3 review F3: the remaining `org_id` predicates, pinned on the admin
# engine following the two above. Each constructs a "rogue" cross-org child
# row directly via the BYPASSRLS admin session -- a shape no application path
# can produce (Task 4's discipline resolves every FK org-scoped) but that the
# FK itself does not forbid -- and proves the predicate is what keeps it from
# being read or mutated by the wrong org. `list_calendars` is Important (the
# ONLY app-layer scoping on an otherwise unqualified list query); the rest are
# Minor defence-in-depth, each additionally scoped by `calendar_id` or an
# already-org-verified id, but genuinely load-bearing against exactly this
# rogue-row shape.
#
# NOT pinned here: `add_holiday`'s post-insert select (`org_id == org_id AND
# id == inserted_id`). `inserted_id` is that row's own primary key, freshly
# generated by the INSERT in the SAME call with `org_id=org_id` -- no row
# with a different `org_id` can ever share it, so dropping the predicate
# there is unobservable by any test, not merely untested (see the comment at
# its call site in `calendars.py`).
# ---------------------------------------------------------------------------


async def test_tenancy_list_calendars_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Important (Task 3 review F3): `list_calendars`' `org_id` predicate is
    the ONLY application-layer scoping on an otherwise unqualified list
    query. Delete it and this function returns every org's calendars on any
    non-RLS session."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    await _seed_calendar(api_env, org_b, name="Org B's calendar")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await list_calendars(session, org_a) == []


async def test_tenancy_delete_calendar_window_delete_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`delete_calendar`'s window-delete predicate is `org_id AND
    calendar_id`, not `calendar_id` alone. A rogue org B window row pointing
    at org A's calendar (constructed directly; no application path can
    produce this) must survive an org A delete attempt -- which the FK
    itself then blocks, via the fallback, precisely because that row is
    untouched."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarWindow(
            org_id=org_b,
            calendar_id=calendar_a,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        session.add(rogue)
        await session.commit()
        rogue_id = rogue.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError):
            await delete_calendar(session, org_a, actor_a, calendar_a)
        await session.rollback()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(
                select(SlaCalendarWindow).where(SlaCalendarWindow.id == rogue_id)
            )
        ).scalar_one_or_none() is not None
        assert (
            await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_a))
        ).scalar_one_or_none() is not None


async def test_tenancy_delete_calendar_holiday_delete_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same proof as the window-delete predicate above, for the
    holiday-delete predicate."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarHoliday(
            org_id=org_b, calendar_id=calendar_a, holiday_date=date(2026, 12, 25), name="Rogue"
        )
        session.add(rogue)
        await session.commit()
        rogue_id = rogue.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError):
            await delete_calendar(session, org_a, actor_a, calendar_a)
        await session.rollback()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(
                select(SlaCalendarHoliday).where(SlaCalendarHoliday.id == rogue_id)
            )
        ).scalar_one_or_none() is not None
        assert (
            await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_a))
        ).scalar_one_or_none() is not None


async def test_tenancy_replace_windows_delete_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`replace_windows`' delete predicate is `org_id AND calendar_id`. A
    rogue org B window row on org A's calendar must survive an org A
    `replace_windows` call untouched.

    Unlike the exception-based tenancy tests above, this one exercises a
    silent success path -- there is nothing to `pytest.raises` on -- so the
    mutation must actually be committed before the follow-up session can see
    it: a bare `AsyncSession` (unlike `org_session`'s `session.begin()`)
    rolls back everything on `__aexit__` if never explicitly committed, which
    would make this assertion vacuously pass regardless of whether the
    predicate is even present."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarWindow(
            org_id=org_b,
            calendar_id=calendar_a,
            weekday=1,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        session.add(rogue)
        await session.commit()
        rogue_id = rogue.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_windows(
            session, org_a, actor_a, calendar_a, [(0, time(10, 0), time(14, 0))]
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(
                select(SlaCalendarWindow).where(SlaCalendarWindow.id == rogue_id)
            )
        ).scalar_one_or_none() is not None


async def test_tenancy_remove_holiday_select_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`remove_holiday`'s select is `org_id AND calendar_id AND id`. Without
    the `org_id` half, an org A caller who happens to know a rogue org B
    holiday's id (attached to org A's own calendar -- a shape no application
    path produces) could delete it. With the predicate, it is
    indistinguishable from a nonexistent id."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarHoliday(
            org_id=org_b, calendar_id=calendar_a, holiday_date=date(2026, 12, 25), name="Rogue"
        )
        session.add(rogue)
        await session.commit()
        rogue_id = rogue.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaNotFoundError):
            await remove_holiday(session, org_a, actor_a, calendar_a, rogue_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(
                select(SlaCalendarHoliday).where(SlaCalendarHoliday.id == rogue_id)
            )
        ).scalar_one_or_none() is not None


async def test_tenancy_load_calendar_spec_window_select_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`load_calendar_spec`'s window select is `org_id AND calendar_id`. A
    rogue org B window row on org A's calendar must not leak into an org A
    load."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarWindow(
            org_id=org_b,
            calendar_id=calendar_a,
            weekday=2,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        session.add(rogue)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        spec = await load_calendar_spec(session, org_a, calendar_a)
    assert spec.windows == ()


async def test_tenancy_load_calendar_spec_holiday_select_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same proof as the window select above, for the holiday select."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rogue = SlaCalendarHoliday(
            org_id=org_b, calendar_id=calendar_a, holiday_date=date(2026, 12, 25), name="Rogue"
        )
        session.add(rogue)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        spec = await load_calendar_spec(session, org_a, calendar_a)
    assert spec.holidays == frozenset()

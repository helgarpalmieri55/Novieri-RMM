"""The nine health tables against the schema `alembic upgrade head` actually
produced (docs/modules/endpoint-health-alerting/spec.md §2.1-§2.8, §2.11).

READ THIS BEFORE ADDING A TEST HERE.

1. THE RLS TESTS ARE BEHAVIOURAL. A test asserting `relrowsecurity` is true
   passes against a policy whose predicate was deleted, so the enable/force
   check is a smoke check only; the cross-org read and insert tests below are
   what actually hold. For an `ALL`-command policy Postgres reuses `USING` as
   `WITH CHECK` when the latter is omitted, so DELETING `WITH CHECK` falsifies
   nothing -- the mutations that falsify these are `WITH CHECK (true)` for the
   insert tests and `USING (true)` for the read tests.

2. THE INDEX TESTS ASSERT BOTH HALVES. `uq_health_alert_open_key` must forbid a
   second *open* alert AND permit a re-open after a clear; a non-partial index
   passes the first assertion alone while making a legitimate re-open
   impossible forever. The three assignment indexes must forbid two rows at the
   SAME level AND permit one row at each of the three levels; a single plain
   `UNIQUE (org_id, device_id, device_group_id)` passes the second alone,
   because `(org, dev, NULL)` does not collide with itself in Postgres.

3. THE 32-CHECK CAP IS A STATEMENT TRIGGER, and the multi-row insert is
   asserted separately from the one-at-a-time insert -- but NOT for the reason
   usually given. It is widely claimed that a row-level trigger "misses"
   multi-row inserts; on PostgreSQL 16 it does not. A PL/pgSQL trigger function
   increments the command counter before each of its own statements, so a
   `FOR EACH ROW` trigger (BEFORE or AFTER) counting siblings DOES see the rows
   the same statement already inserted. Both mutations were run and both stayed
   green. The multi-row test is kept because it pins behaviour that matters --
   33 rows in one statement are refused and NONE of them land -- not because it
   discriminates between the two trigger kinds. The statement trigger is still
   the right implementation: it runs one count per statement instead of one per
   row. What the cap tests DO falsify is the `retired_at IS NULL` qualifier
   (§2.2's amendment: retired rows are history, not policy, and counting them
   would make a profile unusable at 32 LIFETIME checks) and the presence of the
   UPDATE trigger.

4. EVERY CHECK-CONSTRAINT TEST REDUCES BOTH WORLDS TO A COMPARABLE STRING via
   `_refused_by`, rather than asserting `pytest.raises`. A `pytest.raises` test
   that fails prints only "DID NOT RAISE"; this one prints the row the database
   accepted, which is what a deleted CHECK actually produces.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from tests.health_fixtures import (
    HealthOrg,
    seed_alert,
    seed_check,
    seed_device,
    seed_org,
    seed_profile,
    utcnow,
)

TABLES = (
    "health_profile",
    "health_check",
    "health_profile_assignment",
    "health_alert",
    "health_alert_action",
    "device_health_check_state",
    "health_flap_state",
    "health_org_settings",
    "device_health_policy_ack",
)


class _Fx:
    """An org plus one of every health row, so the tenancy tests below have a
    legal row to try to plant in another tenant."""

    def __init__(self, org: HealthOrg, profile_id: UUID, check_id: UUID, alert_id: UUID) -> None:
        self.org = org
        self.org_id = org.org_id
        self.user_id = org.user_id
        self.device_id = org.device_id
        self.group_id = org.group_id
        self.profile_id = profile_id
        self.check_id = check_id
        self.alert_id = alert_id


async def _fixture(admin_engine: AsyncEngine, tag: str) -> _Fx:
    org = await seed_org(admin_engine, tag)
    profile_id = await seed_profile(admin_engine, org.org_id)
    check_id = await seed_check(admin_engine, org.org_id, profile_id)
    alert_id = await seed_alert(
        admin_engine, org, profile_id=profile_id, check_id=check_id
    )
    return _Fx(org, profile_id, check_id, alert_id)


def _row(table: str, fx: _Fx) -> Any:
    """One legal ORM row for `table`, owned by `fx`'s org."""
    from app.models import (
        DeviceHealthCheckState,
        DeviceHealthPolicyAck,
        HealthAlert,
        HealthAlertAction,
        HealthCheck,
        HealthFlapState,
        HealthOrgSettings,
        HealthProfile,
        HealthProfileAssignment,
    )

    now = utcnow()
    if table == "health_profile":
        return HealthProfile(org_id=fx.org_id, name=f"p-{uuid4().hex[:8]}", description="")
    if table == "health_check":
        return HealthCheck(
            org_id=fx.org_id,
            profile_id=fx.profile_id,
            check_type="memory.used_pct",
            selector=f"s-{uuid4().hex[:8]}",
            comparator="gt",
            threshold_num=90,
            severity="warning",
        )
    if table == "health_profile_assignment":
        return HealthProfileAssignment(org_id=fx.org_id, profile_id=fx.profile_id)
    if table == "health_alert":
        return HealthAlert(
            org_id=fx.org_id,
            device_id=fx.device_id,
            check_id=fx.check_id,
            profile_id=fx.profile_id,
            selector=f"s-{uuid4().hex[:8]}",
            check_type="cpu.busy_pct",
            severity="warning",
            state="active",
            observed_value="99.0",
            threshold_display="> 95",
            opened_at=now,
            opened_policy_version=1,
            last_seen_at=now,
        )
    if table == "health_alert_action":
        return HealthAlertAction(
            org_id=fx.org_id, alert_id=fx.alert_id, kind="notify", state="pending"
        )
    if table == "device_health_check_state":
        return DeviceHealthCheckState(
            org_id=fx.org_id,
            device_id=fx.device_id,
            check_id=fx.check_id,
            selector=f"s-{uuid4().hex[:8]}",
            state="ok",
            last_value="41.0",
            policy_version=1,
            reported_at=now,
        )
    if table == "health_flap_state":
        return HealthFlapState(
            org_id=fx.org_id,
            device_id=fx.device_id,
            check_id=fx.check_id,
            selector=f"s-{uuid4().hex[:8]}",
            window_start=now,
            open_count=1,
        )
    if table == "health_org_settings":
        return HealthOrgSettings(org_id=fx.org_id, updated_by=fx.user_id)
    if table == "device_health_policy_ack":
        return DeviceHealthPolicyAck(
            org_id=fx.org_id,
            device_id=fx.device_id,
            profile_id=fx.profile_id,
            policy_version=1,
            acknowledged_at=now,
        )
    raise AssertionError(f"no row factory for {table}")


# ---------------------------------------------------------------------------
# Tenancy (invariant #5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    """A smoke check. FORCE matters separately from ENABLE: without it the
    table owner bypasses the policy, and migrations run as the owner."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sqltext(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_read_returns_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `USING (true)`, which would return org A's row to org B."""
    a = await _fixture(admin_engine, f"ro-a-{table[:6]}")
    b = await _fixture(admin_engine, f"ro-b-{table[:6]}")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_row(table, a))
        await session.commit()

    query = sqltext(f"SELECT org_id FROM {table}")  # noqa: S608 -- table is from TABLES
    async with org_session(b.org_id, engine=app_engine) as session:
        seen_by_b = {r.org_id for r in (await session.execute(query)).all()}
    async with org_session(a.org_id, engine=app_engine) as session:
        seen_by_a = {r.org_id for r in (await session.execute(query)).all()}

    assert a.org_id not in seen_by_b
    assert seen_by_a == {a.org_id}


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_insert_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `WITH CHECK (true)`, which would let org B write a health
    policy -- and therefore a remediation trigger -- into org A's fleet.

    The outcome is reduced to a string rather than left as `pytest.raises`: a
    bare `raises` prints "DID NOT RAISE" when it fails, which says nothing about
    what the database did. This prints the row org B managed to plant in org A,
    read back afterwards on the admin engine.
    """
    a = await _fixture(admin_engine, f"wi-a-{table[:6]}")
    b = await _fixture(admin_engine, f"wi-b-{table[:6]}")
    planted = _row(table, a)
    try:
        async with org_session(b.org_id, engine=app_engine) as session:
            session.add(planted)
            await session.flush()
    except (DBAPIError, ProgrammingError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        async with admin_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext(f"SELECT org_id FROM {table} WHERE org_id = :o"),  # noqa: S608
                    {"o": a.org_id},
                )
            ).all()
        outcome = (
            f"ACCEPTED: org B wrote into {table}; org A ({a.org_id}) now holds "
            f"{len(rows)} row(s) it did not write"
        )
    assert outcome.startswith("refused: "), outcome
    assert "row-level security policy" in outcome, outcome


# ---------------------------------------------------------------------------
# Outcome-as-a-string, so a deleted index or CHECK prints the row it let through
# ---------------------------------------------------------------------------


def _refusal(message: str) -> str:
    """The constraint or index name Postgres blamed, out of its error text."""
    for token in message.replace('"', " ").replace("'", " ").split():
        if token.startswith(("ck_", "uq_")):
            return f"refused by {token}"
    if "at most 32" in message:
        return "refused by the 32-check cap trigger"
    return f"refused: {message.splitlines()[0]}"


async def _add_outcome(
    admin_engine: AsyncEngine, row: Any, *, readback: str, params: dict[str, Any]
) -> str:
    """Commit `row` and reduce the outcome to a STRING: the constraint that
    refused it, or `ACCEPTED:` plus what `readback` then finds in the table.

    Both worlds are then comparable. A `pytest.raises` version of the same test
    prints only "DID NOT RAISE" when the guard is deleted, which does not say
    whether the database stored one row or two.
    """
    try:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            session.add(row)
            await session.commit()
    except (IntegrityError, DBAPIError, ProgrammingError) as exc:
        return _refusal(str(getattr(exc, "orig", exc)))
    async with admin_engine.connect() as conn:
        stored = (await conn.execute(sqltext(readback), params)).all()
    return f"ACCEPTED: {[tuple(r) for r in stored]}"


# ---------------------------------------------------------------------------
# uq_health_alert_open_key -- "the single most important line in the schema"
# ---------------------------------------------------------------------------


_ALERT_STATES_FOR_KEY = (
    "SELECT state, observed_value FROM health_alert WHERE org_id = :o AND selector = ''"
)


async def test_a_second_open_alert_for_one_key_is_refused(admin_engine: AsyncEngine) -> None:
    """§0.8's structural guarantee against failure mode (b), an alert that
    re-opens every tick. Deleting the index makes this print the TWO open alerts
    the database then holds for one key."""
    fx = await _fixture(admin_engine, "openkey")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_row("health_alert", fx))
        await session.commit()

    duplicate = _row("health_alert", fx)
    duplicate.selector = ""  # same key as the alert seeded by `_fixture`
    outcome = await _add_outcome(
        admin_engine, duplicate, readback=_ALERT_STATES_FOR_KEY, params={"o": fx.org_id}
    )
    assert outcome == "refused by uq_health_alert_open_key"


async def test_a_cleared_alert_does_not_block_a_re_open(admin_engine: AsyncEngine) -> None:
    """THE OTHER HALF, and the one a non-partial index fails. Without
    `WHERE state <> 'cleared'` a key that has ever alarmed can never alarm
    again, which is a worse failure than the duplicate it prevents."""
    from app.models import HealthAlert

    fx = await _fixture(admin_engine, "reopen")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await session.get(HealthAlert, fx.alert_id)
        assert alert is not None
        alert.state = "cleared"
        alert.cleared_at = utcnow()
        alert.cleared_reason = "recovery"
        await session.commit()

    reopened = _row("health_alert", fx)
    reopened.selector = ""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(reopened)
        await session.commit()

    async with admin_engine.connect() as conn:
        states = sorted(
            r.state
            for r in (
                await conn.execute(
                    sqltext("SELECT state FROM health_alert WHERE org_id = :o"),
                    {"o": fx.org_id},
                )
            ).all()
        )
    assert states == ["active", "cleared"]


async def test_two_open_alerts_for_different_selectors_coexist(
    admin_engine: AsyncEngine,
) -> None:
    """The dedup key includes `selector`: `C:` full and `D:` full are two
    findings, not one."""
    fx = await _fixture(admin_engine, "selkey")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = _row("health_alert", fx)
        first.selector = "C:"
        second = _row("health_alert", fx)
        second.selector = "D:"
        session.add_all([first, second])
        await session.commit()


# ---------------------------------------------------------------------------
# The three assignment partial indexes (§0.2) and the mutual-exclusion CHECK
# ---------------------------------------------------------------------------


_ASSIGNMENT_READBACK = (
    "SELECT device_id, device_group_id FROM health_profile_assignment WHERE org_id = :o"
)


def _assignment(
    fx: _Fx, *, device_id: UUID | None = None, device_group_id: UUID | None = None
) -> Any:
    from app.models import HealthProfileAssignment

    return HealthProfileAssignment(
        org_id=fx.org_id,
        profile_id=fx.profile_id,
        device_id=device_id,
        device_group_id=device_group_id,
    )


async def _add_assignment(
    admin_engine: AsyncEngine,
    fx: _Fx,
    *,
    device_id: UUID | None = None,
    device_group_id: UUID | None = None,
) -> str:
    return await _add_outcome(
        admin_engine,
        _assignment(fx, device_id=device_id, device_group_id=device_group_id),
        readback=_ASSIGNMENT_READBACK,
        params={"o": fx.org_id},
    )


async def test_two_device_level_assignments_for_one_device_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    """Falsified by replacing the three partial indexes with a plain
    `UNIQUE (org_id, device_id, device_group_id)`: `(org, dev, NULL)` does not
    collide with itself, so both rows store and resolution picks whichever the
    planner returns first. The mutated run prints both stored rows."""
    fx = await _fixture(admin_engine, "asg-dev")
    assert (await _add_assignment(admin_engine, fx, device_id=fx.device_id)).startswith(
        "ACCEPTED:"
    )
    assert (
        await _add_assignment(admin_engine, fx, device_id=fx.device_id)
    ) == "refused by uq_health_assignment_device"


async def test_two_group_level_assignments_for_one_group_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    fx = await _fixture(admin_engine, "asg-grp")
    assert (
        await _add_assignment(admin_engine, fx, device_group_id=fx.group_id)
    ).startswith("ACCEPTED:")
    assert (
        await _add_assignment(admin_engine, fx, device_group_id=fx.group_id)
    ) == "refused by uq_health_assignment_group"


async def test_two_org_default_assignments_are_refused(admin_engine: AsyncEngine) -> None:
    fx = await _fixture(admin_engine, "asg-org")
    assert (await _add_assignment(admin_engine, fx)).startswith("ACCEPTED:")
    assert (await _add_assignment(admin_engine, fx)) == "refused by uq_health_assignment_org"


async def test_one_assignment_at_each_of_the_three_levels_coexists(
    admin_engine: AsyncEngine,
) -> None:
    """The other half. Three levels is the whole point of §3's resolution
    order; an index that forbade this would make precedence unexpressible."""
    fx = await _fixture(admin_engine, "asg-all")
    outcomes = [
        await _add_assignment(admin_engine, fx, device_id=fx.device_id),
        await _add_assignment(admin_engine, fx, device_group_id=fx.group_id),
        await _add_assignment(admin_engine, fx),
    ]
    assert [o.split(":")[0] for o in outcomes] == ["ACCEPTED", "ACCEPTED", "ACCEPTED"], outcomes

    async with admin_engine.connect() as conn:
        count = (
            await conn.execute(
                sqltext(
                    "SELECT count(*) FROM health_profile_assignment WHERE org_id = :o"
                ),
                {"o": fx.org_id},
            )
        ).scalar_one()
    assert count == 3


async def test_an_assignment_naming_both_a_device_and_a_group_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    fx = await _fixture(admin_engine, "asg-both")
    assert (
        await _add_assignment(
            admin_engine, fx, device_id=fx.device_id, device_group_id=fx.group_id
        )
    ) == "refused by ck_health_assignment_one_target"


# ---------------------------------------------------------------------------
# Per-alert and per-key uniqueness
# ---------------------------------------------------------------------------


async def test_one_action_of_each_kind_per_alert(admin_engine: AsyncEngine) -> None:
    """§0.10's idempotency guarantee: one ticket, one remediation dispatch, one
    notify, per alert, forever -- an index, not application logic."""
    from app.models import HealthAlertAction

    fx = await _fixture(admin_engine, "action")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add_all(
            [
                HealthAlertAction(
                    org_id=fx.org_id, alert_id=fx.alert_id, kind="remediate", state="pending"
                ),
                HealthAlertAction(
                    org_id=fx.org_id, alert_id=fx.alert_id, kind="open_ticket", state="pending"
                ),
                HealthAlertAction(
                    org_id=fx.org_id, alert_id=fx.alert_id, kind="notify", state="pending"
                ),
            ]
        )
        await session.commit()

    outcome = await _add_outcome(
        admin_engine,
        HealthAlertAction(
            org_id=fx.org_id, alert_id=fx.alert_id, kind="notify", state="pending"
        ),
        readback="SELECT kind, state FROM health_alert_action WHERE alert_id = :a",
        params={"a": fx.alert_id},
    )
    assert outcome == "refused by uq_health_alert_action_alert_kind"


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        ("device_health_check_state", "uq_device_health_check_state_key"),
        ("health_flap_state", "uq_health_flap_state_key"),
    ],
)
async def test_one_row_per_alert_key(
    admin_engine: AsyncEngine, table: str, constraint: str
) -> None:
    """§0.12 clauses 2 and 3: these two tables are UPSERT-only, one row per
    `(org_id, device_id, check_id, selector)`, constant in time."""
    fx = await _fixture(admin_engine, f"key-{table[:5]}")
    first = _row(table, fx)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(first)
        await session.commit()

    duplicate = _row(table, fx)
    duplicate.selector = first.selector
    outcome = await _add_outcome(
        admin_engine,
        duplicate,
        readback=f"SELECT selector, updated_at FROM {table} WHERE org_id = :o",  # noqa: S608
        params={"o": fx.org_id},
    )
    assert outcome == f"refused by {constraint}"


async def test_one_policy_ack_row_per_device(admin_engine: AsyncEngine) -> None:
    """§2.11: PK is `device_id`. A row per acknowledgement would be a
    `policy_version` time series keyed by device -- a fourth retaining table."""
    fx = await _fixture(admin_engine, "ack")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_row("device_health_policy_ack", fx))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        _row("device_health_policy_ack", fx),
        readback=(
            "SELECT device_id, policy_version FROM device_health_policy_ack WHERE org_id = :o"
        ),
        params={"o": fx.org_id},
    )
    assert outcome.startswith("refused"), outcome


async def test_the_same_check_twice_in_one_profile_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """§2.2's partial unique index. `os_family` is nullable and NULL does not
    collide with NULL in Postgres, so the index carries `NULLS NOT DISTINCT`;
    without it "cpu > 95 on any OS" could be stored twice and pushed twice."""
    fx = await _fixture(admin_engine, "dupcheck")
    assert await _insert_check(
        admin_engine, fx, check_type="cpu.busy_pct", selector="", threshold_num=95
    ) == "refused by uq_health_check_profile_type_selector_os"


async def test_a_retired_check_does_not_block_re_adding_it(
    admin_engine: AsyncEngine,
) -> None:
    """The other half of §2.2's amendment. A non-partial index would make
    re-adding a check an operator once removed permanently impossible, which is
    the failure the retire mechanism exists to avoid rather than cause."""
    from app.models import HealthCheck

    fx = await _fixture(admin_engine, "retired")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(HealthCheck, fx.check_id)
        assert row is not None
        row.retired_at = utcnow()
        await session.commit()

    new_id = await seed_check(
        admin_engine, fx.org_id, fx.profile_id, check_type="cpu.busy_pct", selector=""
    )
    assert new_id != fx.check_id


# ---------------------------------------------------------------------------
# The 32-check cap: a STATEMENT trigger with a transition table
# ---------------------------------------------------------------------------

_INSERT_CHECK_COLUMNS = (
    "id, org_id, profile_id, check_type, selector, comparator, threshold_num, "
    "threshold_bool, for_ticks, severity, os_family, enabled, retired_at, "
    "created_at, updated_at"
)


def _check_values(fx: _Fx, selector: str, *, retired: bool = False) -> str:
    retired_at = "now()" if retired else "NULL"
    return (
        f"(gen_random_uuid(), '{fx.org_id}', '{fx.profile_id}', 'cpu.busy_pct', "
        f"'{selector}', 'gt', 95, NULL, 3, 'warning', NULL, true, {retired_at}, "
        f"now(), now())"
    )


async def _live_check_count(admin_engine: AsyncEngine, profile_id: UUID) -> int:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                sqltext(
                    "SELECT count(*) FROM health_check WHERE profile_id = :p "
                    "AND retired_at IS NULL"
                ),
                {"p": profile_id},
            )
        ).scalar_one()


async def _insert_checks(
    admin_engine: AsyncEngine, fx: _Fx, selectors: list[str], *, retired: bool = False
) -> str:
    """One statement inserting every selector, so a multi-row insert is what the
    trigger actually sees. Returns the outcome as a string: the guard that
    refused it, or `ACCEPTED:` plus the profile's resulting LIVE check count --
    which is the number a deleted or row-level trigger prints."""
    values = ", ".join(_check_values(fx, s, retired=retired) for s in selectors)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sqltext(f"INSERT INTO health_check ({_INSERT_CHECK_COLUMNS}) VALUES {values}")  # noqa: S608
            )
    except (IntegrityError, DBAPIError, ProgrammingError) as exc:
        return _refusal(str(getattr(exc, "orig", exc)))
    return f"ACCEPTED: profile now holds {await _live_check_count(admin_engine, fx.profile_id)}"


async def test_thirty_two_checks_fit_in_one_profile(admin_engine: AsyncEngine) -> None:
    """The positive control for the cap. Without it a trigger that refused
    EVERY insert would pass the three negatives below."""
    fx = await _fixture(admin_engine, "cap32")
    # `_fixture` already seeded one check, so 31 more reaches exactly 32.
    assert await _insert_checks(admin_engine, fx, [f"m{i}" for i in range(31)]) == (
        "ACCEPTED: profile now holds 32"
    )


async def test_a_thirty_third_check_is_refused_one_row_at_a_time(
    admin_engine: AsyncEngine,
) -> None:
    fx = await _fixture(admin_engine, "cap33")
    await _insert_checks(admin_engine, fx, [f"m{i}" for i in range(31)])
    assert await _insert_checks(admin_engine, fx, ["one-too-many"]) == (
        "refused by the 32-check cap trigger"
    )


async def test_a_thirty_three_row_single_insert_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """33 rows in ONE statement are refused, and none of them land.

    This does NOT discriminate statement-level from row-level triggers -- see
    the module docstring, point 3, for the two mutations that proved it does
    not. It pins the property the cap is for: a caller cannot get 33 live
    checks onto a profile by batching, and a refused batch is all-or-nothing
    rather than partially applied.
    """
    org = await seed_org(admin_engine, "cap-multi")
    profile_id = await seed_profile(admin_engine, org.org_id)
    fx = _Fx(org, profile_id, uuid4(), uuid4())
    assert await _insert_checks(admin_engine, fx, [f"m{i}" for i in range(33)]) == (
        "refused by the 32-check cap trigger"
    )
    assert await _live_check_count(admin_engine, profile_id) == 0


async def test_retired_checks_do_not_consume_a_profiles_thirty_two_slots(
    admin_engine: AsyncEngine,
) -> None:
    """§2.2's amendment: the trigger counts `WHERE retired_at IS NULL`.
    Counting retired rows would let a profile become unusable at 32 LIFETIME
    checks rather than 32 live ones, which is the soft delete turning into a
    quota."""
    org = await seed_org(admin_engine, "cap-retired")
    profile_id = await seed_profile(admin_engine, org.org_id)
    fx = _Fx(org, profile_id, uuid4(), uuid4())
    assert (
        await _insert_checks(admin_engine, fx, [f"r{i}" for i in range(20)], retired=True)
    ) == "ACCEPTED: profile now holds 0"
    assert await _insert_checks(admin_engine, fx, [f"m{i}" for i in range(32)]) == (
        "ACCEPTED: profile now holds 32"
    )

    async with admin_engine.connect() as conn:
        count = (
            await conn.execute(
                sqltext("SELECT count(*) FROM health_check WHERE profile_id = :p"),
                {"p": profile_id},
            )
        ).scalar_one()
    assert count == 52


async def test_un_retiring_a_check_over_the_cap_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """The UPDATE half. Transition tables cannot be shared across events, so the
    cap is two triggers over one function; without the UPDATE one, clearing
    `retired_at` on 20 rows is an unpoliced route back over the cap."""
    org = await seed_org(admin_engine, "cap-unretire")
    profile_id = await seed_profile(admin_engine, org.org_id)
    fx = _Fx(org, profile_id, uuid4(), uuid4())
    await _insert_checks(admin_engine, fx, [f"r{i}" for i in range(20)], retired=True)
    await _insert_checks(admin_engine, fx, [f"m{i}" for i in range(32)])

    outcome = await _refused_by(
        admin_engine,
        "UPDATE health_check SET retired_at = NULL WHERE profile_id = :p "
        "AND retired_at IS NOT NULL",
        {"p": profile_id},
    )
    assert outcome == "refused by the 32-check cap trigger"


# ---------------------------------------------------------------------------
# CHECK ranges -- reduced to a comparable string so a deleted CHECK is visible
# ---------------------------------------------------------------------------


async def _refused_by(admin_engine: AsyncEngine, statement: str, params: dict[str, Any]) -> str:
    """Run `statement` as `its_admin` and reduce the outcome to a STRING: the
    name of the constraint that refused it, or `ACCEPTED: <the stored row>`.

    Both worlds are then comparable, so a deleted CHECK produces a visibly
    different value in the failure output rather than merely "DID NOT RAISE".
    """
    try:
        async with admin_engine.begin() as conn:
            rows = (await conn.execute(sqltext(statement + " RETURNING *"), params)).all()
    except (IntegrityError, DBAPIError, ProgrammingError) as exc:
        return _refusal(str(getattr(exc, "orig", exc)))
    return f"ACCEPTED: {[dict(r._mapping) for r in rows]}"


_INSERT_PROFILE = (
    "INSERT INTO health_profile (id, org_id, name, description, tick_interval_s, "
    "posture_interval_s, summary_interval_s, active, policy_version, created_by, "
    "created_at, updated_at) VALUES (gen_random_uuid(), :org, :name, '', :tick, "
    ":posture, :summary, true, 1, NULL, now(), now())"
)


async def _insert_profile(admin_engine: AsyncEngine, org_id: UUID, **kw: Any) -> str:
    params: dict[str, Any] = {
        "org": org_id,
        "name": f"p-{uuid4().hex[:8]}",
        "tick": 300,
        "posture": 3600,
        "summary": 1800,
    }
    params.update(kw)
    return await _refused_by(admin_engine, _INSERT_PROFILE, params)


@pytest.mark.parametrize(
    ("kw", "constraint"),
    [
        ({"tick": 59}, "ck_health_profile_tick_interval"),
        ({"tick": 3601, "posture": 3601}, "ck_health_profile_tick_interval"),
        ({"posture": 899, "tick": 60}, "ck_health_profile_posture_interval"),
        ({"tick": 3000, "posture": 900}, "ck_health_profile_posture_not_faster_than_tick"),
        ({"summary": 299}, "ck_health_profile_summary_interval"),
        ({"summary": 86401}, "ck_health_profile_summary_interval"),
    ],
)
async def test_profile_interval_ranges_are_database_constraints(
    admin_engine: AsyncEngine, kw: dict[str, Any], constraint: str
) -> None:
    """§0.11: "the caps are database constraints, because guidance does not
    hold". A 5-second tick with 400 checks is how an idle budget dies."""
    org = await seed_org(admin_engine, "prof-range")
    assert await _insert_profile(admin_engine, org.org_id, **kw) == f"refused by {constraint}"


async def test_the_default_profile_intervals_are_accepted(admin_engine: AsyncEngine) -> None:
    """The positive control. Without it a NOT NULL or FK failure would be
    indistinguishable from the range CHECK working."""
    org = await seed_org(admin_engine, "prof-ok")
    assert (await _insert_profile(admin_engine, org.org_id)).startswith("ACCEPTED:")


_INSERT_CHECK = (
    "INSERT INTO health_check (id, org_id, profile_id, check_type, selector, "
    "comparator, threshold_num, threshold_bool, for_ticks, severity, os_family, "
    "enabled, retired_at, created_at, updated_at) VALUES (gen_random_uuid(), :org, "
    ":profile, :check_type, :selector, :comparator, :threshold_num, :threshold_bool, "
    ":for_ticks, :severity, :os_family, true, NULL, now(), now())"
)


async def _insert_check(admin_engine: AsyncEngine, fx: _Fx, **kw: Any) -> str:
    params: dict[str, Any] = {
        "org": fx.org_id,
        "profile": fx.profile_id,
        "check_type": "memory.used_pct",
        "selector": f"s-{uuid4().hex[:8]}",
        "comparator": "gt",
        "threshold_num": 90,
        "threshold_bool": None,
        "for_ticks": 3,
        "severity": "warning",
        "os_family": None,
    }
    params.update(kw)
    return await _refused_by(admin_engine, _INSERT_CHECK, params)


@pytest.mark.parametrize(
    ("kw", "constraint"),
    [
        ({"for_ticks": 0}, "ck_health_check_for_ticks"),
        ({"for_ticks": 61}, "ck_health_check_for_ticks"),
        ({"severity": "urgent"}, "ck_health_check_severity"),
        ({"os_family": "bsd"}, "ck_health_check_os_family"),
        ({"comparator": "like"}, "ck_health_check_comparator"),
        ({"check_type": "cpu.temperature"}, "ck_health_check_type"),
        ({"threshold_num": None}, "ck_health_check_one_threshold"),
        ({"threshold_bool": True}, "ck_health_check_one_threshold"),
    ],
)
async def test_check_column_domains_are_database_constraints(
    admin_engine: AsyncEngine, kw: dict[str, Any], constraint: str
) -> None:
    fx = await _fixture(admin_engine, "chk-range")
    assert await _insert_check(admin_engine, fx, **kw) == f"refused by {constraint}"


async def test_a_boolean_threshold_alone_is_accepted(admin_engine: AsyncEngine) -> None:
    """The positive control for `ck_health_check_one_threshold`: exactly one of
    the two columns, either one."""
    fx = await _fixture(admin_engine, "chk-bool")
    result = await _insert_check(
        admin_engine,
        fx,
        check_type="posture.disk_encryption",
        comparator="is",
        threshold_num=None,
        threshold_bool=True,
    )
    assert result.startswith("ACCEPTED:")


_INSERT_ALERT = (
    "INSERT INTO health_alert (id, org_id, device_id, check_id, profile_id, selector, "
    "check_type, severity, state, observed_value, threshold_display, opened_at, "
    "opened_policy_version, last_seen_at, occurrence_count, flapping, flap_count, "
    "escalated_at, acknowledged_at, acknowledged_by, muted_until, muted_by, cleared_at, "
    "cleared_reason, cleared_by, ticket_id, previous_alert_id, created_at, updated_at) "
    "VALUES (gen_random_uuid(), :org, :device, :check, :profile, :selector, "
    "'cpu.busy_pct', 'warning', :state, '99.0', '> 95', :opened_at, 1, now(), 1, false, "
    "0, NULL, NULL, NULL, :muted_until, NULL, :cleared_at, :cleared_reason, NULL, NULL, "
    "NULL, now(), now())"
)


async def _insert_alert(admin_engine: AsyncEngine, fx: _Fx, **kw: Any) -> str:
    params: dict[str, Any] = {
        "org": fx.org_id,
        "device": fx.device_id,
        "check": fx.check_id,
        "profile": fx.profile_id,
        "selector": f"s-{uuid4().hex[:8]}",
        "state": "active",
        "opened_at": utcnow(),
        "muted_until": None,
        "cleared_at": None,
        "cleared_reason": None,
    }
    params.update(kw)
    return await _refused_by(admin_engine, _INSERT_ALERT, params)


async def test_a_cleared_alert_must_carry_cleared_at(admin_engine: AsyncEngine) -> None:
    """`(state='cleared') = (cleared_at IS NOT NULL)`. An alert in state
    `cleared` with no `cleared_at` is invisible to every retention and MTTR
    query that filters on the timestamp."""
    fx = await _fixture(admin_engine, "alert-cl1")
    assert await _insert_alert(
        admin_engine, fx, state="cleared", cleared_reason="recovery"
    ) == "refused by ck_health_alert_cleared_at_matches_state"


async def test_an_active_alert_must_not_carry_cleared_at(admin_engine: AsyncEngine) -> None:
    fx = await _fixture(admin_engine, "alert-cl2")
    assert await _insert_alert(
        admin_engine, fx, state="active", cleared_at=utcnow(), cleared_reason="recovery"
    ) == "refused by ck_health_alert_cleared_at_matches_state"


async def test_a_muted_alert_must_carry_muted_until(admin_engine: AsyncEngine) -> None:
    """§0.8: "unbounded mutes are how a fleet accumulates permanently silent
    alarms that everyone believes are still armed"."""
    fx = await _fixture(admin_engine, "alert-mu1")
    assert await _insert_alert(admin_engine, fx, state="muted") == (
        "refused by ck_health_alert_muted_until_matches_state"
    )


async def test_a_mute_longer_than_thirty_days_is_refused(admin_engine: AsyncEngine) -> None:
    fx = await _fixture(admin_engine, "alert-mu2")
    opened_at = utcnow()
    from datetime import timedelta

    assert await _insert_alert(
        admin_engine,
        fx,
        state="muted",
        opened_at=opened_at,
        muted_until=opened_at + timedelta(days=31),
    ) == "refused by ck_health_alert_mute_bounded"


async def test_a_thirty_day_mute_is_accepted(admin_engine: AsyncEngine) -> None:
    """The positive control for the bound."""
    from datetime import timedelta

    fx = await _fixture(admin_engine, "alert-mu3")
    opened_at = utcnow()
    result = await _insert_alert(
        admin_engine,
        fx,
        state="muted",
        opened_at=opened_at,
        muted_until=opened_at + timedelta(days=29),
    )
    assert result.startswith("ACCEPTED:")


async def test_an_unlisted_clear_reason_is_refused(admin_engine: AsyncEngine) -> None:
    """Six enumerated reasons and no seventh (§0.8). "Which clause fired
    matters: an alert cleared by `unavailable_timeout` is a REMOVED SENSOR and
    must not be counted as a resolution in any MTTR figure"."""
    fx = await _fixture(admin_engine, "alert-cr")
    assert await _insert_alert(
        admin_engine, fx, state="cleared", cleared_at=utcnow(), cleared_reason="resolved"
    ) == "refused by ck_health_alert_cleared_reason"


@pytest.mark.parametrize(
    "reason",
    [
        "recovery",
        "summary_ok",
        "unavailable_timeout",
        "policy_removed",
        "device_inactive",
        "manual",
    ],
)
async def test_each_of_the_six_clear_reasons_is_accepted(
    admin_engine: AsyncEngine, reason: str
) -> None:
    fx = await _fixture(admin_engine, f"alert-ok-{reason[:6]}")
    result = await _insert_alert(
        admin_engine, fx, state="cleared", cleared_at=utcnow(), cleared_reason=reason
    )
    assert result.startswith("ACCEPTED:")


async def test_an_unlisted_alert_state_is_refused(admin_engine: AsyncEngine) -> None:
    """There is no `suppressed` state -- §0.9 suppresses ACTIONS, never
    evidence, and a state for it would be the design that hides the flap."""
    fx = await _fixture(admin_engine, "alert-st")
    assert await _insert_alert(admin_engine, fx, state="suppressed") == (
        "refused by ck_health_alert_state"
    )


async def test_observed_value_is_a_string_column(admin_engine: AsyncEngine) -> None:
    """§2.4, and §0.12's boundary made structural. `observed_value` must hold
    "97.4" and "stopped" and "false" WITHOUT this table acquiring a numeric
    column that someone will later be tempted to aggregate over time. The
    failure this prevents is not a bad row; it is a schema that makes "what was
    this device's CPU last Tuesday?" answerable."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'health_alert' AND data_type IN "
                    "('numeric', 'double precision', 'real', 'integer', 'bigint')"
                )
            )
        ).all()
    numeric_columns = sorted(r.column_name for r in rows)
    assert numeric_columns == ["flap_count", "occurrence_count", "opened_policy_version"]

    async with admin_engine.connect() as conn:
        data_type = (
            await conn.execute(
                sqltext(
                    "SELECT data_type FROM information_schema.columns WHERE "
                    "table_name = 'health_alert' AND column_name = 'observed_value'"
                )
            )
        ).scalar_one()
    assert data_type == "character varying"


# ---------------------------------------------------------------------------
# health_org_settings ranges (§0.9, §2.8)
# ---------------------------------------------------------------------------

_INSERT_SETTINGS = (
    "INSERT INTO health_org_settings (org_id, flap_window_s, flap_threshold, "
    "flap_cooldown_s, unavailable_clear_after_s, alert_retention_days, "
    "escalate_after_s, offline_after_s, approval_ttl_s, default_alert_queue_id, "
    "default_ticket_category_id, monitoring_deep_link_template, "
    "auto_resolve_alert_tickets, updated_by, updated_at) VALUES (:org, :flap_window_s, "
    ":flap_threshold, :flap_cooldown_s, :unavailable_clear_after_s, "
    ":alert_retention_days, :escalate_after_s, :offline_after_s, :approval_ttl_s, "
    "NULL, NULL, NULL, true, NULL, now())"
)

_SETTINGS_DEFAULTS: dict[str, Any] = {
    "flap_window_s": 3600,
    "flap_threshold": 4,
    "flap_cooldown_s": 3600,
    "unavailable_clear_after_s": 86400,
    "alert_retention_days": 400,
    "escalate_after_s": 3600,
    "offline_after_s": 900,
    "approval_ttl_s": 86400,
}


async def _insert_settings(admin_engine: AsyncEngine, org_id: UUID, **kw: Any) -> str:
    params: dict[str, Any] = {"org": org_id, **_SETTINGS_DEFAULTS}
    params.update(kw)
    return await _refused_by(admin_engine, _INSERT_SETTINGS, params)


@pytest.mark.parametrize(
    ("kw", "constraint"),
    [
        ({"flap_window_s": 299}, "ck_health_org_settings_flap_window"),
        ({"flap_window_s": 86401}, "ck_health_org_settings_flap_window"),
        ({"flap_threshold": 1}, "ck_health_org_settings_flap_threshold"),
        ({"flap_threshold": 51}, "ck_health_org_settings_flap_threshold"),
        ({"flap_cooldown_s": 299}, "ck_health_org_settings_flap_cooldown"),
        ({"flap_cooldown_s": 86401}, "ck_health_org_settings_flap_cooldown"),
        ({"alert_retention_days": 89}, "ck_health_org_settings_alert_retention"),
        ({"escalate_after_s": 299}, "ck_health_org_settings_escalate_after"),
        ({"escalate_after_s": 604801}, "ck_health_org_settings_escalate_after"),
        ({"offline_after_s": 299}, "ck_health_org_settings_offline_after"),
        ({"offline_after_s": 86401}, "ck_health_org_settings_offline_after"),
        ({"approval_ttl_s": 299}, "ck_health_org_settings_approval_ttl"),
        ({"unavailable_clear_after_s": 3599}, "ck_health_org_settings_unavailable_clear"),
    ],
)
async def test_org_setting_ranges_are_database_constraints(
    admin_engine: AsyncEngine, kw: dict[str, Any], constraint: str
) -> None:
    org = await seed_org(admin_engine, "set-range")
    assert await _insert_settings(admin_engine, org.org_id, **kw) == f"refused by {constraint}"


async def test_the_documented_setting_defaults_are_accepted(admin_engine: AsyncEngine) -> None:
    """The positive control for all thirteen rows above."""
    org = await seed_org(admin_engine, "set-ok")
    assert (await _insert_settings(admin_engine, org.org_id)).startswith("ACCEPTED:")


# ---------------------------------------------------------------------------
# device_health_check_state / health_alert_action domains
# ---------------------------------------------------------------------------


async def test_a_fourth_check_state_is_refused(admin_engine: AsyncEngine) -> None:
    """§0.7: exactly three states, and `unavailable` is not a fourth flavour of
    `ok`."""
    fx = await _fixture(admin_engine, "dhcs")
    result = await _refused_by(
        admin_engine,
        "INSERT INTO device_health_check_state (id, org_id, device_id, check_id, "
        "selector, state, last_value, policy_version, reported_at, updated_at) VALUES "
        "(gen_random_uuid(), :org, :device, :check, 'x', 'degraded', '1', 1, now(), now())",
        {"org": fx.org_id, "device": fx.device_id, "check": fx.check_id},
    )
    assert result == "refused by ck_device_health_check_state_state"


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "escalate", "ck_health_alert_action_kind"),
        ("state", "queued", "ck_health_alert_action_state"),
    ],
)
async def test_alert_action_domains_are_database_constraints(
    admin_engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    fx = await _fixture(admin_engine, f"aa-{column}")
    params: dict[str, Any] = {
        "org": fx.org_id,
        "alert": fx.alert_id,
        "kind": "notify",
        "state": "pending",
        column: value,
    }
    result = await _refused_by(
        admin_engine,
        "INSERT INTO health_alert_action (id, org_id, alert_id, kind, state, "
        "approved_action_id, job_id, ticket_id, attempts, next_attempt_at, approved_by, "
        "approved_at, expires_at, last_error, created_at, updated_at) VALUES "
        "(gen_random_uuid(), :org, :alert, :kind, :state, NULL, NULL, NULL, 0, NULL, "
        "NULL, NULL, NULL, NULL, now(), now())",
        params,
    )
    assert result == f"refused by {constraint}"


# ---------------------------------------------------------------------------
# The FK resolution (§2.4's amendment): RESTRICT, and retire instead of delete
# ---------------------------------------------------------------------------


async def test_deleting_a_check_that_has_alarmed_is_refused_by_the_database(
    admin_engine: AsyncEngine,
) -> None:
    """§2.4: `health_alert.check_id` is `ON DELETE RESTRICT`, so "deleting a
    check leaves existing alerts intact" is enforced by the database rather
    than by application care. The API's remove path RETIRES instead (Task 5);
    this is the backstop under it, and the reason `check_id` is not nullable --
    it is part of `uq_health_alert_open_key`, and NULLs do not collide."""
    fx = await _fixture(admin_engine, "fk-restrict")
    result = await _refused_by(
        admin_engine,
        "DELETE FROM health_check WHERE id = :c",
        {"c": fx.check_id},
    )
    assert "violates foreign key constraint" in result


async def test_deleting_a_check_that_never_alarmed_is_permitted(
    admin_engine: AsyncEngine,
) -> None:
    """The other half. RESTRICT only fires where an alert actually references
    the row, so the ordinary "added by mistake, removed the same minute" path
    is a plain DELETE and not a permanent retired row."""
    fx = await _fixture(admin_engine, "fk-free")
    spare = await seed_check(admin_engine, fx.org_id, fx.profile_id, check_type="memory.used_pct")
    async with admin_engine.begin() as conn:
        await conn.execute(sqltext("DELETE FROM health_check WHERE id = :c"), {"c": spare})


# ---------------------------------------------------------------------------
# Fleet-shape sanity
# ---------------------------------------------------------------------------


async def test_two_devices_may_hold_the_same_check_state_key(
    admin_engine: AsyncEngine,
) -> None:
    """The uniqueness is per DEVICE, not per check: one check across a fleet is
    one row per device, which is what makes §0.12 clause 2's bound
    devices x 32."""
    fx = await _fixture(admin_engine, "two-dev")
    other = await seed_device(admin_engine, fx.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = _row("device_health_check_state", fx)
        second = _row("device_health_check_state", fx)
        second.selector = first.selector
        second.device_id = other
        session.add_all([first, second])
        await session.commit()

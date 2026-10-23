"""Flap suppression (docs/modules/endpoint-health-alerting/spec.md §0.9, §2.7).

READ THIS BEFORE ADDING A TEST HERE.

1. SUPPRESSION REMOVES ACTIONS, NEVER EVIDENCE. `note_open` decides only
   whether this key's ACTIONS fire. The alert row is still written with
   `flapping = true` (Task 9's job), because a design that suppresses the
   RECORD turns "this device flapped 40 times overnight" into "it was fine",
   which is the story an operator will believe because nothing contradicts it.
   Every test here therefore asserts `flap_count` as well as `suppressed`.

2. THE BOUNDARY IS ASSERTED FROM BOTH SIDES. §0.9 is `open_count >
   flap_threshold`. A test that only checks "the 5th open suppresses" passes
   against `>` and `>=` indifferently; the one that discriminates is the
   assertion that the open AT the threshold does NOT suppress.

3. THE COOLDOWN HAS AN EXIT. Without clearing the window when the cooldown
   expires, `open_count` is still over the threshold on the next open and the
   key re-suppresses immediately -- a cooldown that never ends is a silenced
   alert nobody chose. Tested explicitly.

4. THE `org_id` PREDICATE IS PINNED ON THE ADMIN ENGINE, twice: once on the
   settings read (two orgs with different thresholds) and once on the flap-row
   read (org A's id against org B's key). Through `org_session` both pass with
   the predicate deleted, because RLS rejects first.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import HealthFlapState
from app.modules.health.flap import AlertKey, FlapDecision, note_open
from tests.health_fixtures import seed_check, seed_org, seed_profile, seed_settings

T0 = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


class _Key:
    def __init__(self, org, key: AlertKey) -> None:
        self.org = org
        self.key = key


async def _fixture(env, tag: str, **settings: int) -> _Key:
    org = await seed_org(env, tag)
    profile_id = await seed_profile(env, org.org_id)
    check_id = await seed_check(env, org.org_id, profile_id)
    if settings:
        await seed_settings(env, org.org_id, **settings)
    return _Key(org, AlertKey(device_id=org.device_id, check_id=check_id, selector=""))


async def _open(api_env, fx: _Key, *, now: datetime) -> FlapDecision:
    async with org_session(
        fx.org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await note_open(session, fx.org.org_id, fx.key, now=now)


async def _rows(admin_engine: AsyncEngine, org_id: UUID) -> list[HealthFlapState]:
    from sqlalchemy import select

    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(HealthFlapState).where(HealthFlapState.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# The boundary, from both sides
# ---------------------------------------------------------------------------


async def test_the_open_at_the_threshold_does_not_suppress(api_env) -> None:
    """§0.9 is `open_count > flap_threshold`, strictly. This is the half that
    discriminates `>` from `>=`: with the default threshold of 4, the FOURTH
    open must still fire its actions. Weakening the comparison makes this print
    `suppressed=True` where it expects `False`."""
    fx = await _fixture(api_env, "flap-at")
    decisions = [await _open(api_env, fx, now=T0 + timedelta(seconds=i)) for i in range(4)]
    assert [d.suppressed for d in decisions] == [False, False, False, False]
    assert [d.flap_count for d in decisions] == [1, 2, 3, 4]
    assert decisions[-1].suppressed_until is None


async def test_the_open_after_the_threshold_suppresses(api_env) -> None:
    """The other half. Without it, an implementation that never suppresses
    passes the test above."""
    fx = await _fixture(api_env, "flap-over")
    for i in range(4):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))
    fifth = await _open(api_env, fx, now=T0 + timedelta(seconds=4))
    assert fifth.suppressed is True
    assert fifth.flap_count == 5
    assert fifth.suppressed_until == T0 + timedelta(seconds=4) + timedelta(seconds=3600)


async def test_the_threshold_comes_from_the_orgs_settings(api_env) -> None:
    """Parametrised by DATA, not by the default: an implementation with the
    number 4 hardcoded passes both boundary tests above and fails here."""
    fx = await _fixture(api_env, "flap-cfg", flap_threshold=2, flap_cooldown_s=600)
    decisions = [await _open(api_env, fx, now=T0 + timedelta(seconds=i)) for i in range(3)]
    assert [d.suppressed for d in decisions] == [False, False, True]
    assert decisions[-1].suppressed_until == T0 + timedelta(seconds=2) + timedelta(seconds=600)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


async def test_a_window_older_than_flap_window_s_resets_the_count(api_env) -> None:
    """§0.9's first line. Without the reset, a key that opened four times over a
    year is one open away from being suppressed forever -- which is the opposite
    of what a WINDOW means. Deleting it makes this print `flap_count=5`."""
    fx = await _fixture(api_env, "flap-win")
    for i in range(4):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))

    later = await _open(api_env, fx, now=T0 + timedelta(seconds=3601))
    assert later.suppressed is False
    assert later.flap_count == 1

    rows = await _rows(api_env.admin_engine, fx.org.org_id)
    assert len(rows) == 1
    assert rows[0].window_start == T0 + timedelta(seconds=3601)


async def test_an_open_exactly_at_the_window_edge_does_not_reset(api_env) -> None:
    """`now - window_start > flap_window_s`, strictly: an open at exactly the
    window length is still inside it. The pair with the test above is what pins
    the comparison rather than the direction."""
    fx = await _fixture(api_env, "flap-edge")
    for i in range(4):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))
    edge = await _open(api_env, fx, now=T0 + timedelta(seconds=3600))
    assert edge.flap_count == 5
    assert edge.suppressed is True


# ---------------------------------------------------------------------------
# The cooldown
# ---------------------------------------------------------------------------


async def test_further_opens_inside_the_cooldown_stay_suppressed(api_env) -> None:
    """"Refuse further opens for this key until `suppressed_until`" (§0.9). The
    count still moves, because `flap_count` is the finding: the console sorts by
    it, and a flapping device is often more important than any single breach."""
    fx = await _fixture(api_env, "flap-cool")
    for i in range(5):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))

    during = [
        await _open(api_env, fx, now=T0 + timedelta(seconds=100 + i * 100)) for i in range(3)
    ]
    assert [d.suppressed for d in during] == [True, True, True]
    assert [d.flap_count for d in during] == [6, 7, 8]
    assert {d.suppressed_until for d in during} == {
        T0 + timedelta(seconds=4) + timedelta(seconds=3600)
    }


async def test_the_key_is_armed_again_once_the_cooldown_expires(api_env) -> None:
    """THE COOLDOWN MUST HAVE AN EXIT. When it lapses, the window is cleared as
    well as the flag: leaving `open_count` above the threshold means the very
    next open re-suppresses immediately, and the "cooldown" is a permanent
    silence nobody chose. Without this test an implementation that only clears
    the flag passes every other test in this file."""
    fx = await _fixture(api_env, "flap-rearm")
    for i in range(5):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))

    after = await _open(api_env, fx, now=T0 + timedelta(seconds=4 + 3601))
    assert after.suppressed is False
    assert after.flap_count == 1
    assert after.suppressed_until is None


# ---------------------------------------------------------------------------
# One row per key (§0.12 clause 3)
# ---------------------------------------------------------------------------


async def test_fifty_opens_produce_exactly_one_row(api_env) -> None:
    """§0.12: `health_flap_state` is one row per alert key, upserted, constant
    in time. Asserted by COUNT, because "no exception was raised" is compatible
    with fifty rows."""
    fx = await _fixture(api_env, "flap-one")
    for i in range(50):
        await _open(api_env, fx, now=T0 + timedelta(seconds=i))

    rows = await _rows(api_env.admin_engine, fx.org.org_id)
    assert len(rows) == 1
    assert rows[0].open_count == 50
    assert rows[0].suppressed_until is not None


async def test_two_selectors_on_one_check_are_two_windows(api_env) -> None:
    """The key includes the selector: `C:` flapping does not silence `D:`."""
    fx = await _fixture(api_env, "flap-sel")
    key_c = AlertKey(device_id=fx.key.device_id, check_id=fx.key.check_id, selector="C:")
    key_d = AlertKey(device_id=fx.key.device_id, check_id=fx.key.check_id, selector="D:")
    async with org_session(
        fx.org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        for i in range(5):
            await note_open(session, fx.org.org_id, key_c, now=T0 + timedelta(seconds=i))
        on_d = await note_open(session, fx.org.org_id, key_d, now=T0 + timedelta(seconds=5))
    assert on_d.suppressed is False
    assert on_d.flap_count == 1
    assert len(await _rows(api_env.admin_engine, fx.org.org_id)) == 2


# ---------------------------------------------------------------------------
# The tunables are DATABASE-enforced (§0.9), asserted on the admin engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("flap_window_s", 299, "ck_health_org_settings_flap_window"),
        ("flap_window_s", 86401, "ck_health_org_settings_flap_window"),
        ("flap_threshold", 1, "ck_health_org_settings_flap_threshold"),
        ("flap_threshold", 51, "ck_health_org_settings_flap_threshold"),
        ("flap_cooldown_s", 299, "ck_health_org_settings_flap_cooldown"),
        ("flap_cooldown_s", 86401, "ck_health_org_settings_flap_cooldown"),
    ],
)
async def test_the_flap_tunables_are_bounded_by_postgres(
    admin_engine: AsyncEngine, field: str, value: int, constraint: str
) -> None:
    """Run as `its_admin` with raw SQL: no Pydantic validator, no service,
    nothing but Postgres left to refuse the row. A `flap_threshold` of 1 would
    suppress every second breach on the fleet, and a `flap_window_s` of 0 would
    make the window meaningless -- both are exactly the kind of value a service
    layer alone cannot keep out of a direct SQL fixup."""
    org = await seed_org(admin_engine, "flap-bound")
    values = {
        "flap_window_s": 3600,
        "flap_threshold": 4,
        "flap_cooldown_s": 3600,
        field: value,
    }
    try:
        async with admin_engine.begin() as conn:
            row = (
                await conn.execute(
                    sqltext(
                        "INSERT INTO health_org_settings (org_id, flap_window_s, "
                        "flap_threshold, flap_cooldown_s, updated_at) VALUES (:o, :w, :t, "
                        ":c, now()) RETURNING flap_window_s, flap_threshold, flap_cooldown_s"
                    ),
                    {
                        "o": org.org_id,
                        "w": values["flap_window_s"],
                        "t": values["flap_threshold"],
                        "c": values["flap_cooldown_s"],
                    },
                )
            ).one()
    except Exception as exc:  # noqa: BLE001 -- reduced to a string below
        message = str(getattr(exc, "orig", exc))
        outcome = next(
            (f"refused by {t}" for t in message.replace('"', " ").split() if t.startswith("ck_")),
            f"refused: {message.splitlines()[0]}",
        )
    else:
        outcome = f"ACCEPTED: {tuple(row)}"
    assert outcome == f"refused by {constraint}"


async def test_the_documented_flap_defaults_are_accepted(admin_engine: AsyncEngine) -> None:
    """The positive control for the six rejections above."""
    org = await seed_org(admin_engine, "flap-bound-ok")
    async with admin_engine.begin() as conn:
        await conn.execute(
            sqltext(
                "INSERT INTO health_org_settings (org_id, flap_window_s, flap_threshold, "
                "flap_cooldown_s, updated_at) VALUES (:o, 3600, 4, 3600, now())"
            ),
            {"o": org.org_id},
        )


# ---------------------------------------------------------------------------
# Tenancy -- load-bearing, and only visible on the admin engine
# ---------------------------------------------------------------------------


async def test_two_orgs_do_not_share_a_flap_window(api_env) -> None:
    """Behavioural: org B flapping hard must not suppress org A's first breach.
    This one passes through RLS either way -- the two admin-engine tests below
    are what pin the application predicates."""
    a = await _fixture(api_env, "flap-xa")
    b = await _fixture(api_env, "flap-xb")
    for i in range(10):
        await _open(api_env, b, now=T0 + timedelta(seconds=i))
    first_for_a = await _open(api_env, a, now=T0 + timedelta(seconds=10))
    assert first_for_a.suppressed is False
    assert first_for_a.flap_count == 1


async def test_the_settings_read_is_scoped_to_this_org(admin_engine: AsyncEngine) -> None:
    """THE FIRST ONE RLS CANNOT MASK. Two orgs with different thresholds: on the
    admin engine the `org_id` predicate inside the settings read is the only
    thing deciding which org's threshold governs org A's fleet. Delete it and
    the read matches both rows and raises `MultipleResultsFound` -- which is
    still better than the silent alternative it would have if only one org had a
    row: org A silently governed by org B's tuning."""
    a = await _fixture(admin_engine, "flap-sa", flap_threshold=2, flap_cooldown_s=600)
    b = await _fixture(admin_engine, "flap-sb", flap_threshold=50)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        decisions = [
            await note_open(session, a.org.org_id, a.key, now=T0 + timedelta(seconds=i))
            for i in range(3)
        ]
        b_decision = await note_open(session, b.org.org_id, b.key, now=T0)
        await session.commit()

    assert [d.suppressed for d in decisions] == [False, False, True]
    assert b_decision.suppressed is False


async def test_a_mismatched_org_never_touches_another_orgs_flap_row(
    admin_engine: AsyncEngine,
) -> None:
    """THE SECOND ONE RLS CANNOT MASK. Org A's `org_id` against org B's key is
    what a dropped predicate looks like from here: the row read is keyed on
    `(org_id, device_id, check_id, selector)`, and without the `org_id` half the
    lookup finds org B's row -- and after the upsert has added org A's, finds
    BOTH and raises `MultipleResultsFound`. Either way org B's window must be
    untouched, because a shared window means one tenant can silence another's
    alerts by flapping."""
    a = await _fixture(admin_engine, "flap-ma")
    b = await _fixture(admin_engine, "flap-mb")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for i in range(5):
            await note_open(session, b.org.org_id, b.key, now=T0 + timedelta(seconds=i))
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stolen = await note_open(session, a.org.org_id, b.key, now=T0 + timedelta(seconds=10))
        await session.commit()

    assert stolen.flap_count == 1, "org A must start its own window, not inherit org B's"
    b_rows = await _rows(admin_engine, b.org.org_id)
    assert len(b_rows) == 1
    assert b_rows[0].open_count == 5, "org B's window must be untouched"

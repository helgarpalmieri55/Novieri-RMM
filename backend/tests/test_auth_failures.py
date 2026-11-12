"""Failed attempts per person and per factor: `app/core/auth/failures.py`.

Design §2.3 and falsifier §10.3: counted per account and per factor, never per
address alone [S5]; from the 5th consecutive failure a wait of 1 s that
doubles up to 15 min; at 100 the factor is disabled until an administrator
restores it [S1 §3.2.2]; a success clears that factor's failures.
"""

import asyncio
import inspect
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth import failures
from app.core.auth.failures import Factor, FactorState
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.models.auth import AuthFailure, AuthTrustedDevice
from tests.race_helpers import backend_pid, until_blocked_by

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


@dataclass
class _World:
    org_id: UUID
    user_id: UUID
    app_engine: AsyncEngine
    admin_engine: AsyncEngine


async def _person(admin_engine: AsyncEngine, org_id: UUID | None = None) -> tuple[UUID, UUID]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        if org_id is None:
            slug = f"fail-{uuid4().hex[:8]}"
            org = Organization(name=slug, slug=slug)
            session.add(org)
            await session.flush()
            org_id = org.id
        user = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"{uuid4().hex[:8]}@toeshee.com",
        )
        session.add(user)
        await session.commit()
        return org_id, user.id


@pytest.fixture()
async def world(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> _World:
    org_id, user_id = await _person(admin_engine)
    return _World(org_id, user_id, app_engine, admin_engine)


async def _fail(
    w: _World,
    factor: Factor = "totp",
    *,
    times: int = 1,
    at: datetime = NOW,
    user_id: UUID | None = None,
) -> FactorState:
    state: FactorState | None = None
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        for _ in range(times):
            state = await failures.record_failure(
                session, org_id=w.org_id, user_id=user_id or w.user_id, factor=factor, now=at
            )
    assert state is not None
    return state


async def _check(
    w: _World, factor: Factor = "totp", *, at: datetime = NOW, user_id: UUID | None = None
) -> FactorState:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await failures.check_allowed(session, user_id or w.user_id, factor, now=at)


async def _succeed(w: _World, factor: Factor = "totp") -> None:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        await failures.record_success(session, w.user_id, factor, clear_mail_mark=True)


async def _consecutive(w: _World, factor: Factor = "totp") -> int | None:
    async with AsyncSession(w.admin_engine) as session:
        value: int | None = await session.scalar(
            select(AuthFailure.consecutive).where(
                AuthFailure.user_id == w.user_id, AuthFailure.factor == factor
            )
        )
        return value


async def _audit(w: _World) -> list[AuditEvent]:
    async with AsyncSession(w.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == w.org_id).order_by(AuditEvent.id)
                )
            ).scalars()
        )


ALLOWED = FactorState(allowed=True, wait_s=0, disabled=False)


@pytest.mark.parametrize(
    ("consecutive", "wait"),
    [(0, 0), (4, 0), (5, 1), (6, 2), (7, 4), (14, 512), (15, 900), (99, 900)],
)
def test_the_wait_starts_at_the_fifth_failure_and_doubles_to_fifteen_minutes(
    consecutive: int, wait: int
) -> None:
    assert failures.backoff_s(consecutive) == wait


async def test_a_factor_with_no_failures_is_allowed(world: _World) -> None:
    assert await _check(world) == ALLOWED


async def test_four_failures_do_not_wait(world: _World) -> None:
    await _fail(world, times=4)

    assert await _check(world) == ALLOWED


async def test_the_fifth_failure_waits_one_second(world: _World) -> None:
    """`backoff_s` and `_state`'s comparison with `last_failed_at`: remove
    either and the attempt at NOW is allowed."""
    state = await _fail(world, times=5)

    assert state == FactorState(allowed=False, wait_s=1, disabled=False)
    assert await _check(world) == FactorState(allowed=False, wait_s=1, disabled=False)
    assert await _check(world, at=NOW + timedelta(seconds=1)) == ALLOWED


async def test_the_wait_doubles(world: _World) -> None:
    await _fail(world, times=7)

    assert await _check(world, at=NOW + timedelta(seconds=1)) == FactorState(
        allowed=False, wait_s=3, disabled=False
    )
    assert await _check(world, at=NOW + timedelta(seconds=4)) == ALLOWED


async def test_the_wait_stops_at_fifteen_minutes(world: _World) -> None:
    await _fail(world, times=30)

    assert await _check(world) == FactorState(allowed=False, wait_s=900, disabled=False)
    assert await _check(world, at=NOW + timedelta(minutes=15)) == ALLOWED


async def test_the_hundredth_failure_disables_the_factor_and_audits_it_once(world: _World) -> None:
    """Falsifier §10.3, first half. `DISABLE_AT` and the `case(...)` in the
    upsert: remove it and the factor is never disabled; and the audit fires on
    the transition only, not on the 101st."""
    assert not (await _fail(world, times=99)).disabled

    assert await _fail(world) == FactorState(allowed=False, wait_s=0, disabled=True)
    await _fail(world)

    assert await _check(world, at=NOW + timedelta(days=365)) == FactorState(
        allowed=False, wait_s=0, disabled=True
    )
    (event,) = [e for e in await _audit(world) if e.action == "auth.factor_disabled"]
    assert (event.actor_type, event.actor_id) == ("system", None)
    assert (event.target_type, event.target_id) == ("user", str(world.user_id))
    assert event.meta["factor"] == "totp"
    assert event.meta["consecutive"] == failures.DISABLE_AT


async def test_a_success_clears_that_factors_failures(world: _World) -> None:
    await _fail(world, times=6)

    await _succeed(world)

    assert await _check(world) == ALLOWED
    assert await _consecutive(world) is None


async def test_a_success_does_not_revive_a_disabled_factor(world: _World) -> None:
    """Decisión del plan 7: only an administrator restores it."""
    await _fail(world, times=failures.DISABLE_AT)

    await _succeed(world)

    assert (await _check(world)).disabled


async def test_an_administrator_reset_restores_the_factor(world: _World) -> None:
    await _fail(world, times=failures.DISABLE_AT)

    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        await failures.reset_factor(session, world.user_id, "totp")

    assert await _check(world) == ALLOWED


async def test_factors_and_people_are_counted_apart(world: _World) -> None:
    """Per account AND per factor [S5]: a wall of TOTP failures says nothing
    about the password, nor about a colleague."""
    _org, colleague = await _person(world.admin_engine, world.org_id)
    await _fail(world, "totp", times=20)

    assert await _check(world, "password") == ALLOWED
    assert await _check(world, "totp", user_id=colleague) == ALLOWED


async def test_concurrent_failures_are_all_counted(world: _World) -> None:
    """The single-statement upsert: a read-then-write counter would lose
    increments to concurrent attempts, and an attacker could run attempts in
    parallel to stay under 100."""
    await asyncio.gather(*[_fail(world) for _ in range(10)])

    assert await _consecutive(world) == 10


async def test_a_success_clears_only_that_person_and_factor(world: _World) -> None:
    """`record_success` is scoped by user AND factor: remove either filter and
    the password's wait or the colleague's count disappears."""
    _org, colleague = await _person(world.admin_engine, world.org_id)
    await _fail(world, "totp", times=6)
    await _fail(world, "password", times=6)
    await _fail(world, "totp", times=3, user_id=colleague)

    await _succeed(world, "totp")

    assert await _consecutive(world, "totp") is None
    assert not (await _check(world, "password")).allowed
    async with AsyncSession(world.admin_engine) as session:
        assert (
            await session.scalar(
                select(AuthFailure.consecutive).where(
                    AuthFailure.user_id == colleague, AuthFailure.factor == "totp"
                )
            )
        ) == 3


async def test_an_administrator_reset_touches_only_that_person_and_factor(
    world: _World,
) -> None:
    _org, colleague = await _person(world.admin_engine, world.org_id)
    await _fail(world, "totp", times=failures.DISABLE_AT)
    await _fail(world, "password", times=6)
    await _fail(world, "totp", times=failures.DISABLE_AT, user_id=colleague)

    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        await failures.reset_factor(session, world.user_id, "totp")

    assert await _check(world, "totp") == ALLOWED
    assert not (await _check(world, "password")).allowed
    assert (await _check(world, "totp", user_id=colleague)).disabled


async def test_a_naive_time_is_refused(world: _World) -> None:
    naive = datetime(2026, 11, 4, 12, 0, 0)
    with pytest.raises(ValueError):
        await _check(world, at=naive)
    with pytest.raises(ValueError):
        await _fail(world, at=naive)


async def test_concurrent_attempts_at_99_failures_cannot_both_get_through(
    world: _World,
) -> None:
    """`check_allowed` takes an advisory lock held to the commit, so check ->
    wrong code -> record serialises: the second attempt sees the 100th
    failure. Without the lock both pass the check before either records."""
    await _fail(world, times=99, at=NOW - timedelta(days=1))
    arrived = asyncio.Event()

    async def attempt(first: bool) -> bool:
        async with org_session(
            world.org_id, engine=world.app_engine, clients=ClientScope.all()
        ) as session:
            if not first:
                await arrived.wait()
            state = await failures.check_allowed(session, world.user_id, "totp", now=NOW)
            if first:
                arrived.set()
                # Hold the transaction open until the other attempt is SEEN
                # blocked on the lock in its own check_allowed. (A fixed 0.5 s
                # let a pause in this process carry it past this commit: it
                # then read the 100th failure without ever meeting the lock,
                # and the test passed with the lock or without it.)
                await until_blocked_by(world.app_engine, await backend_pid(session))
            if state.allowed:
                await failures.record_failure(
                    session, org_id=world.org_id, user_id=world.user_id, factor="totp", now=NOW
                )
            return state.allowed

    results = await asyncio.gather(attempt(True), attempt(False))

    assert results == [True, False]
    assert await _consecutive(world) == failures.DISABLE_AT


# --- the device-cookie counter (operator decision 2026-11-05, design §2.3) ---
#
# A password attempt from a browser that `devices.recognised` accepts for the
# account waits and counts on THAT device's counter (`auth_trusted_devices.
# password_failures`); every other browser on the account's row. The stop at
# `DISABLE_AT` stays per account: it counts both. One advisory lock for both.


async def _device(w: _World, *, user_id: UUID | None = None) -> UUID:
    async with AsyncSession(w.admin_engine, expire_on_commit=False) as session:
        device = AuthTrustedDevice(
            org_id=w.org_id,
            user_id=user_id or w.user_id,
            token_hash=secrets.token_bytes(32),
            created_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=14),
        )
        session.add(device)
        await session.commit()
        return device.id


async def _fail_on(
    w: _World, device_id: UUID | None, *, times: int = 1, at: datetime = NOW
) -> FactorState:
    state: FactorState | None = None
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        for _ in range(times):
            state = await failures.record_failure(
                session,
                org_id=w.org_id,
                user_id=w.user_id,
                factor="password",
                device_id=device_id,
                now=at,
            )
    assert state is not None
    return state


async def _check_on(w: _World, device_id: UUID | None, *, at: datetime = NOW) -> FactorState:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await failures.check_allowed(
            session, w.user_id, "password", device_id=device_id, now=at
        )


async def _device_failures(w: _World, device_id: UUID) -> tuple[int, datetime | None]:
    async with AsyncSession(w.admin_engine) as session:
        row = (
            await session.execute(
                select(
                    AuthTrustedDevice.password_failures, AuthTrustedDevice.password_failed_at
                ).where(AuthTrustedDevice.id == device_id)
            )
        ).one()
        return row.password_failures, row.password_failed_at


async def test_a_recognised_device_waits_on_its_own_counter(world: _World) -> None:
    """(a) The account's wait does not hold the recognised browser, and the
    browser's failures do not feed the account's wait. Drop `device_id` from
    `check_allowed` and the device sees the account's 15 minutes: red."""
    device = await _device(world)
    await _fail(world, "password", times=30)

    assert (await _check_on(world, None)).wait_s == 900
    assert await _check_on(world, device) == ALLOWED

    assert (await _fail_on(world, device, times=5)) == FactorState(
        allowed=False, wait_s=1, disabled=False
    )
    assert await _device_failures(world, device) == (5, NOW)
    assert await _consecutive(world, "password") == 30
    assert await _check_on(world, device, at=NOW + timedelta(seconds=1)) == ALLOWED


async def test_another_persons_device_is_no_separate_counter(world: _World) -> None:
    """(b) A device id that is not this person's names no counter: the attempt
    waits on the account's row, and the stranger's device is not touched."""
    _org, colleague = await _person(world.admin_engine, world.org_id)
    theirs = await _device(world, user_id=colleague)
    await _fail(world, "password", times=30)

    assert (await _check_on(world, theirs)).wait_s == 900
    await _fail_on(world, theirs)
    assert await _consecutive(world, "password") == 31
    assert await _device_failures(world, theirs) == (0, None)


async def test_the_stop_counts_both_counters_and_is_audited_once(world: _World) -> None:
    """(d) 100 consecutive failures disable the password whichever counter
    they came from (NIST §3.2.2 is per authenticator, per account). Count only
    the account's row toward `DISABLE_AT` and 50 + 50 never stops: red."""
    device = await _device(world)
    await _fail(world, "password", times=50)
    assert not (await _fail_on(world, device, times=49)).disabled

    assert (await _fail_on(world, device)).disabled
    await _fail(world, "password")
    await _fail_on(world, device)

    assert (await _check_on(world, None, at=NOW + timedelta(days=365))).disabled
    assert (await _check_on(world, device, at=NOW + timedelta(days=365))).disabled
    (event,) = [e for e in await _audit(world) if e.action == "auth.factor_disabled"]
    assert event.meta == {"factor": "password", "consecutive": failures.DISABLE_AT}


async def test_a_success_clears_both_counters(world: _World) -> None:
    """(e) A success from any browser clears the consecutive count: the
    account's row and every device's counter of the person."""
    device, other = await _device(world), await _device(world)
    await _fail(world, "password", times=9)
    await _fail_on(world, device, times=7)
    await _fail_on(world, other, times=3)

    await _succeed(world, "password")

    assert await _consecutive(world, "password") is None
    assert await _device_failures(world, device) == (0, None)
    assert await _device_failures(world, other) == (0, None)


async def test_a_reset_clears_both_counters(world: _World) -> None:
    """`reset_factor` (the mail reset, a recovery link, an administrator's
    reset) restores the password whichever counter disabled it."""
    device = await _device(world)
    await _fail(world, "password", times=60)
    await _fail_on(world, device, times=40)
    assert (await _check_on(world, device, at=NOW + timedelta(days=1))).disabled

    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        await failures.reset_factor(session, world.user_id, "password")

    assert await _check_on(world, device) == ALLOWED
    assert await _check_on(world, None) == ALLOWED
    assert await _device_failures(world, device) == (0, None)


async def test_the_device_counter_is_the_passwords_alone(world: _World) -> None:
    device = await _device(world)
    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(ValueError):
            await failures.check_allowed(session, world.user_id, "totp", device_id=device, now=NOW)
        with pytest.raises(ValueError):
            await failures.record_failure(
                session,
                org_id=world.org_id,
                user_id=world.user_id,
                factor="totp",
                device_id=device,
                now=NOW,
            )


async def _attempt_on(w: _World, device_id: UUID | None) -> bool:
    """check -> (a wrong password) -> record, in ONE transaction, as login does."""
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        state = await failures.check_allowed(
            session, w.user_id, "password", device_id=device_id, now=NOW
        )
        # Long enough for the rest of the burst to reach its own check.
        await asyncio.sleep(0.05)
        if state.allowed:
            await failures.record_failure(
                session,
                org_id=w.org_id,
                user_id=w.user_id,
                factor="password",
                device_id=device_id,
                now=NOW,
            )
        return state.allowed


async def test_a_parallel_burst_on_both_counters_stops_at_one_hundred(world: _World) -> None:
    """(f) Both counters sit under ONE advisory lock (`check_allowed`), so a
    burst of check -> wrong password -> record from both kinds of browser is
    serialised: at 99 the first failure is the 100th, it disables the
    password, and every other attempt of the burst sees it disabled. With a
    lock per counter, both counters' first attempts pass the check before
    either records: two failures counted and a count past the stop."""
    device = await _device(world)
    long_ago = NOW - timedelta(days=1)
    await _fail(world, "password", times=60, at=long_ago)
    await _fail_on(world, device, times=39, at=long_ago)

    results = await asyncio.gather(*(_attempt_on(world, d) for d in [None, device] * 6))

    assert results.count(True) == 1
    account = await _consecutive(world, "password")
    on_device, _at = await _device_failures(world, device)
    assert account is not None and account + on_device == failures.DISABLE_AT
    assert [e.action for e in await _audit(world)].count("auth.factor_disabled") == 1


async def test_a_parallel_burst_on_one_device_waits_its_turn(world: _World) -> None:
    """(f) The device's own wait holds under a burst: at 4 failures the next
    one starts a wait, and the rest of the burst sees it."""
    device = await _device(world)
    await _fail_on(world, device, times=4, at=NOW - timedelta(days=1))

    results = await asyncio.gather(*(_attempt_on(world, device) for _ in range(8)))

    assert results.count(True) == 1
    assert await _device_failures(world, device) == (5, NOW)


def test_whether_a_success_clears_the_mail_mark_is_always_said() -> None:
    """Task 9b fix round 1. `record_success` clears the "password set by
    mail" mark only when its caller says so, and every caller must say:
    `clear_mail_mark` is a required keyword with no default, as
    `sessions.start_session(first_step=)` is. A default of True failed open
    -- a new caller that forgot it cleared the mark for a factor proved
    without the password (the Task 9 review's I1)."""
    parameter = inspect.signature(failures.record_success).parameters["clear_mail_mark"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        failures.record_success(None, uuid4(), "totp")  # type: ignore[call-arg, arg-type]

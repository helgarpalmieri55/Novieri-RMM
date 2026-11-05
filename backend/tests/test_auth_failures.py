"""Failed attempts per person and per factor: `app/core/auth/failures.py`.

Design §2.3 and falsifier §10.3: counted per account and per factor, never per
address alone [S5]; from the 5th consecutive failure a wait of 1 s that
doubles up to 15 min; at 100 the factor is disabled until an administrator
restores it [S1 §3.2.2]; a success clears that factor's failures.
"""

import asyncio
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
from app.models.auth import AuthFailure

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
        await failures.record_success(session, w.user_id, factor)


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
                # Hold the transaction open long enough for the other attempt
                # to reach its own check_allowed (it must block on the lock).
                await asyncio.sleep(0.5)
            if state.allowed:
                await failures.record_failure(
                    session, org_id=world.org_id, user_id=world.user_id, factor="totp", now=NOW
                )
            return state.allowed

    results = await asyncio.gather(attempt(True), attempt(False))

    assert results == [True, False]
    assert await _consecutive(world) == failures.DISABLE_AT

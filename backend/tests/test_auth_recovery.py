"""Recovery codes: `app/core/auth/recovery.py` (design §2.2).

Ten single-use look-up secrets, shown once, stored salted and hashed with a
password hashing scheme -- NIST SP 800-63B-4 §3.1.2.2 for a secret shorter
than 112 bits.
"""

import asyncio
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth import failures, recovery
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Organization, User
from app.models.auth import AuthFailure, AuthRecoveryCode

SHAPE = re.compile(r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}")


@dataclass
class _World:
    org_id: UUID
    user_id: UUID
    app_engine: AsyncEngine
    admin_engine: AsyncEngine


async def _person(admin_engine: AsyncEngine, org_id: UUID | None = None) -> tuple[UUID, UUID]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        if org_id is None:
            slug = f"rec-{uuid4().hex[:8]}"
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


async def _replace(w: _World, user_id: UUID | None = None) -> list[str]:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await recovery.replace_codes(session, org_id=w.org_id, user_id=user_id or w.user_id)


async def _use(w: _World, code: str, user_id: UUID | None = None) -> bool:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await recovery.use_code(session, user_id or w.user_id, code)


async def _remaining(w: _World) -> int:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await recovery.remaining(session, w.user_id)


async def _stored(w: _World) -> list[bytes]:
    async with AsyncSession(w.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuthRecoveryCode.code_hash).where(AuthRecoveryCode.user_id == w.user_id)
                )
            ).scalars()
        )


def test_ten_codes_in_the_shown_format() -> None:
    codes = recovery.new_codes()

    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(SHAPE.fullmatch(code) for code in codes)


async def test_codes_are_stored_salted_and_hashed_with_argon2id(world: _World) -> None:
    """NIST §3.1.2.2, Decisión del plan 3: argon2id at OWASP's minimum, one
    random salt per code, and the code itself nowhere."""
    codes = await _replace(world)

    stored = await _stored(world)
    assert len(stored) == 10
    assert all(h.startswith(b"$argon2id$v=19$m=19456,t=2,p=1$") for h in stored)
    assert len({h.split(b"$")[4] for h in stored}) == 10  # ten distinct salts
    for code in codes:
        assert all(code.replace("-", "").encode() not in h for h in stored)


async def test_each_code_works_once(world: _World) -> None:
    codes = await _replace(world)

    assert await _use(world, codes[3])
    assert not await _use(world, codes[3])
    assert await _remaining(world) == 9


async def test_typing_is_forgiving(world: _World) -> None:
    """Case, spaces and the hyphen do not matter; O reads as 0 and I or L as 1,
    as Crockford's alphabet says."""
    codes = await _replace(world)
    typed = codes[0].replace("-", " ").lower().replace("0", "o").replace("1", "l")

    assert await _use(world, f"  {typed} ")


@pytest.mark.parametrize("code", ["", "AAAAA-AAAAU", "AAAAA-AAAA", "AAAAA-AAAAAA", "ÄAAAA-AAAAA"])
async def test_a_malformed_code_is_refused_and_spends_nothing(world: _World, code: str) -> None:
    await _replace(world)

    assert not await _use(world, code)
    assert await _remaining(world) == 10


async def test_a_code_never_issued_is_refused(world: _World) -> None:
    codes = await _replace(world)
    stranger = next(c for c in recovery.new_codes() if c not in codes)

    assert not await _use(world, stranger)
    assert await _remaining(world) == 10


async def test_new_codes_replace_the_old_ones(world: _World) -> None:
    old = await _replace(world)
    new = await _replace(world)

    assert not await _use(world, old[0])
    assert await _use(world, new[0])
    assert len(await _stored(world)) == 10


async def test_another_persons_code_is_refused(world: _World) -> None:
    _org, colleague = await _person(world.admin_engine, world.org_id)
    theirs = await _replace(world, colleague)
    await _replace(world)

    assert not await _use(world, theirs[0])


async def test_replacing_the_codes_keeps_the_failure_count(world: _World) -> None:
    """Decisión del plan 7: new codes are not a way to reset the counter."""
    await _replace(world)
    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        for _ in range(3):
            await failures.record_failure(
                session, org_id=world.org_id, user_id=world.user_id, factor="recovery"
            )

    await _replace(world)

    async with AsyncSession(world.admin_engine) as session:
        assert (
            await session.scalar(
                select(AuthFailure.consecutive).where(
                    AuthFailure.user_id == world.user_id, AuthFailure.factor == "recovery"
                )
            )
        ) == 3


async def test_two_requests_with_one_code_cannot_both_win(world: _World) -> None:
    """The conditional UPDATE (`used_at IS NULL`): NIST §3.1.2.2, "SHALL be
    used successfully only once"."""
    codes = await _replace(world)

    results = await asyncio.gather(_use(world, codes[0]), _use(world, codes[0]))

    assert sorted(results) == [False, True]

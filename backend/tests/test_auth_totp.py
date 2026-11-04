"""TOTP: `app/core/auth/totp.py` (design §2.2, falsifier §10.3).

RFC 6238 with a 30-second step, six digits and one step of tolerance either
way; a code accepted once is never accepted again, which `pyotp` 2.10.0 does
not do for us [S43]; the secret is sealed by the vault and bound to its
person. The expected codes are computed with `pyotp`; the RFC 6238 Appendix B
vectors pin the algorithm independently of it.
"""

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pyotp
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth import totp
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import Organization, User
from app.models.auth import AuthTotp

NOW = datetime(2026, 11, 4, 12, 0, 15, tzinfo=UTC)
STEP = int(NOW.timestamp()) // 30
FIXED = bytes(range(20))


def _code(secret: bytes, step: int) -> str:
    return pyotp.TOTP(base64.b32encode(secret).decode("ascii")).generate_otp(step)


@dataclass
class _World:
    org_id: UUID
    user_id: UUID
    vault: LocalVaultService
    app_engine: AsyncEngine
    admin_engine: AsyncEngine


async def _person(admin_engine: AsyncEngine, org_id: UUID | None = None) -> tuple[UUID, UUID]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        if org_id is None:
            slug = f"totp-{uuid4().hex[:8]}"
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
async def world(admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path) -> _World:
    org_id, user_id = await _person(admin_engine)
    vault = LocalVaultService(tmp_path / "root.key")
    return _World(org_id, user_id, vault, app_engine, admin_engine)


async def _stage(w: _World, secret: bytes, *, user_id: UUID | None = None) -> None:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        await totp.stage_secret(
            session, org_id=w.org_id, user_id=user_id or w.user_id, secret=secret, vault=w.vault
        )


async def _confirm(
    w: _World, code: str, *, at: datetime = NOW, user_id: UUID | None = None
) -> bool:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await totp.confirm_secret(
            session, user_id or w.user_id, code, vault=w.vault, now=at
        )


async def _verify(
    w: _World, code: str, *, at: datetime = NOW, user_id: UUID | None = None
) -> bool:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await totp.verify_totp(session, user_id or w.user_id, code, vault=w.vault, now=at)


async def _has(w: _World) -> bool:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        return await totp.has_totp(session, w.user_id)


async def _enrolled(
    w: _World, secret: bytes | None = None, *, user_id: UUID | None = None
) -> bytes:
    """A confirmed factor whose last accepted step is ten minutes before NOW, so
    the tests at NOW start from a clean window."""
    secret = secret or totp.new_secret()
    await _stage(w, secret, user_id=user_id)
    earlier = NOW - timedelta(minutes=10)
    assert await _confirm(w, _code(secret, totp.step_of(earlier)), at=earlier, user_id=user_id)
    return secret


async def _last_step(w: _World) -> int | None:
    async with AsyncSession(w.admin_engine) as session:
        value: int | None = await session.scalar(
            select(AuthTotp.last_step).where(AuthTotp.user_id == w.user_id)
        )
        return value


async def _sealed(w: _World, user_id: UUID) -> bytes:
    """The stored column, read on the ADMIN engine: what a database dump holds."""
    async with AsyncSession(w.admin_engine) as session:
        sealed: bytes = (
            await session.execute(select(AuthTotp.secret_sealed).where(AuthTotp.user_id == user_id))
        ).scalar_one()
        return sealed


def test_a_new_secret_is_160_random_bits() -> None:
    first, second = totp.new_secret(), totp.new_secret()

    assert len(first) == 20 and len(second) == 20
    assert first != second


def test_the_uri_is_the_key_uri_format_an_authenticator_reads() -> None:
    """No `digits`, `period` or `algorithm` parameter: the Key Uri Format's
    defaults are 6, 30 and SHA-1, which is exactly what this module verifies."""
    uri = totp.otpauth_uri(FIXED, account="alice@toeshee.com", issuer="ITMS")

    parts = urlsplit(uri)
    assert (parts.scheme, parts.netloc) == ("otpauth", "totp")
    assert parts.path == "/ITMS:alice%40toeshee.com"
    assert parse_qs(parts.query) == {
        "secret": [base64.b32encode(FIXED).decode("ascii")],
        "issuer": ["ITMS"],
    }
    parsed = pyotp.parse_uri(uri)
    assert isinstance(parsed, pyotp.TOTP)
    assert (parsed.digits, parsed.interval) == (6, 30)


async def test_the_rfc_6238_vectors_are_accepted(world: _World) -> None:
    """RFC 6238 Appendix B, SHA-1, secret "12345678901234567890": T=59 is
    94287082, T=1111111109 is 07081804, T=1234567890 is 89005924 at eight
    digits; six digits are the last six. Pins the step, the digits and the
    hash without trusting `pyotp` to agree with itself."""
    await _stage(world, b"12345678901234567890")

    assert await _confirm(world, "287082", at=datetime.fromtimestamp(59, UTC))
    assert await _verify(world, "081804", at=datetime.fromtimestamp(1111111109, UTC))
    assert await _verify(world, "005924", at=datetime.fromtimestamp(1234567890, UTC))


async def test_a_code_is_accepted_once(world: _World) -> None:
    """Falsifier §10.3, second half. NIST §3.1.4: "Verifiers SHALL accept a
    given OTP only once while it is valid". The `step > after` test and the
    conditional UPDATE: remove either and the second call answers True."""
    secret = await _enrolled(world)
    code = _code(secret, STEP)

    assert await _verify(world, code)
    assert not await _verify(world, code)
    # Still inside the window at the next step, and still refused.
    assert not await _verify(world, code, at=NOW + timedelta(seconds=20))
    assert await _verify(world, _code(secret, STEP + 1), at=NOW + timedelta(seconds=30))


async def test_an_older_code_is_refused_once_a_newer_one_was_accepted(world: _World) -> None:
    secret = await _enrolled(world)

    assert await _verify(world, _code(secret, STEP + 1))
    assert not await _verify(world, _code(secret, STEP))


@pytest.mark.parametrize(
    ("offset", "accepted"), [(-2, False), (-1, True), (0, True), (1, True), (2, False)]
)
async def test_one_step_of_tolerance_either_way(world: _World, offset: int, accepted: bool) -> None:
    await _enrolled(world, FIXED)

    assert await _verify(world, _code(FIXED, STEP + offset)) is accepted


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12a456", "１２３４５６", "123456\n"])
async def test_a_malformed_code_is_refused_and_changes_nothing(world: _World, code: str) -> None:
    await _enrolled(world, FIXED)
    before = await _last_step(world)

    assert not await _verify(world, code)
    assert await _last_step(world) == before


async def test_spaces_inside_a_code_are_ignored(world: _World) -> None:
    secret = await _enrolled(world)
    code = _code(secret, STEP)

    assert await _verify(world, f" {code[:3]} {code[3:]} ")


async def test_a_staged_secret_is_not_a_factor_until_confirmed(world: _World) -> None:
    """Decisión del plan 2: `last_step IS NULL` is an enrolment in progress."""
    secret = totp.new_secret()
    await _stage(world, secret)

    assert not await _has(world)
    assert not await _verify(world, _code(secret, STEP))
    assert await _confirm(world, _code(secret, STEP))
    assert await _has(world)
    # And once it is a factor, `confirm_secret` no longer accepts against it.
    assert not await _confirm(world, _code(secret, STEP + 1), at=NOW + timedelta(seconds=30))


async def test_staging_never_replaces_a_confirmed_secret(world: _World) -> None:
    secret = await _enrolled(world)

    with pytest.raises(totp.TotpAlreadyEnrolled):
        await _stage(world, totp.new_secret())

    assert await _verify(world, _code(secret, STEP))


async def test_staging_again_replaces_a_pending_secret(world: _World) -> None:
    first, second = totp.new_secret(), totp.new_secret()
    await _stage(world, first)
    await _stage(world, second)

    assert not await _confirm(world, _code(first, STEP))
    assert await _confirm(world, _code(second, STEP))


async def test_the_secret_is_sealed_not_stored(world: _World) -> None:
    secret = await _enrolled(world)

    sealed = await _sealed(world, world.user_id)

    assert secret not in sealed
    assert base64.b32encode(secret) not in sealed
    assert set(json.loads(base64.b64decode(sealed))) == {"wrapped_dek", "key_version", "ct_b64"}


async def test_a_sealed_secret_moved_to_another_person_does_not_open(world: _World) -> None:
    """The seal is bound to the user id (`key_id=user_id`, AAD `org|user`):
    copying one person's secret onto another's row is an integrity failure,
    not a working factor."""
    secret = await _enrolled(world)
    _org, colleague = await _person(world.admin_engine, world.org_id)
    await _enrolled(world, user_id=colleague)
    sealed = await _sealed(world, world.user_id)
    async with AsyncSession(world.admin_engine) as session:
        await session.execute(
            update(AuthTotp).where(AuthTotp.user_id == colleague).values(secret_sealed=sealed)
        )
        await session.commit()
    code = _code(secret, STEP)

    assert not await _verify(world, code, user_id=colleague)
    assert await _verify(world, code)


async def test_two_requests_with_one_code_cannot_both_win(world: _World) -> None:
    """The conditional UPDATE (`last_step < step`): with a read-then-write the
    two would both see the old step and both answer True."""
    secret = await _enrolled(world)
    code = _code(secret, STEP)

    results = await asyncio.gather(_verify(world, code), _verify(world, code))

    assert sorted(results) == [False, True]


async def test_removing_the_factor(world: _World) -> None:
    await _enrolled(world)

    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        assert await totp.remove_totp(session, world.user_id)
    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        assert not await totp.remove_totp(session, world.user_id)
    assert not await _has(world)


def test_a_naive_time_is_refused() -> None:
    """A naive datetime is read as local time by `timestamp()`: a server in
    another zone would accept codes hours away."""
    with pytest.raises(ValueError):
        totp.step_of(datetime(2026, 11, 4, 12, 0, 0))

"""The password's two counters at sign-in (operator decision 2026-11-05, design §2.3).

A sign-in carrying a `__Host-itms_device` cookie that `devices.recognised`
accepts for that account waits and counts on that device's own counter; every
other browser shares the account's. Somebody who knows a technician's address
but holds no cookie of theirs can keep the account's counter waiting -- and
with it only the browsers the platform does not recognise (OWASP, "Slow Down
Online Guessing Attacks with Device Cookies"). The NIST SP 800-63B-4 §3.2.2
stop stays per account: 100 consecutive failures, from either counter,
disable the password. A success from any browser clears both.

THE ANSWERS DO NOT CHANGE. A browser in its wait, recognised or not, gets the
one generic 401 before the password is tried, as before.

Each test names the line it protects; the unit-level halves are in
`tests/test_auth_failures.py`.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE
from app.models import AuditEvent
from app.models.auth import AuthFailure, AuthOrgSettings, AuthPassword, AuthTrustedDevice
from tests.auth_support import (
    Staff,
    browser,
    capture_mail,
    console_ready,
    cookie,
    fresh_code,
    login,
    signed_in,
)

WRONG = "Not-The-Password-41"


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _remembered(env: Any, org: UUID, *roles: str) -> Staff:
    """A technician whose browser is remembered: the device cookie, no session."""
    who = await signed_in(env, org, *(roles or ("technician",)))
    who.session = None
    await login(env, who)
    done = await env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(env, who), "remember_device": True},
    )
    assert done.status_code == 200, done.text
    who.device = cookie(done, DEVICE_COOKIE)
    assert who.device
    who.session = None
    return who


def _elsewhere(who: Staff, *, device: str | None = None) -> Staff:
    """The same account from a browser the platform does not recognise (or
    one carrying `device`), at an address of its own."""
    return replace(who, session=None, device=device, network=browser())


async def _account_failures(
    env: Any, who: Staff, consecutive: int, *, at: datetime | None = None
) -> None:
    """The account's counter, as `consecutive` paced failures would leave it."""
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            pg_insert(AuthFailure)
            .values(
                org_id=who.org_id,
                user_id=who.user_id,
                factor="password",
                consecutive=consecutive,
                last_failed_at=at or datetime.now(UTC),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "factor"],
                set_={"consecutive": consecutive, "last_failed_at": at or datetime.now(UTC)},
            )
        )
        await session.commit()


async def _device_row(env: Any, who: Staff) -> AuthTrustedDevice:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(AuthTrustedDevice).where(AuthTrustedDevice.user_id == who.user_id)
            )
        ).scalar_one()


async def _set_device(env: Any, who: Staff, **values: Any) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthTrustedDevice)
            .where(AuthTrustedDevice.user_id == who.user_id)
            .values(**values)
        )
        await session.commit()


async def _counts(env: Any, who: Staff) -> tuple[int | None, int]:
    """(the account's consecutive failures or None, the device's)."""
    async with AsyncSession(env.admin_engine) as session:
        account = await session.scalar(
            select(AuthFailure.consecutive).where(
                AuthFailure.user_id == who.user_id, AuthFailure.factor == "password"
            )
        )
    return account, (await _device_row(env, who)).password_failures


async def _disabled(env: Any, org: UUID) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org, AuditEvent.action == "auth.factor_disabled"
                    )
                )
            ).scalars()
        )


async def test_the_attackers_wait_does_not_hold_the_remembered_browser(auth_env: Any) -> None:
    """(a) The attacker's paced failures, without the cookie, are counted on
    the account; while that counter waits, the remembered browser signs in
    with the right password and an unrecognised one gets the generic 401.
    Make `flows.login` ignore the device for the counter (`device_id=None`)
    and the remembered browser is refused too: red."""
    org = await auth_env.create_org("devctr-a")
    who = await _remembered(auth_env, org)
    for _ in range(3):
        attacker = await login(auth_env, _elsewhere(who), password=WRONG)
        assert attacker.status_code == 401
    assert await _counts(auth_env, who) == (3, 0)
    # Twenty-odd minutes of paced failures: the account waits 15 minutes.
    await _account_failures(auth_env, who, 20)

    unrecognised = await login(auth_env, _elsewhere(who))
    remembered = await login(auth_env, who)

    assert unrecognised.status_code == 401, unrecognised.text
    wrong = await login(auth_env, _elsewhere(who), password=WRONG)
    assert unrecognised.json() == wrong.json()
    assert unrecognised.json()["code"] == "invalid_credentials"
    assert remembered.status_code == 200, remembered.text
    assert remembered.json() == {"state": "signed_in"}
    me = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert me.status_code == 200, me.text


async def test_the_remembered_browsers_failures_wait_on_their_own(auth_env: Any) -> None:
    """(a), the other direction. Failures from the remembered browser count
    on its device and start ITS wait -- answered with the same generic 401 --
    and leave the account's counter alone. Count them on the account and the
    unrecognised browser below waits too: red."""
    org = await auth_env.create_org("devctr-own")
    who = await _remembered(auth_env, org)

    for _ in range(5):
        assert (await login(auth_env, who, password=WRONG)).status_code == 401
    assert await _counts(auth_env, who) == (None, 5)
    # A few more, paced: the device waits 16 seconds, whatever the runner's speed.
    await _set_device(
        auth_env, who, password_failures=9, password_failed_at=datetime.now(UTC)
    )

    waiting = await login(auth_env, who)
    elsewhere = await login(auth_env, _elsewhere(who))

    assert waiting.status_code == 401
    assert waiting.json()["code"] == "invalid_credentials"
    assert elsewhere.status_code == 200, elsewhere.text
    assert elsewhere.json()["state"] == "factor_required"


async def test_another_accounts_device_cookie_is_no_separate_counter(auth_env: Any) -> None:
    """(b) `recognised` binds a device to ONE person: B's cookie, presented
    for A, is A's unrecognised browser. It counts on A's account and touches
    nothing of B's, and while A's account waits it does not let A's right
    password through."""
    org = await auth_env.create_org("devctr-b")
    victim = await _remembered(auth_env, org)
    other = await _remembered(auth_env, org)
    borrowed = _elsewhere(victim, device=other.device)

    for _ in range(2):
        assert (await login(auth_env, borrowed, password=WRONG)).status_code == 401
    assert await _counts(auth_env, victim) == (2, 0)
    assert await _counts(auth_env, other) == (None, 0)

    await _account_failures(auth_env, victim, 20)
    refused = await login(auth_env, _elsewhere(victim, device=other.device))
    assert refused.status_code == 401 and refused.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("why", ["revoked", "expired", "remembering_off", "password_changed"])
async def test_a_device_that_is_no_longer_recognised_gives_no_separate_counter(
    auth_env: Any, why: str
) -> None:
    """(c) The counter is chosen by `devices.recognised`'s rule, every limit
    of it: a revoked or expired device, an organization that stopped
    remembering devices, a password changed since the device was remembered.
    Each is an unrecognised browser, held by the account's wait."""
    org = await auth_env.create_org(f"devctr-c-{why.replace('_', '-')}")
    who = await _remembered(auth_env, org)
    now = datetime.now(UTC)
    async with AsyncSession(auth_env.admin_engine) as session:
        if why == "revoked":
            await session.execute(
                update(AuthTrustedDevice)
                .where(AuthTrustedDevice.user_id == who.user_id)
                .values(revoked_at=now)
            )
        elif why == "expired":
            await session.execute(
                update(AuthTrustedDevice)
                .where(AuthTrustedDevice.user_id == who.user_id)
                .values(expires_at=now - timedelta(seconds=1))
            )
        elif why == "remembering_off":
            session.add(AuthOrgSettings(org_id=org, remember_device_enabled=False))
        else:
            await session.execute(
                update(AuthPassword)
                .where(AuthPassword.user_id == who.user_id)
                .values(changed_at=now)
            )
        await session.commit()
    await _account_failures(auth_env, who, 20)

    refused = await login(auth_env, who)

    assert refused.status_code == 401 and refused.json()["code"] == "invalid_credentials"
    assert (await login(auth_env, who, password=WRONG)).status_code == 401
    assert await _counts(auth_env, who) == (20, 0)


async def test_a_hundred_failures_across_both_counters_disable_the_password(
    auth_env: Any,
) -> None:
    """(d) The stop is per account: 60 failures from other browsers and 40
    from the remembered one are 100. The password is disabled for every
    browser, audited once and told once."""
    org = await auth_env.create_org("devctr-d")
    who = await _remembered(auth_env, org)
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await _account_failures(auth_env, who, 60, at=long_ago)
    await _set_device(auth_env, who, password_failures=39, password_failed_at=long_ago)
    told = len(auth_env.mail.sent)

    last = await login(auth_env, who, password=WRONG)
    after_here = await login(auth_env, who)
    after_elsewhere = await login(auth_env, _elsewhere(who))

    assert last.status_code == 401
    assert after_here.status_code == 401 and after_elsewhere.status_code == 401
    assert after_here.json() == last.json() == after_elsewhere.json()
    assert [e.meta for e in await _disabled(auth_env, org)] == [
        {"factor": "password", "consecutive": 100}
    ]
    locked = [m for m in auth_env.mail.sent[told:] if m.to == who.email]
    assert len(locked) == 1 and "locked" in locked[0].subject


@pytest.mark.parametrize("where", ["remembered", "unrecognised"])
async def test_a_success_from_either_browser_clears_both_counts(
    auth_env: Any, where: str
) -> None:
    """(e) `record_success` clears the account's row and every device's
    count: one consecutive count, cleared by a success from any browser."""
    org = await auth_env.create_org(f"devctr-e-{where}")
    who = await _remembered(auth_env, org)
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await _account_failures(auth_env, who, 9, at=long_ago)
    await _set_device(auth_env, who, password_failures=7, password_failed_at=long_ago)

    signed = await login(auth_env, who if where == "remembered" else _elsewhere(who))

    assert signed.status_code == 200, signed.text
    assert await _counts(auth_env, who) == (None, 0)
    assert (await _device_row(auth_env, who)).password_failed_at is None


async def test_a_burst_from_the_remembered_browser_is_counted_one_at_a_time(
    auth_env: Any,
) -> None:
    """(f) The device's counter sits under the account's advisory lock: a
    burst of wrong passwords at the 8th failure counts exactly one -- the
    9th starts a 16-second wait the rest of the burst meets -- and every
    answer is the same 401. Take the lock in a transaction of its own and the
    burst is counted many times over: red."""
    org = await auth_env.create_org("devctr-f")
    who = await _remembered(auth_env, org)
    await _set_device(
        auth_env,
        who,
        password_failures=8,
        password_failed_at=datetime.now(UTC) - timedelta(days=1),
    )

    answers = await asyncio.gather(*(login(auth_env, who, password=WRONG) for _ in range(6)))

    assert [a.status_code for a in answers] == [401] * 6
    assert all(a.json() == answers[0].json() for a in answers)
    assert await _counts(auth_env, who) == (None, 9)
    assert all(cookie(a, SESSION_COOKIE) is None for a in answers)

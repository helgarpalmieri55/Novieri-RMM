"""Signing a technician in (P2 phase 1 Task 7, design §2.2-§2.6, §6.4, §10).

The falsifiers held here, each by a test that fails without its line:

- 2: an unknown address, a wrong password and an account that may not sign in
  answer the same body after exactly one argon2 verification each
  (`passwords.verify_against_nobody` in `flows.login`).
- 3 (route half): a TOTP code is not taken twice, and the 100th failure locks
  the factor (Task 4 audits it) and the person is told (`factors.count_failure`).
- 7 (the login half): a login without the console header, or with
  `Origin: null`, is refused (Task 6's rule over every `/api/v1/auth/`).
- 5 (the device half): a remembered device skips the factor at sign-in and
  never the recent confirmation; changing the password forgets it.

Plus this task's own rules: a session waiting for its factor is not a console
session; the identifier changes when the factor is confirmed; a person who
already has a factor cannot enrol another with the password alone;
`enroll/finish` only upgrades the session that enrolled; a burst of attempts
is counted one attempt at a time (check, verify and count in one transaction).
"""

import asyncio
import base64
import ipaddress
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import flows, keys
from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.session_policy import SessionPolicy
from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE
from app.core.config import Settings
from app.models import AuditEvent, Client, User
from app.models.auth import (
    AuthFailure,
    AuthPasskey,
    AuthPassword,
    AuthSession,
    AuthWebauthnChallenge,
)
from tests.auth_support import (
    CONSOLE,
    PASSWORD,
    add_passkey,
    browser,
    capture_mail,
    console_ready,
    cookie,
    enrol_totp,
    fresh_code,
    login,
    point_at,
    signed_in,
    staff,
    wrong_code,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _audit(env: Any, org_id: Any, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _factor(env: Any, who: Any, *, remember: bool = False) -> Any:
    return await env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(env, who), "remember_device": remember},
    )


async def test_a_new_technician_enrols_a_factor_and_only_then_reaches_the_console(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("signin")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)

    first = await login(auth_env, who)
    assert first.status_code == 200, first.text
    assert first.json() == {"state": "enroll_required"}
    pending = who.session
    assert pending
    # Design §2.2: "la persona no llega a la consola hasta registrar uno".
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401

    await enrol_totp(auth_env, who)

    assert who.session and who.session != pending
    me = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == str(who.user_id)
    assert len(who.recovery_codes) == 10
    signed = await _audit(auth_env, org, "auth.signed_in")
    assert [e.meta for e in signed] == [{"method": "enrolment", "trusted_device": False}]


async def test_the_next_sign_in_asks_for_the_factor_and_changes_the_identifier(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("signin-2")
    who = await signed_in(auth_env, org, "technician")
    who.session = None

    step = await login(auth_env, who)
    assert step.json() == {
        "state": "factor_required",
        "methods": ["totp", "recovery"],
        "remember_device_days": 15,
    }
    pending = who.session

    done = await _factor(auth_env, who)
    assert done.status_code == 200, done.text
    assert done.json() == {"state": "signed_in"}
    who.session = cookie(done, SESSION_COOKIE)
    assert who.session and who.session != pending
    assert cookie(done, DEVICE_COOKIE) is None

    # Design §2.4: the identifier changes when the factor is confirmed
    # (`sessions.mark_factor`, Task 6); the pending value names nothing now.
    stale = {**browser(), "Cookie": f"{SESSION_COOKIE}={pending}"}
    again = await auth_env.client.post(
        "/api/v1/auth/factor/totp", headers=stale, json={"code": "123456", "remember_device": False}
    )
    assert again.status_code == 401, again.text
    assert again.json()["code"] == "auth_required"


async def test_whoever_is_refused_is_refused_alike_and_in_the_same_time(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FALSIFIER 2. One argon2 verification per attempt whatever the account
    is (`PasswordHasher.verify`, the call inside both `verify_password` and
    `verify_against_nobody`): delete either `verify_against_nobody` call in
    `flows.login` and the count below drops. The 10-15 ms the known
    address's queries add is accepted, not equalised: the argon2 work is the
    same and the per-IP limits leave too few samples to see it
    (`test_auth_falsifiers.py::test_falsifier_2_...`, final review C-M2)."""
    org = await auth_env.create_org("generic")
    known = await staff(auth_env, org, "technician")
    disabled = await staff(auth_env, org, "technician")
    async with AsyncSession(auth_env.admin_engine, expire_on_commit=False) as session:
        await session.execute(
            update(User).where(User.id == disabled.user_id).values(status="disabled")
        )
        client = Client(org_id=org, name="Acme", kind="customer")
        session.add(client)
        await session.flush()
        session.add(
            User(
                org_id=org,
                kind="contact",
                client_id=client.id,
                email="ana@acme.test",
                external_subject="contact-pending:generic",
            )
        )
        await session.commit()
    await point_at(auth_env, org)

    calls: list[int] = []
    real = PasswordHasher.verify

    def counting(self: PasswordHasher, stored: Any, password: Any) -> Any:
        calls.append(1)
        return real(self, stored, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)

    async def attempt(email: str, password: str) -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/login", headers=browser(), json={"email": email, "password": password}
        )

    answers = [
        await attempt(known.email, "Not-The-Password-41"),
        await attempt("nobody@test.local", "Not-The-Password-41"),
        await attempt(disabled.email, PASSWORD),
        await attempt("ana@acme.test", PASSWORD),
    ]

    assert [a.status_code for a in answers] == [401, 401, 401, 401]
    bodies = [a.json() for a in answers]
    assert all(body == bodies[0] for body in bodies), bodies
    assert bodies[0]["code"] == "invalid_credentials"
    assert len(calls) == 4
    assert all(cookie(a, SESSION_COOKIE) is None for a in answers)


async def test_while_the_password_waits_even_the_right_one_gets_the_generic_answer(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 5: the wait is not told, and the real hash is not tried --
    delete `or not state.allowed` in `flows.login` and the spy sees it."""
    org = await auth_env.create_org("waiting")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)
    async with AsyncSession(auth_env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="password",
                consecutive=8,
                last_failed_at=datetime.now(UTC),
            )
        )
        await session.commit()
    tried: list[str] = []
    nobody: list[int] = []
    real, real_nobody = flows.verify_password, flows.verify_against_nobody

    async def spy(org_id: Any, plain: str, digest: str, version: int) -> bool:
        tried.append(digest)
        return await real(org_id, plain, digest, version)

    async def spy_nobody(org_id: Any, plain: str) -> bool:
        nobody.append(1)
        return await real_nobody(org_id, plain)

    monkeypatch.setattr(flows, "verify_password", spy)
    monkeypatch.setattr(flows, "verify_against_nobody", spy_nobody)

    response = await login(auth_env, who)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert tried == [] and nobody == [1]


async def test_with_the_keys_unavailable_sign_in_answers_503_and_nothing_else(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 13: OpenBao down is a 503 through `guards.keys_unavailable_handler`,
    the same body for a known and an unknown address; never a 500."""
    org = await auth_env.create_org("keys-down")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)

    class Down:
        async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
            raise AuthKeysUnavailable("transit unreachable")

    monkeypatch.setattr(keys, "_backend", Down())

    known = await login(auth_env, who)
    unknown = await auth_env.client.post(
        "/api/v1/auth/login",
        headers=browser(),
        json={"email": "nobody@test.local", "password": "Not-The-Password-41"},
    )

    assert (known.status_code, unknown.status_code) == (503, 503)
    assert known.json() == unknown.json()
    assert known.json()["code"] == "sign_in_unavailable"


async def test_a_password_kept_under_an_unknown_pepper_version_is_a_wrong_one(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verifier stored under a pepper version the local key backend does
    not hold raises `UnknownKeyVersion` (a LookupError) from `verify_password`.
    That is a password that cannot be verified, so the login answers what it
    answers for any other refusal -- 401 `invalid_credentials`, after the same
    one argon2 verification (`verify_against_nobody`) -- never a 500, and it
    counts no failure the person did not make. Remove the `except
    UnknownKeyVersion` in `flows._password_matches` and this is a 500: red."""
    org = await auth_env.create_org("pepper")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == who.user_id)
            .values(pepper_version=99)
        )
        await session.commit()
    calls: list[int] = []
    real = PasswordHasher.verify

    def counting(self: PasswordHasher, stored: Any, password: Any) -> Any:
        calls.append(1)
        return real(self, stored, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)

    stale = await login(auth_env, who)
    unknown = await auth_env.client.post(
        "/api/v1/auth/login",
        headers=browser(),
        json={"email": "nobody@test.local", "password": PASSWORD},
    )

    assert stale.status_code == 401, stale.text
    assert stale.json() == unknown.json()
    assert len(calls) == 2
    async with AsyncSession(auth_env.admin_engine) as session:
        counted = (
            await session.execute(select(AuthFailure).where(AuthFailure.user_id == who.user_id))
        ).scalars().all()
    assert counted == []


async def test_the_login_is_refused_without_the_console_header_or_its_origin(
    auth_env: Any,
) -> None:
    """FALSIFIER 7, the login half: Task 6's rule covers `/api/v1/auth/` with
    no cookie at all (login CSRF), and this route is behind it."""
    org = await auth_env.create_org("csrf")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)
    body = {"email": who.email, "password": who.password}
    forged = [
        {"Origin": CONSOLE},
        {"X-ITMS-Request": "1", "Origin": "null"},
        {"X-ITMS-Request": "1"},
        {"X-ITMS-Request": "1", "Origin": "https://evil.test"},
    ]
    for headers in forged:
        response = await auth_env.client.post("/api/v1/auth/login", headers=headers, json=body)
        assert response.status_code == 403, (headers, response.text)
        assert response.json()["code"] == "csrf"
        assert cookie(response, SESSION_COOKIE) is None


async def test_one_address_is_slowed_down_and_another_is_not(auth_env: Any) -> None:
    """The per-IP limit of `api/v1/auth.py::_LOGIN` (by default 30 in 10 minutes)."""
    org = await auth_env.create_org("ip-limit")
    await point_at(auth_env, org)
    noisy = browser()
    body = {"email": "nobody@test.local", "password": "Not-The-Password-41"}
    for _ in range(30):
        answer = await auth_env.client.post("/api/v1/auth/login", headers=noisy, json=body)
        assert answer.status_code == 401
    limited = await auth_env.client.post("/api/v1/auth/login", headers=noisy, json=body)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert limited.json()["wait_s"] == 600
    assert limited.headers["Retry-After"] == "600"
    other = await auth_env.client.post("/api/v1/auth/login", headers=browser(), json=body)
    assert other.status_code == 401


# --------------------------------------------------------------------------- #
# The two per-IP sign-in limits are the operator's (decision of 2026-11-06,
# «Hacerlo configurable», design §2.3): `ITS_AUTH_LOGIN_IP_LIMIT` and
# `ITS_AUTH_FACTOR_IP_LIMIT`, 30 by default, 5 to 1000, read by `_limit` at
# request time from `app.state.settings`. What is counted -- every request,
# successes included -- and the 600 s window do not change, and neither do the
# bucket names, so the Redis keys a running deployment holds stay its own.
# --------------------------------------------------------------------------- #

#: Each limiter: a request it guards that answers the same 401 every time (an
#: unknown address's password; a code with no session waiting for it), the
#: setting that sizes it and its bucket.
_IP_LIMITED = [
    pytest.param(
        "/api/v1/auth/login",
        {"email": "nobody@test.local", "password": "Not-The-Password-41"},
        "auth_login_ip_limit",
        "auth-login",
        id="login",
    ),
    pytest.param(
        "/api/v1/auth/factor/totp",
        {"code": "000000", "remember_device": False},
        "auth_factor_ip_limit",
        "auth-factor",
        id="factor",
    ),
]
_IP_LIMITED_ARGS = ("path", "body", "setting", "bucket")

#: What `Settings` needs to construct at all, so a refusal below can only be
#: the limit's.
_REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://app_user:app@localhost/x",
    "database_admin_url": "postgresql+asyncpg://admin:a@localhost/x",
    "oidc_issuer": "https://kc.example/realms/it-suite",
    "oidc_jwks_url": "https://kc.example/realms/it-suite/certs",
    "ca_dir": Path("/tmp/ca"),
}


async def _knock(
    env: Any, path: str, body: dict[str, Any], network: dict[str, str], times: int
) -> list[int]:
    """`times` requests to `path` from one address; their status codes."""
    statuses = []
    for _ in range(times):
        answer = await env.client.post(path, headers=network, json=body)
        statuses.append(answer.status_code)
    return statuses


def _slowed_down(answer: Any) -> None:
    """The per-IP refusal, with the window it always had."""
    assert answer.status_code == 429, answer.text
    assert answer.json()["code"] == "rate_limited"
    assert answer.json()["wait_s"] == 600
    assert answer.headers["Retry-After"] == "600"


@pytest.mark.parametrize(_IP_LIMITED_ARGS, _IP_LIMITED)
async def test_by_default_thirty_from_one_address_pass_and_the_next_is_slowed_down(
    auth_env: Any, path: str, body: dict[str, Any], setting: str, bucket: str
) -> None:
    """The default is the setting's and is the 30 `_limit` was given before,
    counted under the bucket's existing Redis key -- so a deployment that sets
    nothing sees no change. Another address keeps a budget of its own."""
    assert getattr(auth_env.settings, setting) == 30
    org = await auth_env.create_org("ip-default")
    await point_at(auth_env, org)
    noisy = browser()

    assert await _knock(auth_env, path, body, noisy, 30) == [401] * 30
    _slowed_down(await auth_env.client.post(path, headers=noisy, json=body))

    address = ipaddress.ip_address(noisy["X-Forwarded-For"])
    assert await auth_env.app.state.redis.get(f"ratelimit:{bucket}:ip:{address}") == "31"
    other = await auth_env.client.post(path, headers=browser(), json=body)
    assert other.status_code == 401, other.text


@pytest.mark.parametrize(_IP_LIMITED_ARGS, _IP_LIMITED)
async def test_a_lowered_limit_is_honoured(
    auth_env: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
    setting: str,
    bucket: str,
) -> None:
    """`ITS_AUTH_*_IP_LIMIT=5`: the sixth request from one address is refused,
    with the same window. A limiter that kept its own 30, or read the other
    route's setting, lets it through: red."""
    monkeypatch.setattr(auth_env.settings, setting, 5)
    org = await auth_env.create_org("ip-lowered")
    await point_at(auth_env, org)
    noisy = browser()

    assert await _knock(auth_env, path, body, noisy, 5) == [401] * 5
    _slowed_down(await auth_env.client.post(path, headers=noisy, json=body))


@pytest.mark.parametrize(_IP_LIMITED_ARGS, _IP_LIMITED)
async def test_a_raised_limit_is_honoured(
    auth_env: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
    setting: str,
    bucket: str,
) -> None:
    """`ITS_AUTH_*_IP_LIMIT=40` -- an office behind one NAT address: the 31st
    request passes, which the hard-coded 30 refused, and the 41st does not."""
    monkeypatch.setattr(auth_env.settings, setting, 40)
    org = await auth_env.create_org("ip-raised")
    await point_at(auth_env, org)
    noisy = browser()

    assert await _knock(auth_env, path, body, noisy, 40) == [401] * 40
    _slowed_down(await auth_env.client.post(path, headers=noisy, json=body))


@pytest.mark.parametrize("value", [4, 1001, 0])
@pytest.mark.parametrize("setting", ["auth_login_ip_limit", "auth_factor_ip_limit"])
def test_a_limit_outside_five_to_a_thousand_stops_startup(setting: str, value: int) -> None:
    """5 to 1000, refused rather than clamped: the floor keeps the limit from
    being switched off, and `create_app` builds `Settings()`, so the api does
    not start. The only error is this field's."""
    with pytest.raises(ValidationError) as refused:
        Settings(**_REQUIRED, **{setting: value})
    assert [error["loc"] for error in refused.value.errors()] == [(setting,)]


@pytest.mark.parametrize(
    ("setting", "variable"),
    [
        ("auth_login_ip_limit", "ITS_AUTH_LOGIN_IP_LIMIT"),
        ("auth_factor_ip_limit", "ITS_AUTH_FACTOR_IP_LIMIT"),
    ],
)
def test_the_limits_come_from_their_variables_within_their_bounds(
    monkeypatch: pytest.MonkeyPatch, setting: str, variable: str
) -> None:
    """The names `deploy/.env.example` documents, both ends of the range, and
    an out-of-range value in `.env` stopping startup."""
    monkeypatch.delenv(variable, raising=False)
    assert getattr(Settings(**_REQUIRED), setting) == 30
    for value in (5, 1000):
        monkeypatch.setenv(variable, str(value))
        assert getattr(Settings(**_REQUIRED), setting) == value
    monkeypatch.setenv(variable, "4")
    with pytest.raises(ValidationError) as refused:
        Settings(**_REQUIRED)
    assert [error["loc"] for error in refused.value.errors()] == [(setting,)]


async def test_a_code_is_not_taken_twice(auth_env: Any) -> None:
    """FALSIFIER 3, the route half: the code that confirmed the enrolment is
    refused when replayed (`verify_totp`'s `last_step`, Task 4)."""
    org = await auth_env.create_org("replay")
    who = await signed_in(auth_env, org, "technician")
    response = await auth_env.client.post(
        "/api/v1/auth/step-up/totp", headers=who.headers(), json={"code": who.last_code}
    )
    # 400, not 401: a step-up is asked from inside the console (decision 14).
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "factor_failed"


async def test_a_factor_in_its_wait_says_how_long_in_the_body_and_the_header(
    auth_env: Any,
) -> None:
    """Design §2.3 after the password: the wait is told (decision 5), as
    `wait_s` for the console and `Retry-After` for anything else. Without
    `extra=` in `factors.gate` the body has no `wait_s`."""
    org = await auth_env.create_org("wait")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)
    async with AsyncSession(auth_env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="totp",
                consecutive=8,
                last_failed_at=datetime.now(UTC),
            )
        )
        await session.commit()

    waiting = await _factor(auth_env, who)

    assert waiting.status_code == 429, waiting.text
    body = waiting.json()
    assert body["code"] == "factor_wait"
    assert isinstance(body["wait_s"], int) and body["wait_s"] > 0
    assert waiting.headers["Retry-After"] == str(body["wait_s"])


async def test_a_burst_of_wrong_codes_is_counted_one_attempt_at_a_time(auth_env: Any) -> None:
    """The one-transaction rule (Task 4's `failures.check_allowed` takes an
    advisory lock held to the caller's commit): `factors.gate`, the
    verification and `count_failure` run in ONE transaction per attempt, so
    concurrent attempts are serialised and the wait that the first failure
    starts already stops the second. Commit between the check and the count
    (`gate` in a transaction of its own) and the burst is verified and counted
    many times over -- more than one 401, a count above 9: red."""
    org = await auth_env.create_org("burst")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)
    assert who.totp_secret is not None
    async with AsyncSession(auth_env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="totp",
                consecutive=8,
                last_failed_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()
    wrong = wrong_code(who.totp_secret)

    async def attempt() -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/factor/totp",
            headers=who.headers(),
            json={"code": wrong, "remember_device": False},
        )

    answers = await asyncio.gather(*(attempt() for _ in range(6)))

    assert sorted(a.status_code for a in answers) == [401] + [429] * 5, [a.text for a in answers]
    async with AsyncSession(auth_env.admin_engine) as session:
        failure = (
            await session.execute(
                select(AuthFailure).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "totp"
                )
            )
        ).scalar_one()
    assert failure.consecutive == 9


async def test_with_remembering_off_the_factor_step_says_null(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 15: `remember_device_days` is null, not absent and not 15,
    when the organization does not remember devices."""
    org = await auth_env.create_org("no-remember")
    who = await signed_in(auth_env, org, "technician")

    async def off(session: Any, org_id: Any) -> SessionPolicy:
        return SessionPolicy(remember_device_enabled=False)

    monkeypatch.setattr(flows, "read_policy", off)
    who.session = None
    step = await login(auth_env, who)

    assert step.json() == {
        "state": "factor_required",
        "methods": ["totp", "recovery"],
        "remember_device_days": None,
    }


async def test_the_hundredth_failure_locks_the_factor_and_tells_the_person(
    auth_env: Any,
) -> None:
    """FALSIFIER 3, the lockout: Task 4 disables and audits it; this task's
    `factors.count_failure` mails the person and the next step says so."""
    org = await auth_env.create_org("lockout")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)
    assert who.totp_secret is not None
    async with AsyncSession(auth_env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="totp",
                consecutive=99,
                last_failed_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

    failed = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": wrong_code(who.totp_secret), "remember_device": False},
    )
    right = await _factor(auth_env, who)

    assert failed.status_code == 401
    assert right.status_code == 403 and right.json()["code"] == "factor_disabled"
    locked = await _audit(auth_env, org, "auth.factor_disabled")
    assert [(e.actor_type, e.meta) for e in locked] == [
        ("system", {"factor": "totp", "consecutive": 100})
    ]
    assert auth_env.mail.sent[-1].to == who.email
    assert "locked" in auth_env.mail.sent[-1].subject


async def test_a_recovery_code_signs_in_once_and_says_so(auth_env: Any) -> None:
    org = await auth_env.create_org("recovery")
    who = await signed_in(auth_env, org, "technician")
    code = who.recovery_codes[0]
    who.session = None
    await login(auth_env, who)

    used = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": code, "remember_device": False},
    )
    assert used.status_code == 200, used.text
    who.session = None
    await login(auth_env, who)
    again = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": code, "remember_device": False},
    )

    assert again.status_code == 401
    events = await _audit(auth_env, org, "auth.recovery_code_used")
    assert [e.meta for e in events] == [{"left": 9}]
    assert any(m.to == who.email and "recovery" in m.subject.lower() for m in auth_env.mail.sent)


async def test_a_passkey_signs_in_only_as_its_own_person(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`flows._passkey_matches` checks the passkey's owner, and hands Task 5
    the console's own origin and host (`csrf.console_origin`/`console_host`)."""
    org = await auth_env.create_org("passkey")
    who = await signed_in(auth_env, org, "technician")
    other = await signed_in(auth_env, org, "technician")
    mine = await add_passkey(auth_env, who)
    theirs = await add_passkey(auth_env, other)
    offered: list[str] = []

    async def options(
        session: Any, user_id: Any, *, org_id: Any, rp_id: str, purpose: str
    ) -> dict[str, Any]:
        offered.append(f"{rp_id}:{purpose}")
        return {"challenge": "Y2hhbGxlbmdl"}

    async def verify(
        session: Any, credential: dict[str, Any], *, expected_origin: str, rp_id: str, purpose: str
    ) -> AuthPasskey:
        assert (expected_origin, rp_id, purpose) == (CONSOLE, "itms.test", "authenticate")
        wanted = mine if credential["id"] == "mine" else theirs
        return (
            await session.execute(select(AuthPasskey).where(AuthPasskey.id == wanted))
        ).scalar_one()

    monkeypatch.setattr(flows, "authentication_options", options)
    monkeypatch.setattr(flows, "verify_authentication", verify)
    who.session = None
    step = await login(auth_env, who)
    assert step.json()["methods"] == ["passkey", "totp", "recovery"]

    asked = await auth_env.client.post(
        "/api/v1/auth/factor/passkey/options", headers=who.headers()
    )
    wrong = await auth_env.client.post(
        "/api/v1/auth/factor/passkey",
        headers=who.headers(),
        json={"credential": {"id": "theirs"}, "remember_device": False},
    )
    right = await auth_env.client.post(
        "/api/v1/auth/factor/passkey",
        headers=who.headers(),
        json={"credential": {"id": "mine"}, "remember_device": False},
    )

    assert asked.status_code == 200 and asked.json() == {"challenge": "Y2hhbGxlbmdl"}
    assert offered == ["itms.test:authenticate"]
    assert wrong.status_code == 401 and wrong.json()["code"] == "factor_failed"
    assert right.status_code == 200 and right.json() == {"state": "signed_in"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _client_data(kind: str, challenge: str) -> str:
    """A `clientDataJSON` naming `challenge` (base64url, as the options carry
    it) from the console's own origin: enough for Task 5 to find and spend the
    challenge before the rest of the response is refused."""
    payload = {"type": kind, "challenge": challenge, "origin": CONSOLE, "crossOrigin": False}
    return _b64url(json.dumps(payload).encode())


async def _challenges(env: Any, who: Any, purpose: str) -> list[AuthWebauthnChallenge]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuthWebauthnChallenge).where(
                        AuthWebauthnChallenge.user_id == who.user_id,
                        AuthWebauthnChallenge.purpose == purpose,
                    )
                )
            ).scalars()
        )


async def test_a_refused_passkey_enrolment_spends_its_challenge(auth_env: Any) -> None:
    """Task 5's contract, held by `flows.enroll_passkey` against the REAL
    ceremony code: the challenge is spent in the caller's transaction, so the
    400 is raised only after the block commits. Raise `passkey_rejected` from
    inside the block and the rollback leaves the challenge live -- the same
    response could be tried again: red."""
    org = await auth_env.create_org("passkey-enrol")
    who = await staff(auth_env, org, "technician")
    await point_at(auth_env, org)
    assert (await login(auth_env, who)).json() == {"state": "enroll_required"}
    options = await auth_env.client.post(
        "/api/v1/auth/enroll/passkey/options", headers=who.headers()
    )
    assert options.status_code == 200, options.text
    credential = {
        "id": "AAAA",
        "rawId": "AAAA",
        "type": "public-key",
        "response": {
            "clientDataJSON": _client_data("webauthn.create", options.json()["challenge"]),
            "attestationObject": _b64url(b"not an attestation"),
        },
    }

    refused = await auth_env.client.post(
        "/api/v1/auth/enroll/passkey",
        headers=who.headers(),
        json={"credential": credential, "name": "Laptop"},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "passkey_rejected"
    assert [c.used_at is not None for c in await _challenges(auth_env, who, "register")] == [True]


async def test_a_refused_passkey_answer_is_counted_and_spends_its_challenge(
    auth_env: Any,
) -> None:
    """The sign-in half, against the REAL ceremony code (Task 5): a refused
    assertion is one counted `passkey` failure and its challenge stays spent,
    because `flows._passkey_matches` catches `PasskeyRejected` inside the block
    and the refusal follows the commit. The same response again is refused on
    the spent challenge, and counted again."""
    org = await auth_env.create_org("passkey-refused")
    who = await signed_in(auth_env, org, "technician")
    await add_passkey(auth_env, who)
    who.session = None
    await login(auth_env, who)
    options = await auth_env.client.post(
        "/api/v1/auth/factor/passkey/options", headers=who.headers()
    )
    assert options.status_code == 200, options.text
    raw_id = options.json()["allowCredentials"][0]["id"]
    credential = {
        "id": raw_id,
        "rawId": raw_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": _client_data("webauthn.get", options.json()["challenge"]),
            "authenticatorData": _b64url(bytes(37)),
            "signature": _b64url(bytes(64)),
        },
    }

    answers = [
        await auth_env.client.post(
            "/api/v1/auth/factor/passkey",
            headers=who.headers(),
            json={"credential": credential, "remember_device": False},
        )
        for _ in range(2)
    ]

    assert [(a.status_code, a.json()["code"]) for a in answers] == [(401, "factor_failed")] * 2
    assert [c.used_at is not None for c in await _challenges(auth_env, who, "authenticate")] == [
        True
    ]
    async with AsyncSession(auth_env.admin_engine) as session:
        failure = (
            await session.execute(
                select(AuthFailure).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "passkey"
                )
            )
        ).scalar_one()
    assert failure.consecutive == 2


async def test_a_person_with_a_factor_cannot_enrol_another_with_the_password_alone(
    auth_env: Any,
) -> None:
    """Decision 6, both halves: `flows._enrollable` and the `auth:enrolled:`
    flag `flows.enroll_finish` requires."""
    org = await auth_env.create_org("no-enrol")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)

    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    finish = await auth_env.client.post("/api/v1/auth/enroll/finish", headers=who.headers())

    assert start.status_code == 403 and start.json()["code"] == "factor_required"
    assert finish.status_code == 409 and finish.json()["code"] == "enroll_required"
    assert cookie(finish, SESSION_COOKIE) is None
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401


async def test_a_remembered_device_skips_the_factor_but_not_the_recent_confirmation(
    auth_env: Any,
) -> None:
    """Design §2.6 and FALSIFIER 5: the device signs in without the factor;
    the password change still asks for it; the change forgets the device."""
    org = await auth_env.create_org("device")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)
    done = await _factor(auth_env, who, remember=True)
    who.session, who.device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    assert who.device
    remembered = await _audit(auth_env, org, "auth.device_remembered")
    assert [e.meta for e in remembered] == [{"days": 15}]

    who.session = None
    by_device = await login(auth_env, who)
    assert by_device.json() == {"state": "signed_in"}
    new_password = "Pelican-Orbit-Lantern-58"
    body = {"current": who.password, "new": new_password}
    refused = await auth_env.client.put("/api/v1/me/password", headers=who.headers(), json=body)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"

    confirm = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert confirm.status_code == 204, confirm.text
    changed = await auth_env.client.put("/api/v1/me/password", headers=who.headers(), json=body)
    assert changed.status_code == 204, changed.text
    # The change ends the OTHER sessions; the caller's, which the device
    # started, is detached from it and stays (`account.change_password`),
    # under a new identifier the answer sets (final review B-I1).
    who.session = cookie(changed, SESSION_COOKIE)
    me = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert me.status_code == 200, me.text

    # A new sign-in from this browser: the device is forgotten, so the factor.
    who.session, who.password = None, new_password
    after = await login(auth_env, who)
    assert after.json()["state"] == "factor_required"


async def test_signing_out_ends_this_session_and_everywhere_ends_them_all(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("logout")
    who = await signed_in(auth_env, org, "technician")
    first = who.session
    who.session = None
    await login(auth_env, who)
    other = cookie(await _factor(auth_env, who), SESSION_COOKIE)

    who.session = first
    out = await auth_env.client.post("/api/v1/auth/logout", headers=who.headers())
    assert out.status_code == 204
    cleared = out.headers.get_list("set-cookie")
    assert any(h.startswith(f'{SESSION_COOKIE}="";') for h in cleared), cleared
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401
    # Only that one: the other browser is still signed in.
    who.session = other
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 200

    everywhere = await auth_env.client.post(
        "/api/v1/auth/logout-everywhere", headers=who.headers()
    )
    assert everywhere.status_code == 204
    async with AsyncSession(auth_env.admin_engine) as session:
        still = (
            await session.execute(
                select(AuthSession.id).where(
                    AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
                )
            )
        ).scalars().all()
    assert still == []
    ended = await _audit(auth_env, org, "auth.sessions_ended")
    assert [e.meta for e in ended] == [{"by": "self", "scope": "everywhere"}]


async def test_signing_out_of_an_idle_session_ends_it(auth_env: Any) -> None:
    """Final review B-M1. A person comes back to a tab left open past the idle
    limit and presses "Sign out": `resolve` no longer admits that session,
    but the row the cookie names is ended all the same -- otherwise it stays
    resumable with the password alone (and a copy of the cookie) until its
    maximum. Look the row up with `resolve` again and it is not ended: red."""
    org = await auth_env.create_org("logout-idle")
    who = await signed_in(auth_env, org, "technician")
    idle = who.session
    assert idle
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None))
            .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=31))
        )
        await session.commit()
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401

    out = await auth_env.client.post("/api/v1/auth/logout", headers=who.headers())

    assert out.status_code == 204
    async with AsyncSession(auth_env.admin_engine) as session:
        rows = (
            await session.execute(
                select(AuthSession.ended_reason).where(AuthSession.user_id == who.user_id)
            )
        ).scalars().all()
    assert rows and all(reason is not None for reason in rows), rows
    assert "signed_out" in rows
    # The password alone no longer resumes it: the next sign-in asks for the factor.
    who.session = idle
    assert (await login(auth_env, who)).json()["state"] == "factor_required"


async def test_signing_out_with_no_session_or_a_dead_one_is_the_same_204(auth_env: Any) -> None:
    for jar in ({}, {"Cookie": f"{SESSION_COOKIE}={'A' * 43}"}):
        out = await auth_env.client.post("/api/v1/auth/logout", headers={**browser(), **jar})
        assert out.status_code == 204, out.text


async def test_no_sign_in_answer_carries_a_secret(
    auth_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the cookies carry the session; the bodies, the audit and the
    platform's own log lines carry no code, hash or token (Global
    Constraints). A log line that names one -- the code, the cookie, the
    password -- turns the last loop red."""
    caplog.set_level(logging.DEBUG, logger="app")
    org = await auth_env.create_org("no-secret")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    step = await login(auth_env, who)
    done = await _factor(auth_env, who, remember=True)
    code = who.last_code
    token, device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    assert token and device and code
    for body in (step.text, done.text):
        for secret in (token, device, code, who.password):
            assert secret not in body
    async with AsyncSession(auth_env.admin_engine) as session:
        metas = [
            str(e.meta)
            for e in (
                await session.execute(select(AuditEvent).where(AuditEvent.org_id == org))
            ).scalars()
        ]
    for meta in metas:
        for secret in (token, device, code, who.password, *who.recovery_codes):
            assert secret not in meta
    assert who.totp_secret is not None
    for secret in (token, device, code, who.password, who.totp_secret, *who.recovery_codes):
        assert secret not in caplog.text


async def test_an_over_long_secret_is_refused_as_a_wrong_one_and_never_repeated(
    auth_env: Any,
) -> None:
    """Decision 16. A password or a code in a request body has no Pydantic
    bound: a field failure repeats its INPUT in the 422, and the input is the
    secret. The flow bounds it and answers as for a wrong one --
    `invalid_credentials` for the password (`staff_password.SUBMITTED_MAX`,
    held in `flows.login`), `factor_failed` for a code (Task 4's `verify_totp`
    and `recovery.normalise` drop spaces and refuse any other shape). Put a
    `max_length` back on `LoginIn.password`, or a `pattern` or a length on
    `CodeIn.code` / `RecoveryIn.code`, and the 422 repeats the secret: red. A
    code typed with a space still signs in, which `^[0-9]{6}$` refused."""
    org = await auth_env.create_org("signin-long")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    long_password = who.password + "x" * 2000
    long_code = "9" * 2000
    long_recovery = "Z" * 2000

    refused = await auth_env.client.post(
        "/api/v1/auth/login",
        headers=who.headers(),
        json={"email": who.email, "password": long_password},
    )
    step = await login(auth_env, who)
    by_code = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": long_code, "remember_device": False},
    )
    by_recovery = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": long_recovery, "remember_device": False},
    )
    code = await fresh_code(auth_env, who)
    spaced = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": f"{code[:3]} {code[3:]}", "remember_device": False},
    )

    assert refused.status_code == 401, refused.text
    assert refused.json()["code"] == "invalid_credentials"
    assert long_password not in refused.text and who.password not in refused.text
    assert step.json()["state"] == "factor_required", step.text
    for response, secret in ((by_code, long_code), (by_recovery, long_recovery)):
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "factor_failed"
        assert secret not in response.text
    assert spaced.status_code == 200, spaced.text
    assert spaced.json() == {"state": "signed_in"}

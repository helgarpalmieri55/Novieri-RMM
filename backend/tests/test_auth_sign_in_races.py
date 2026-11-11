"""A person's OWN sign-in steps against an administrator's act on that person
(own authentication phase 2, Task 9b, item A).

THE LOCK ORDER OF A PERSON'S ACCOUNT is THEIR `users` ROW (FOR NO KEY
UPDATE), THEN THEIR LINKS, SESSIONS AND CREDENTIALS, THEN THE AUDIT CHAIN.
Tasks 8 and 9 put the administrator's acts in that order -- a contact's
access reset (`contact_access._contact`), the archive's contact cut
(`_lock_contacts`, then `_cut_access`), a technician's deletion
(`user_admin.service._target(lock=True)`) -- and every use of a person's
link (`flows._consumed`). The person's own sign-in steps took the other
order: a session, a credential, a link's row or the audit chain first, and
`users.last_login_at` last (`sessions.mark_factor`, `start_session`). Each
pair below is one of those meetings, which Postgres broke with a 40P01: one
side aborted, a 500.

Each test holds the administrator's act at a point named, inside its
transaction, AFTER it has locked the person's row; starts the person's step;
waits until the step is SEEN waiting on the act (`tests/race_helpers.py`);
lets the act finish; and checks both outcomes, then WHERE the step waited:
on the person's row, before it took anything else -- so the act never waits
on the step, and no cycle can form.
"""

import asyncio
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.passkeys import user_handle
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthExternalIdentity, AuthPasskey, AuthSession
from app.modules.user_admin import service as ua
from app.services import clients as clients_service
from app.services import contact_access
from tests.auth_support import (
    Staff,
    browser,
    capture_mail,
    console_ready,
    contact,
    fresh_code,
    login,
    point_at,
    signed_in,
)
from tests.entra_support import FakeMicrosoft
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import CONSOLE
from tests.test_auth_entra_flows import (
    OTHER_TENANT,
    _linked,
    _lists_other_tenant,
    _org,
    _round_trip,
)
from tests.test_auth_passkey_signin import _register


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    api_env.microsoft = FakeMicrosoft()
    api_env.microsoft.install(monkeypatch, api_env.app)
    return api_env


def _pause(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str
) -> tuple[asyncio.Event, asyncio.Event, dict[str, int]]:
    """Hold the administrator's act at its call to `<module>.<name>`, inside
    its transaction: (paused, go, {"pid": the act's backend})."""
    paused, go = asyncio.Event(), asyncio.Event()
    seen: dict[str, int] = {}
    real: Callable[..., Awaitable[Any]] = getattr(module, name)

    async def held(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        if "pid" not in seen:
            seen["pid"] = await backend_pid(session)
            paused.set()
            await go.wait()
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(module, name, held)
    return paused, go, seen


def _on_the_person(waiting: list[str]) -> bool:
    """Every waiter waits on the person's `users` row, taken first FOR NO KEY
    UPDATE -- never on its own later write to it (`UPDATE users SET
    last_login_at`), which is what closed the cycle."""
    return bool(waiting) and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    )


async def _race(
    env: Any,
    go: asyncio.Event,
    paused: asyncio.Event,
    seen: dict[str, int],
    act: "asyncio.Task[Any]",
    step: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any, list[str], bool]:
    """The act is running and will pause; the step is started once it has;
    once the step is seen waiting on the act, the act goes on. Returns (the
    act's outcome, the step's, what the step waited in, whether it was still
    waiting when seen)."""
    await asyncio.wait_for(paused.wait(), 30)
    stepping = asyncio.create_task(step())
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not stepping.done()
    finally:
        go.set()
    acted, stepped = await asyncio.wait_for(
        asyncio.gather(act, stepping, return_exceptions=True), timeout=60
    )
    return acted, stepped, waiting, held


async def _admin(env: Any, org: UUID) -> tuple[UUID, dict[str, str]]:
    sub = await env.create_user(org, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
    assert user_id is not None
    return user_id, await env.headers(org, sub)


async def _live_sessions(env: Any, user_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as s:
        live = await s.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.ended_at.is_(None))
        )
    return int(live or 0)


def _reset_access(env: Any, who: Staff, headers: dict[str, str]) -> "asyncio.Task[Any]":
    return asyncio.create_task(
        env.client.post(
            f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/access/reset",
            headers=headers,
        )
    )


async def _contact_waiting_for_totp(env: Any, org: UUID) -> Staff:
    """A contact who turned the authenticator app on (C5: their second factor),
    signed in again with the password: a session waiting for the code."""
    who = await contact(env, org)
    await point_at(env, org)
    first = await login(env, who)
    assert first.json() == {"state": "signed_in"}, first.text
    start = await env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    who.totp_secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirmed = await env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(who.totp_secret).now()},
    )
    assert confirmed.status_code == 204, confirmed.text
    await env.client.post("/api/v1/auth/logout", headers=who.headers())
    who.session = None
    again = await login(env, who)
    assert again.json()["state"] == "factor_required", again.text
    return who


async def test_a_totp_step_against_a_contacts_access_reset(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 1. The reset holds the contact's row and is about to wipe their
    factors. The code step used to write the TOTP row and its own session,
    then wait on the contact's row for `last_login_at`; the reset then waited
    on the TOTP row it deletes. Now the step waits on the row before
    anything, the reset finishes, and the step finds its session ended."""
    org = await auth_env.create_org("race-totp-reset")
    who = await _contact_waiting_for_totp(auth_env, org)
    code = await fresh_code(auth_env, who)
    _actor, admin = await _admin(auth_env, org)
    paused, go, seen = _pause(monkeypatch, contact_access, "lock_person")
    resetting = _reset_access(auth_env, who, admin)

    async def step() -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/factor/totp", headers=who.headers(), json={"code": code}
        )

    reset, factor, waiting, held = await _race(auth_env, go, paused, seen, resetting, step)
    assert not isinstance(reset, BaseException), reset
    assert not isinstance(factor, BaseException), factor
    assert reset.status_code == 200, reset.text
    assert factor.status_code == 401, factor.text
    assert factor.json()["code"] == "auth_required"
    assert held
    assert _on_the_person(waiting), waiting
    assert await _live_sessions(auth_env, who.user_id) == 0


async def test_a_totp_step_against_a_technicians_deletion(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 2. The deletion holds the technician's row and is about to end
    their sessions. The code step used to mark its own session done and wait
    on the row for `last_login_at`; the deletion then waited on that session.
    Now the step waits on the row first and finds the account deleted."""
    org = await auth_env.create_org("race-totp-delete")
    admin_id, _headers = await _admin(auth_env, org)
    tech = await signed_in(auth_env, org, "technician")
    await auth_env.client.post("/api/v1/auth/logout", headers=tech.headers())
    tech.session = None
    assert (await login(auth_env, tech)).json()["state"] == "factor_required"
    code = await fresh_code(auth_env, tech)
    paused, go, seen = _pause(monkeypatch, ua, "end_all_for_user")

    async def remove() -> str:
        async with org_session(org, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
            await ua.delete_user(s, org_id=org, actor_id=admin_id, user_id=tech.user_id)
        return "delete committed"

    async def step() -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/factor/totp", headers=tech.headers(), json={"code": code}
        )

    deleted, factor, waiting, held = await _race(
        auth_env, go, paused, seen, asyncio.create_task(remove()), step
    )
    assert deleted == "delete committed", deleted
    assert not isinstance(factor, BaseException), factor
    assert factor.status_code == 401, factor.text
    assert factor.json()["code"] == "auth_required"
    assert held
    assert _on_the_person(waiting), waiting
    assert await _live_sessions(auth_env, tech.user_id) == 0


async def test_a_password_sign_in_against_an_archives_contact_cut(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 3. The archive holds the contact's row and is about to cut what
    they have open. The password sign-in used to audit (the chain) and then
    wait on the row for `last_login_at`; the archive then waited on the
    chain. Now the sign-in waits on the row before the password is even
    tried, and finds the contact switched off: the generic 401."""
    org = await auth_env.create_org("race-login-archive")
    who = await contact(auth_env, org)
    await point_at(auth_env, org)
    _actor, admin = await _admin(auth_env, org)
    paused, go, seen = _pause(monkeypatch, clients_service, "_cut_access")
    archiving = asyncio.create_task(
        auth_env.client.post(f"/api/v1/clients/{who.client_id}/archive", headers=admin)
    )

    async def step() -> Any:
        return await login(auth_env, who)

    archived, signed, waiting, held = await _race(auth_env, go, paused, seen, archiving, step)
    assert not isinstance(archived, BaseException), archived
    assert not isinstance(signed, BaseException), signed
    assert archived.status_code == 200, archived.text
    assert signed.status_code == 401, signed.text
    assert signed.json()["code"] == "invalid_credentials"
    assert held
    assert _on_the_person(waiting), waiting
    assert await _live_sessions(auth_env, who.user_id) == 0


async def test_a_passkey_sign_in_against_a_contacts_access_reset(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 4, with a real passkey. The reset holds the contact's row and is
    about to delete their passkeys. The passkey sign-in used to write the
    passkey's counter and then wait on the row for `last_login_at`; the
    reset then waited on the passkey row. Now the sign-in learns whose
    passkey the assertion names, waits on that person's row first, and finds
    the passkey gone: the same 401 as any refused assertion."""
    org = await auth_env.create_org("race-passkey-reset")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    authenticator = await _register(auth_env, who)
    assert authenticator.user_handle == user_handle(who.user_id)
    _actor, admin = await _admin(auth_env, org)
    options = await auth_env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    assertion = authenticator.get(options.json(), origin=CONSOLE)
    paused, go, seen = _pause(monkeypatch, contact_access, "lock_person")
    resetting = _reset_access(auth_env, who, admin)

    async def step() -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/passkey", headers=who.network, json={"credential": assertion}
        )

    reset, signed, waiting, held = await _race(auth_env, go, paused, seen, resetting, step)
    assert not isinstance(reset, BaseException), reset
    assert not isinstance(signed, BaseException), signed
    assert reset.status_code == 200, reset.text
    assert signed.status_code == 401, signed.text
    assert signed.json()["code"] == "passkey_rejected"
    assert held
    assert _on_the_person(waiting), waiting
    assert await _live_sessions(auth_env, who.user_id) == 0
    async with AsyncSession(auth_env.admin_engine) as s:
        keys = await s.scalar(
            select(func.count()).select_from(AuthPasskey).where(AuthPasskey.user_id == who.user_id)
        )
    assert keys == 0


async def test_a_microsoft_sign_in_against_a_contacts_access_reset(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 5, with the fake Microsoft. The reset holds the contact's row and
    is about to delete their Microsoft link. The sign-in used to write the
    link's `last_used_at`, audit, and wait on the row for `last_login_at`;
    the reset then waited on the link. Now the sign-in reads the link
    unlocked only to learn whose it is, waits on that person's row, reads
    the link again and finds it gone: the 401 of an account linked to
    nobody."""
    org, admin = await _org(auth_env)
    who = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, who.client_id)
    oid = uuid4()
    await _linked(auth_env, who, tenant=OTHER_TENANT, oid=oid)
    paused, go, seen = _pause(monkeypatch, contact_access, "lock_person")
    resetting = _reset_access(auth_env, who, admin)

    async def step() -> Any:
        return await _round_trip(
            auth_env, browser(), {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT
        )

    reset, signed, waiting, held = await _race(auth_env, go, paused, seen, resetting, step)
    assert not isinstance(reset, BaseException), reset
    assert not isinstance(signed, BaseException), signed
    assert reset.status_code == 200, reset.text
    assert signed.status_code == 401, signed.text
    assert signed.json()["code"] == "microsoft_not_linked"
    assert held
    assert _on_the_person(waiting), waiting
    assert await _live_sessions(auth_env, who.user_id) == 0
    async with AsyncSession(auth_env.admin_engine) as s:
        links = await s.scalar(
            select(func.count())
            .select_from(AuthExternalIdentity)
            .where(AuthExternalIdentity.user_id == who.user_id)
        )
    assert links == 0

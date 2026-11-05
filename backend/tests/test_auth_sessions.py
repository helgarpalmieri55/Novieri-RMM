"""Sessions, their policy and remembered devices (P2 design §2.4-§2.6, §6.4).

`app/core/auth/sessions.py`, `session_policy.py` and `devices.py` against the
real database (`auth_sessions`, `auth_trusted_devices`, `auth_org_settings`,
`auth_passwords`, Task 1) and `its_auth_session`. The door that uses them is
`tests/test_auth_session_door.py`; the CSRF rule, `tests/test_auth_csrf.py`.
Each refusal names the line it protects.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth.devices import (
    RememberingOff,
    recognised,
    remember,
    revoke,
    revoke_all_for_user,
)
from app.core.auth.session_policy import DEFAULT_POLICY, read_policy
from app.core.auth.sessions import (
    DEVICE_COOKIE,
    SESSION_COOKIE,
    STEP_UP_WINDOW,
    clear_device_cookie,
    clear_session_cookie,
    end,
    end_all_for_user,
    mark_factor,
    mark_step_up,
    resolve,
    resumable,
    set_device_cookie,
    set_session_cookie,
    start_session,
    step_up_fresh,
)
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthOrgSettings, AuthPassword, AuthSession, AuthTrustedDevice
from tests.session_support import (
    cookie_header,
    http_request,
    remembered_device,
    sign_in,
    user_id_of,
)

NOW_SLACK = timedelta(seconds=30)


async def _person(env: Any, *roles: str) -> tuple[UUID, str]:
    org_id = await env.create_org("sess")
    return org_id, await env.create_user(org_id, *roles)


async def _resolve(env: Any, token: str, *, touch: bool = True) -> Any:
    return await resolve(http_request(env, cookie=cookie_header(token)), touch=touch)


async def _set(env: Any, model: Any, row_id: UUID, **values: Any) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(update(model).where(model.id == row_id).values(**values))
        await s.commit()


async def _row(env: Any, session_id: UUID) -> AuthSession:
    async with AsyncSession(env.admin_engine) as s:
        query = select(AuthSession).where(AuthSession.id == session_id)
        return (await s.execute(query)).scalar_one()


async def _policy(env: Any, org_id: UUID, **values: Any) -> None:
    async with AsyncSession(env.admin_engine) as s:
        s.add(AuthOrgSettings(org_id=org_id, **values))
        await s.commit()


def _in_all(env: Any, org_id: UUID) -> Any:
    return org_session(org_id, engine=env.app_engine, clients=ClientScope.all())


# --- the cookie and the row ----------------------------------------------


async def test_a_session_is_kept_only_as_the_hash_of_its_cookie(api_env: Any) -> None:
    """Design §6.4 and the Global Constraints: 32 random bytes in the cookie,
    their SHA-256 in the row, the value itself nowhere in the database."""
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    row = await _row(api_env, signed.session_id)

    assert len(signed.token) == 43
    assert row.token_hash == hashlib.sha256(signed.token.encode()).digest()
    assert signed.token not in repr({c: getattr(row, c) for c in AuthSession.__table__.c.keys()})
    assert row.ip == "203.0.113.9" and row.user_agent == "pytest"


def _cookie_attributes(response: Response) -> tuple[str, set[str]]:
    [header] = response.headers.getlist("set-cookie")
    first, *rest = (part.strip() for part in header.split(";"))
    return first, {part.lower() for part in rest}


@pytest.mark.parametrize(
    ("setter", "name"),
    [(set_session_cookie, SESSION_COOKIE), (set_device_cookie, DEVICE_COOKIE)],
    ids=["session", "device"],
)
def test_each_cookie_is_host_only_secure_http_only_and_strict(setter: Any, name: str) -> None:
    """Design §6.4 and the Global Constraints: `__Host-` (Secure, Path=/ and
    no Domain, or the browser refuses it), HttpOnly (the page's JavaScript
    cannot read it), SameSite=Strict, and it expires with what it names.
    Protects `_set` in `sessions.py`."""
    token = "t" * 43
    response = Response()
    setter(response, token, expires_at=datetime.now(UTC) + timedelta(hours=1))
    first, attributes = _cookie_attributes(response)

    assert name.startswith("__Host-") and first == f"{name}={token}"
    assert {"httponly", "secure", "samesite=strict", "path=/"} <= attributes
    assert not any(a.startswith("domain") for a in attributes)
    [max_age] = [int(a.split("=", 1)[1]) for a in attributes if a.startswith("max-age=")]
    assert 3590 <= max_age <= 3600


@pytest.mark.parametrize(
    ("clear", "name"),
    [(clear_session_cookie, SESSION_COOKIE), (clear_device_cookie, DEVICE_COOKIE)],
    ids=["session", "device"],
)
def test_clearing_a_cookie_keeps_its_attributes(clear: Any, name: str) -> None:
    """A browser ignores a `__Host-` deletion that is not itself `Secure` with
    `Path=/`: the cookie would survive sign-out."""
    response = Response()
    clear(response)
    first, attributes = _cookie_attributes(response)
    assert first in (f"{name}=", f'{name}=""')
    assert {"httponly", "secure", "samesite=strict", "path=/", "max-age=0"} <= attributes


async def test_a_session_starts_from_an_admin_session_and_a_bare_request(api_env: Any) -> None:
    """Task 13's harness (`ApiEnv.headers`) and the recovery command build
    sessions this way: no tenant session, a request with no app or client."""
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        row, token = await start_session(
            s,
            user,
            kind="staff",
            request=Request({"type": "http", "headers": []}),
            factor_done=True,
            trusted_device_id=None,
        )
        await s.commit()
    assert row.ip == "" and row.user_agent == ""
    assert await _resolve(api_env, token) is not None


async def test_resolve_names_the_person_and_touches_at_most_once_a_minute(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    principal = await _resolve(api_env, signed.token)
    assert principal is not None
    assert principal.user_id == signed.user_id and principal.org_id == org_id
    assert principal.email == f"{sub}@test.local" and principal.factor_done is True

    two_minutes_ago = datetime.now(UTC) - timedelta(minutes=2)
    await _set(api_env, AuthSession, signed.session_id, last_seen_at=two_minutes_ago)
    await _resolve(api_env, signed.token, touch=False)
    assert (await _row(api_env, signed.session_id)).last_seen_at == two_minutes_ago
    await _resolve(api_env, signed.token)
    assert (await _row(api_env, signed.session_id)).last_seen_at > datetime.now(UTC) - NOW_SLACK


async def test_an_idle_session_stops_but_is_not_ended(api_env: Any) -> None:
    """Protects `now - last_seen_at > idle_limit` in `resolve`. Read per
    request from the policy: raising the limit brings the session back, and
    it was never ended -- the password alone may resume it (design §2.4)."""
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    await _set(
        api_env,
        AuthSession,
        signed.session_id,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=31),
    )

    assert await _resolve(api_env, signed.token) is None
    assert (await _row(api_env, signed.session_id)).ended_at is None
    await _policy(api_env, org_id, staff_idle_minutes=60)
    assert await _resolve(api_env, signed.token) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"ended_at": datetime.now(UTC), "ended_reason": "signed_out"},
    ],
    ids=["past-its-maximum", "ended"],
)
async def test_a_session_past_its_maximum_or_ended_is_not_resolved(
    api_env: Any, change: dict[str, Any]
) -> None:
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    await _set(api_env, AuthSession, signed.session_id, **change)
    assert await _resolve(api_env, signed.token) is None


async def test_a_disabled_person_has_no_session(api_env: Any) -> None:
    """Protects `row.status != "active"` in `resolve`: disabling somebody
    stops their sessions on the next request, even if nothing ended them
    (design §2.4)."""
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    await _set(api_env, User, signed.user_id, status="disabled")
    assert await _resolve(api_env, signed.token) is None


@pytest.mark.parametrize("cookie", ["", "short", "x" * 43 + "!", "A" * 44])
async def test_a_cookie_of_another_shape_names_no_session(api_env: Any, cookie: str) -> None:
    assert await resolve(http_request(api_env, cookie=f"{SESSION_COOKIE}={cookie}")) is None


async def test_the_maximum_follows_the_policy_and_the_device(api_env: Any) -> None:
    """12 hours by default, `remembered_max_hours` (168) on a remembered
    device, and whatever the organisation saved."""
    org_id, sub = await _person(api_env, "read-only")
    now = datetime.now(UTC)
    plain = await _row(api_env, (await sign_in(api_env, org_id, sub)).session_id)
    remembered = await _row(
        api_env,
        (
            await sign_in(
                api_env,
                org_id,
                sub,
                trusted_device_id=await remembered_device(api_env, org_id, sub),
            )
        ).session_id,
    )
    assert abs(plain.expires_at - (now + timedelta(hours=12))) < NOW_SLACK
    assert abs(remembered.expires_at - (now + timedelta(hours=168))) < NOW_SLACK

    await _policy(api_env, org_id, staff_max_hours=4)
    shorter = await _row(api_env, (await sign_in(api_env, org_id, sub)).session_id)
    assert abs(shorter.expires_at - (now + timedelta(hours=4))) < NOW_SLACK


async def test_the_code_defaults_are_the_tables(api_env: Any) -> None:
    """A row saved with nothing set reads back as `DEFAULT_POLICY`: the two
    statements of the defaults (Task 1's columns, `SessionPolicy`) agree."""
    org_id = await api_env.create_org("sess-defaults")
    await _policy(api_env, org_id)
    async with _in_all(api_env, org_id) as s:
        assert await read_policy(s, org_id) == DEFAULT_POLICY


async def test_a_new_sign_in_ends_the_session_the_browser_still_holds(api_env: Any) -> None:
    """Session fixation [S6]: protects the `replaced` update in
    `start_session`. A planted (or old) identifier never survives a sign-in."""
    org_id, sub = await _person(api_env, "read-only")
    old = await sign_in(api_env, org_id, sub)
    async with _in_all(api_env, org_id) as s:
        user = (await s.execute(select(User).where(User.id == old.user_id))).scalar_one()
        new, _ = await start_session(
            s,
            user,
            kind="staff",
            request=http_request(api_env, cookie=cookie_header(old.token)),
            factor_done=False,
            trusted_device_id=None,
        )
    previous = await _row(api_env, old.session_id)
    assert previous.ended_reason == "replaced" and previous.ended_at is not None
    assert new.id != old.session_id


async def test_confirming_the_factor_changes_the_identifier(api_env: Any) -> None:
    """Design §2.4: "El identificador de sesión cambia ... al confirmar el
    segundo factor". Protects the `token_hash=` of `mark_factor`."""
    org_id, sub = await _person(api_env, "read-only")
    half = await sign_in(api_env, org_id, sub, factor_done=False)
    before = await _resolve(api_env, half.token)
    assert before is not None and before.factor_done is False

    async with _in_all(api_env, org_id) as s:
        token = await mark_factor(s, half.session_id)

    assert token != half.token
    assert await _resolve(api_env, half.token) is None
    after = await _resolve(api_env, token)
    assert after is not None and after.factor_done is True
    assert after.step_up_at is not None
    async with AsyncSession(api_env.admin_engine) as s:
        last_login = (
            await s.execute(select(User.last_login_at).where(User.id == half.user_id))
        ).scalar_one()
    assert last_login is not None and last_login > datetime.now(UTC) - NOW_SLACK


async def test_a_remembered_device_is_not_a_recent_confirmation(api_env: Any) -> None:
    """Design §2.6: "las acciones de §2.5 siguen pidiendo el segundo factor".
    Protects `step_up_at = ... trusted_device_id is None` in `start_session`;
    `mark_step_up` makes it fresh for exactly `STEP_UP_WINDOW`."""
    org_id, sub = await _person(api_env, "read-only")
    signed = await sign_in(
        api_env, org_id, sub, trusted_device_id=await remembered_device(api_env, org_id, sub)
    )
    principal = await _resolve(api_env, signed.token)
    assert principal is not None and principal.factor_done is True
    assert step_up_fresh(principal, datetime.now(UTC)) is False

    async with _in_all(api_env, org_id) as s:
        await mark_step_up(s, signed.session_id)
    confirmed = await _resolve(api_env, signed.token)
    assert confirmed is not None and confirmed.step_up_at is not None
    assert step_up_fresh(confirmed, confirmed.step_up_at + STEP_UP_WINDOW) is True
    later = confirmed.step_up_at + STEP_UP_WINDOW + timedelta(seconds=1)
    assert step_up_fresh(confirmed, later) is False


async def test_an_idle_session_resumes_with_its_maximum_and_without_a_confirmation(
    api_env: Any,
) -> None:
    """NIST 800-63B-4 §2.2.3 [S1]: within the maximum, the password and the
    session secret are enough again. The resumed session keeps the old
    maximum and factor, and is NOT a recent confirmation."""
    org_id, sub = await _person(api_env, "read-only")
    old = await sign_in(api_env, org_id, sub)
    await _set(
        api_env, AuthSession, old.session_id, last_seen_at=datetime.now(UTC) - timedelta(hours=1)
    )
    request = http_request(api_env, cookie=cookie_header(old.token))
    async with _in_all(api_env, org_id) as s:
        user = (await s.execute(select(User).where(User.id == old.user_id))).scalar_one()
        idle = await resumable(s, request, user.id)
        assert idle is not None and idle.id == old.session_id
        new, token = await start_session(
            s, user, kind="staff", request=request, factor_done=True, trusted_device_id=None,
            resume=idle,
        )
        expected_max, expected_factor = idle.expires_at, idle.factor_at
    resumed = await _resolve(api_env, token)
    assert resumed is not None and resumed.step_up_at is None
    assert resumed.expires_at == expected_max
    assert (await _row(api_env, new.id)).factor_at == expected_factor
    assert (await _row(api_env, old.session_id)).ended_reason == "resumed"


async def test_only_ones_own_completed_session_is_resumable(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    other = await api_env.create_user(org_id, "read-only")
    half = await sign_in(api_env, org_id, sub, factor_done=False)
    full = await sign_in(api_env, org_id, sub)
    mine, theirs = await user_id_of(api_env, sub), await user_id_of(api_env, other)
    half_request = http_request(api_env, cookie=cookie_header(half.token))
    full_request = http_request(api_env, cookie=cookie_header(full.token))
    async with _in_all(api_env, org_id) as s:
        assert await resumable(s, half_request, mine) is None
        assert await resumable(s, full_request, theirs) is None
        assert await resumable(s, full_request, mine) is not None


async def test_ending_one_or_every_session(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    first, second, third = [await sign_in(api_env, org_id, sub) for _ in range(3)]
    async with _in_all(api_env, org_id) as s:
        assert await end(s, first.session_id, "signed_out") is True
        assert await end(s, first.session_id, "signed_out") is False
        ended = await end_all_for_user(
            s, first.user_id, "signed_out_everywhere", except_id=third.session_id
        )
    assert ended == 1
    assert (await _row(api_env, second.session_id)).ended_reason == "signed_out_everywhere"
    assert await _resolve(api_env, third.token) is not None


# --- remembered devices ----------------------------------------------------


async def _remember(env: Any, org_id: UUID, user_id: UUID) -> tuple[AuthTrustedDevice, str]:
    async with _in_all(env, org_id) as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        return await remember(s, user, http_request(env, user_agent="Firefox on Linux"))


async def _recognised(env: Any, org_id: UUID, user_id: UUID, token: str) -> Any:
    async with _in_all(env, org_id) as s:
        return await recognised(s, user_id, http_request(env, cookie=cookie_header(None, token)))


async def test_a_remembered_device_is_its_persons_alone(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    other = await user_id_of(api_env, await api_env.create_user(org_id, "read-only"))
    device, token = await _remember(api_env, org_id, user_id)

    assert device.token_hash == hashlib.sha256(token.encode()).digest()
    assert device.label == "Firefox on Linux"
    assert abs(device.expires_at - (datetime.now(UTC) + timedelta(days=15))) < NOW_SLACK
    assert (await _recognised(api_env, org_id, user_id, token)).id == device.id
    assert await _recognised(api_env, org_id, other, token) is None


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"revoked_at": datetime.now(UTC)},
    ],
    ids=["expired", "revoked"],
)
async def test_an_expired_or_revoked_device_is_not_recognised(
    api_env: Any, change: dict[str, Any]
) -> None:
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    device, token = await _remember(api_env, org_id, user_id)
    await _set(api_env, AuthTrustedDevice, device.id, **change)
    assert await _recognised(api_env, org_id, user_id, token) is None


async def test_switching_the_feature_off_stops_every_device_at_once(api_env: Any) -> None:
    """Protects the `remember_device_enabled` checks of `remember` and
    `recognised`: the setting decides now, not when the device was saved."""
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    _, token = await _remember(api_env, org_id, user_id)
    await _policy(api_env, org_id, remember_device_enabled=False)

    assert await _recognised(api_env, org_id, user_id, token) is None
    with pytest.raises(RememberingOff):
        await _remember(api_env, org_id, user_id)


async def test_a_password_change_forgets_every_device_remembered_before_it(
    api_env: Any,
) -> None:
    """FALSIFIER 5, the device half (design §10): protects the
    `changed_at >= device.created_at` comparison in `recognised`. It holds even
    if the password change forgot to call `revoke_all_for_user`."""
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    _, token = await _remember(api_env, org_id, user_id)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(
            AuthPassword(
                user_id=user_id,
                org_id=org_id,
                hash="not-a-real-hash",
                pepper_version=1,
                changed_at=datetime.now(UTC),
            )
        )
        await s.commit()
    assert await _recognised(api_env, org_id, user_id, token) is None
    _, after = await _remember(api_env, org_id, user_id)
    assert await _recognised(api_env, org_id, user_id, after) is not None


async def test_revoking_a_device_ends_the_sessions_it_started(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    device, token = await _remember(api_env, org_id, user_id)
    on_it = await sign_in(api_env, org_id, sub, trusted_device_id=device.id)
    elsewhere = await sign_in(api_env, org_id, sub)
    async with _in_all(api_env, org_id) as s:
        assert await revoke(s, user_id, device.id) is True
        assert await revoke(s, user_id, device.id) is False
    assert await _recognised(api_env, org_id, user_id, token) is None
    assert (await _row(api_env, on_it.session_id)).ended_reason == "device_revoked"
    assert await _resolve(api_env, elsewhere.token) is not None


async def test_revoking_every_device_of_a_person(api_env: Any) -> None:
    org_id, sub = await _person(api_env, "read-only")
    user_id = await user_id_of(api_env, sub)
    tokens = [(await _remember(api_env, org_id, user_id))[1] for _ in range(2)]
    async with _in_all(api_env, org_id) as s:
        assert await revoke_all_for_user(s, user_id) == 2
    for token in tokens:
        assert await _recognised(api_env, org_id, user_id, token) is None

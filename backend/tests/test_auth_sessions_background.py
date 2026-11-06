"""What counts as activity: a request the console made with no person behind it
does not renew the session (fix round 2, console re-review P1; design §2.4).

THE DEFECT. The console polls every 60 s (the shell's health signal, the
review count), and every HTTP door resolved the session with `touch=True`, so
`last_seen_at` moved every minute and an open, unattended console tab never
reached the 30-minute inactivity limit: the session lived to its 12-hour
maximum. NIST SP 800-63B-4 §5.2: "An inactivity timeout terminates a session
without activity from the subscriber for a specific period", and "Session
activity SHALL reset the inactivity timeout" -- a poll is not the subscriber's.

THE CONTRACT (coordinator decision). The console sends `X-ITMS-Background: 1`
on every request made without a person's input in the last 60 s. A request
whose header is exactly that is resolved with `touch=False`; any other value,
or none, touches as before. The header only DECLINES a renewal: it grants
nothing, and a door that already passes `touch=False` stays so.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.auth import guards, sessions
from app.core.auth.sessions import BACKGROUND_HEADER
from app.models.auth import AuthSession
from tests.session_support import cookie_header, sign_in

BACKGROUND = {"X-ITMS-Background": "1"}


async def _signed(env: Any) -> Any:
    org_id = await env.create_org("bg")
    return await sign_in(env, org_id, await env.create_user(org_id, "read-only"))


async def _last_seen(env: Any, session_id: UUID) -> datetime:
    async with AsyncSession(env.admin_engine) as s:
        seen: datetime = (
            await s.execute(select(AuthSession.last_seen_at).where(AuthSession.id == session_id))
        ).scalar_one()
        return seen


async def _seen_ago(env: Any, session_id: UUID, ago: timedelta) -> datetime:
    async with AsyncSession(env.admin_engine) as s:
        at = (
            await s.execute(select(AuthSession.last_seen_at).where(AuthSession.id == session_id))
        ).scalar_one() - ago
        await s.execute(
            update(AuthSession).where(AuthSession.id == session_id).values(last_seen_at=at)
        )
        await s.commit()
        return at


async def _minutes_of_polls(
    env: Any, monkeypatch: pytest.MonkeyPatch, signed: Any, minutes: int, headers: dict[str, str]
) -> list[int]:
    """One `GET /me` per minute for `minutes` minutes, the clock moved, as the
    shell's 60-second poll does. The answers' statuses, in order."""
    start = await _last_seen(env, signed.session_id)
    clock = [start]
    monkeypatch.setattr(sessions, "_now", lambda: clock[0])
    statuses = []
    for minute in range(1, minutes + 1):
        clock[0] = start + timedelta(minutes=minute)
        response = await env.client.get("/api/v1/me", headers={**signed.headers, **headers})
        statuses.append(response.status_code)
    return statuses


async def test_thirty_one_minutes_of_background_polls_end_the_session(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect's scenario. Each poll is admitted while the session is not
    yet idle, and none renews it: at minute 31 the 30-minute limit has passed
    and the poll is a 401. Let the header be ignored and every poll renews
    `last_seen_at`: 31 answers of 200, red."""
    signed = await _signed(api_env)

    statuses = await _minutes_of_polls(api_env, monkeypatch, signed, 31, BACKGROUND)

    assert statuses == [200] * 30 + [401]
    # Idle, not ended: the password alone may still resume it (design §2.4).
    async with AsyncSession(api_env.admin_engine) as s:
        ended = (
            await s.execute(select(AuthSession.ended_at).where(AuthSession.id == signed.session_id))
        ).scalar_one()
    assert ended is None
    after = await api_env.client.get("/api/v1/me", headers=signed.headers)
    assert after.status_code == 401


async def test_the_same_requests_without_the_header_keep_it_alive(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity, and the other half of the contract: any client that does
    not send the header is unchanged -- a request is activity."""
    signed = await _signed(api_env)

    statuses = await _minutes_of_polls(api_env, monkeypatch, signed, 31, {})

    assert statuses == [200] * 31


@pytest.mark.parametrize(
    "values", [["0"], ["true"], ["yes"], ["1", "1"]], ids=["0", "true", "yes", "twice"]
)
async def test_a_header_other_than_exactly_one_touches(api_env: Any, values: list[str]) -> None:
    """Exactly one header, exactly `1`: anything else is a request like any
    other, and renews the session."""
    signed = await _signed(api_env)
    before = await _seen_ago(api_env, signed.session_id, timedelta(minutes=2))

    response = await api_env.client.get(
        "/api/v1/me",
        headers=[*signed.headers.items(), *((BACKGROUND_HEADER, v) for v in values)],
    )

    assert response.status_code == 200, response.text
    assert await _last_seen(api_env, signed.session_id) > before


def _request(env: Any, token: str, *, background: bool) -> Request:
    headers = [(b"cookie", cookie_header(token).encode())]
    if background:
        headers.append((BACKGROUND_HEADER.lower().encode(), b"1"))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/step-up/totp",
            "headers": headers,
            "query_string": b"",
            "client": ("203.0.113.9", 50000),
            "app": env.app,
        }
    )


async def test_every_http_door_honours_the_header(api_env: Any) -> None:
    """Both HTTP doors that resolve a session: `deps._admit_session` (every
    `/api/v1` route) and `guards.session_of` (the `/auth` steps). With the
    header neither renews `last_seen_at`; without it both do. The rule lives
    in `sessions.resolve`, so no door can forget it."""
    signed = await _signed(api_env)

    before = await _seen_ago(api_env, signed.session_id, timedelta(minutes=2))
    door = await api_env.client.get("/api/v1/me", headers={**signed.headers, **BACKGROUND})
    assert door.status_code == 200
    assert await _last_seen(api_env, signed.session_id) == before
    principal = await guards.session_of(_request(api_env, signed.token, background=True))
    assert principal.session_id == signed.session_id
    assert await _last_seen(api_env, signed.session_id) == before

    await guards.session_of(_request(api_env, signed.token, background=False))
    assert await _last_seen(api_env, signed.session_id) > before
    renewed = await _seen_ago(api_env, signed.session_id, timedelta(minutes=2))
    await api_env.client.get("/api/v1/me", headers=signed.headers)
    assert await _last_seen(api_env, signed.session_id) > renewed


async def test_the_header_never_grants_a_renewal(api_env: Any) -> None:
    """It only declines: a caller that resolves with `touch=False` (the live
    socket's re-check) does not touch whatever the header says, and the
    header admits nothing a cookie would not."""
    signed = await _signed(api_env)
    before = await _seen_ago(api_env, signed.session_id, timedelta(minutes=2))

    quiet = await sessions.resolve(_request(api_env, signed.token, background=True), touch=False)

    assert quiet is not None
    assert await _last_seen(api_env, signed.session_id) == before
    nobody = await api_env.client.get("/api/v1/me", headers=BACKGROUND)
    assert nobody.status_code == 401

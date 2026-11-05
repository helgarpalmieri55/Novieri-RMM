"""Console sessions for API tests (P2 phase 1, Task 6).

`sign_in` starts a REAL session through `app.core.auth.sessions.start_session`
-- the same row, hash and lifetimes a sign-in makes -- and returns the headers a
browser on the console would send with it: the cookie, `X-ITMS-Request` and
the console's `Origin` (`app/core/auth/csrf.py`).

THE COOKIE IS A HEADER HERE, ON PURPOSE (coordinator decision D12). httpx keeps
a cookie jar, and it will not send a `Secure` cookie -- every `__Host-` one is
-- to `http://api.test`. The tests therefore pass `Cookie:` explicitly, which
is exactly what the browser puts on the wire.

`use_console` sets `ITS_PUBLIC_CONSOLE_URL` for the test: without it there is
no console origin, and every write on a session is refused.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.auth.csrf import CSRF_HEADER
from app.core.auth.devices import remember
from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE, start_session
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User

CONSOLE = "https://itms.test"


def use_console(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    monkeypatch.setattr(env.app.state.settings, "public_console_url", CONSOLE)


def console_headers() -> dict[str, str]:
    """What the console's `ApiClient` adds to every request (Task 10)."""
    return {CSRF_HEADER: "1", "Origin": CONSOLE}


def cookie_header(session_token: str | None = None, device_token: str | None = None) -> str:
    parts = []
    if session_token is not None:
        parts.append(f"{SESSION_COOKIE}={session_token}")
    if device_token is not None:
        parts.append(f"{DEVICE_COOKIE}={device_token}")
    return "; ".join(parts)


def http_request(env: Any, *, cookie: str = "", user_agent: str = "pytest") -> Request:
    """A request as a sign-in route receives it: what `start_session` reads."""
    headers = [(b"user-agent", user_agent.encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": headers,
            "query_string": b"",
            "client": ("203.0.113.9", 50000),
            "app": env.app,
        }
    )


async def user_id_of(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        user_id: UUID = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        return user_id


@dataclass(frozen=True)
class SignedIn:
    session_id: UUID
    user_id: UUID
    token: str
    headers: dict[str, str]


async def sign_in(
    env: Any,
    org_id: UUID,
    sub: str,
    *,
    factor_done: bool = True,
    trusted_device_id: UUID | None = None,
) -> SignedIn:
    """A session for the user `ApiEnv.create_user` returned `sub` for."""
    user_id = await user_id_of(env, sub)
    async with org_session(org_id, engine=env.app_engine, clients=ClientScope.all()) as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        row, token = await start_session(
            s,
            user,
            kind=user.kind,
            request=http_request(env),
            factor_done=factor_done,
            trusted_device_id=trusted_device_id,
        )
        session_id = row.id
    headers = {"Cookie": cookie_header(token), **console_headers()}
    return SignedIn(session_id=session_id, user_id=user_id, token=token, headers=headers)


async def remembered_device(env: Any, org_id: UUID, sub: str) -> UUID:
    """A REAL remembered device of the person (`devices.remember`), for
    `sign_in(..., trusted_device_id=...)`: a session may name only a device row
    of the same person (FK `fk_auth_sessions_device_user`, Task 1)."""
    user_id = await user_id_of(env, sub)
    async with org_session(org_id, engine=env.app_engine, clients=ClientScope.all()) as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        device, _token = await remember(s, user, http_request(env))
        device_id: UUID = device.id
    return device_id

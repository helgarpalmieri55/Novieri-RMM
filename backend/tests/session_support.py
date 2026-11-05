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
no console origin, and every write on a session is refused. Since own
authentication Task 13 the `api_env` fixture's Settings carry that value
already, and `ApiEnv.headers` is `sign_in(..., step_up=True).headers`; the
calls to `use_console` that remain say what the test relies on.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection, Request

from app.core.auth.csrf import CSRF_HEADER
from app.core.auth.devices import remember
from app.core.auth.sessions import (
    DEVICE_COOKIE,
    SESSION_COOKIE,
    SessionPrincipal,
    mark_step_up,
    start_session,
)
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthSession
from app.modules.remote.tunnel import admit_tunnel_session

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
    step_up: bool = False,
) -> SignedIn:
    """A session for the user `ApiEnv.create_user` returned `sub` for.

    `step_up=True` also confirms the second factor now (design §2.5), in the
    same transaction: what a person who has just signed in WITH their factor
    has, and what the vault asks for (Task 6). A session started with
    `factor_done` and no remembered device already has it (`start_session`);
    on a remembered device it does not, and this adds it. Only for a session
    whose factor is done; anything else is the test's mistake."""
    assert factor_done or not step_up, "a session without its factor has nothing to confirm"
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
        if step_up:
            await s.flush()
            await mark_step_up(s, session_id)
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


async def principal_for(env: Any, session_id: UUID) -> SessionPrincipal:
    """The principal `resolve` would build for that session, read from its rows
    (for tests that mint the assistant's token without a request)."""
    async with AsyncSession(env.admin_engine) as session:
        row = await session.get(AuthSession, session_id)
        assert row is not None
        user = await session.get(User, row.user_id)
        assert user is not None
        return SessionPrincipal(
            session_id=row.id,
            user_id=row.user_id,
            org_id=row.org_id,
            kind=row.kind,
            factor_done=row.factor_at is not None,
            step_up_at=row.step_up_at,
            email=user.email,
            display_name=user.display_name,
            expires_at=row.expires_at,
        )


def refusal_code(response: httpx.Response) -> str | None:
    """The `code` of a refusal (`auth_required`, `csrf`, `step_up_required`):
    top level, as `auth_refusal_handler` writes it (Task 6), or under `detail`."""
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return str(body["code"])
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return str(detail["code"])
    return None


def ws_cookie(token: str, *, origin: str = CONSOLE) -> dict[str, Any]:
    """`websockets.connect(url, **ws_cookie(token))`: the session cookie and the
    console's Origin on the handshake, as a browser sends them (D15), and no
    subprotocol."""
    return {"additional_headers": {"Cookie": cookie_header(token)}, "origin": origin}


async def authorize_tunnel_session(
    *,
    connection: HTTPConnection,
    session_id: UUID,
    settings: Any,
    app_engine: Any,
) -> tuple[UUID, UUID, ClientScope]:
    """`tunnel.admit_tunnel_session` without the session it was admitted by:
    ``(org_id, viewer_id, clients)``, for the door tests that only ask
    "admitted, or which close code". TEST ONLY (final review B-M6), as Task
    13 did with `read_door_identity`: in `app/` it was an admission with no
    watchdog, and a route that picked it up would open a screen that
    outlives its sign-in. `session_screen` admits with `admit_tunnel_session`
    and keeps the principal for `watch_tunnel_session`."""
    admitted = await admit_tunnel_session(
        connection=connection, session_id=session_id, settings=settings, app_engine=app_engine
    )
    return admitted.org_id, admitted.viewer_id, admitted.clients


def socket_connection(env: Any, token: str, *, origin: str = CONSOLE) -> HTTPConnection:
    """A socket handshake carrying the session cookie, for the tests that call
    `authorize_tunnel_session` directly rather than through a socket. `app` is
    in the scope because `sessions.resolve` reads `connection.app.state`."""
    return HTTPConnection(
        {
            "type": "websocket",
            "app": env.app,
            "path": "/api/v1/remote/sessions/x/screen",
            "query_string": b"",
            "headers": [
                (b"cookie", cookie_header(token).encode()),
                (b"origin", origin.encode()),
            ],
        }
    )

"""The console's two WebSockets admit its session cookie (P2 design §6.5; coordinator D15).

`/api/v1/ws` (live status) and `/api/v1/remote/sessions/{id}/screen` (remote
screen) take the session cookie when no bearer subprotocol is offered, and only
from the console's `Origin`: a WebSocket handshake is not subject to CORS, so
without that check any page the person opens could ride on their cookie
(cross-site WebSocket hijacking). Driven as raw ASGI, as
`tests/test_domain_gate_doors.py` drives them, because the close CODE is the
claim and uvicorn does not put it on the wire before the handshake completes.

Codes: 4401 no live signed-in session; 4403 another origin, or an identity the
door refuses; 4409 (screen) past the door, no live screen session to join.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.ws as ws_module
from app.core.auth.sessions import end
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, RemoteSession
from tests.session_support import CONSOLE, cookie_header, sign_in, use_console


@pytest.fixture(autouse=True)
def _console(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    use_console(monkeypatch, api_env)


async def _messages(
    env: Any,
    path: str,
    *,
    cookie: str,
    origin: str | None,
    disconnect_after: bool = False,
    wait_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Every message the route sent, offered NO subprotocol (what the
    console's `new WebSocket(url)` sends once Task 10 drops the token)."""
    sent: list[dict[str, Any]] = []
    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        if disconnect_after:
            return {"type": "websocket.disconnect", "code": 1000}
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"host", b"itms.test"), (b"cookie", cookie.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "wss",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 41234),
        "server": ("127.0.0.1", 443),
        "subprotocols": [],
    }
    call = env.app(scope, receive, send)
    if wait_s:
        await asyncio.wait_for(call, timeout=wait_s)
    else:
        await call
    return sent


def _closes(messages: list[dict[str, Any]]) -> list[int]:
    return [m.get("code", 1000) for m in messages if m["type"] == "websocket.close"]


def _accepted(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m["type"] == "websocket.accept"]


async def _reader(env: Any, *, factor_done: bool = True) -> Any:
    org_id = await env.create_org("sock")
    sub = await env.create_user(org_id, "read-only")
    return org_id, await sign_in(env, org_id, sub, factor_done=factor_done)


# --- /api/v1/ws --------------------------------------------------------------


async def test_the_live_socket_admits_a_session_from_the_console(api_env: Any) -> None:
    _, signed = await _reader(api_env)
    sent = await _messages(
        api_env, "/api/v1/ws", cookie=cookie_header(signed.token), origin=CONSOLE,
        disconnect_after=True,
    )
    accepted = _accepted(sent)
    assert accepted, sent
    # Nothing was offered, so nothing is echoed: a browser that offered no
    # subprotocol fails a handshake that names one.
    assert accepted[0].get("subprotocol") is None
    assert _closes(sent) == []


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
async def test_the_live_socket_refuses_any_other_origin(api_env: Any, origin: str | None) -> None:
    """Protects `origin_allowed` in `ws.py::_admit_session`: checked first."""
    _, signed = await _reader(api_env)
    sent = await _messages(api_env, "/api/v1/ws", cookie=cookie_header(signed.token), origin=origin)
    assert _closes(sent) == [4403]
    assert not _accepted(sent)


async def test_the_live_socket_refuses_a_session_without_its_second_factor(
    api_env: Any,
) -> None:
    _, half = await _reader(api_env, factor_done=False)
    sent = await _messages(api_env, "/api/v1/ws", cookie=cookie_header(half.token), origin=CONSOLE)
    assert _closes(sent) == [4401]


async def test_an_unknown_cookie_is_4401(api_env: Any) -> None:
    sent = await _messages(
        api_env, "/api/v1/ws", cookie=cookie_header("A" * 43), origin=CONSOLE
    )
    assert _closes(sent) == [4401]


async def test_the_live_socket_closes_when_its_session_ends(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 60-second re-check, shortened: a session ended while the socket is
    open (signed out elsewhere, disabled, cut by an administrator) closes it
    4401 on the next check. Protects `recheck` in `ws.py::_admit_session`."""
    monkeypatch.setattr(ws_module, "RECHECK_INTERVAL_S", 0.2)
    org_id, signed = await _reader(api_env)
    task = asyncio.create_task(
        _messages(
            api_env, "/api/v1/ws", cookie=cookie_header(signed.token), origin=CONSOLE, wait_s=10
        )
    )
    await asyncio.sleep(0.3)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, signed.session_id, "signed_out")
    sent = await task
    assert _accepted(sent)
    assert _closes(sent) == [4401]


async def test_without_a_cookie_or_a_token_it_is_still_4401(api_env: Any) -> None:
    """No credential at all goes down the bearer path with "" -- 4401, as
    before, not a 4403 for the missing Origin."""
    sent = await _messages(api_env, "/api/v1/ws", cookie="", origin=None)
    assert _closes(sent) == [4401]


# --- the remote screen -------------------------------------------------------


async def _screen_session(env: Any, org_id: UUID, technician_id: UUID) -> UUID:
    device_id = uuid4()
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        s.add(
            Device(
                id=device_id,
                org_id=org_id,
                hostname="h",
                os_family="linux",
                client_id=org_id,
                site_id=org_id,
            )
        )
        row = RemoteSession(
            org_id=org_id,
            device_id=device_id,
            technician_id=technician_id,
            protocol="screen",
            consent_mode="attended",
            state="connecting",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _technician(env: Any) -> tuple[UUID, Any]:
    org_id = await env.create_org("sock-screen")
    sub = await env.create_user(org_id, "technician")
    return org_id, await sign_in(env, org_id, sub)


async def test_the_screen_admits_its_owners_session(api_env: Any) -> None:
    """Past the door: the route then finds no live screen session in this
    process and closes 4409, which only an admitted owner reaches."""
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    sent = await _messages(
        api_env,
        f"/api/v1/remote/sessions/{sid}/screen",
        cookie=cookie_header(signed.token),
        origin=CONSOLE,
    )
    assert _closes(sent) == [4409]


async def test_the_screen_refuses_another_origin(api_env: Any) -> None:
    """Protects `origin_allowed` in `tunnel.py::authorize_tunnel_session`."""
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    sent = await _messages(
        api_env,
        f"/api/v1/remote/sessions/{sid}/screen",
        cookie=cookie_header(signed.token),
        origin="https://evil.example",
    )
    assert _closes(sent) == [4403]


async def test_the_screen_refuses_someone_elses_session_and_a_dead_one(api_env: Any) -> None:
    org_id, owner = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, owner.user_id)
    other = await sign_in(api_env, org_id, await api_env.create_user(org_id, "technician"))
    path = f"/api/v1/remote/sessions/{sid}/screen"

    not_owner = await _messages(api_env, path, cookie=cookie_header(other.token), origin=CONSOLE)
    assert _closes(not_owner) == [4403]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, owner.session_id, "signed_out")
    ended = await _messages(api_env, path, cookie=cookie_header(owner.token), origin=CONSOLE)
    assert _closes(ended) == [4401]

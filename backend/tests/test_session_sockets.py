"""The console's two WebSockets admit its session cookie (P2 design §6.5; coordinator D15).

`/api/v1/ws` (live status) and `/api/v1/remote/sessions/{id}/screen` (remote
screen) take the session cookie -- and, since Task 13, nothing else -- and only
from the console's `Origin`: a WebSocket handshake is not subject to CORS, so
without that check any page the person opens could ride on their cookie
(cross-site WebSocket hijacking). Driven as raw ASGI, as
`tests/test_domain_gate_doors.py` drives them, because the close CODE is the
claim and uvicorn does not put it on the wire before the handshake completes.

Codes: 4401 no live signed-in session; 4403 another origin, or an identity the
door refuses; 4409 (screen) past the door, no live screen session to join.

EVERY CALL IS BOUNDED (`_messages`'s `wait_s`, `SOCKET_WITHIN_S` by default):
a socket still open when it runs out answers with what it sent so far, so a
door that stops refusing fails on the assertion about its close code instead of
hanging the run. A refusal also tells the socket the browser left
(`disconnect_after`), so a regression that accepts goes on to close rather than
wait.

NO STEP IS GIVEN A FIXED TIME TO HAPPEN IN. Each test waits for the thing
itself -- the socket's accept (`opened`), the screen's tunnel starting
(`_open_screen`) -- within a bound far past any pause measured, and nothing
waits for the bound when the doors work. They used to: the live socket was
given 0.3 s to open, the screen 5 s for its tunnel to start, the session's
maximum 1.5 s from being set to the door reading it. This process stops for a
full garbage collection whenever one is due, and how long that takes grows
with the run, because the heap does (measured 2026-11-11, own authentication
phase 2: ~0.25 s with this file alone; 0.5-0.65 s a few hundred tests in;
4.5-5.0 s by this file's place in the full suite, at ~5 M objects; in a
112-test sample, 86% of the growth was the tests' engines, kept alive by
SQLAlchemy's per-mapper statement caches). So each of those budgets failed
whenever a collection landed inside it -- not because a door was slow: with
collection off, the doors take the same ~36 ms before and after phase 2.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.ws as ws_module
import app.core.auth.sessions as sessions_module
import app.modules.remote.api as remote_api
import app.modules.remote.tunnel as tunnel_module
from app.core.auth.sessions import end
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, RemoteSession, UserRole
from app.models.auth import AuthSession
from app.modules.remote.service import SCREEN
from tests.session_support import CONSOLE, cookie_header, sign_in, use_console

#: How long a socket may take to OPEN -- the door's reads, then its accept or
#: the screen's tunnel starting -- before a test calls it broken (module
#: docstring: far past the longest pause measured; nothing waits for it when
#: the door works, and a door that refuses ends the wait at once).
OPEN_WITHIN_S = 30.0

#: The whole life of a socket a test drives: its opening, then whatever the
#: test does and the re-check that answers it, each of which a pause can
#: lengthen. Shorter than the screen re-check a test sets to 3600 s, so a
#: deadline that is not honoured stays open past it and fails.
SOCKET_WITHIN_S = OPEN_WITHIN_S + 30.0


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
    wait_s: float = SOCKET_WITHIN_S,
    opened: asyncio.Event | None = None,
) -> list[dict[str, Any]]:
    """Every message the route sent, offered NO subprotocol (what the
    console's `new WebSocket(url)` sends once Task 10 drops the token), within
    `wait_s`: a route still running then is cancelled, and what it had sent
    is the answer (module docstring). `opened`, if given, is set when the
    route accepts: the moment a test may act on an open socket."""
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
        if opened is not None and message["type"] == "websocket.accept":
            opened.set()

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
    try:
        await asyncio.wait_for(env.app(scope, receive, send), timeout=wait_s)
    except TimeoutError:
        pass  # still open: the assertions on `sent` say what that means
    return sent


def _closes(messages: list[dict[str, Any]]) -> list[int]:
    return [m.get("code", 1000) for m in messages if m["type"] == "websocket.close"]


def _accepted(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m["type"] == "websocket.accept"]


async def _until(event: asyncio.Event, socket: asyncio.Task[list[dict[str, Any]]]) -> None:
    """Wait until `event` is set, or until `socket` ends first -- a door that
    refuses ends the wait at once, and the test's assertions then say what it
    sent -- for at most `OPEN_WITHIN_S`."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait(
            {waiter, socket}, timeout=OPEN_WITHIN_S, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


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
    sent = await _messages(
        api_env,
        "/api/v1/ws",
        cookie=cookie_header(signed.token),
        origin=origin,
        disconnect_after=True,
    )
    assert _closes(sent) == [4403]
    assert not _accepted(sent)


async def test_the_live_socket_refuses_a_session_without_its_second_factor(
    api_env: Any,
) -> None:
    _, half = await _reader(api_env, factor_done=False)
    sent = await _messages(
        api_env, "/api/v1/ws", cookie=cookie_header(half.token), origin=CONSOLE,
        disconnect_after=True,
    )
    assert _closes(sent) == [4401]
    assert not _accepted(sent)


async def test_an_unknown_cookie_is_4401(api_env: Any) -> None:
    sent = await _messages(
        api_env, "/api/v1/ws", cookie=cookie_header("A" * 43), origin=CONSOLE,
        disconnect_after=True,
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
    opened = asyncio.Event()
    task = asyncio.create_task(
        _messages(
            api_env,
            "/api/v1/ws",
            cookie=cookie_header(signed.token),
            origin=CONSOLE,
            opened=opened,
        )
    )
    # Open first, then end the session: the claim is about an OPEN socket.
    # (A fixed 0.3 s here lost to a full collection that held the door past
    # it: the session ended first, and the door refused 4401 before any
    # accept -- module docstring.)
    await _until(opened, task)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, signed.session_id, "signed_out")
    sent = await task
    assert _accepted(sent)
    assert _closes(sent) == [4401]


async def test_without_a_cookie_or_a_token_it_is_still_4401(api_env: Any) -> None:
    """No credential at all is 4401, as before -- the `SESSION_COOKIE not in
    websocket.cookies` line of `ui_websocket` since Task 13 (it went down the
    bearer path with "" until then) -- and not a 4403 for the missing
    Origin."""
    sent = await _messages(api_env, "/api/v1/ws", cookie="", origin=None, disconnect_after=True)
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
    """Protects `origin_allowed` in `tunnel.py::admit_tunnel_session`."""
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    sent = await _messages(
        api_env,
        f"/api/v1/remote/sessions/{sid}/screen",
        cookie=cookie_header(signed.token),
        origin="https://evil.example",
        disconnect_after=True,
    )
    assert _closes(sent) == [4403]
    assert not _accepted(sent)


async def test_the_screen_refuses_someone_elses_session_and_a_dead_one(api_env: Any) -> None:
    org_id, owner = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, owner.user_id)
    other = await sign_in(api_env, org_id, await api_env.create_user(org_id, "technician"))
    path = f"/api/v1/remote/sessions/{sid}/screen"

    not_owner = await _messages(
        api_env, path, cookie=cookie_header(other.token), origin=CONSOLE, disconnect_after=True
    )
    assert _closes(not_owner) == [4403]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, owner.session_id, "signed_out")
    ended = await _messages(
        api_env, path, cookie=cookie_header(owner.token), origin=CONSOLE, disconnect_after=True
    )
    assert _closes(ended) == [4401]


async def test_the_screen_refuses_a_session_without_its_second_factor(api_env: Any) -> None:
    """Protects `or not principal.factor_done` in
    `tunnel.py::admit_tunnel_session`: the password alone opens the sign-in
    routes, never a remote desktop."""
    org_id = await api_env.create_org("sock-screen")
    sub = await api_env.create_user(org_id, "technician")
    half = await sign_in(api_env, org_id, sub, factor_done=False)
    sid = await _screen_session(api_env, org_id, half.user_id)
    sent = await _messages(
        api_env,
        f"/api/v1/remote/sessions/{sid}/screen",
        cookie=cookie_header(half.token),
        origin=CONSOLE,
        disconnect_after=True,
    )
    assert _closes(sent) == [4401]
    assert not _accepted(sent)


# --- a live screen outlives nothing: the watchdog ------------------------------


def _live_screen(
    env: Any, monkeypatch: pytest.MonkeyPatch, sid: UUID, *, hold: bool
) -> dict[str, Any]:
    """Past the door with a live screen session in this process:
    `connection_params` names a screen for `sid`, and `run_screen_tunnel` is a
    stand-in that, with `hold`, runs until cancelled and records that it was
    (without, it returns at once: the browser left)."""
    seen: dict[str, Any] = {"started": asyncio.Event(), "cancelled": False}
    service = env.app.state.remote_service
    monkeypatch.setattr(
        service,
        "connection_params",
        lambda session_id: {"protocol": SCREEN} if session_id == sid else None,
    )

    async def tunnel(**_kwargs: Any) -> None:
        seen["started"].set()
        if not hold:
            return
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    monkeypatch.setattr(remote_api, "run_screen_tunnel", tunnel)
    return seen


async def _open_screen(
    env: Any, sid: UUID, token: str, seen: dict[str, Any]
) -> asyncio.Task[list[dict[str, Any]]]:
    """The screen, opened from the console and running (the tunnel started).

    Waits for the tunnel to start, within `OPEN_WITHIN_S` (it gave it 5 s,
    which a full collection late in the suite outlasts: module docstring), or
    for the socket to end first -- a door that refuses then fails HERE on what
    it sent, instead of on a timeout that cannot tell refused from slow."""
    task = asyncio.create_task(
        _messages(
            env,
            f"/api/v1/remote/sessions/{sid}/screen",
            cookie=cookie_header(token),
            origin=CONSOLE,
        )
    )
    await _until(seen["started"], task)
    assert seen["started"].is_set(), (
        f"the screen did not open: {task.result()}"
        if task.done()
        else f"the screen did not open within {OPEN_WITHIN_S} s"
    )
    return task


async def test_the_screen_accepts_a_session_without_a_subprotocol(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects `websocket.accept()` (no subprotocol) in `session_screen`: the
    browser offered none, and a handshake that names one fails there."""
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    seen = _live_screen(api_env, monkeypatch, sid, hold=False)
    sent = await _messages(
        api_env,
        f"/api/v1/remote/sessions/{sid}/screen",
        cookie=cookie_header(signed.token),
        origin=CONSOLE,
    )
    accepted = _accepted(sent)
    assert accepted, sent
    assert accepted[0].get("subprotocol") is None
    assert seen["started"].is_set()


async def test_the_screen_closes_when_its_session_ends(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design §2.4: signing out, an administrator's cut, a replacement or a
    disabled account stops the person's sessions AT ONCE -- remote control of
    a desktop included. Protects the re-check in
    `tunnel.py::watch_tunnel_session` and its wiring in `session_screen`: the
    session ends, the tunnel is cancelled and the socket closes 4401."""
    monkeypatch.setattr(tunnel_module, "RECHECK_INTERVAL_S", 0.2)
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    seen = _live_screen(api_env, monkeypatch, sid, hold=True)
    task = await _open_screen(api_env, sid, signed.token, seen)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, signed.session_id, "ended_by_admin")
    sent = await task
    assert _accepted(sent)
    assert _closes(sent) == [4401]
    assert seen["cancelled"] is True


async def test_the_screen_closes_when_its_remote_control_grant_is_revoked(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review B-M5: the watchdog re-checks the DOOR, not only the
    session. A `remote.control` grant revoked while the screen is open
    leaves the session alive, so the session check alone keeps the desktop
    open; the door's re-read (`read_session_identity` with the screen's
    code) finds an empty scope and the screen closes 4403, as the door would
    refuse it. Re-check only the session and this stays open: red."""
    monkeypatch.setattr(tunnel_module, "RECHECK_INTERVAL_S", 0.2)
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    seen = _live_screen(api_env, monkeypatch, sid, hold=True)
    task = await _open_screen(api_env, sid, signed.token, seen)

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(delete(UserRole).where(UserRole.user_id == signed.user_id))
        await s.commit()
    sent = await task

    assert _accepted(sent)
    assert _closes(sent) == [4403]
    assert seen["cancelled"] is True
    async with AsyncSession(api_env.admin_engine) as s:
        alive = (
            await s.execute(select(AuthSession.ended_at).where(AuthSession.id == signed.session_id))
        ).scalar_one()
    assert alive is None  # the console session itself was not touched


async def test_the_screen_closes_when_the_domain_rule_refuses_its_viewer(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-M5, the domain rule: switched on while a screen is open, it closes
    the screen with the door's 4403."""
    monkeypatch.setattr(tunnel_module, "RECHECK_INTERVAL_S", 0.2)
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    seen = _live_screen(api_env, monkeypatch, sid, hold=True)
    task = await _open_screen(api_env, sid, signed.token, seen)

    monkeypatch.setattr(api_env.app.state.settings, "console_email_domain", "elsewhere.example")
    sent = await task

    assert _closes(sent) == [4403]
    assert seen["cancelled"] is True


async def test_the_screen_closes_at_its_sessions_maximum(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The absolute maximum (`expires_at`) stops the screen by itself, with no
    re-check due: the interval here is longer than the whole test. Protects
    the deadline in `watch_tunnel_session`.

    The DOOR reads the session as of the moment the maximum was set (`_now`,
    which `resolve` asks), however long the opening then takes: a full
    collection longer than the 1.5 s, landing between this row and the door,
    used to make the door find the session past its maximum and refuse the
    screen before it opened (module docstring). The WATCHDOG's deadline runs
    on the real clock (`time.time()` in `watch_tunnel_session`); that deadline
    is the claim, and nothing here moves it."""
    monkeypatch.setattr(tunnel_module, "RECHECK_INTERVAL_S", 3600.0)
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    set_at = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(expires_at=set_at + timedelta(seconds=1.5))
        )
        await s.commit()
    monkeypatch.setattr(sessions_module, "_now", lambda: set_at)
    seen = _live_screen(api_env, monkeypatch, sid, hold=True)
    task = await _open_screen(api_env, sid, signed.token, seen)

    sent = await task
    assert _accepted(sent)
    assert _closes(sent) == [4401]
    assert seen["cancelled"] is True


async def test_an_unattended_open_screen_is_idle_ended(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX ROUND 2 (console re-review P1) REVERSES the Task 6 decision "an
    open remote screen is activity". The watchdog's re-check no longer
    touches the session (`touch=False` in `watch_tunnel_session`): a person
    driving the desktop keeps the session alive through the console, which
    counts their pointer and keyboard on the viewer page as activity. An
    open screen nobody works is idle like any tab -- cut at the idle limit
    with 4401, its tunnel cancelled. Touch at the re-check again and the
    first re-check renews `last_seen_at`, the clock's two minutes never make
    it idle, and the screen stays open: red.

    Was `test_an_open_screen_keeps_its_session_alive`, which asserted the
    opposite: `last_seen_at` renewed by the re-check, the task still open,
    and no 4401."""
    monkeypatch.setattr(tunnel_module, "RECHECK_INTERVAL_S", 0.2)
    org_id, signed = await _technician(api_env)
    sid = await _screen_session(api_env, org_id, signed.user_id)
    seen = _live_screen(api_env, monkeypatch, sid, hold=True)
    task = await _open_screen(api_env, sid, signed.token, seen)

    # 29 minutes without activity: not idle yet, and the re-checks that run
    # now must not renew it.
    twenty_nine_ago = datetime.now(UTC) - timedelta(minutes=29)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(last_seen_at=twenty_nine_ago)
        )
        await s.commit()
    await asyncio.sleep(0.6)
    async with AsyncSession(api_env.admin_engine) as s:
        seen_at = (
            await s.execute(
                select(AuthSession.last_seen_at).where(AuthSession.id == signed.session_id)
            )
        ).scalar_one()
    assert seen_at == twenty_nine_ago, "the re-check must not count as activity"
    assert not task.done(), "not idle yet: still open"
    # Two more minutes pass with nobody at the screen: 31 minutes idle.
    later = datetime.now(UTC) + timedelta(minutes=2)
    monkeypatch.setattr(sessions_module, "_now", lambda: later)

    sent = await task

    assert _accepted(sent)
    assert _closes(sent) == [4401]
    assert seen["cancelled"] is True

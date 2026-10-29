"""UI WebSocket: authenticated live status channel, served by a real uvicorn
instance so the WebSocket path is exercised end-to-end in one event loop.

THE TOKEN TRAVELS IN THE SUBPROTOCOL HEADER, NOT THE QUERY STRING. It used to
be `?token=<jwt>`, which uvicorn's access line writes to stdout verbatim -- a
live bearer token in the log, unreachable by the logging kernel's redaction
(which matches on `extra=` field names, and uvicorn's records have none). The
last two tests in this file are the pair that makes the fix a measurement
rather than a claim: one asserts a real handshake leaves no token in the log
output, and its non-vacuity control asserts that a URL-borne token DOES appear
there -- the same capture, the same server, the exposure still demonstrable on
the path that was removed.
"""

import asyncio
import json
import logging
import socket

import pytest
import redis.asyncio as aioredis
import uvicorn
import websockets

from app.api.v1.ws import AUTH_SUBPROTOCOL
from app.gateway.dispatch import publish_ui_event

REDIS_URL = "redis://localhost:6379/9"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _auth(token: str) -> list[str]:
    """What a browser passes as `new WebSocket(url, [...])`."""
    return [AUTH_SUBPROTOCOL, token]


@pytest.fixture()
async def live_api(api_env):
    port = _free_port()
    config = uvicorn.Config(api_env.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield port
    server.should_exit = True
    await task


@pytest.fixture()
async def live_api_logged(api_env):
    """A uvicorn instance with everything it logs captured.

    `log_config=None` is load-bearing twice over: uvicorn's default config
    REPLACES the handlers on its own loggers (which would discard the capture
    below) and sets `propagate=False`, and it is also what the deployed API
    wants, since the logging kernel configures logging itself
    (`app.core.logging.configure_logging`, called by `create_app`).

    Captures the ROOT logger rather than `uvicorn.access` alone: the claim
    under test is "the token does not reach the log", not "the token does not
    reach one particular logger". uvicorn reports the WebSocket handshake --
    accepted, 403, or otherwise -- on `uvicorn.error`, with the path AND
    query string, while ordinary HTTP requests go to `uvicorn.access`. A
    capture aimed at one of them would miss the line that matters.

    The two uvicorn loggers are reset to the state `configure_logging`
    leaves them in (no handlers of their own, propagating, INFO-eligible).
    This is not cosmetic: the `live_api` fixture above runs uvicorn with its
    DEFAULT `log_config`, which pins those loggers to WARNING with
    `propagate=False` process-wide and survives that fixture's teardown. With
    that state left in place, this fixture captures nothing from uvicorn at
    all -- and the token-absence assertion below passes for a completely
    empty log. The control at the end of the test is what caught it.
    """
    port = _free_port()
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    uvicorn_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access", "uvicorn.error")]
    restore = [(lg, lg.level, lg.propagate, list(lg.handlers)) for lg in uvicorn_loggers]
    for lg in uvicorn_loggers:
        lg.handlers[:] = []
        lg.propagate = True
        lg.setLevel(logging.NOTSET)

    config = uvicorn.Config(
        api_env.app, host="127.0.0.1", port=port, log_config=None, access_log=True
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield port, lines
    finally:
        server.should_exit = True
        await task
        root.removeHandler(handler)
        root.setLevel(previous_level)
        for lg, level, propagate, handlers in restore:
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers[:] = handlers


async def test_ui_ws_receives_org_events_with_valid_token(api_env, live_api) -> None:
    port = live_api
    org_id = await api_env.create_org("ws")
    reader = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(reader, slug)

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/api/v1/ws", subprotocols=_auth(token)
    ) as ws:
        # The server must select the protocol it was offered, or a browser
        # would fail the handshake outright.
        assert ws.subprotocol == AUTH_SUBPROTOCOL
        await asyncio.sleep(0.3)  # allow the server-side subscription to settle
        # On the org's internal client (its id is the org's): a read-only
        # role granted on every client hears every client's channel.
        await publish_ui_event(
            redis, org_id, org_id, {"kind": "job_status", "job_id": "x", "state": "running"}
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
    await redis.aclose()
    event = json.loads(raw)
    assert event == {"kind": "job_status", "job_id": "x", "state": "running"}


async def test_ui_ws_rejects_bad_token(api_env, live_api) -> None:
    port = live_api
    errors = (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    with pytest.raises(errors):
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/v1/ws", subprotocols=_auth("garbage")
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)


async def test_ui_ws_refuses_a_token_offered_in_the_query_string(api_env, live_api) -> None:
    """The removed path stays removed.

    A VALID token in the query string -- the exact request the old client
    made -- is refused, so the exposure is not merely unused by this repo's
    frontend but unreachable. Without this, deleting the query-parameter read
    and leaving a fallback would look identical from the outside.
    """
    port = live_api
    org_id = await api_env.create_org("ws-query")
    reader = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(reader, slug)

    errors = (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    with pytest.raises(errors):
        async with websockets.connect(f"ws://127.0.0.1:{port}/api/v1/ws?token={token}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)


async def test_the_token_does_not_reach_the_log_but_a_url_borne_one_would(
    api_env, live_api_logged
) -> None:
    """THE MEASUREMENT, with its own control.

    Part 1 connects the way the frontend now does and asserts the token
    appears nowhere in what the server logged.

    Part 2 is the non-vacuity half, and it is the reason this test is worth
    having: it makes the SAME capture, against the SAME server, show the
    token when the token is put in the URL. Without it, part 1 would pass
    just as happily against a capture that recorded nothing at all -- which
    is precisely how a guard ends up green and meaningless.
    """
    port, lines = live_api_logged
    org_id = await api_env.create_org("ws-log")
    reader = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(reader, slug)

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/api/v1/ws", subprotocols=_auth(token)
    ) as ws:
        assert ws.subprotocol == AUTH_SUBPROTOCOL
    await asyncio.sleep(0.2)

    clean = list(lines)
    # NON-VACUITY, directly: uvicorn reported THIS handshake, so the absence
    # below is an absence from a log that was actually written.
    assert any("/api/v1/ws" in line for line in clean), (
        f"uvicorn logged nothing about the handshake -- capture is dead: {clean}"
    )
    leaked = [line for line in clean if token in line]
    assert leaked == [], f"the bearer token reached the log: {leaked}"

    lines.clear()
    errors = (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    with pytest.raises(errors):
        async with websockets.connect(f"ws://127.0.0.1:{port}/api/v1/ws?token={token}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)
    await asyncio.sleep(0.2)

    assert any(token in line for line in lines), (
        "the control did not reproduce the exposure -- this capture cannot see "
        "a token in the request URL, so the assertion above proves nothing. "
        f"captured: {lines}"
    )


# --------------------------------------------------------------------------- #
# The channel is per client (MSP clients spec §2.6). A technician limited to
# client X hears X and the org's global channel, and nothing about client Y.
# --------------------------------------------------------------------------- #

# How long "not received" is waited for. A leak arrives in milliseconds: the
# publisher and the socket share one Redis and one event loop.
_SILENCE_S = 1.0


async def _technician_on(api_env, org_id, *client_ids) -> str:
    """A staff user holding `technician` on exactly `client_ids` (none: no
    grant at all); returns the OIDC sub."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Role, User, UserRole

    sub = await api_env.create_user(org_id)
    async with AsyncSession(api_env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role_id = (
            await s.execute(
                select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
            )
        ).scalar_one()
        for client_id in client_ids:
            s.add(UserRole(org_id=org_id, user_id=user_id, role_id=role_id, client_id=client_id))
        await s.commit()
    return sub


async def _token(api_env, org_id, sub: str) -> str:
    return api_env.idp.token(sub, await api_env.org_slug(org_id), email=f"{sub}@test.local")


async def _device_on(api_env, org_id, client_id, site_id, hostname: str):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Device

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="linux",
            client_id=client_id,
            site_id=site_id,
        )
        s.add(device)
        await s.commit()
        return device.id


async def _two_clients(api_env, prefix: str):
    """An org with customer clients X and Y and one device on each:
    `(org_id, (x, y), (device_x, device_y))`."""
    from tests.client_scope_support import client_with_site

    org_id = await api_env.create_org(prefix)
    x, site_x = await client_with_site(api_env, org_id, "Cliente X")
    y, site_y = await client_with_site(api_env, org_id, "Cliente Y")
    device_x = await _device_on(api_env, org_id, x, site_x, "dev-x")
    device_y = await _device_on(api_env, org_id, y, site_y, "dev-y")
    return org_id, (x, y), (device_x, device_y)


async def _nothing_within(ws, seconds: float = _SILENCE_S) -> None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
    except TimeoutError:
        return
    raise AssertionError(f"an event reached a socket outside its scope: {raw}")


def _security_events_msg(uid: str):
    from app.gateway.protocol import parse_agent_message

    return parse_agent_message(
        json.dumps(
            {
                "v": 1,
                "kind": "security_events",
                "events": [
                    {
                        "event_uid": uid,
                        "ts": "2026-10-24T10:00:00Z",
                        "event_type": "account_lockout",
                        "username": "victim",
                        "outcome": "failure",
                    }
                ],
            }
        )
    )


async def test_a_technician_on_one_client_hears_it_and_not_another(
    api_env, live_api, tmp_path
) -> None:
    """THE BRIEF'S CASE, through a real publisher: the gateway's
    security-events branch, which derives the channel from the device."""
    from app.core.signing import LocalEd25519Signer
    from app.gateway.dispatch import handle_agent_message
    from app.jobs.engine import JobEngine

    org_id, (x, _y), (device_x, device_y) = await _two_clients(api_env, "ws-xy")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x))
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)  # allow the server-side subscription to settle
            await handle_agent_message(
                api_env.app_engine, job_engine, redis, _security_events_msg("y1"),
                device_id=device_y, org_id=org_id,
            )
            await _nothing_within(ws)
            await handle_agent_message(
                api_env.app_engine, job_engine, redis, _security_events_msg("x1"),
                device_id=device_x, org_id=org_id,
            )
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    finally:
        await redis.aclose()
    assert event == {
        "kind": "security_events_ingested",
        "device_id": str(device_x),
        "count": 1,
    }


async def test_a_job_status_change_reaches_only_its_clients_technician(
    api_env, live_api, tmp_path
) -> None:
    """AFTER AN UPDATE. `jobs.client_id` is `server_onupdate=FetchedValue()`:
    once `mark_running` flushes, the attribute is EXPIRED, and reading it in
    async code raises MissingGreenlet. The publisher must read the client,
    not the stale instance -- this drives the real job-status branch, whose
    UPDATE runs before the event is published."""
    from app.core.client_scope import ClientScope
    from app.core.signing import LocalEd25519Signer
    from app.core.tenancy import org_session
    from app.gateway.dispatch import handle_agent_message
    from app.gateway.protocol import parse_agent_message
    from app.jobs.engine import JobEngine

    org_id, (x, _y), (device_x, device_y) = await _two_clients(api_env, "ws-job")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x))
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    jobs = {}
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        for device_id in (device_x, device_y):
            job = await job_engine.create(
                s, org_id=org_id, type="hello", target_device_id=device_id, params={}
            )
            await job_engine.mark_dispatched(s, job)
            jobs[device_id] = job.id

    def running(job_id):
        return parse_agent_message(
            json.dumps({"v": 1, "kind": "job_status", "job_id": str(job_id), "state": "running"})
        )

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await handle_agent_message(
                api_env.app_engine, job_engine, redis, running(jobs[device_y]),
                device_id=device_y, org_id=org_id,
            )
            await _nothing_within(ws)
            await handle_agent_message(
                api_env.app_engine, job_engine, redis, running(jobs[device_x]),
                device_id=device_x, org_id=org_id,
            )
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    finally:
        await redis.aclose()
    assert event == {"kind": "job_status", "job_id": str(jobs[device_x]), "state": "running"}


async def test_a_module_event_queued_after_an_update_names_the_jobs_client(
    api_env, tmp_path
) -> None:
    """The module side of the same trap: a result handler runs inside
    `JobEngine.complete`, after the job's UPDATE has flushed. The queued event
    carries the channel of the JOB's client, read rather than taken from the
    expired attribute."""
    from app.core.client_scope import ClientScope
    from app.core.signing import LocalEd25519Signer
    from app.core.tenancy import org_session
    from app.gateway.dispatch import queue_job_ui_event, ui_channel
    from app.jobs.engine import JobEngine

    org_id, (x, _y), (device_x, _device_y) = await _two_clients(api_env, "ws-queue")
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        job = await job_engine.create(
            s, org_id=org_id, type="hello", target_device_id=device_x, params={}
        )
        await job_engine.mark_dispatched(s, job)
        await job_engine.mark_running(s, job)  # an UPDATE: client_id is now expired
        await queue_job_ui_event(s, job, {"kind": "probe", "job_id": str(job.id)})
        queued = list(s.info.get("ui_events", []))
    assert queued == [(ui_channel(org_id, x), {"kind": "probe", "job_id": str(job.id)})]


async def test_a_global_event_reaches_a_narrowed_technician(api_env, live_api) -> None:
    """Rows with no client (an O table's every-client rows, org settings)
    publish on `ui:{org}:global`, which every scope hears."""
    from app.gateway.dispatch import publish_global_ui_event

    org_id, (x, _y), _devices = await _two_clients(api_env, "ws-global")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_global_ui_event(redis, org_id, {"kind": "settings_changed"})
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    finally:
        await redis.aclose()
    assert event == {"kind": "settings_changed"}


async def test_the_client_parameter_narrows_an_every_client_technician(
    api_env, live_api
) -> None:
    """The console's client selector travels as `?client=`: an every-client
    technician who chose X hears X (and the global channel), not Y."""
    from app.gateway.dispatch import publish_global_ui_event, publish_ui_event

    org_id, (x, y), _devices = await _two_clients(api_env, "ws-narrow")
    token = await _token(api_env, org_id, await api_env.create_user(org_id, "technician"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws?client={x}", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_ui_event(redis, org_id, y, {"kind": "for-y"})
            await _nothing_within(ws)
            await publish_ui_event(redis, org_id, x, {"kind": "for-x"})
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            await publish_global_ui_event(redis, org_id, {"kind": "for-everyone"})
            second = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    finally:
        await redis.aclose()
    assert (first, second) == ({"kind": "for-x"}, {"kind": "for-everyone"})


async def test_an_every_client_technician_hears_every_client(api_env, live_api) -> None:
    """Scope all, no parameter: one pattern subscription covers every client
    of the org and its global channel -- and no other org."""
    from app.gateway.dispatch import publish_global_ui_event, publish_ui_event

    org_id, (x, y), _devices = await _two_clients(api_env, "ws-all")
    other_org = await api_env.create_org("ws-all-other")
    token = await _token(api_env, org_id, await api_env.create_user(org_id, "technician"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_ui_event(redis, other_org, other_org, {"kind": "other-org"})
            await publish_global_ui_event(redis, other_org, {"kind": "other-org-global"})
            await _nothing_within(ws)
            received = []
            for client_id in (x, y):
                await publish_ui_event(redis, org_id, client_id, {"kind": str(client_id)})
                received.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))
            await publish_global_ui_event(redis, org_id, {"kind": "global"})
            received.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))
    finally:
        await redis.aclose()
    assert received == [{"kind": str(x)}, {"kind": str(y)}, {"kind": "global"}]


# The refusals are asserted at the ASGI boundary, where `ws.py` sets the code:
# a close sent before the handshake is accepted reaches a live peer as an HTTP
# 403, not as a close frame (`tests/test_domain_gate_doors.py`'s docstring).


async def _ws_messages(api_env, *, token: str, query: bytes = b"") -> list:
    sent: list = []
    inbound = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]

    async def receive():
        if inbound:
            return inbound.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def send(message) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/ws",
        "raw_path": b"/api/v1/ws",
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"api.test")],
        "client": ("127.0.0.1", 41234),
        "server": ("127.0.0.1", 80),
        "subprotocols": [AUTH_SUBPROTOCOL, token],
    }
    await api_env.app(scope, receive, send)
    return sent


def _closes(messages: list) -> list[int]:
    return [m.get("code") for m in messages if m.get("type") == "websocket.close"]


def _accepted(messages: list) -> bool:
    return any(m["type"] == "websocket.accept" for m in messages)


@pytest.mark.parametrize(
    "query",
    [b"client=not-a-uuid", b"client=", b"client=%s&client=%s"],
    ids=["garbage", "empty", "two-values"],
)
async def test_a_malformed_client_parameter_closes_4400(api_env, query: bytes) -> None:
    org_id, (x, y), _devices = await _two_clients(api_env, "ws-4400")
    token = await _token(api_env, org_id, await api_env.create_user(org_id, "technician"))
    if b"%s" in query:
        query = query % (str(x).encode(), str(y).encode())
    sent = await _ws_messages(api_env, token=token, query=query)
    assert _closes(sent) == [4400]
    assert not _accepted(sent)


async def test_the_parameter_naming_a_valid_client_is_accepted(api_env) -> None:
    """The control for the refusals around it: the same harness, a well-formed
    parameter inside the scope, and the socket is accepted."""
    org_id, (x, _y), _devices = await _two_clients(api_env, "ws-ok")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x))
    sent = await _ws_messages(api_env, token=token, query=f"client={x}".encode())
    assert _accepted(sent)
    assert _closes(sent) == []


async def test_a_technician_with_no_grant_closes_4403(api_env) -> None:
    org_id, _clients, _devices = await _two_clients(api_env, "ws-none")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id))
    sent = await _ws_messages(api_env, token=token)
    assert _closes(sent) == [4403]
    assert not _accepted(sent)


async def test_a_client_outside_the_scope_closes_4403(api_env) -> None:
    """Narrowing never widens: X's technician naming Y gets the EMPTY scope,
    and an empty scope is refused rather than subscribed to nothing."""
    org_id, (x, y), _devices = await _two_clients(api_env, "ws-out")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x))
    sent = await _ws_messages(api_env, token=token, query=f"client={y}".encode())
    assert _closes(sent) == [4403]
    assert not _accepted(sent)


# --------------------------------------------------------------------------- #
# A scope of more than one client (Task 10 review M1), and the LIVE RE-CHECK
# (final review, T10R-I1): the scope was evaluated once, at connect, so a
# revoked grant, a disabled account or an expired token kept receiving that
# client's live events -- a job's full result body among them -- for as long
# as the socket stayed open. The door is re-run every `RECHECK_INTERVAL_S`.
# --------------------------------------------------------------------------- #


async def _three_clients(api_env, prefix: str):
    from tests.client_scope_support import client_with_site

    org_id = await api_env.create_org(prefix)
    clients = []
    for name in ("Cliente X", "Cliente Y", "Cliente Z"):
        client_id, _site = await client_with_site(api_env, org_id, name)
        clients.append(client_id)
    return org_id, tuple(clients)


async def _hears(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))


async def _closed_with(ws, timeout: float = 10.0) -> int | None:
    """Wait for the server to close the socket; its close code."""
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
    except websockets.ConnectionClosed as closed:
        return closed.rcvd.code if closed.rcvd is not None else None


@pytest.fixture()
def fast_recheck(monkeypatch):
    """Re-run the door every 0.2 s instead of every minute."""
    from app.api.v1 import ws as ws_module

    monkeypatch.setattr(ws_module, "RECHECK_INTERVAL_S", 0.2)


async def test_a_two_client_technician_hears_both_and_not_a_third(api_env, live_api) -> None:
    org_id, (x, y, z) = await _three_clients(api_env, "ws-xyz")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x, y))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_ui_event(redis, org_id, z, {"kind": "for-z"})
            await _nothing_within(ws)
            await publish_ui_event(redis, org_id, x, {"kind": "for-x"})
            first = await _hears(ws)
            await publish_ui_event(redis, org_id, y, {"kind": "for-y"})
            second = await _hears(ws)
    finally:
        await redis.aclose()
    assert (first, second) == ({"kind": "for-x"}, {"kind": "for-y"})


async def test_the_client_parameter_drops_the_other_client_of_a_two_client_scope(
    api_env, live_api
) -> None:
    org_id, (x, y, _z) = await _three_clients(api_env, "ws-xy-narrow")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x, y))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws?client={x}", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_ui_event(redis, org_id, y, {"kind": "for-y"})
            await _nothing_within(ws)
            await publish_ui_event(redis, org_id, x, {"kind": "for-x"})
            heard = await _hears(ws)
    finally:
        await redis.aclose()
    assert heard == {"kind": "for-x"}


async def test_an_unchanged_scope_keeps_the_socket_open(
    api_env, live_api, fast_recheck
) -> None:
    """The control for the closes below: several re-checks pass with nothing
    changed, and the socket still hears its clients."""
    org_id, (x, y, _z) = await _three_clients(api_env, "ws-steady")
    token = await _token(api_env, org_id, await _technician_on(api_env, org_id, x, y))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(1.5)  # several re-checks at 0.2 s
            await publish_ui_event(redis, org_id, y, {"kind": "for-y"})
            heard = await _hears(ws)
    finally:
        await redis.aclose()
    assert heard == {"kind": "for-y"}


async def test_a_revoked_grant_closes_the_socket_4403(api_env, live_api, fast_recheck) -> None:
    """Y's grant is revoked while the socket is open: the next re-check sees
    a scope that no longer covers what is subscribed and closes 4403 -- no Y
    event after it. The console reconnects and subscribes to what is left."""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import User, UserRole

    org_id, (x, y, _z) = await _three_clients(api_env, "ws-revoke")
    sub = await _technician_on(api_env, org_id, x, y)
    token = await _token(api_env, org_id, sub)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
        ) as ws:
            await asyncio.sleep(0.3)
            await publish_ui_event(redis, org_id, y, {"kind": "before"})
            assert await _hears(ws) == {"kind": "before"}
            async with AsyncSession(api_env.admin_engine) as s:
                user_id = (
                    await s.execute(select(User.id).where(User.external_subject == sub))
                ).scalar_one()
                await s.execute(
                    delete(UserRole).where(UserRole.user_id == user_id, UserRole.client_id == y)
                )
                await s.commit()
            code = await _closed_with(ws)
    finally:
        await redis.aclose()
    assert code == 4403


async def test_a_disabled_person_is_closed_4403(api_env, live_api, fast_recheck) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import User

    org_id, (x, _y, _z) = await _three_clients(api_env, "ws-disabled")
    sub = await _technician_on(api_env, org_id, x)
    token = await _token(api_env, org_id, sub)
    async with websockets.connect(
        f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
    ) as ws:
        await asyncio.sleep(0.3)
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                update(User).where(User.external_subject == sub).values(status="disabled")
            )
            await s.commit()
        code = await _closed_with(ws)
    assert code == 4403


async def test_an_expired_token_is_closed_4401(api_env, live_api) -> None:
    """Checked on every turn of the loop, not only at the re-check: the token
    is what admitted the socket, and it says when it stops."""
    org_id, (x, _y, _z) = await _three_clients(api_env, "ws-expiry")
    sub = await _technician_on(api_env, org_id, x)
    token = api_env.idp.token(
        sub, await api_env.org_slug(org_id), email=f"{sub}@test.local", exp_in=2
    )
    async with websockets.connect(
        f"ws://127.0.0.1:{live_api}/api/v1/ws", subprotocols=_auth(token)
    ) as ws:
        code = await _closed_with(ws)
    assert code == 4401

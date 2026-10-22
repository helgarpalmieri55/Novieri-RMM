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
        await publish_ui_event(
            redis, org_id, {"kind": "job_status", "job_id": "x", "state": "running"}
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

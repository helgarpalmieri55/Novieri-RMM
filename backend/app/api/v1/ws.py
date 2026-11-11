"""Operator UI WebSocket: live job/device status for the clients in the
authenticated person's scope.

Authentication is the console's session cookie, as on the HTTP API, with the
console's `Origin` on the handshake (P2 design §6.5; coordinator D15). Events
come from the Redis UI channels of the clients in the person's scope, plus the
org's global channel -- the channels the gateway publishes job and device
status into (MSP clients spec §2.6, `app/gateway/dispatch.py`).

THE SCOPE IS THE DOOR'S, NARROWED BY `?client=`. `read_session_identity`
returns every client the person holds any role in; the console's client selector
travels as the `client` query parameter (a UUID, not a credential, so the
query string is the right place for it) and is intersected with that scope --
narrowing never widens. A scope of every client subscribes to the org's
pattern, `ui:{org}:*`; a list subscribes to each client's channel and
`ui:{org}:global`. A malformed parameter closes 4400; an empty scope -- no
grant at all, or a client outside the scope -- closes 4403 and subscribes to
nothing.

THE DOOR IS RE-RUN WHILE THE SOCKET IS OPEN (MSP clients, final review). A
scope read once at connect would keep a revoked grant, a disabled account or
an ended session receiving that client's live events -- a job's full result
body among them -- for as long as the socket stayed open. So every
`RECHECK_INTERVAL_S` the socket re-reads the session and the person exactly as
at connect (`resolve`, `read_session_identity`, then `?client=`): a session
that ended, was replaced or went idle closes 4401; refused, or a scope that is
no longer the one subscribed, closes 4403 -- any difference, a wider one
included, since the console reconnects with backoff and subscribes to what is
current. And once the session's absolute maximum (`expires_at`) has passed it
closes 4401, checked on every turn of the loop.

NO CREDENTIAL IN THE URL, AND NONE IN THE SUBPROTOCOL ANY MORE. This endpoint
once took `?token=<jwt>`, which put a live bearer token in the request URL that
uvicorn's access line records verbatim (invariant 3, "never written to logs",
broken by the transport). The console then carried its Keycloak token in
`Sec-WebSocket-Protocol`; since own authentication phase 1 (Task 13) it
carries nothing but the session cookie the browser attaches itself, offers no
subprotocol, and this endpoint echoes none. A query-string token is still
refused (`tests/test_ui_ws.py`), and so is a subprotocol token.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket

from app.core.auth.csrf import origin_allowed
from app.core.auth.sessions import SESSION_COOKIE, SessionPrincipal, resolve
from app.core.client_scope import ClientScope, parse_client_header
from app.core.identity.door import read_session_identity
from app.gateway.dispatch import ui_channel, ui_channel_pattern, ui_global_channel

logger = logging.getLogger("its.ui-ws")

router = APIRouter(tags=["ws"])

#: Close codes. 4401: no live signed-in session, or it has ended since. 4400:
#: the `client` parameter is not one UUID. 4403: another origin, the identity
#: is not admitted, its scope is empty, or -- on a re-check -- its scope changed.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_BAD_CLIENT = 4400
CLOSE_FORBIDDEN = 4403

#: How often an open socket re-runs the door (module docstring). A module
#: constant so a test can shorten it; one tenant read per socket per minute.
RECHECK_INTERVAL_S = 60.0


def _requested_client(websocket: WebSocket) -> UUID | None:
    """The `client` query parameter: absent is None; anything but exactly one
    UUID raises ValueError -- `X-Client-Id`'s rule, through the same parser."""
    values = websocket.query_params.getlist("client")
    if not values:
        return None
    if len(values) > 1:
        raise ValueError("more than one client")  # i18n-exempt: caught, the socket closes 4400
    return parse_client_header(values[0])


def _narrowed(scope: ClientScope, requested: UUID | None) -> ClientScope:
    """`?client=` intersected with the scope: never wider, possibly EMPTY."""
    return scope if requested is None else scope.intersect(ClientScope.only([requested]))


@dataclass(frozen=True)
class _Admitted:
    """A socket past the door: its organisation, the person's scope before
    `?client=`, how to re-run the door, and when the session stops."""

    org_id: UUID
    scope: ClientScope
    recheck: Callable[[], Awaitable[ClientScope | int]]
    deadline: float


async def _session_scope(websocket: WebSocket, principal: SessionPrincipal) -> ClientScope | None:
    """The door for a session (`read_session_identity`): None when refused.

    ADMISSION, DOOR 2 OF 3 (spec §3.2): the org's email-domain rule against
    the person's row, then `users.status` and `users.kind` (a contact never
    enters the console, MSP clients spec §1.3) -- tenant-scoped through
    `org_session`, outside this route file because it runs before the
    person's client scope exists (MSP clients spec §2.2-2.3). 4403, not 4401:
    the session is live and the identity is not admitted, and a domain lockout
    must stay distinguishable from an ended session on the wire."""
    state = websocket.app.state
    identity = await read_session_identity(
        door="console-ws",
        org_id=principal.org_id,
        user_id=principal.user_id,
        settings=state.settings,
        engine=state.app_engine,
    )
    if identity.refusal is not None or identity.user_id is None:
        return None
    return identity.clients


async def _admit_session(websocket: WebSocket) -> _Admitted | int:
    """The console's session cookie (P2 design §6.5; coordinator D15).

    `Origin` FIRST, before anything is read: the cookie rides on any handshake
    the browser makes, and a WebSocket is not subject to CORS, so a page on
    another site that opens this socket would otherwise be the person
    (cross-site WebSocket hijacking). Not the console's origin: 4403. Then the
    session, untouched (an open socket is not activity): none, ended, idle or
    without its second factor, 4401. Then the person (`_session_scope`)."""
    state = websocket.app.state
    if not origin_allowed(websocket.headers.get("origin"), state.settings):
        return CLOSE_FORBIDDEN
    principal = await resolve(websocket, touch=False)
    if principal is None or not principal.factor_done:
        return CLOSE_UNAUTHENTICATED
    admitted = await _session_scope(websocket, principal)
    if admitted is None:
        return CLOSE_FORBIDDEN

    async def recheck() -> ClientScope | int:
        current = await resolve(websocket, touch=False)
        if current is None or current.session_id != principal.session_id:
            return CLOSE_UNAUTHENTICATED
        scope = await _session_scope(websocket, current)
        return CLOSE_FORBIDDEN if scope is None else scope

    return _Admitted(
        org_id=principal.org_id,
        scope=admitted,
        recheck=recheck,
        deadline=principal.expires_at.timestamp(),
    )


@router.websocket("/ws")
async def ui_websocket(websocket: WebSocket) -> None:
    state = websocket.app.state
    # The console's session cookie, and nothing else (own authentication
    # phase 1, Task 13). No cookie at all is 4401 -- "sign in", which is what
    # the console does with it -- and not a 4403 for an Origin a handshake
    # with no credential need not carry; `tests/test_session_sockets.py::
    # test_without_a_cookie_or_a_token_it_is_still_4401` holds that.
    if SESSION_COOKIE not in websocket.cookies:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    admitted = await _admit_session(websocket)
    if isinstance(admitted, int):
        await websocket.close(code=admitted)
        return

    # THE SCOPE (MSP clients spec §2.6): the door's, narrowed by the client
    # the console chose. `intersect` never widens: a client outside the scope
    # leaves the EMPTY scope, which is refused below rather than subscribed to
    # nothing, so the console learns at once that its choice is not allowed.
    try:
        requested = _requested_client(websocket)
    except ValueError:
        await websocket.close(code=CLOSE_BAD_CLIENT)
        return
    scope = _narrowed(admitted.scope, requested)
    if scope.is_empty:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    # The console offers no subprotocol, so none is echoed.
    await websocket.accept()
    pubsub = state.redis.pubsub()
    if scope.is_all:
        # Every client of THIS org and its global channel; the pattern is
        # anchored on the org, so no other org's channel matches it.
        await pubsub.psubscribe(ui_channel_pattern(admitted.org_id))
    else:
        await pubsub.subscribe(
            *(ui_channel(admitted.org_id, client_id) for client_id in sorted(scope.ids)),
            ui_global_channel(admitted.org_id),
        )
    receive_task: asyncio.Task[Any] = asyncio.create_task(websocket.receive())
    poll_task: asyncio.Task[Any] | None = None
    loop = asyncio.get_running_loop()
    next_check = loop.time() + RECHECK_INTERVAL_S
    close_code: int | None = None
    try:
        while True:
            # The session's maximum first, every turn: past it, nothing more is sent.
            if time.time() >= admitted.deadline:
                close_code = CLOSE_UNAUTHENTICATED
                break
            if loop.time() >= next_check:
                current = await admitted.recheck()
                if isinstance(current, int):
                    close_code = current
                    break
                if _narrowed(current, requested) != scope:
                    close_code = CLOSE_FORBIDDEN
                    break
                next_check = loop.time() + RECHECK_INTERVAL_S
            wait = min(1.0, next_check - loop.time(), admitted.deadline - time.time())
            current_poll: asyncio.Task[Any] = asyncio.create_task(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=max(wait, 0.05))
            )
            poll_task = current_poll
            done, _ = await asyncio.wait(
                {receive_task, current_poll}, return_when=asyncio.FIRST_COMPLETED
            )
            if receive_task in done:
                # client spoke or disconnected; we only ever expect disconnect
                incoming = receive_task.result()
                if incoming.get("type") == "websocket.disconnect":
                    break
                receive_task = asyncio.create_task(websocket.receive())
            if current_poll in done:
                message = current_poll.result()
                if message is not None:
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    await websocket.send_text(data)
    except Exception:  # noqa: BLE001 - treat any transport error as a disconnect
        pass
    finally:
        for task in (receive_task, poll_task):
            if task is not None and not task.done():
                task.cancel()
        await pubsub.unsubscribe()
        await pubsub.punsubscribe()
        await pubsub.aclose()
    if close_code is not None:
        try:
            await websocket.close(code=close_code)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass

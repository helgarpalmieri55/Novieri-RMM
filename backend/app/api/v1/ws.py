"""Operator UI WebSocket: live job/device status for the clients in the
authenticated person's scope.

Authentication uses the same OIDC bearer token as the HTTP API, carried in the
**WebSocket subprotocol header**, not in the query string. Events come from the
Redis UI channels of the clients in the person's scope, plus the org's global
channel -- the channels the gateway publishes job and device status into
(MSP clients spec §2.6, `app/gateway/dispatch.py`).

THE SCOPE IS THE DOOR'S, NARROWED BY `?client=`. `read_door_identity` returns
every client the person holds any role in; the console's client selector
travels as the `client` query parameter (a UUID, not a credential, so the
query string is the right place for it) and is intersected with that scope --
narrowing never widens. A scope of every client subscribes to the org's
pattern, `ui:{org}:*`; a list subscribes to each client's channel and
`ui:{org}:global`. A malformed parameter closes 4400; an empty scope -- no
grant at all, or a client outside the scope -- closes 4403 and subscribes to
nothing.

THE DOOR IS RE-RUN WHILE THE SOCKET IS OPEN (MSP clients, final review). A
scope read once at connect would keep a revoked grant, a disabled account or
an expired token receiving that client's live events -- a job's full result
body among them -- for as long as the socket stayed open. So every
`RECHECK_INTERVAL_S` the socket re-reads the person exactly as at connect
(`read_door_identity`, then `?client=`): refused, or a scope that is no longer
the one subscribed, closes 4403 -- any difference, a wider one included, since
the console reconnects with backoff and subscribes to what is current. And
once the token's `exp` has passed it closes 4401, checked on every turn of
the loop: the token is what admitted the socket, and it says when it stops.

WHY THE SUBPROTOCOL, AND WHAT IT REPLACED. A browser cannot set headers on a
WebSocket handshake, so this endpoint originally took `?token=<jwt>`. That put
a live bearer token in the request URL, and uvicorn's access line records the
URL verbatim — a real credential written to stdout, formatted by the logging
kernel but not redactable by it (uvicorn's records carry no `extra=` fields for
`looks_like_secret_key` to match; see `configure_logging`'s docstring in
`app/core/logging.py`). That is invariant 3 ("never written to logs") broken by
the transport rather than by any logging bug, which is why no logging change
could close it.

`new WebSocket(url, ["bearer", token])` is the one header a browser CAN
influence: the list becomes `Sec-WebSocket-Protocol`, a request header, and
headers are not in the access line. A JWT is legal in that header without
encoding — its alphabet (base64url plus `.`) is a subset of RFC 7230's `tchar`.
The server must echo back the protocol it selected or the browser fails the
handshake, so `accept(subprotocol=...)` names `bearer`, never the token.

The query parameter is GONE rather than deprecated: leaving it accepted would
leave the exposure reachable by anything that still used it, and this repo owns
the only client. `tests/test_ui_ws.py` proves both halves — a token offered in
the query string is refused, and a real handshake's log output does not contain
the token while a URL-borne one does.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope, parse_client_header
from app.core.identity.door import read_door_identity
from app.core.security import (
    TokenClaims,
    TokenValidationError,
    require_console_token,
    validate_token,
)
from app.core.ws_auth import AUTH_SUBPROTOCOL, token_from_subprotocols
from app.gateway.dispatch import ui_channel, ui_channel_pattern, ui_global_channel
from app.models import Organization

logger = logging.getLogger("its.ui-ws")

router = APIRouter(tags=["ws"])

#: Close codes. 4401: the token is not genuine, or it has expired since. 4400:
#: the `client` parameter is not one UUID. 4403: the identity is not admitted,
#: its scope is empty, or -- on a re-check -- its scope changed.
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


async def _door_scope(
    websocket: WebSocket, *, org_id: UUID, claims: TokenClaims
) -> ClientScope | None:
    """The door (`read_door_identity`), at connect and at every re-check:
    `None` when the identity is refused, else every client the person holds
    any role in."""
    state = websocket.app.state
    identity = await read_door_identity(
        door="console-ws",
        org_id=org_id,
        claims=claims,
        settings=state.settings,
        engine=state.app_engine,
    )
    return None if identity.refusal is not None else identity.clients


def _narrowed(scope: ClientScope, requested: UUID | None) -> ClientScope:
    """`?client=` intersected with the scope: never wider, possibly EMPTY."""
    return scope if requested is None else scope.intersect(ClientScope.only([requested]))


@router.websocket("/ws")
async def ui_websocket(websocket: WebSocket) -> None:
    state = websocket.app.state
    # From the subprotocol header, NEVER from `websocket.query_params` -- see
    # this module's docstring. A query-string token would be logged verbatim
    # by uvicorn's access line, which no logging-side change can redact.
    token = token_from_subprotocols(websocket)
    try:
        claims = await validate_token(token, settings=state.settings, jwks=state.jwks)
        require_console_token(claims, state.settings)
    except TokenValidationError:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    async with AsyncSession(state.admin_engine) as session:
        org_id = (
            await session.execute(
                select(Organization.id).where(Organization.slug == claims.org_slug)
            )
        ).scalar_one_or_none()
    if org_id is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    # ADMISSION, DOOR 2 OF 3 (spec §3.2): the org's email-domain rule, then
    # `users.status` and `users.kind` (a contact never enters the console,
    # MSP clients spec §1.3), read by `read_door_identity` -- tenant-scoped
    # through `org_session` rather than bolted onto the admin read above
    # (invariant 5 wants the predicate in the database), and outside this
    # route file because it runs before the person's client scope exists
    # (MSP clients spec §2.2-2.3). `identity.clients` is that scope: every
    # client the person holds any role in.
    #
    # 4403, NOT 4401, and the distinction is the point: 4401 above means the
    # token is not genuine, 4403 here means it is genuine and the identity is
    # not admitted. A single "refused" code would make a domain lockout
    # indistinguishable from an expired session on the wire, and the operator
    # debugging a lockout has only the wire. Ordering note: a token naming an
    # unknown org closes 4401 (above) rather than 4403 -- the org has to exist
    # before its setting can be read.
    #
    # A `sub` with NO row passes ADMISSION, as it did before the status check
    # existed: the HTTP door self-heals such a user into an active row, so
    # refusing it here would contradict the door next to it. What admission
    # closes is the disabled user whose row exists and whose token has not
    # yet expired. Such a `sub` holds no grant, though, so its scope is empty
    # and the scope check below refuses it -- exactly as the HTTP door shows
    # it zero rows.
    admitted = await _door_scope(websocket, org_id=org_id, claims=claims)
    if admitted is None:
        await websocket.close(code=CLOSE_FORBIDDEN)
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
    scope = _narrowed(admitted, requested)
    if scope.is_empty:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    # Echoing the selected subprotocol is mandatory: a browser that offered
    # one and got none back fails the handshake and closes the connection.
    # The NAME is echoed, never the token.
    await websocket.accept(subprotocol=AUTH_SUBPROTOCOL)
    pubsub = state.redis.pubsub()
    if scope.is_all:
        # Every client of THIS org and its global channel; the pattern is
        # anchored on the org, so no other org's channel matches it.
        await pubsub.psubscribe(ui_channel_pattern(org_id))
    else:
        await pubsub.subscribe(
            *(ui_channel(org_id, client_id) for client_id in sorted(scope.ids)),
            ui_global_channel(org_id),
        )
    receive_task: asyncio.Task[Any] = asyncio.create_task(websocket.receive())
    poll_task: asyncio.Task[Any] | None = None
    loop = asyncio.get_running_loop()
    next_check = loop.time() + RECHECK_INTERVAL_S
    close_code: int | None = None
    try:
        while True:
            # The token first, every turn: past its `exp`, nothing more is sent.
            if time.time() >= claims.exp:
                close_code = CLOSE_UNAUTHENTICATED
                break
            if loop.time() >= next_check:
                current = await _door_scope(websocket, org_id=org_id, claims=claims)
                if current is None or _narrowed(current, requested) != scope:
                    close_code = CLOSE_FORBIDDEN
                    break
                next_check = loop.time() + RECHECK_INTERVAL_S
            wait = min(1.0, next_check - loop.time(), claims.exp - time.time())
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

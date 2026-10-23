"""Operator UI WebSocket: live job/device status for the authenticated org.

Authentication uses the same OIDC bearer token as the HTTP API, carried in the
**WebSocket subprotocol header**, not in the query string. Events come from the
org's Redis UI channel — the same channel the gateway publishes job status into.

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
from typing import Any

from fastapi import APIRouter, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.identity import domain_refusal, log_admission_refusal, status_refusal
from app.core.identity.policy import resolve_console_domain
from app.core.security import TokenValidationError, require_console_token, validate_token
from app.core.tenancy import org_session
from app.core.ws_auth import AUTH_SUBPROTOCOL, token_from_subprotocols
from app.gateway.dispatch import ui_channel
from app.models import Organization, User

logger = logging.getLogger("its.ui-ws")

router = APIRouter(tags=["ws"])

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
        await websocket.close(code=4401)
        return

    async with AsyncSession(state.admin_engine) as session:
        org_id = (
            await session.execute(
                select(Organization.id).where(Organization.slug == claims.org_slug)
            )
        ).scalar_one_or_none()
    if org_id is None:
        await websocket.close(code=4401)
        return

    # `users.status`. This door never needed the `users` row before -- it
    # authorizes per-org, not per-permission -- so the lookup is new, and it is
    # TENANT-SCOPED through `org_session` rather than bolted onto the admin
    # read above: invariant 5 wants the predicate in the database, and a
    # BYPASSRLS read keyed on a token-supplied `sub` is exactly the
    # application-code-only scoping it calls a security bug.
    #
    # A `sub` with NO row is left alone, as it was before this: the HTTP door
    # self-heals such a user into an active row, so refusing here would
    # contradict the door next to it for no gain. What this closes is the
    # disabled user whose row exists and whose token has not yet expired.
    async with org_session(org_id, engine=state.app_engine, clients=ClientScope.all()) as session:
        # ADMISSION, DOOR 2 OF 3 (spec §3.2). 4403, NOT 4401, and the
        # distinction is the point: 4401 above means the token is not genuine,
        # 4403 here means it is genuine and the identity is not admitted. A
        # single "refused" code would make a domain lockout indistinguishable
        # from an expired session on the wire, and the operator debugging a
        # lockout has only the wire.
        #
        # The allowlist is the ORG's setting now (invisible-identity-layer
        # design §2.2), so it is read here rather than before the org lookup:
        # inside the tenant session, RLS-scoped, resolved per request so a
        # console change applies without a restart. Ordering note: a token
        # naming an unknown org now closes 4401 (above) rather than 4403 — the
        # org has to exist before its setting can be read, and "we do not know
        # that tenant" is the more accurate of the two answers anyway.
        domain_reason = domain_refusal(
            claims.email, await resolve_console_domain(session, org_id, state.settings)
        )
        if domain_reason is not None:
            log_admission_refusal(
                "console-ws", sub=claims.sub, email=claims.email, reason=domain_reason
            )
            await websocket.close(code=4403)
            return

        user_status = (
            await session.execute(
                select(User.status).where(User.external_subject == claims.sub)
            )
        ).scalar_one_or_none()
    if user_status is not None:
        status_reason = status_refusal(user_status)
        if status_reason is not None:
            log_admission_refusal(
                "console-ws", sub=claims.sub, email=claims.email, reason=status_reason
            )
            await websocket.close(code=4403)
            return

    # Echoing the selected subprotocol is mandatory: a browser that offered
    # one and got none back fails the handshake and closes the connection.
    # The NAME is echoed, never the token.
    await websocket.accept(subprotocol=AUTH_SUBPROTOCOL)
    pubsub = state.redis.pubsub()
    await pubsub.subscribe(ui_channel(org_id))
    receive_task: asyncio.Task[Any] = asyncio.create_task(websocket.receive())
    poll_task: asyncio.Task[Any] | None = None
    try:
        while True:
            current_poll: asyncio.Task[Any] = asyncio.create_task(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
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
        await pubsub.aclose()

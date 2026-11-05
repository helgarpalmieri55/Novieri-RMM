"""Admission for the browser's screen WebSocket (spec-screen-share.md §2.1).

``admit_tunnel_session`` is the auth/ownership gate, factored out of the
WebSocket route so it can be exercised without a live socket, and
``watch_tunnel_session`` holds an open screen to the console session that
opened it. Authorization is always server-side (RBAC + ownership), never gated
by the frontend. The credential is the console's session cookie only: the
console's Keycloak bearer left this door in own authentication phase 1
(Task 13). The guacd media tunnel that used to live here was retired with
guacd (operator decision 2026-09-23).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.requests import HTTPConnection

from app.core.auth.csrf import origin_allowed
from app.core.auth.sessions import SessionPrincipal, resolve
from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.identity.door import DoorIdentity, read_session_identity
from app.core.rbac import has_permission
from app.core.tenancy import org_session
from app.models import RemoteSession

# --------------------------------------------------------------------------- #
# Auth/ownership gate (factored out of the WS route so it is testable without a
# live socket). Raises TunnelAuthError with the WebSocket close code to use.
# --------------------------------------------------------------------------- #

#: The code this door guards; the caller's scope is where they hold it.
REMOTE_CONTROL = "remote.control"

#: The code that lets a person stop or join ANOTHER technician's session, on
#: the session's client (phase 3 design §5.3, migration 0244).
REMOTE_SUPERVISE = "remote.supervise"

#: How often an open screen re-reads the console session that admitted its
#: viewer (`watch_tunnel_session`). A module constant so a test can shorten it.
RECHECK_INTERVAL_S = 60.0

#: The close code when that session stops: the browser must sign in again.
CLOSE_SESSION_ENDED = 4401


class TunnelAuthError(Exception):
    """Authorization failure for the tunnel WS. ``code`` is the WS close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def owner_or_admin(
    session: AsyncSession, *, user_id: UUID, session_technician_id: UUID, client_id: UUID
) -> bool:
    """Spec §11 ownership predicate, shared by the tunnel WS gate and the REST
    stop route so the check is defined ONCE.

    True iff ``user_id`` owns the session (is its technician) OR holds
    ``remote.supervise`` ON THE SESSION'S CLIENT, ``client_id`` -- its
    machine's (MSP clients plan 2026-10-26 Task 14, T4-C2 item 13): a
    supervisor of another client is not one here.

    It asked ``orgs.manage`` until migration 0244 (phase 3 design §5.3): a
    code whose description -- organisation settings, users and roles --
    says nothing about watching or cutting a person's remote session. 0244
    granted ``remote.supervise`` to every role that held ``orgs.manage``, so
    the people who had the power kept it; ``orgs.manage`` alone no longer
    gives it. The name stays: the "admin" here is the supervisor.

    The caller is responsible for having already established the
    ``remote.control`` permission — this predicate only decides
    owner-or-supervisor, not the base capability.
    """
    if session_technician_id == user_id:
        return True
    return await has_permission(session, user_id, REMOTE_SUPERVISE, client_id=client_id)


async def authorize_tunnel_session(
    *,
    connection: HTTPConnection,
    session_id: UUID,
    settings: Settings,
    app_engine: AsyncEngine,
) -> tuple[UUID, UUID, ClientScope]:
    """`admit_tunnel_session` without the session it was admitted by:
    ``(org_id, viewer_id, clients)``. No route calls it -- `session_screen`
    needs the session for its watchdog -- but the door tests that only ask
    "admitted or which close code" call it directly
    (`tests/session_support.py::socket_connection`)."""
    admitted = await admit_tunnel_session(
        connection=connection, session_id=session_id, settings=settings, app_engine=app_engine
    )
    return admitted.org_id, admitted.viewer_id, admitted.clients


@dataclass(frozen=True)
class TunnelSession:
    """A viewer the console's session cookie admitted, and that session --
    which `watch_tunnel_session` holds the open screen to."""

    org_id: UUID
    viewer_id: UUID
    clients: ClientScope
    principal: SessionPrincipal


async def admit_tunnel_session(
    *,
    connection: HTTPConnection,
    session_id: UUID,
    settings: Settings,
    app_engine: AsyncEngine,
) -> TunnelSession:
    """Admit the console's session cookie to ``session_id``'s screen (P2
    design §6.5; coordinator D15), or raise ``TunnelAuthError`` with the close
    code:
      * 4403 — the handshake's `Origin` is not the console's: the cookie rides
        on a handshake any page opens, and a WebSocket is not subject to CORS;
      * 4401 — no live, signed-in session (none, ended, idle, past its
        maximum, or without its second factor), or its person is gone;
      * 4403 — not admitted (email domain, a non-active account, or a
        contact), lacks ``remote.control`` everywhere, or not the owner and
        without ``remote.supervise`` on the session's client;
      * 4404 — no such session in the caller's scope.

    ADMISSION, DOOR 3 OF 3 (spec §3.2). This door is the one with real teeth:
    it is the entry point to a live interactive desktop on a managed endpoint,
    so an identity this platform no longer admits must not reach it. The
    admission rules (`read_session_identity`: the domain rule first, then
    `users.status` and `users.kind`) are checked ahead of ``remote.control``
    and ahead of ownership -- a refusal should not depend on which of several
    reasons the caller happens to also trip. THE SCOPE IS WHERE THE PERSON
    HOLDS ``remote.control``, exactly as ``require_permission`` scopes a route
    (spec §2.1-2.2): the session lookup runs under it, so a session on a
    device of a client where they lack it does not exist for them (spec §2.6).

    Opening a screen is the person's own act, so it TOUCHES the session, as
    an HTTP request does: an open remote screen counts as activity
    (coordinator decision, Task 6 review; `watch_tunnel_session`)."""
    if not origin_allowed(connection.headers.get("origin"), settings):
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            4403, "not the console's origin"
        )
    principal = await resolve(connection, touch=True)
    if principal is None or not principal.factor_done:
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            CLOSE_SESSION_ENDED, "no live session"
        )
    identity = await read_session_identity(
        door="remote-tunnel",
        org_id=principal.org_id,
        user_id=principal.user_id,
        settings=settings,
        engine=app_engine,
        code=REMOTE_CONTROL,
    )
    org_id, viewer_id, clients = await _authorize_person(
        org_id=principal.org_id, identity=identity, session_id=session_id, app_engine=app_engine
    )
    return TunnelSession(org_id=org_id, viewer_id=viewer_id, clients=clients, principal=principal)


async def watch_tunnel_session(connection: HTTPConnection, principal: SessionPrincipal) -> int:
    """Wait until the console session that admitted an open screen stops, and
    return the code to close it with (4401).

    A SCREEN MUST NOT OUTLIVE ITS SIGN-IN. It is remote control of a live
    desktop, and design §2.4 cuts a person's sessions at once when they sign
    out, an administrator ends them, a sign-in replaces them, or the account
    is disabled. `session_screen` runs this beside the tunnel and cancels the
    tunnel when it returns. Every `RECHECK_INTERVAL_S` the handshake's cookie
    is resolved again and must still name THE SAME session (`session_id`):
    ended, replaced, its person no longer `active`, or past its maximum, it
    stops. At the session's absolute maximum (`expires_at`) it stops on its
    own, whenever the next re-check would fall.

    AN OPEN REMOTE SCREEN COUNTS AS ACTIVITY (coordinator decision, Task 6
    review). The re-read TOUCHES the session (`touch=True`), so the inactivity
    limit never cuts a screen while it is open -- the person is driving a
    desktop through it -- while everything above still does. The live-status
    socket decides the other way (`app/api/v1/ws.py`): a tab left open there
    is not activity."""
    deadline = principal.expires_at.timestamp()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return CLOSE_SESSION_ENDED
        await asyncio.sleep(min(RECHECK_INTERVAL_S, remaining))
        if time.time() >= deadline:
            return CLOSE_SESSION_ENDED
        current = await resolve(connection, touch=True)
        if current is None or current.session_id != principal.session_id:
            return CLOSE_SESSION_ENDED


async def _authorize_person(
    *, org_id: UUID, identity: DoorIdentity, session_id: UUID, app_engine: AsyncEngine
) -> tuple[UUID, UUID, ClientScope]:
    """The gate's tail: refusal (4403), no person (4401), no `remote.control`
    (4403), then the session and its owner under that scope."""
    if identity.refusal is not None:
        raise TunnelAuthError(4403, identity.refusal)
    if identity.user_id is None:
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            4401, "unknown user"
        )
    user_id, clients = identity.user_id, identity.clients
    if clients.is_empty:
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            4403, "requires remote.control"
        )

    # AUTHORIZATION, as the person: the session row and ownership are read
    # under the person's `remote.control` scope, so the session lookup
    # inherits the client RLS (spec §2.6) instead of seeing every client.
    async with org_session(org_id, engine=app_engine, clients=clients) as s:
        row = (
            await s.execute(
                select(RemoteSession.technician_id, RemoteSession.client_id).where(
                    RemoteSession.id == session_id
                )
            )
        ).one_or_none()
        if row is None:
            raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
                4404, "no such session"
            )
        if not await owner_or_admin(
            s, user_id=user_id, session_technician_id=row.technician_id, client_id=row.client_id
        ):
            raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
                4403, "not the session owner"
            )
    return org_id, user_id, clients

"""Admission for the browser's screen WebSocket (spec-screen-share.md §2.1).

``authorize_tunnel`` is the auth/ownership gate, factored out of the WebSocket
route so it can be exercised without a live socket. Authorization is always
server-side (RBAC + ownership), never gated by the frontend. The guacd media
tunnel that used to live here was retired with guacd (operator decision
2026-09-23).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.identity.door import read_door_identity
from app.core.rbac import has_permission
from app.core.security import (
    JwksCache,
    TokenValidationError,
    require_console_token,
    validate_token,
)
from app.core.tenancy import org_session
from app.models import Organization, RemoteSession

# --------------------------------------------------------------------------- #
# Auth/ownership gate (factored out of the WS route so it is testable without a
# live socket). Raises TunnelAuthError with the WebSocket close code to use.
# --------------------------------------------------------------------------- #

#: The code this door guards; the caller's scope is where they hold it.
REMOTE_CONTROL = "remote.control"

#: The code that lets a person stop or join ANOTHER technician's session, on
#: the session's client (phase 3 design §5.3, migration 0244).
REMOTE_SUPERVISE = "remote.supervise"


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


async def authorize_tunnel(
    *,
    token: str,
    session_id: UUID,
    settings: Settings,
    jwks: JwksCache,
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> tuple[UUID, UUID, ClientScope]:
    """Authenticate the token and authorize this caller for ``session_id``.

    Returns ``(org_id, user_id, clients)`` on success -- ``clients`` is the
    caller's client scope, which the screen tunnel's own sessions run under
    (MSP clients spec §2.3), exactly as a route passes ``ctx.clients``.

    Requires the ``remote.control`` permission on at least one client -- the
    session is then looked up under the clients where it is held -- AND that
    the caller either owns the session (is its technician) or holds
    ``remote.supervise`` on the session's client (``owner_or_admin``).
    Raises ``TunnelAuthError`` otherwise:
      * 4401 — bad/absent token, unknown org, or unknown user;
      * 4403 — not admitted (email domain, a non-active account, or a
        contact), lacks remote.control everywhere, or not the owner and
        without ``remote.supervise`` on the session's client;
      * 4404 — no such session in the caller's tenant.

    ADMISSION, DOOR 3 OF 3 (spec §3.2). This door is the one with real teeth:
    it is the entry point to a live interactive desktop on a managed endpoint,
    so an identity this platform no longer admits must not reach it even
    while a previously issued token is still inside its lifetime. The
    admission rules are checked FIRST, ahead of ``remote.control`` and ahead
    of ownership — a refusal should not depend on which of several reasons the
    caller happens to also trip.
    """
    try:
        claims = await validate_token(token, settings=settings, jwks=jwks)
        require_console_token(claims, settings)
    except TokenValidationError as exc:
        raise TunnelAuthError(4401, str(exc)) from exc

    async with AsyncSession(admin_engine) as s:
        org_id = (
            await s.execute(
                select(Organization.id).where(Organization.slug == claims.org_slug)
            )
        ).scalar_one_or_none()
    if org_id is None:
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            4401, "unknown organization"
        )

    # IDENTITY: the org's email-domain rule, then the `users` row and its
    # status (`read_door_identity`). It runs before the person's client scope
    # exists -- the scope is computed from those rows -- and it is the only
    # part of this gate that does (MSP clients spec §2.2-2.3).
    #
    # 4403, matching this door's existing vocabulary (4401 bad token / 4403
    # authenticated-but-refused / 4404 no such session) rather than inventing
    # a fourth code, so "the allowlist turned you away" is legible to the same
    # client-side handling that already distinguishes the other three. The
    # domain rule stays FIRST, ahead of `users.status`, `remote.control` and
    # ownership: a refusal must not depend on which of several reasons the
    # caller happens to also trip.
    #
    # `users.status`: a disabled or deleted account stops driving a remote
    # session immediately, not when its access token happens to expire.
    #
    # THE SCOPE IS WHERE THE PERSON HOLDS `remote.control` -- the code this
    # door guards, exactly as `require_permission("remote.control")` scopes a
    # route (spec §2.1-2.2). Holding it on SOME client passes; the session
    # lookup below then runs under that scope, so a session on a device of a
    # client where they lack it does not exist for them (spec §2.6).
    identity = await read_door_identity(
        door="remote-tunnel",
        org_id=org_id,
        claims=claims,
        settings=settings,
        engine=app_engine,
        code=REMOTE_CONTROL,
    )
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

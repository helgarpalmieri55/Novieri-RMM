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


class TunnelAuthError(Exception):
    """Authorization failure for the tunnel WS. ``code`` is the WS close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def owner_or_admin(
    session: AsyncSession, *, user_id: UUID, session_technician_id: UUID
) -> bool:
    """Spec §11 ownership predicate, shared by the tunnel WS gate and the REST
    stop route so the check is defined ONCE.

    True iff ``user_id`` owns the session (is its technician) OR holds
    ``orgs.manage`` (org-admin). The caller is responsible for having already
    established the ``remote.control`` permission — this predicate only decides
    owner-or-admin, not the base capability.
    """
    if session_technician_id == user_id:
        return True
    return await has_permission(session, user_id, "orgs.manage")


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

    Requires the ``remote.control`` permission AND that the caller either owns
    the session (is its technician) or holds ``orgs.manage`` (org-admin).
    Raises ``TunnelAuthError`` otherwise:
      * 4401 — bad/absent token, unknown org, or unknown user;
      * 4403 — not admitted (email domain, or a non-active account), lacks
        remote.control, or not the owner and not an org-admin;
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
    identity = await read_door_identity(
        door="remote-tunnel",
        org_id=org_id,
        claims=claims,
        settings=settings,
        engine=app_engine,
    )
    if identity.refusal is not None:
        raise TunnelAuthError(4403, identity.refusal)
    if identity.user_id is None:
        raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
            4401, "unknown user"
        )
    user_id, clients = identity.user_id, identity.clients

    # AUTHORIZATION, as the person: `remote.control`, the session row and
    # ownership are read under the person's own client scope, so the session
    # lookup inherits the client RLS (spec §2.6) instead of seeing every client.
    async with org_session(org_id, engine=app_engine, clients=clients) as s:
        if not await has_permission(s, user_id, "remote.control"):
            raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
                4403, "requires remote.control"
            )
        row = (
            await s.execute(
                select(RemoteSession.technician_id).where(RemoteSession.id == session_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
                4404, "no such session"
            )
        if not await owner_or_admin(s, user_id=user_id, session_technician_id=row):
            raise TunnelAuthError(  # i18n-exempt: only the close code reaches the browser
                4403, "not the session owner"
            )
    return org_id, user_id, clients

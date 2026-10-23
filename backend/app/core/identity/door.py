"""What a token door reads about a person before any client scope exists.

Doors 2 and 3 -- the console WebSocket (``app/api/v1/ws.py``) and the
remote-control tunnel (``app/modules/remote/tunnel.py``) -- turn a validated
token into a person by the same two admission reads: the org's email-domain
rule and the ``users`` row's status. Both reads happen BEFORE the person's
client scope can be known, because that scope is computed from the very rows
being read (MSP clients spec §2.2), so they run under ``ClientScope.all()``.
That makes this module, not the doors, the reviewed place that does it
(``ALL_SCOPE_ALLOWLIST``, spec §2.3): a route file never holds the "every
client" scope itself.

Door 1 (``app/api/deps.py::admit``) does the same reads inline, because it
also self-heals a missing ``users`` row and records the first-seen
``auth.login`` -- neither of which the other two doors do.

A CONTACT IS REFUSED HERE TOO (spec §1.3): both doors are the console's, and a
contact never enters the console. Read with ``users.status``, from the same row.

THE SCOPE IS COMPUTED HERE, in the same session, from the person's grants
(``clients_holding``): every client they hold any role in, or -- for a door
that guards one permission, like the tunnel's ``remote.control`` -- every
client they hold THAT code in. ``person_scope`` is the same computation for a
gate that already has an admitted person (``require_permission``, the portal's
gate, the MCP server), so that none of them holds the "every client" scope
itself.

THE ALLOWLIST IS THE ORG'S OWN SETTING (invisible-identity-layer design §2.2),
so it is read here, inside the tenant session, RLS-scoped and resolved per
request: a console change applies to the next connection without a restart.
It is read FIRST, ahead of ``users.status``, so a refusal does not depend on
which of several reasons the caller happens to also trip.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.identity import contact_refusal, log_admission_refusal, status_refusal
from app.core.identity.email_domain import domain_refusal
from app.core.identity.policy import resolve_console_domain
from app.core.rbac import clients_holding
from app.core.security import TokenClaims
from app.core.tenancy import org_session
from app.models import User


@dataclass(frozen=True)
class DoorIdentity:
    """The outcome of the admission reads.

    ``refusal`` is the admission refusal reason (the domain rule's or
    ``users.status``'s), already logged under the door's name, or ``None``.
    ``user_id`` is ``None`` when this ``sub`` has no ``users`` row; what that
    means is the door's decision (the WebSocket admits it, the tunnel does
    not). ``clients`` is the person's client scope -- where they hold any role,
    or the door's ``code`` -- and is EMPTY for anybody refused or unknown.
    """

    refusal: str | None
    user_id: UUID | None
    clients: ClientScope


async def read_door_identity(
    *,
    door: str,
    org_id: UUID,
    claims: TokenClaims,
    settings: Settings,
    engine: AsyncEngine,
    code: str | None = None,
) -> DoorIdentity:
    """The domain rule, then the ``users`` row, its status and its kind, for
    ``claims``; then the person's scope.

    ``door`` names the door in the refusal log line (``console-ws``,
    ``remote-tunnel``), exactly as each door named itself before this was
    shared. ``code`` is the permission the door guards: the scope is then the
    clients where the person holds it, as ``require_permission(code)`` makes it
    for a route. ``None`` (the console WebSocket, which needs no permission) is
    every client where they hold any role. Nothing is written.
    """
    nobody = ClientScope.only([])
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        domain_reason = domain_refusal(
            claims.email, await resolve_console_domain(session, org_id, settings)
        )
        if domain_reason is not None:
            log_admission_refusal(door, sub=claims.sub, email=claims.email, reason=domain_reason)
            return DoorIdentity(refusal=domain_reason, user_id=None, clients=nobody)
        row = (
            await session.execute(
                select(User.id, User.status, User.kind).where(
                    User.external_subject == claims.sub
                )
            )
        ).one_or_none()
        if row is None:
            return DoorIdentity(refusal=None, user_id=None, clients=nobody)
        user_id, user_status, user_kind = row
        # `users.status`: a disabled or deleted account is refused at once, not
        # when its access token happens to expire. Then `users.kind`: a contact
        # never enters the console.
        reason = status_refusal(user_status) or contact_refusal(user_kind)
        if reason is not None:
            log_admission_refusal(door, sub=claims.sub, email=claims.email, reason=reason)
            return DoorIdentity(refusal=reason, user_id=user_id, clients=nobody)
        scope = await clients_holding(session, user_id, code)
    return DoorIdentity(refusal=None, user_id=user_id, clients=scope)


async def person_scope(
    *, org_id: UUID, user_id: UUID, code: str | None, engine: AsyncEngine
) -> ClientScope:
    """The clients where an ADMITTED person holds ``code`` (any role, when
    ``None``): the grants are read before any scope exists, because the scope
    is what they define (spec §2.2). A gate intersects this with the person's
    own, header-narrowed scope; it never uses it to widen one."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        return await clients_holding(session, user_id, code)

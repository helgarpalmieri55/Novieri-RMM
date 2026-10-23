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

Door 1 (``app/api/deps.py::auth_context``) does the same two reads inline,
because it also self-heals a missing ``users`` row and records the first-seen
``auth.login`` -- neither of which the other two doors do.

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
from app.core.identity import log_admission_refusal, status_refusal
from app.core.identity.email_domain import domain_refusal
from app.core.identity.policy import resolve_console_domain
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
    not). ``clients`` is the person's client scope, and is EMPTY for anybody
    refused or unknown.
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
) -> DoorIdentity:
    """The domain rule, then the ``users`` row and its status, for ``claims``.

    ``door`` names the door in the refusal log line (``console-ws``,
    ``remote-tunnel``), exactly as each door named itself before this was
    shared. Nothing is written.
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
                select(User.id, User.status).where(User.external_subject == claims.sub)
            )
        ).one_or_none()
    if row is None:
        return DoorIdentity(refusal=None, user_id=None, clients=nobody)
    user_id, user_status = row
    # `users.status`: a disabled or deleted account is refused at once, not
    # when its access token happens to expire.
    status_reason = status_refusal(user_status)
    if status_reason is not None:
        log_admission_refusal(door, sub=claims.sub, email=claims.email, reason=status_reason)
        return DoorIdentity(refusal=status_reason, user_id=user_id, clients=nobody)
    # Task 4 replaces this with the person's scope.
    scope = ClientScope.all()
    return DoorIdentity(refusal=None, user_id=user_id, clients=scope)

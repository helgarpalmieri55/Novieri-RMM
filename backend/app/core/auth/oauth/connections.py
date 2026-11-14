"""Settings → AI connections, where it touches a person's grants (own
authentication design §5.3; plan 2026-11-06, Task 10; B3-28, B3-31, D12).

A person's grants (`oauth_refresh_families`) are global O rows under `users`,
client NULL, which only an every-client scope reads or writes. This module
holds that scope (`oauth_session`) for the console's AI-connections routes,
which open none themselves (`tests/test_client_catalog.py::
OAUTH_SESSION_USERS`).

- `registrations`: each named app's registration here (`oauth_clients`, G),
  whose `id` revoking names: a metadata-document client's `client_id` is a
  URL, which is no path segment (D12);
- `held_apps`: the apps holding a live grant in the organization -- the
  person's own, and everyone's for an `orgs.manage` holder -- so an app
  approved a moment ago, which has called no tool, can be revoked (B3-28;
  `CLAUDE.md` 1c(d): "every registered AI client can be revoked, with effect
  on its next call"); each says whether one of those grants is the
  person's, so a row only a colleague holds can show "revoked for you"
  (re-review f3-3, N4);
- `revoke_app_access`: the app's grants end -- the actor's for "me",
  everyone's in the organization for "everyone" -- together with the
  `mcp_revocation` row and the `mcp.app_revoked` event, in ONE transaction
  (D12). A grant that ended stops every token it issued at its next call
  (`deps.admit` reads it, D6) and its refresh token mints nothing.

THE APP'S UNSPENT CODES END FIRST (operator decision D2, part c, through
`grants.revoke_app`): their expiry set to the database's clock, never their
`used_at`, and then, in SEPARATE statements, the app's live families. A
code approved a moment before the revocation is then a plain
`invalid_grant`, and an exchange already past its code committed a family
the families' statements read and end.

A REVOCATION TAKES NO PERSON'S ROW AND NO APP ROW (`oauth.store`'s
carve-out; `guards.lock_user_row`'s lock map): the app is read unlocked,
then its codes and its families, the revocation row, and the audit chain
last. "For everyone" ends many people's grants, and taking each person's
row would put one act across every account of the organization.

IN ID ORDER (Task 10 review, Important 2). An account act on a person ends
that person's codes and grants while this ends the app's: the two share the
person's rows of this app -- two grants after a reconnect, two codes
approved within a minute. Each locks its rows of each table by `SELECT id …
ORDER BY id FOR NO KEY UPDATE` before ending them by id
(`grants._lock_in_id_order`), codes before families, so both take the
shared rows in one order and cannot deadlock -- whatever plan reads them,
which is not the case for the scans' own order.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import audit_service
from app.core.auth.oauth import grants
from app.core.auth.oauth.redirects import host_of
from app.core.auth.oauth.store import oauth_session
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.models.mcp import McpRevocation
from app.models.oauth import OAuthClient, OAuthRefreshFamily

#: The columns a `Registration` is read from.
_COLUMNS = (
    OAuthClient.id,
    OAuthClient.client_id,
    OAuthClient.client_name,
    OAuthClient.kind,
    OAuthClient.redirect_uris,
)


@dataclass(frozen=True)
class Registration:
    id: UUID
    client_id: str
    name: str | None
    kind: str
    #: What the row is titled by (plan B3-10): the host the app cannot fake --
    #: its metadata document's, or the hosts its registered redirects go to.
    host: str


@dataclass(frozen=True)
class HeldApp:
    registration: Registration
    #: Whether one of the live grants is the caller's own.
    mine: bool


def _registration(row: Any) -> Registration:
    app_id, client_id, name, kind, redirect_uris = row
    if kind == "metadata_document":
        # The one host rule (B3-10): the host the consent screen named.
        host = host_of(client_id)
    else:
        host = ", ".join(sorted({host_of(uri) for uri in redirect_uris} - {""}))
    return Registration(app_id, client_id, name, kind, host)


async def registrations(
    engine: AsyncEngine, *, org_id: UUID, client_ids: Sequence[str]
) -> dict[str, Registration]:
    """The registration of each app in `client_ids` this organization has."""
    if not client_ids:
        return {}
    async with oauth_session(org_id, engine) as session:
        rows = (
            await session.execute(
                select(*_COLUMNS).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id.in_(list(client_ids))
                )
            )
        ).all()
    found = [_registration(row) for row in rows]
    return {registration.client_id: registration for registration in found}


async def held_apps(
    engine: AsyncEngine, *, org_id: UUID, user_id: UUID, now: datetime, limit: int
) -> list[HeldApp]:
    """The apps holding a live grant here: `user_id`'s own, and anyone's when
    they hold `orgs.manage` across the organization; in `client_id` order,
    each with whether `user_id` holds one of them."""
    live = (
        OAuthRefreshFamily.org_id == org_id,
        OAuthRefreshFamily.revoked_at.is_(None),
        OAuthRefreshFamily.expires_at > now,
    )
    async with oauth_session(org_id, engine) as session:
        everyone = await has_permission(session, user_id, "orgs.manage")
        query = (
            select(*_COLUMNS)
            .join(OAuthRefreshFamily, OAuthRefreshFamily.oauth_client_id == OAuthClient.id)
            .where(OAuthClient.org_id == org_id, *live)
            .distinct()
            .order_by(OAuthClient.client_id)
            .limit(limit)
        )
        if not everyone:
            query = query.where(OAuthRefreshFamily.user_id == user_id)
        rows = (await session.execute(query)).all()
        mine = set(
            (
                await session.execute(
                    select(OAuthRefreshFamily.oauth_client_id).where(
                        OAuthRefreshFamily.user_id == user_id, *live
                    )
                )
            ).scalars()
        )
    found = [_registration(row) for row in rows]
    return [HeldApp(registration, registration.id in mine) for registration in found]


async def revoke_app_access(
    engine: AsyncEngine,
    *,
    org_id: UUID,
    actor_id: UUID,
    app_id: UUID,
    scope: Literal["me", "everyone"],
    now: datetime,
) -> int:
    """How many grants ended. 403 without `orgs.manage` for "everyone", 404
    when this organization has no such app: either way nothing is written."""
    async with oauth_session(org_id, engine) as session:
        # NO CLIENT, BY DESIGN (plan 2026-10-26 Task 14 keeps it): revoking
        # everyone writes the organisation's revocation list, which is no one
        # client's, so `orgs.manage` is asked on every client.
        if scope == "everyone" and not await has_permission(session, actor_id, "orgs.manage"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires {code}", code="orgs.manage")
            )
        # Read, never locked (the carve-out, module docstring).
        app = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.id == app_id, OAuthClient.org_id == org_id)
            )
        ).scalar_one_or_none()
        if app is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, tr("there is no such AI app in this organization")
            )
        whose = None if scope == "everyone" else actor_id
        # The app's unspent codes, then its live families, each locked in id order.
        ended = await grants.revoke_app(
            session, oauth_client_id=app.id, user_id=whose, reason="revoked_in_settings", now=now
        )
        session.add(
            McpRevocation(
                org_id=org_id, user_id=whose, client_id=app.client_id, revoked_by=actor_id
            )
        )
        # Written before the chain: `audit_service.record` takes it with a
        # `text()` execute, which does not autoflush (`guards.lock_user_row`).
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="mcp.app_revoked",
            metadata={"client_id": app.client_id, "scope": scope, "grants_revoked": ended},
        )
    return ended

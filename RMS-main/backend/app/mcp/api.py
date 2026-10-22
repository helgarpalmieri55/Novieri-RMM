"""The console's side of the MCP server: Settings → AI connections
(MCP server design §3.4, §7, §8).

    GET  /api/v1/mcp/connections                      `mcp.use`
    POST /api/v1/mcp/connections/{client_id}/revoke   `mcp.use`; "everyone"
                                                      also needs `orgs.manage`

Neither route is a tool (`app/mcp/exclusions.py::NOT_FOR_MCP`): the person
manages their AI connections in the console, not through one of them.

THE LIST IS DERIVED, NOT REGISTERED. An app "has access in this organization"
when it has acted here: the apps are the `app` (the token's `azp`) of this
org's `mcp.tool_called` audit events, read under `org_session`, so RLS keeps
another tenant's use out as well as the `WHERE` clause does. Registered apps are
realm-wide in Keycloak, shared by every organization, so the realm's client
list would show one tenant another's apps.

THE READ IS BOUNDED TO THE LAST 90 DAYS (`USAGE_WINDOW`) and to `MAX_APPS`
rows, AND INDEXED. `audit_events` is the largest table in the platform and
grows forever, and its only indexes are `(org_id, id)` and
`(org_id, target_type, target_id)` -- neither serves a `ts` window, so a window
alone would still read the org's whole audit history. Migration 0220 adds
`ix_audit_mcp_tool_called`, a partial index on `(org_id, ts) WHERE action =
'mcp.tool_called'`: this read is a range scan of one org's tool calls in the
window, and the index carries no other action's rows. The predicate here is
an inline SQL literal, never a bound parameter, so the planner can prove the
partial index applies even under a generic prepared-statement plan. An app
last used before the window drops off the list; revoking it still works,
because revocation is keyed on the client id and needs no listing.

REVOKE WRITES FIRST AND CALLS KEYCLOAK AFTER (design §3.4). The
`mcp_revocation` row and the `mcp.app_revoked` audit event commit together,
and only then is the app's consent removed in Keycloak, per person, best
effort, OUTSIDE the transaction (a slow Keycloak must not hold a database
connection or the audit chain's per-org lock). The two halves close different
doors:

- the ROW is what `/mcp` checks on every request (`app/mcp/server.py::_gate`):
  any token for that app issued before it is refused at once, Keycloak or no
  Keycloak;
- the CONSENT is what stops the app's refresh token minting a NEW access token,
  which the row alone would admit, because it was issued after the row.

So a Keycloak failure leaves the revocation in force for every token that
exists, and is reported (`keycloak_failures`) because the next refresh would
bring access back. "Everyone" removes consent for every person of the org with
a Keycloak identity, which extends the design's "Revoke for one person removes
consent" to the org-wide case: without it an org-wide revoke would last one
token lifetime.

A FAILED KEYCLOAK HALF IS ON THE RECORD. When any consent was not removed, a
second event, `mcp.app_revoke_incomplete`, says how many, so the audit log does
not read "revoked" for an app whose refresh tokens still work.

THE CLIENT ID IS ONE PATH SEGMENT AND NOT ONE OF THE PLATFORM'S OWN. It is put
in a Keycloak URL path, and `..` there once resolved to the user itself -- a
revoke for everyone deleted every identity in the organization. The route
refuses a leading dot (so `.`, `..` and every dot segment) and
`kc_client._user_url` refuses it again. The console's own client, the
provisioning and events clients and Keycloak's built-ins are refused too:
removing the console client's consent for everyone would sign the whole
organization out, and none of them is an AI app.

REVOKE NEVER DELETES THE KEYCLOAK CLIENT. It is shared by every organization
in a multi-tenant install; one organization's revocation never reaches
another's users (design §3.4).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.config import Settings
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource
from app.core.rbac import has_permission
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.models.mcp import McpRevocation
from app.modules.user_admin.kc_client import (
    KeycloakAdminConfig,
    KeycloakAdminConnection,
    KeycloakAdminError,
    revoke_consent,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

#: Both routes' gate. "Everyone" additionally needs `orgs.manage`, checked in
#: the handler because a route dependency can name one code.
_MCP_USE = Depends(require_permission("mcp.use"))

#: How far back `GET /connections` looks for tool calls. See the module docstring.
USAGE_WINDOW = timedelta(days=90)
#: At most this many apps are listed, most recently used first.
MAX_APPS = 200
#: `ix_audit_mcp_tool_called`'s predicate value, as SQL (migration 0220).
_TOOL_CALLED_SQL = "'mcp.tool_called'"

#: The shape a client id may take. Keycloak's dynamic client registration
#: issues UUIDs; this admits the general shape of a client id, but it must
#: START with a letter or digit, so no dot segment (`.`, `..`) can ever be one.
#: `kc_client._user_url` refuses dot segments again before any request.
CLIENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$"

#: Clients that are the platform's own or Keycloak's, never an AI app. Revoking
#: one is refused; the deployment's configured ids are added at request time.
FIRST_PARTY_CLIENTS = frozenset(
    {
        "it-suite",  # the console's token audience, reserved in the realm
        "it-suite-web",  # the console
        "it-suite-provisioning",
        "it-suite-events",
        "account",
        "account-console",
        "admin-cli",
        "security-admin-console",
        "realm-management",
        "broker",
    }
)

#: Keycloak calls in flight at once for one revoke, and the time all of them
#: together may take. People not reached inside the budget are counted as
#: failures; the revocation row is already committed.
KEYCLOAK_CONCURRENCY = 4
KEYCLOAK_BUDGET_SECONDS = 30.0


class McpAppOut(BaseModel):
    """One AI app that has called tools in this organization.

    The revocation flags are true when a revocation of this app is NEWER than
    its last use: `revoked_for_me` compares your own revocation with your own
    last use of the app, `revoked_for_everyone` the organization-wide
    revocation with anyone's last use. Using the app again after reconnecting
    therefore clears the flag, because the revocation no longer describes the
    app's current access.
    """

    client_id: str = Field(
        description="The app's OAuth client id, as the identity provider registered it."
    )
    people: int = Field(
        description="How many people of this organization used the app in the last 90 days."
    )
    last_used_at: datetime | None = Field(
        description="When anyone in this organization last called a tool through the app."
    )
    revoked_for_me: bool = Field(
        description="True when you revoked the app for yourself after your last use of it."
    )
    revoked_for_everyone: bool = Field(
        description="True when the app was revoked for the organization after its last use."
    )


class McpConnectionsOut(BaseModel):
    """This organization's MCP switch, the address AI apps connect to, and the
    apps that used it in the last 90 days, most recently used first."""

    enabled: bool = Field(description="Whether AI apps may connect to this organization.")
    address: str | None = Field(
        description=(
            "The MCP server's address, to give to an AI app; null when the deployment "
            "has no public console address configured."
        )
    )
    apps: list[McpAppOut] = Field(
        description=f"The apps that called tools here, at most {MAX_APPS}."
    )


class RevokeIn(BaseModel):
    """Whose access to the app to revoke."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["me", "everyone"] = Field(
        description=(
            "`me` revokes the app for you; `everyone` for the whole organization, "
            "which also needs orgs.manage."
        )
    )


class RevokeOut(BaseModel):
    """The revocation is in force; the identity provider's half may not be."""

    revoked: bool = Field(
        description="Always true: the revocation is recorded and refuses existing tokens."
    )
    keycloak_failures: int = Field(
        description=(
            "How many people's consent for the app could not be removed in the identity "
            "provider. Their existing tokens are still refused, but the app could obtain "
            "new ones until the consent is removed."
        )
    )


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


def usage_query(*, org_id: UUID, user_id: UUID, since: datetime) -> Select[Any]:
    """Per app: people, last use by anyone, last use by `user_id`, since `since`.

    `action` and the `app` key are INLINE SQL LITERALS, never bound parameters.
    `ix_audit_mcp_tool_called` is a partial index (migration 0220), and the
    planner can use it only when it can prove `action = 'mcp.tool_called'` while
    planning; a generic plan comparing `action` with a bound `$n` -- which
    asyncpg's prepared statements reach after a few executions -- never can.
    `tests/test_mcp_admin_api.py` pins both the literal and the plan.
    """
    # `metadata ->> 'app'` inline, so the SELECT and the GROUP BY are the same
    # expression to the planner, not two bound parameters that happen to match.
    app_col = AuditEvent.meta.op("->>")(literal_column("'app'"))
    last_used = func.max(AuditEvent.ts)
    my_last_used = func.max(AuditEvent.ts).filter(AuditEvent.actor_id == user_id)
    return (
        select(
            app_col,
            func.count(func.distinct(AuditEvent.actor_id)),
            last_used,
            my_last_used,
        )
        .where(
            AuditEvent.org_id == org_id,
            AuditEvent.action == literal_column(_TOOL_CALLED_SQL),
            AuditEvent.ts >= since,
            app_col.is_not(None),
            app_col != "",
        )
        .group_by(app_col)
        .order_by(last_used.desc(), app_col)
        .limit(MAX_APPS)
    )


async def list_connections(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, now: datetime
) -> list[McpAppOut]:
    """The apps that called tools in this org inside `USAGE_WINDOW`."""
    usage = (
        await session.execute(
            usage_query(org_id=org_id, user_id=user_id, since=now - USAGE_WINDOW)
        )
    ).all()
    if not usage:
        return []

    revocations = (
        await session.execute(
            select(
                McpRevocation.client_id,
                McpRevocation.user_id,
                func.max(McpRevocation.revoked_at),
            )
            .where(
                McpRevocation.org_id == org_id,
                McpRevocation.client_id.in_([row[0] for row in usage]),
                or_(McpRevocation.user_id.is_(None), McpRevocation.user_id == user_id),
            )
            .group_by(McpRevocation.client_id, McpRevocation.user_id)
        )
    ).all()
    for_everyone = {client: at for client, who, at in revocations if who is None}
    for_me = {client: at for client, who, at in revocations if who is not None}

    apps: list[McpAppOut] = []
    for client_id, people, last, mine in usage:
        everyone_at = for_everyone.get(client_id)
        me_at = for_me.get(client_id)
        apps.append(
            McpAppOut(
                client_id=client_id,
                people=people,
                last_used_at=last,
                revoked_for_everyone=everyone_at is not None and everyone_at > last,
                revoked_for_me=me_at is not None and (mine is None or me_at > mine),
            )
        )
    return apps


async def record_revocation(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: str,
    scope: Literal["me", "everyone"],
) -> list[str]:
    """Write the revocation row and its audit event, and return the Keycloak
    user ids whose consent is to be removed: the actor's own for "me", every
    person of the org who has one for "everyone" (RLS scopes that read too).
    The caller commits before calling Keycloak."""
    session.add(
        McpRevocation(
            org_id=org_id,
            user_id=None if scope == "everyone" else actor_id,
            client_id=client_id,
            revoked_by=actor_id,
        )
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="mcp.app_revoked",
        metadata={"client_id": client_id, "scope": scope},
    )
    query = select(User.external_subject).where(
        User.org_id == org_id, User.external_subject.is_not(None)
    )
    if scope == "me":
        query = query.where(User.id == actor_id)
    subjects = (await session.execute(query.order_by(User.id))).scalars().all()
    return [s for s in subjects if s]


async def remove_consents(
    settings: Settings, *, org_id: UUID, client_id: str, subjects: list[str]
) -> int:
    """Remove the app's consent for each person, over ONE admin connection
    (one client, one token), `KEYCLOAK_CONCURRENCY` calls at a time, inside
    `KEYCLOAK_BUDGET_SECONDS` in all. Returns how many were NOT removed. Never
    raises: the revocation is already committed, and a failure here is a
    number in the answer and an audit event, not a failed request.

    A deployment with no provisioning client removes nothing, so the app's
    refresh tokens still work for everyone: each person is a failure to
    report, not a quiet success."""
    removed = 0
    try:
        config = KeycloakAdminConfig.from_settings(settings)
        if config is None:
            return len(subjects)
        pending = iter(subjects)
        async with (
            asyncio.timeout(KEYCLOAK_BUDGET_SECONDS),
            KeycloakAdminConnection(config) as connection,
        ):

            async def worker() -> None:
                nonlocal removed
                # One shared iterator: each worker takes the next person, so
                # at most KEYCLOAK_CONCURRENCY calls are in flight however
                # large the organization is.
                for subject in pending:
                    try:
                        await revoke_consent(
                            config,
                            user_id=subject,
                            client_id=client_id,
                            connection=connection,
                        )
                    except Exception:  # noqa: BLE001 - best effort after commit; counted
                        logger.warning(
                            "could not remove an AI app's consent at the identity provider",
                            extra={"org_id": org_id, "client_id": client_id},
                            exc_info=True,
                        )
                        continue
                    removed += 1

            await asyncio.gather(*(worker() for _ in range(KEYCLOAK_CONCURRENCY)))
    except (TimeoutError, OSError, ValueError, KeyError, httpx.HTTPError, KeycloakAdminError):
        # Out of time; the admin token could not be had; or the provisioning
        # client is configured but its secret file is missing or empty
        # (`KeycloakAdminConfig.from_settings`).
        logger.warning(
            "an AI app's consent was not removed for everyone at the identity provider",
            extra={"org_id": org_id, "client_id": client_id},
            exc_info=True,
        )
    return len(subjects) - removed


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/connections", response_model=McpConnectionsOut)
async def get_connections(
    request: Request,
    ctx: AuthContext = _MCP_USE,
) -> McpConnectionsOut:
    """This organization's AI connections: the switch, the address to give an
    AI app, and the apps that called tools here in the last 90 days."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        org_settings = (
            await session.execute(
                select(Organization.settings).where(Organization.id == ctx.org_id)
            )
        ).scalar_one_or_none()
        apps = await list_connections(
            session, org_id=ctx.org_id, user_id=ctx.user_id, now=datetime.now(UTC)
        )
    return McpConnectionsOut(
        # `is True`, as the gate reads it: a malformed value is off.
        enabled=(org_settings or {}).get("mcp_enabled") is True,
        address=mcp_resource(request.app.state.settings),
        apps=apps,
    )


@router.post("/connections/{client_id}/revoke", response_model=RevokeOut)
async def revoke_connection(
    body: RevokeIn,
    request: Request,
    client_id: str = Path(pattern=CLIENT_ID_PATTERN, max_length=255),
    ctx: AuthContext = _MCP_USE,
) -> RevokeOut:
    """Revoke an AI app for yourself, or (with orgs.manage) for everyone in
    this organization. Tokens the app already holds are refused at once; its
    consent is then removed in the identity provider, and any person for whom
    that failed is counted in `keycloak_failures`."""
    settings: Settings = request.app.state.settings
    first_party = FIRST_PARTY_CLIENTS | {
        settings.oidc_audience,
        settings.oidc_console_client_id,
        settings.keycloak_admin_client_id,
        settings.keycloak_events_client_id,
    }
    if client_id in first_party:
        # Not an AI app: refused before anything is written or sent.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr(
                "{client_id} is part of the platform, not an AI app, and cannot be revoked",
                client_id=client_id,
            ),
        )
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        # The route is gated on `mcp.use`, which "me" needs; "everyone" also
        # needs `orgs.manage`. Refused before anything is written.
        if body.scope == "everyone" and not await has_permission(
            session, ctx.user_id, "orgs.manage"
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires {code}", code="orgs.manage")
            )
        subjects = await record_revocation(
            session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            client_id=client_id,
            scope=body.scope,
        )
    # Committed. Keycloak now, outside the transaction.
    failures = await remove_consents(
        settings, org_id=ctx.org_id, client_id=client_id, subjects=subjects
    )
    if failures:
        await _record_incomplete(
            request, ctx, client_id=client_id, scope=body.scope, failures=failures
        )
    return RevokeOut(revoked=True, keycloak_failures=failures)


async def _record_incomplete(
    request: Request,
    ctx: AuthContext,
    *,
    client_id: str,
    scope: Literal["me", "everyone"],
    failures: int,
) -> None:
    """`mcp.app_revoke_incomplete`, after the revocation is committed. Never
    raises: a failure here must not turn a revocation that is in force into a
    500 that reads as "nothing happened". The response still carries the
    failure count, and the WARNING says which org and app went unrecorded."""
    try:
        async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="mcp.app_revoke_incomplete",
                metadata={
                    "client_id": client_id,
                    "scope": scope,
                    "keycloak_failures": failures,
                },
            )
    except Exception:  # noqa: BLE001 - after commit; the answer still says it
        logger.warning(
            "could not record that an AI app's consent was not removed for everyone",
            extra={"org_id": ctx.org_id, "client_id": client_id},
            exc_info=True,
        )

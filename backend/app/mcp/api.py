"""The console's side of the MCP server: Settings → AI connections
(MCP server design §3.4, §7, §8; own authentication phase 3, plan 2026-11-06
Task 10).

    GET  /api/v1/mcp/connections                   `mcp.use`
    POST /api/v1/mcp/connections/{app_id}/revoke   `mcp.use`; "everyone"
                                                   also needs `orgs.manage`

Neither route is a tool (`app/mcp/exclusions.py::NOT_FOR_MCP`): the person
manages their AI connections in the console, not through one of them.

THE LIST IS DERIVED FROM USE, AND FROM GRANTS (plan B3-28). An app is listed
when it has acted here -- the `app` (the token's `client_id`) of this org's
`mcp.tool_called` audit events, read under `org_session`, so RLS keeps another
tenant's use out as well as the `WHERE` clause does -- or when it holds a live
grant here and has not acted yet: the person's own, and everyone's for an
`orgs.manage` holder. An app approved a moment ago has called no tool, and must
be revocable (1c(d)). Each one carries the `id` and `name` of its registration
here (`oauth_clients`, G) when it has one; revoking names that `id`, because a
metadata-document client's `client_id` is a URL, which is no path segment
(plan D12).

THE READ IS BOUNDED TO THE LAST 90 DAYS (`USAGE_WINDOW`) and to `MAX_APPS`
rows, AND INDEXED. `audit_events` is the largest table in the platform and
grows forever, and its only indexes are `(org_id, id)` and
`(org_id, target_type, target_id)` -- neither serves a `ts` window, so a window
alone would still read the org's whole audit history. Migration 0220 adds
`ix_audit_mcp_tool_called`, a partial index on `(org_id, ts) WHERE action =
'mcp.tool_called'`: this read is a range scan of one org's tool calls in the
window, and the index carries no other action's rows. The predicate here is
an inline SQL literal, never a bound parameter, so the planner can prove the
partial index applies even under a generic prepared-statement plan.

REVOKE IS ONE TRANSACTION (plan D12), in `app.core.auth.oauth.connections`,
which holds the every-client scope over a person's grants; this module opens
none (plan B3-31). The app's grants end -- the person's for "me", everyone's in
the organization for "everyone" -- together with the `mcp_revocation` row and
the `mcp.app_revoked` event, or none of them does:

- a grant that ended stops every token it issued at its next call, and its
  refresh token mints nothing (`deps.admit` reads the grant on every call,
  plan D6; `grants.refresh_grant`);
- the app's unspent authorization codes end first, by their expiry, so a code
  approved a moment before the revocation opens no grant after it (operator
  decision D2, part c);
- the ROW keeps doing what it did (`app/mcp/server.py::_gate`): any token of
  that app issued before it is refused, and the listing says the app was
  revoked until it is used again.

No identity provider is called: the platform issued the grants, and ends them
itself. One organization's revocation never reaches another's users -- the
grants are read under its own `org_id` and RLS.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import AuthContext, require_all_clients, require_permission
from app.core.auth.oauth import connections
from app.core.client_scope import ClientScope
from app.core.mcp_channel import mcp_resource
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization
from app.models.mcp import McpRevocation

router = APIRouter(prefix="/mcp", tags=["mcp"])

#: Both routes' gate. "Everyone" additionally needs `orgs.manage`, checked in
#: `connections.revoke_app_access` because a route dependency can name one code.
_MCP_USE = Depends(require_permission("mcp.use"))

#: How far back `GET /connections` looks for tool calls. See the module docstring.
USAGE_WINDOW = timedelta(days=90)
#: At most this many apps are listed, most recently used first.
MAX_APPS = 200
#: `ix_audit_mcp_tool_called`'s predicate value, as SQL (migration 0220).
_TOOL_CALLED_SQL = "'mcp.tool_called'"


class McpAppOut(BaseModel):
    """One AI app that has called tools in this organization, or that holds a
    live grant here and has not yet.

    The revocation flags are true when a revocation of this app is NEWER than
    its last use: `revoked_for_me` compares your own revocation with your own
    last use of the app, `revoked_for_everyone` the organization-wide
    revocation with anyone's last use. Using the app again after reconnecting
    therefore clears the flag, because the revocation no longer describes the
    app's current access.
    """

    client_id: str = Field(
        description=(
            "The app's OAuth client id: the one this platform issued when it registered, "
            "or the address of its client metadata document."
        )
    )
    id: UUID | None = Field(
        description=(
            "The app's registration in this organization, which revoking names; null when it "
            "has none (an app last used before it registered here, or a registration that "
            "expired), and then there is nothing left to revoke."
        )
    )
    name: str | None = Field(
        description=(
            "The name the app gave itself, as it gave it (anyone can call an app anything); "
            "null when it gave none."
        )
    )
    host: str | None = Field(
        description=(
            "What the app is named by, which it cannot fake: the host of its client metadata "
            "document, or the host(s) its registered redirects go to; null when it has no "
            "registration here."
        )
    )
    kind: Literal["registered", "metadata_document"] | None = Field(
        description=(
            "`registered` when it registered itself here (any app can, under any name), "
            "`metadata_document` when a document at its client id describes it; null when "
            "it has no registration here."
        )
    )
    people: int = Field(
        description=(
            "How many people of this organization used the app in the last 90 days; 0 for an "
            "app that holds a grant and has not called a tool yet."
        )
    )
    last_used_at: datetime | None = Field(
        description=(
            "When anyone in this organization last called a tool through the app; null when "
            "nobody has yet."
        )
    )
    revoked_for_me: bool = Field(
        description="True when you revoked the app for yourself after your last use of it."
    )
    revoked_for_everyone: bool = Field(
        description="True when the app was revoked for the organization after its last use."
    )


class McpConnectionsOut(BaseModel):
    """This organization's MCP switch, the address AI apps connect to, and the
    apps that used it in the last 90 days, most recently used first, then the
    apps holding a live grant that have not used it yet."""

    enabled: bool = Field(description="Whether AI apps may connect to this organization.")
    address: str | None = Field(
        description=(
            "The MCP server's address, to give to an AI app; null when the deployment "
            "has no public console address configured."
        )
    )
    apps: list[McpAppOut] = Field(
        description=(
            f"The apps that called tools here, most recently used first, then the apps holding "
            f"a live grant that have not yet; at most {MAX_APPS}."
        )
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
    """The revocation is in force: the app's tokens stop at their next call."""

    revoked: bool = Field(
        description="Always true: the revocation is recorded and the app's grants have ended."
    )
    grants_revoked: int = Field(
        description=(
            "How many of the app's grants ended: yours for `me`, everyone's in this "
            "organization for `everyone`."
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


def _kind(kind: str) -> Literal["registered", "metadata_document"]:
    return "metadata_document" if kind == "metadata_document" else "registered"


async def _usage_and_revocations(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    now: datetime,
    held: list[connections.HeldApp],
) -> tuple[list[Any], dict[str, datetime], dict[str, datetime]]:
    """The use (`usage_query`) and, for every app it or `held` names, the
    latest revocation for everyone and for `user_id`."""
    usage = list(
        (
            await session.execute(
                usage_query(org_id=org_id, user_id=user_id, since=now - USAGE_WINDOW)
            )
        ).all()
    )
    # The held rows read the revocations too (re-review f3-3, N4).
    asked = [row[0] for row in usage] + [held_app.registration.client_id for held_app in held]
    if not asked:
        return usage, {}, {}
    revocations = (
        await session.execute(
            select(
                McpRevocation.client_id,
                McpRevocation.user_id,
                func.max(McpRevocation.revoked_at),
            )
            .where(
                McpRevocation.org_id == org_id,
                McpRevocation.client_id.in_(asked),
                or_(McpRevocation.user_id.is_(None), McpRevocation.user_id == user_id),
            )
            .group_by(McpRevocation.client_id, McpRevocation.user_id)
        )
    ).all()
    for_everyone = {client: at for client, who, at in revocations if who is None}
    for_me = {client: at for client, who, at in revocations if who is not None}
    return usage, for_everyone, for_me


async def list_connections(
    engine: AsyncEngine, *, clients: ClientScope, org_id: UUID, user_id: UUID, now: datetime
) -> list[McpAppOut]:
    """The apps that called tools in this org inside `USAGE_WINDOW`, most
    recently used first, then those holding a live grant here that have not
    (plan B3-28); at most `MAX_APPS` in all.

    ONE TRANSACTION AT A TIME, never one inside another (the rule
    `flows.signed_in_session` states: a request never holds two connections
    at once, so a burst of these cannot drain the pool waiting on itself).
    The live grants first, in `connections`' every-client session; then the
    use and the revocations in the caller's scope (`clients`), where RLS
    keeps another tenant's audit rows out; then the registrations of the
    apps that use names."""
    held = await connections.held_apps(
        engine, org_id=org_id, user_id=user_id, now=now, limit=MAX_APPS
    )
    async with org_session(org_id, engine=engine, clients=clients) as session:
        usage, for_everyone, for_me = await _usage_and_revocations(
            session, org_id=org_id, user_id=user_id, now=now, held=held
        )
    used = [row[0] for row in usage]
    known = await connections.registrations(engine, org_id=org_id, client_ids=used)

    apps: list[McpAppOut] = []
    for client_id, people, last, mine in usage:
        registration = known.get(client_id)
        everyone_at = for_everyone.get(client_id)
        me_at = for_me.get(client_id)
        apps.append(
            McpAppOut(
                client_id=client_id,
                id=registration.id if registration else None,
                name=registration.name if registration else None,
                host=registration.host if registration else None,
                kind=_kind(registration.kind) if registration else None,
                people=people,
                last_used_at=last,
                revoked_for_everyone=everyone_at is not None and everyone_at > last,
                revoked_for_me=me_at is not None and (mine is None or me_at > mine),
            )
        )
    listed = set(used)
    for held_app in held:
        registration = held_app.registration
        if len(apps) >= MAX_APPS:
            break
        if registration.client_id in listed:
            continue
        me_at = for_me.get(registration.client_id)
        apps.append(
            McpAppOut(
                client_id=registration.client_id,
                id=registration.id,
                name=registration.name,
                host=registration.host,
                kind=_kind(registration.kind),
                people=0,
                last_used_at=None,
                # A live grant is newer than any revocation for everyone,
                # which ends every grant in its own transaction (D12).
                revoked_for_everyone=False,
                # Revoked for me, and no grant of mine since.
                revoked_for_me=me_at is not None and not held_app.mine,
            )
        )
    return apps


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/connections", response_model=McpConnectionsOut)
async def get_connections(
    request: Request,
    ctx: AuthContext = _MCP_USE,
) -> McpConnectionsOut:
    """This organization's AI connections: the switch, the address to give an
    AI app, and the apps that called tools here in the last 90 days or hold a
    live grant."""
    engine: AsyncEngine = request.app.state.app_engine
    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        org_settings = (
            await session.execute(
                select(Organization.settings).where(Organization.id == ctx.org_id)
            )
        ).scalar_one_or_none()
    apps = await list_connections(
        engine,
        clients=ctx.clients,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        now=datetime.now(UTC),
    )
    return McpConnectionsOut(
        # `is True`, as the gate reads it: a malformed value is off.
        enabled=(org_settings or {}).get("mcp_enabled") is True,
        address=mcp_resource(request.app.state.settings),
        apps=apps,
    )


@router.post("/connections/{app_id}/revoke", response_model=RevokeOut)
async def revoke_connection(
    body: RevokeIn,
    request: Request,
    app_id: UUID,
    ctx: AuthContext = _MCP_USE,
) -> RevokeOut:
    """Revoke an AI app for yourself, or (with orgs.manage) for everyone in
    this organization: its grants end, and the tokens it holds stop at their
    next call."""
    # The route is gated on `mcp.use`, which "me" needs; "everyone" also needs
    # `orgs.manage` (checked in `connections.revoke_app_access`) and -- because
    # it writes the organization's revocation list, which is no one client's --
    # a scope of every client (MSP clients spec §2.2). Refused before anything
    # is written.
    if body.scope == "everyone":
        require_all_clients(ctx)
    ended = await connections.revoke_app_access(
        request.app.state.app_engine,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        app_id=app_id,
        scope=body.scope,
        now=datetime.now(UTC),
    )
    return RevokeOut(revoked=True, grants_revoked=ended)

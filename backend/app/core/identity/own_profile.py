"""The caller's own profile, for `GET` and `PATCH /me` (MSP clients spec §2.2).

`/me` IS PERSONAL, SO IT RUNS UNDER EVERY CLIENT. A person's `users` row is a
staff row -- `client_id` NULL, a global row of an O table -- and migration
0231 hides global rows from the EMPTY scope, which is exactly the scope of an
admitted person who holds no grant yet: under the request's own scope `/me`
answered 404 to someone the door had just let in. A per-client technician's
scope reads the row but may not change a global row (`app.core.global_rows`),
so their own timezone would have been a 403. Neither is a statement about
clients: the profile belongs to the person, not to any client.

THAT IS SAFE ONLY BECAUSE OF WHAT THESE SESSIONS TOUCH, and nothing else may
be added to them:

- the caller's OWN `users` row, selected by the `user_id` the validated token
  resolved at the door (`AuthContext.user_id`), never by a request field;
- G tables, which carry no client: the grants (`user_roles`,
  `role_permissions`, `permissions`), `roles`, and the audit chain
  (`audit_events`).

No client table is queried under every client. The write surface is the four
presentation fields, written out below; what a person IS (`kind`,
`external_subject`, `client_id`, `status`) is not reachable -- `ProfileUpdate`
forbids every other field, and the database's `its_user_identity_guard` does
not apply under scope all, so the request schema is the guard here.

That makes this module, not the route file, the reviewed place that holds the
"every client" scope for `/me` (`ALL_SCOPE_ALLOWLIST`, spec §2.3: a route
file never holds it).

THE CLIENT MAP IS READ UNDER THE PERSON'S OWN SCOPE, in a session of its own
(MSP clients phase 3 design §1.3). The names, kinds and modules of the clients
a person holds a role in are client data, so RLS decides which ones they see
(CLAUDE.md invariant 5a): under the scope `bound_auth_context` starts from
(`clients_holding(..., None)`, before `X-Client-Id`), migration 0231's
`client_isolation` policy on `clients` shows exactly the clients they hold a
role in, and `unassigned` only to a grant on every client -- the rule
`GET /clients` answers by. Never every client, and never the request's
narrowed scope: the selector lists every client while one is chosen. That
makes this module the one reader of an un-narrowed scope that is not a door
(`tests/test_client_catalog.py::UNNARROWED_SCOPE_DOORS`), and the scope it
reads is handed to no route.
"""

from collections.abc import Set
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.rbac import clients_holding, scopes_by_code, user_role_names
from app.core.tenancy import org_session
from app.models import Client, User
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.services.client_sites import CLIENT_ARCHIVED


class OwnRowMissing(LookupError):
    """The admitted person's own row is gone (admission found or created it)."""


@dataclass(frozen=True)
class OwnClient:
    """A client the person holds a role in, as the selector lists it."""

    id: UUID
    name: str
    kind: str
    enabled_modules: tuple[str, ...]


@dataclass(frozen=True)
class OwnGrants:
    """What the person's grants say, read once under every client."""

    #: Every client they hold any role in: their own scope, before
    #: `X-Client-Id` (`clients_holding(..., None)`).
    scope: ClientScope
    #: Each code they hold, with the clients holding it (`scopes_by_code`).
    by_code: dict[str, ClientScope]
    roles: list[str]


@dataclass(frozen=True)
class OwnProfile:
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    #: The codes held on EVERY client -- an all-clients grant
    #: (`user_roles.client_id` NULL): `require_permission_on_every_client`'s
    #: rule, whatever `X-Client-Id` says.
    codes_everywhere: frozenset[str]
    #: The clients they hold a role in, archived ones left out: by name,
    #: `unassigned` last.
    clients: tuple[OwnClient, ...]
    #: The codes held in each of `clients`. An all-clients grant counts in
    #: every one, a client grant in its own (`user_permission_codes`).
    codes_by_client: dict[UUID, frozenset[str]]
    roles: list[str]
    #: The row's identity at the sign-in provider: where `PATCH /me` mirrors a
    #: changed language. Read from the row `user_id` selected, never a request.
    external_subject: str

    def codes_in(self, scope: ClientScope) -> frozenset[str]:
        """The codes held in at least one of `clients` that `scope` allows:
        `require_permission`'s rule for a request with that scope. A client
        outside `clients` (archived, or no role there) adds nothing."""
        held: set[str] = set()
        for client_id, codes in self.codes_by_client.items():
            if scope.allows(client_id):
                held |= codes
        return frozenset(held)


async def _own_row(session: AsyncSession, user_id: UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise OwnRowMissing(str(user_id))  # i18n-exempt: mapped to a translated 404
    return user


async def _own_grants(session: AsyncSession, user_id: UUID) -> OwnGrants:
    """The caller's grants: G tables only (`user_roles`, `role_permissions`,
    `permissions`, `roles`)."""
    return OwnGrants(
        scope=await clients_holding(session, user_id, None),
        by_code=await scopes_by_code(session, user_id),
        roles=await user_role_names(session, user_id),
    )


async def _own_clients(
    org_id: UUID, scope: ClientScope, *, engine: AsyncEngine
) -> tuple[OwnClient, ...]:
    """The clients `scope` -- the person's own -- reaches, archived ones left
    out. RLS decides which (module docstring): this session is never every
    client unless the person holds a grant on every client."""
    async with org_session(org_id, engine=engine, clients=scope) as session:
        rows = (
            await session.execute(
                select(Client.id, Client.name, Client.kind, Client.enabled_modules)
                .where(Client.org_id == org_id, Client.status != CLIENT_ARCHIVED)
                .order_by(
                    case((Client.kind == CLIENT_KIND_UNASSIGNED, 1), else_=0),
                    func.lower(Client.name),
                    Client.id,
                )
            )
        ).all()
    return tuple(
        OwnClient(
            id=row.id,
            name=row.name,
            kind=row.kind,
            enabled_modules=tuple(sorted(row.enabled_modules)),
        )
        for row in rows
    )


async def _profile(user: User, grants: OwnGrants, *, engine: AsyncEngine) -> OwnProfile:
    clients = await _own_clients(user.org_id, grants.scope, engine=engine)
    return OwnProfile(
        user_id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        timezone=user.timezone,
        locale=user.locale,
        codes_everywhere=frozenset(code for code, held in grants.by_code.items() if held.is_all),
        clients=clients,
        codes_by_client={
            client.id: frozenset(
                code for code, held in grants.by_code.items() if held.allows(client.id)
            )
            for client in clients
        },
        roles=grants.roles,
        external_subject=user.external_subject,
    )


async def read_own_profile(*, org_id: UUID, user_id: UUID, engine: AsyncEngine) -> OwnProfile:
    """`GET /me`: the caller's own row and grants under every client, then
    the clients those grants reach under the caller's own scope."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        user = await _own_row(session, user_id)
        grants = await _own_grants(session, user_id)
    return await _profile(user, grants, engine=engine)


async def read_own_locale(*, org_id: UUID, user_id: UUID, engine: AsyncEngine) -> str | None:
    """The caller's saved language, read again after a mirror (`PATCH /me`)."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        return (await _own_row(session, user_id)).locale


async def update_own_profile(
    *,
    org_id: UUID,
    user_id: UUID,
    engine: AsyncEngine,
    supplied: Set[str],
    display_name: str | None,
    phone: str | None,
    timezone: str | None,
    locale: str | None,
) -> tuple[OwnProfile, str | None]:
    """`PATCH /me`: write the SUPPLIED presentation fields of the caller's own
    row, audited (keys only), and return the profile and the locale it had."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        user = await _own_row(session, user_id)
        previous_locale = user.locale
        # WRITTEN OUT, not `setattr(user, field, ...)` over `supplied`. The
        # reflective form is one model edit away from assigning to any User
        # attribute whose name a request field happens to share; these four
        # lines are the write surface, readable as such by whoever reviews
        # this next.
        if "display_name" in supplied and display_name is not None:
            user.display_name = display_name
        if "phone" in supplied:
            user.phone = phone
        if "timezone" in supplied:
            user.timezone = timezone
        if "locale" in supplied:
            user.locale = locale
        session.add(user)

        # KEYS ONLY, never values. A phone number is directory data about a
        # person and the audit log is read far more widely than the profile
        # form; recording which fields moved is what makes the event useful
        # without turning the chain into a contact list.
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=user_id,
            action="user.profile_updated",
            target_type="user",
            target_id=str(user_id),
            metadata={"fields": sorted(supplied)},
        )
        grants = await _own_grants(session, user_id)
        await session.flush()
    # AFTER THE COMMIT, so the answer is the row as saved.
    return await _profile(user, grants, engine=engine), previous_locale

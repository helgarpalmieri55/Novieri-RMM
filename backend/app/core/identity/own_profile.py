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

No client table is queried. The write surface is the four presentation
fields, written out below; what a person IS (`kind`, `external_subject`,
`client_id`, `status`) is not reachable -- `ProfileUpdate` forbids every other
field, and the database's `its_user_identity_guard` does not apply under
scope all, so the request schema is the guard here.

That makes this module, not the route file, the reviewed place that holds the
"every client" scope for `/me` (`ALL_SCOPE_ALLOWLIST`, spec §2.3: a route
file never holds it).
"""

from collections.abc import Set
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.rbac import user_permission_codes, user_role_names
from app.core.tenancy import org_session
from app.models import User


class OwnRowMissing(LookupError):
    """The admitted person's own row is gone (admission found or created it)."""


@dataclass(frozen=True)
class OwnProfile:
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    permissions: frozenset[str]
    roles: list[str]
    #: The row's identity at the sign-in provider: where `PATCH /me` mirrors a
    #: changed language. Read from the row `user_id` selected, never a request.
    external_subject: str


async def _own_row(session: AsyncSession, user_id: UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise OwnRowMissing(str(user_id))  # i18n-exempt: mapped to a translated 404
    return user


async def _profile(session: AsyncSession, user: User) -> OwnProfile:
    return OwnProfile(
        user_id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        timezone=user.timezone,
        locale=user.locale,
        # EVERY-CLIENT GRANTS ONLY, BY DESIGN (plan 2026-10-26 Task 14 keeps it;
        # T4-C2 item 1): `/me` answers "what may I do everywhere", which is what
        # the console's chrome renders today. A map of codes per client is
        # phase 3's, with the client selector that would read it.
        permissions=frozenset(await user_permission_codes(session, user.id)),
        roles=await user_role_names(session, user.id),
        external_subject=user.external_subject,
    )


async def read_own_profile(*, org_id: UUID, user_id: UUID, engine: AsyncEngine) -> OwnProfile:
    """`GET /me`: the caller's own row and grants."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        return await _profile(session, await _own_row(session, user_id))


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
        profile = await _profile(session, user)
        await session.flush()
        return profile, previous_locale

"""People service: the person record, account mirroring, sign-in names and
who uses which computer (docs/modules/people/spec.md §1-§3).

Every query carries an explicit `org_id` predicate; RLS is the backstop.
Every operator write is audited under `people.*`.

A person belongs to ONE client (plan 2026-10-26 Task 5, migration 0235), and
every lookup by a natural key -- an address, a sign-in name -- is a lookup
within one client: two customers can each employ an ana@gmail.com, and a
computer's sign-in names a person of the computer's own client. The person of
an account is the account's client's (C4): a contact's own client, the
internal client for staff (`account_client`); `its_person_account_guard`
holds the same rule at the database.
"""

import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import (
    Device,
    DevicePerson,
    License,
    LicenseAllocation,
    Person,
    PersonLogin,
    SeatAssignment,
    SoftwareProduct,
    Subscription,
    User,
)
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.people.errors import (
    PeopleClientError,
    PeopleConflictError,
    PeopleError,
    PeopleNotFoundError,
)
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

_EDITABLE = ("display_name", "email", "department", "job_title", "start_date", "end_date")
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


async def _audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID | str,
    client_id: UUID | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """`client_id` is the subject's client: the person's, or the device's for
    who uses a device. Required, so every new event decides it."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata or {},
        client_id=client_id,
    )


def account_client(kind: str, client_id: UUID | None, org_id: UUID) -> UUID:
    """The client the person of an account belongs to (plan C4): a contact's
    own client; for staff, who belong to no client, the internal client,
    whose id is the organisation's."""
    if kind == "contact" and client_id is not None:
        return client_id
    return org_id


#: `account_client` in SQL, over `users`.
ACCOUNT_CLIENT: ColumnElement[Any] = case(
    (User.kind == "contact", User.client_id), else_=User.org_id
)


def clean_email(email: str | None) -> str | None:
    if email is None:
        return None
    stripped = email.strip()
    return stripped or None


async def load_person(session: AsyncSession, org_id: UUID, person_id: UUID) -> Person:
    person = (
        await session.execute(
            select(Person).where(Person.org_id == org_id, Person.id == person_id)
        )
    ).scalar_one_or_none()
    if person is None:
        raise PeopleNotFoundError(tr("person not found"))
    return person


async def _person_with_email(
    session: AsyncSession, org_id: UUID, email: str | None, *, client_id: UUID
) -> Person | None:
    """The person of `client_id` holding this address. One at most:
    `uq_person_client_lower_email`. Without the client, two customers'
    ana@gmail.com would be two rows here (research-2 §I2)."""
    email = clean_email(email)
    if email is None:
        return None
    return (
        await session.execute(
            select(Person).where(
                Person.org_id == org_id,
                Person.client_id == client_id,
                func.lower(Person.email) == email.lower(),
            )
        )
    ).scalar_one_or_none()


async def person_id_for_email(
    session: AsyncSession, org_id: UUID, email: str | None, *, client_id: UUID
) -> UUID | None:
    """The person of `client_id` holding this address, case-insensitively.
    Used by seats and by the sign-in resolver."""
    person = await _person_with_email(session, org_id, email, client_id=client_id)
    return person.id if person is not None else None


async def person_for_user(
    session: AsyncSession, org_id: UUID, user_id: UUID
) -> Person | None:
    """The person linked to an account, or else the one holding its address."""
    linked = (
        await session.execute(
            select(Person).where(Person.org_id == org_id, Person.user_id == user_id)
        )
    ).scalar_one_or_none()
    if linked is not None:
        return linked
    user = (
        await session.execute(
            select(User.email, User.kind, User.client_id).where(
                User.org_id == org_id, User.id == user_id
            )
        )
    ).one_or_none()
    if user is None:
        return None
    return await _person_with_email(
        session, org_id, user.email, client_id=account_client(user.kind, user.client_id, org_id)
    )


async def person_id_for_user(
    session: AsyncSession, org_id: UUID, user_id: UUID
) -> UUID | None:
    person = await person_for_user(session, org_id, user_id)
    return person.id if person is not None else None


# --------------------------------------------------------------------------- #
# Mirroring accounts (spec §3.1)
# --------------------------------------------------------------------------- #

# Each account is read with `holder`: the client its person belongs to
# (`account_client`, plan C4). An account links to, or gets, a person of THAT
# client only -- `its_person_account_guard` refuses anything else -- and an
# address is unique per client, so "the same address" means the same address
# in the same client.

_LINK_BY_EMAIL = text(
    """
    UPDATE person p SET user_id = u.id, updated_at = now()
    FROM (
        SELECT DISTINCT ON (a.holder, lower(btrim(a.email))) a.id, a.email, a.holder
        FROM (
            SELECT id, email, status, created_at,
                   CASE WHEN kind = 'contact' THEN client_id ELSE org_id END AS holder
            FROM users
            WHERE org_id = :org AND btrim(email) <> ''
              AND NOT EXISTS (SELECT 1 FROM person x WHERE x.user_id = users.id)
        ) a
        ORDER BY a.holder, lower(btrim(a.email)), (a.status = 'active') DESC,
                 a.created_at, a.id
    ) u
    WHERE p.org_id = :org AND p.user_id IS NULL AND p.email IS NOT NULL
      AND p.client_id = u.holder
      AND lower(p.email) = lower(btrim(u.email))
    """
)

# `its_client_visible(holder)` (migration 0231): an account whose person
# belongs to a client this session cannot see is skipped, not attempted. A
# technician limited to X lists people too, and a staff account's person is
# the internal client's: inserting it would fail the client policy's WITH
# CHECK -- an error, not a conflict, so not one the savepoint absorbs
# (research-2 §I1). Whoever does see that client creates it.
_CREATE_FOR_ACCOUNTS = text(
    """
    INSERT INTO person (id, org_id, client_id, display_name, email, user_id, status, source)
    SELECT gen_random_uuid(), u.org_id, u.holder,
           left(COALESCE(NULLIF(btrim(u.display_name), ''), NULLIF(u.email, ''),
                         u.external_subject), 200),
           NULLIF(u.email, ''), u.id,
           CASE WHEN u.status = 'deleted' THEN 'left' ELSE 'active' END,
           'account'
    FROM (
        SELECT DISTINCT ON (a.holder, CASE WHEN btrim(a.email) = '' THEN a.id::text
                                           ELSE lower(btrim(a.email)) END)
               a.id, a.org_id, a.holder, btrim(a.email) AS email, a.display_name,
               a.external_subject, a.status
        FROM (
            SELECT id, org_id, email, display_name, external_subject, status, created_at,
                   CASE WHEN kind = 'contact' THEN client_id ELSE org_id END AS holder
            FROM users
            WHERE org_id = :org
        ) a
        WHERE its_client_visible(a.holder)
          AND NOT EXISTS (SELECT 1 FROM person x WHERE x.user_id = a.id)
          AND NOT EXISTS (
              SELECT 1 FROM person x
              WHERE x.org_id = :org AND x.client_id = a.holder AND x.email IS NOT NULL
                AND lower(x.email) = lower(btrim(a.email)))
        ORDER BY a.holder,
                 CASE WHEN btrim(a.email) = '' THEN a.id::text ELSE lower(btrim(a.email)) END,
                 (a.status = 'active') DESC, a.created_at, a.id
    ) u
    ON CONFLICT DO NOTHING
    """
)


async def sync_accounts(session: AsyncSession, org_id: UUID) -> tuple[int, int]:
    """Give every account a person. Idempotent; returns (linked, created).

    Runs on read so new portal and console users appear without an edit to
    the kernel's admission path. A concurrent run can collide on the unique
    indexes; the savepoint makes that a no-op for this request.
    """
    try:
        async with session.begin_nested():
            linked_result = await session.execute(_LINK_BY_EMAIL, {"org": org_id})
            created_result = await session.execute(_CREATE_FOR_ACCOUNTS, {"org": org_id})
    except IntegrityError:
        # The concurrent run did the same work.
        return (0, 0)
    linked = int(getattr(linked_result, "rowcount", 0) or 0)
    created = int(getattr(created_result, "rowcount", 0) or 0)
    if linked or created:
        await _audit(
            session,
            org_id=org_id,
            actor_id=None,
            action="people.accounts_mirrored",
            target_type="org",
            target_id=org_id,
            # The organisation's bookkeeping, not one client's.
            client_id=None,
            metadata={"linked": linked, "created": created},
        )
    return (linked, created)


# --------------------------------------------------------------------------- #
# The person record
# --------------------------------------------------------------------------- #


async def list_people(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[tuple[Person, int]]:
    """People with the number of computers linked to each."""
    devices = (
        select(func.count())
        .select_from(DevicePerson)
        .where(DevicePerson.org_id == org_id, DevicePerson.person_id == Person.id)
        .scalar_subquery()
    )
    stmt = select(Person, devices).where(Person.org_id == org_id)
    if status:
        stmt = stmt.where(Person.status == status)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Person.display_name).like(like)
            | func.lower(func.coalesce(Person.email, "")).like(like)
            | func.lower(func.coalesce(Person.department, "")).like(like)
        )
    rows = (
        await session.execute(stmt.order_by(func.lower(Person.display_name)).limit(limit))
    ).all()
    return [(row[0], int(row[1])) for row in rows]


async def _client_for_new_person(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """The client a new person is filed under, loaded as every new row's is
    (`client_for_new_rows`, held FOR SHARE until commit): one this session
    cannot see is a 404, never a 403 (spec §3.2's existence oracle), an
    archived one a 409. "Sin asignar" is the inbox of what has no owner, and
    nobody works there (C10): 422."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise PeopleNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise PeopleConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise PeopleClientError(tr("a person cannot be filed under Unassigned"))


async def create_person(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    display_name: str,
    email: str | None,
    department: str | None = None,
    job_title: str | None = None,
    start_date: date | None = None,
) -> Person:
    """A person of `client_id`. With an address, the account holding it is
    linked -- only one whose person belongs to this client (`account_client`):
    a contact of another client keeps its own."""
    await _client_for_new_person(session, org_id, client_id)
    email = clean_email(email)
    if await _person_with_email(session, org_id, email, client_id=client_id) is not None:
        raise PeopleConflictError(tr("someone here already has that email address"))
    person = Person(
        org_id=org_id,
        client_id=client_id,
        display_name=display_name.strip(),
        email=email,
        department=department,
        job_title=job_title,
        start_date=start_date,
        status="active",
        source="operator",
        created_by=actor_id,
    )
    session.add(person)
    await session.flush()
    # An account with this address already exists: link it now.
    if email is not None:
        user_id = (
            await session.execute(
                select(User.id)
                .where(User.org_id == org_id, func.lower(func.btrim(User.email)) == email.lower())
                .where(ACCOUNT_CLIENT == client_id)
                .where(~select(Person.id).where(Person.user_id == User.id).exists())
                .order_by(User.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        person.user_id = user_id
        await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.created",
        target_type="person",
        target_id=person.id,
        client_id=person.client_id,
        metadata={"display_name": person.display_name, "email": email},
    )
    return person


async def update_person(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    person_id: UUID,
    changes: dict[str, Any],
) -> Person:
    person = await load_person(session, org_id, person_id)
    applied: dict[str, Any] = {}
    for field in _EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "email":
            value = clean_email(value)
            other = await _person_with_email(session, org_id, value, client_id=person.client_id)
            if other is not None and other.id != person.id:
                raise PeopleConflictError(tr("someone here already has that email address"))
        if field == "display_name":
            if value is None or not str(value).strip():
                raise PeopleError(tr("a person needs a name"))
            value = str(value).strip()
        if getattr(person, field) != value:
            setattr(person, field, value)
            applied[field] = value.isoformat() if isinstance(value, date) else value
    if applied:
        await session.flush()
        await _audit(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="people.updated",
            target_type="person",
            target_id=person.id,
            client_id=person.client_id,
            metadata={"changes": applied},
        )
    return person


async def link_account(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    person_id: UUID,
    user_id: UUID | None,
) -> Person:
    person = await load_person(session, org_id, person_id)
    if user_id is not None:
        account = (
            await session.execute(
                select(User.kind, User.client_id).where(User.org_id == org_id, User.id == user_id)
            )
        ).one_or_none()
        if account is None:
            raise PeopleError(tr("no such account"))
        # The person of an account is the account's client's (C4); the
        # database's `its_person_account_guard` is the net under this.
        if account_client(account.kind, account.client_id, org_id) != person.client_id:
            raise PeopleClientError(tr("that account belongs to another client"))
        holder = (
            await session.execute(
                select(Person.id).where(Person.org_id == org_id, Person.user_id == user_id)
            )
        ).scalar_one_or_none()
        if holder is not None and holder != person.id:
            raise PeopleConflictError(tr("that account already belongs to someone else"))
    previous = person.user_id
    person.user_id = user_id
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.account_linked" if user_id else "people.account_unlinked",
        target_type="person",
        target_id=person.id,
        client_id=person.client_id,
        metadata={
            "user_id": str(user_id) if user_id else None,
            "previous_user_id": str(previous) if previous else None,
        },
    )
    return person


# --------------------------------------------------------------------------- #
# Sign-in names and who uses which computer (spec §2)
# --------------------------------------------------------------------------- #


def normalise_login(raw: str) -> str:
    return raw.strip().lower()[:256]


def _name_key(login: str) -> str:
    """`corp\\maria.garcia` or `azuread\\mariagarcia` -> `mariagarcia`."""
    name = login.rsplit("\\", 1)[-1].split("@", 1)[0]
    return _NOT_ALNUM.sub("", name)


async def resolve_login(
    session: AsyncSession, org_id: UUID, raw: str, *, client_id: UUID
) -> UUID | None:
    """The person of `client_id` -- the computer's client -- a sign-in name
    belongs to, or None.

    First rule with exactly one match wins; ambiguity is never guessed. Every
    rule looks inside the one client: `corp\\jperez` at two customers is two
    people, and a computer of Y is never held by X's (research-2 §I6,
    `fk_device_person_person_client`).
    """
    login = normalise_login(raw)
    if not login:
        return None
    alias = (
        await session.execute(
            select(PersonLogin.person_id).where(
                PersonLogin.org_id == org_id,
                PersonLogin.client_id == client_id,
                PersonLogin.login == login,
            )
        )
    ).scalar_one_or_none()
    if alias is not None:
        return alias
    if "@" in login:
        by_email = await person_id_for_email(session, org_id, login, client_id=client_id)
        if by_email is not None:
            return by_email
    key = _name_key(login)
    if len(key) < 2:
        return None
    local_part = func.regexp_replace(
        func.lower(func.split_part(Person.email, "@", 1)), "[^a-z0-9]", "", "g"
    )
    matches = (
        await session.execute(
            select(Person.id)
            .where(
                Person.org_id == org_id,
                Person.client_id == client_id,
                Person.email.is_not(None),
                local_part == key,
            )
            .limit(2)
        )
    ).scalars().all()
    return matches[0] if len(matches) == 1 else None


async def add_login(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, person_id: UUID, login: str
) -> PersonLogin:
    person = await load_person(session, org_id, person_id)
    value = normalise_login(login)
    if not value:
        raise PeopleError(tr("a sign-in name cannot be blank"))
    existing = (
        await session.execute(
            select(PersonLogin).where(
                PersonLogin.org_id == org_id,
                PersonLogin.client_id == person.client_id,
                PersonLogin.login == value,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.person_id == person.id:
            return existing
        raise PeopleConflictError(tr("that sign-in name already belongs to someone else"))
    row = PersonLogin(org_id=org_id, person_id=person.id, login=value, created_by=actor_id)
    session.add(row)
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.login_added",
        target_type="person",
        target_id=person.id,
        client_id=person.client_id,
        metadata={"login": value},
    )
    return row


async def list_logins(session: AsyncSession, org_id: UUID, person_id: UUID) -> list[PersonLogin]:
    return list(
        (
            await session.execute(
                select(PersonLogin)
                .where(PersonLogin.org_id == org_id, PersonLogin.person_id == person_id)
                .order_by(PersonLogin.login)
            )
        ).scalars()
    )


async def remove_login(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, person_id: UUID, login_id: UUID
) -> None:
    row = (
        await session.execute(
            select(PersonLogin).where(
                PersonLogin.org_id == org_id,
                PersonLogin.person_id == person_id,
                PersonLogin.id == login_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise PeopleNotFoundError(tr("sign-in name not found"))
    await session.delete(row)
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.login_removed",
        target_type="person",
        target_id=person_id,
        client_id=row.client_id,
        metadata={"login": row.login},
    )


async def _load_device(session: AsyncSession, org_id: UUID, device_id: UUID) -> Device:
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise PeopleNotFoundError(tr("device not found"))
    return device


async def _link_row(session: AsyncSession, org_id: UUID, device_id: UUID) -> DevicePerson | None:
    return (
        await session.execute(
            select(DevicePerson).where(
                DevicePerson.org_id == org_id, DevicePerson.device_id == device_id
            )
        )
    ).scalar_one_or_none()


async def observe_login(
    session: AsyncSession, *, org_id: UUID, device_id: UUID, client_id: UUID, login: str
) -> DevicePerson:
    """Record who is signed in at a computer and, unless an operator set it,
    who uses it. Called from the inventory post-ingest hook, which passes the
    device's client: the sign-in resolves among that client's people only."""
    link = await _link_row(session, org_id, device_id)
    if link is None:
        link = DevicePerson(org_id=org_id, device_id=device_id, source="inventory")
        session.add(link)
    link.observed_login = login.strip()[:256]
    link.observed_at = datetime.now(UTC)
    if link.source == "operator":
        await session.flush()
        return link
    resolved = await resolve_login(session, org_id, login, client_id=client_id)
    previous = link.person_id
    link.person_id = resolved
    await session.flush()
    if resolved != previous:
        await _audit(
            session,
            org_id=org_id,
            actor_id=None,
            action="people.device_person_detected",
            target_type="device",
            target_id=device_id,
            client_id=client_id,
            metadata={
                "person_id": str(resolved) if resolved else None,
                "previous_person_id": str(previous) if previous else None,
                "login": link.observed_login,
            },
        )
    return link


async def set_device_person(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_id: UUID,
    person_id: UUID | None,
) -> DevicePerson:
    """Operator override. Inventory never replaces it.

    Who uses a device is one of the device's client's people: a person of
    another client is a 422 here, and `fk_device_person_person_client` at
    the database."""
    device = await _load_device(session, org_id, device_id)
    if person_id is not None:
        person = await load_person(session, org_id, person_id)
        if person.client_id != device.client_id:
            raise PeopleClientError(tr("that person belongs to another client"))
    link = await _link_row(session, org_id, device_id)
    if link is None:
        link = DevicePerson(org_id=org_id, device_id=device_id)
        session.add(link)
    previous = link.person_id
    link.person_id = person_id
    link.source = "operator"
    link.set_by = actor_id
    await session.flush()
    # Teach the resolver: the next computer this sign-in name uses resolves too.
    if person_id is not None and link.observed_login:
        login = normalise_login(link.observed_login)
        taken = (
            await session.execute(
                select(PersonLogin.id).where(
                    PersonLogin.org_id == org_id,
                    PersonLogin.client_id == device.client_id,
                    PersonLogin.login == login,
                )
            )
        ).scalar_one_or_none()
        if taken is None and login:
            session.add(
                PersonLogin(org_id=org_id, person_id=person_id, login=login, created_by=actor_id)
            )
            await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.device_person_set",
        target_type="device",
        target_id=device_id,
        client_id=device.client_id,
        metadata={
            "person_id": str(person_id) if person_id else None,
            "previous_person_id": str(previous) if previous else None,
        },
    )
    return link


async def clear_device_override(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, device_id: UUID
) -> DevicePerson | None:
    """Back to automatic, resolved again from the last observed sign-in."""
    device = await _load_device(session, org_id, device_id)
    link = await _link_row(session, org_id, device_id)
    if link is None:
        return None
    previous = link.person_id
    link.source = "inventory"
    link.set_by = None
    link.person_id = (
        await resolve_login(session, org_id, link.observed_login, client_id=device.client_id)
        if link.observed_login
        else None
    )
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.device_person_automatic",
        target_type="device",
        target_id=device_id,
        client_id=device.client_id,
        metadata={
            "person_id": str(link.person_id) if link.person_id else None,
            "previous_person_id": str(previous) if previous else None,
        },
    )
    return link


async def device_link(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> tuple[DevicePerson | None, Person | None]:
    await _load_device(session, org_id, device_id)
    link = await _link_row(session, org_id, device_id)
    if link is None or link.person_id is None:
        return link, None
    return link, await load_person(session, org_id, link.person_id)


# --------------------------------------------------------------------------- #
# Needs a person (spec §1.1, §3)
# --------------------------------------------------------------------------- #


async def unmatched(session: AsyncSession, org_id: UUID) -> dict[str, list[dict[str, Any]]]:
    seats = (
        await session.execute(
            select(SeatAssignment, Subscription.service_name)
            .join(Subscription, Subscription.id == SeatAssignment.subscription_id)
            .where(
                SeatAssignment.org_id == org_id,
                Subscription.org_id == org_id,
                SeatAssignment.person_id.is_(None),
                SeatAssignment.released_at.is_(None),
            )
            .order_by(func.lower(SeatAssignment.assignee_email))
        )
    ).all()
    devices = (
        await session.execute(
            select(Device, DevicePerson)
            .outerjoin(
                DevicePerson,
                (DevicePerson.device_id == Device.id) & (DevicePerson.org_id == org_id),
            )
            .where(
                Device.org_id == org_id,
                DevicePerson.person_id.is_(None),
                (DevicePerson.observed_login.is_not(None))
                | (func.coalesce(func.btrim(Device.owner), "") != ""),
            )
            .order_by(Device.hostname)
        )
    ).all()
    allocations = (
        await session.execute(
            select(LicenseAllocation, SoftwareProduct.name)
            .join(License, License.id == LicenseAllocation.license_id)
            .join(SoftwareProduct, SoftwareProduct.id == License.product_id)
            .where(
                LicenseAllocation.org_id == org_id,
                License.org_id == org_id,
                LicenseAllocation.target_type == "user",
                LicenseAllocation.person_id.is_(None),
                LicenseAllocation.released_at.is_(None),
            )
        )
    ).all()
    return {
        "seats": [
            {
                "seat_id": seat.id,
                "subscription_id": seat.subscription_id,
                "service_name": service_name,
                "assignee_name": seat.assignee_name,
                "assignee_email": seat.assignee_email,
            }
            for seat, service_name in seats
        ],
        "devices": [
            {
                "device_id": device.id,
                "hostname": device.hostname,
                "owner": device.owner,
                "observed_login": link.observed_login if link is not None else None,
            }
            for device, link in devices
        ],
        "allocations": [
            {
                "allocation_id": allocation.id,
                "license_id": allocation.license_id,
                "product_name": product_name,
                "user_id": allocation.target_id,
            }
            for allocation, product_name in allocations
        ],
    }


async def link_seat(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    seat_id: UUID,
    person_id: UUID | None,
) -> SeatAssignment:
    """Link a seat to a person of the seat's client (its subscription's,
    plan 2026-10-26 Task 6); with no person, add one there from the seat's
    name and address (or reuse the one of that client holding that address).
    A person of another client is a 422: `fk_seat_assignment_person_client`
    would refuse it anyway, as a 500."""
    seat = (
        await session.execute(
            select(SeatAssignment).where(
                SeatAssignment.org_id == org_id, SeatAssignment.id == seat_id
            )
        )
    ).scalar_one_or_none()
    if seat is None:
        raise PeopleNotFoundError(tr("seat not found"))
    # Read before the UPDATE: it expires the trigger-kept column.
    client_id = seat.client_id
    if person_id is not None:
        person = await load_person(session, org_id, person_id)
        if person.client_id != client_id:
            raise PeopleClientError(tr("that person belongs to another client"))
    else:
        found = await _person_with_email(
            session, org_id, seat.assignee_email, client_id=client_id
        )
        person = found or await create_person(
            session,
            org_id=org_id,
            actor_id=actor_id,
            client_id=client_id,
            display_name=seat.assignee_name.strip() or seat.assignee_email,
            email=seat.assignee_email,
        )
    seat.person_id = person.id
    await session.flush()
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="people.seat_linked",
        target_type="seat_assignment",
        target_id=seat.id,
        client_id=client_id,
        metadata={"person_id": str(person.id)},
    )
    return seat

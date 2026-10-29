"""People belong to a client (plan 2026-10-26 Task 5)."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    Device,
    DevicePerson,
    Permission,
    Person,
    Role,
    RolePermission,
    User,
    UserRole,
)
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

API = "/api/v1/people"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID, name: str = "pc") -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname=name, os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def _new(
    env: ApiEnv, headers: dict[str, str], client: UUID, name: str, email: str | None = None
) -> str:
    body: dict[str, str] = {"display_name": name, "client_id": str(client)}
    if email is not None:
        body["email"] = email
    r = await env.client.post(API, json=body, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _contact(env: ApiEnv, org: UUID, client: UUID, email: str) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        user = User(
            org_id=org, external_subject=uuid4().hex, email=email, kind="contact",
            client_id=client,
        )
        s.add(user)
        await s.commit()
        return user.id


async def _user_id(env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _grant(
    env: ApiEnv, org: UUID, sub: str, codes: tuple[str, ...], client: UUID | None
) -> None:
    """Give the user `codes` through a role of their own, on `client` (None:
    every client)."""
    user_id = await _user_id(env, sub)
    async with AsyncSession(env.admin_engine) as s:
        role = Role(org_id=org, name=f"r-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for code in codes:
            permission = (
                await s.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            s.add(RolePermission(role_id=role.id, permission_id=permission))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=client))
        await s.commit()


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_a_person_is_created_in_the_stated_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-create")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    r = await api_env.client.post(
        API, json={"display_name": "Ana", "client_id": str(x)}, headers=admin
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)
    missing = await api_env.client.post(API, json={"display_name": "Sin"}, headers=admin)
    assert missing.status_code == 422


async def test_a_person_of_y_cannot_hold_a_device_of_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-holder")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_person = (
        await api_env.client.post(API, json={"display_name": "Yo", "client_id": str(y)},
                                  headers=admin)
    ).json()["id"]
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.put(
        f"{API}/devices/{device}", json={"person_id": y_person}, headers=admin
    )
    assert r.status_code == 422, r.text
    with pytest.raises(DBAPIError, match="fk_device_person_person_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(DevicePerson(org_id=org, device_id=device, person_id=UUID(y_person)))
            await s.flush()


async def test_a_staff_account_s_person_is_the_internal_client_s(api_env: ApiEnv) -> None:
    """C4. `GET /people` creates the people of accounts that have none
    (`sync_accounts`)."""
    org = await api_env.create_org("p-staff")
    admin = await _admin(api_env, org)
    assert (await api_env.client.get(API, headers=admin)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        clients = set(
            (
                await s.execute(
                    select(Person.client_id).join(User, User.id == Person.user_id)
                    .where(Person.org_id == org, User.kind == "staff")
                )
            ).scalars()
        )
    assert clients == {org}


async def test_listing_people_as_a_technician_of_one_client_does_not_fail(
    api_env: ApiEnv,
) -> None:
    """research-2 §I1: `sync_accounts` runs inside GET /people. A staff
    account's person belongs to the internal client, which a technician
    limited to X cannot see; the insert must skip it, not raise."""
    org = await api_env.create_org("p-sync")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get(API, headers=x_tech)
    assert r.status_code == 200, r.text


async def test_the_same_login_in_two_clients_resolves_by_the_device_s_client(
    api_env: ApiEnv,
) -> None:
    from app.modules.people.service import resolve_login

    org = await api_env.create_org("p-login")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    ids = {}
    for client in (x, y):
        person = (
            await api_env.client.post(
                API, json={"display_name": "Juan", "client_id": str(client)}, headers=admin
            )
        ).json()["id"]
        r = await api_env.client.post(f"{API}/{person}/logins", json={"login": "jperez"},
                                      headers=admin)
        assert r.status_code == 201, r.text
        ids[client] = UUID(person)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = await resolve_login(s, org, "jperez", client_id=y)
    assert found == ids[y]


async def test_offboarding_a_person_needs_the_permission_in_their_client_only(
    api_env: ApiEnv,
) -> None:
    """T4-C2 (`people/api.py:69-71`): a leaver checklist was refused to
    anyone without people.offboard on EVERY client."""
    org = await api_env.create_org("p-offboard")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    person = (
        await api_env.client.post(API, json={"display_name": "Sale", "client_id": str(x)},
                                  headers=admin)
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{API}/{person}/checklists", json={"kind": "leaver"}, headers=x_tech
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Creating a person: which clients take one
# --------------------------------------------------------------------------- #


async def test_a_person_is_not_filed_under_unassigned_an_archived_or_an_unseen_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("p-refuse")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    other_org = await api_env.create_org("p-refuse-other")
    admin = await _admin(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()

    async def post(client: UUID, headers: dict[str, str] = admin) -> int:
        r = await api_env.client.post(
            API, json={"display_name": "N", "client_id": str(client)}, headers=headers
        )
        return r.status_code

    assert await post(unassigned) == 422
    assert await post(archived) == 409
    assert await post(other_org) == 404
    x_tech = await everything_on(api_env, org, x)
    assert await post(y, x_tech) == 404, "another client's id is not an existence oracle"
    assert await post(x, x_tech) == 201


async def test_one_address_per_client_not_per_organisation(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-email")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _new(api_env, admin, x, "Ana X", "ana@corp.example")
    await _new(api_env, admin, y, "Ana Y", "ana@corp.example")
    again = await api_env.client.post(
        API,
        json={"display_name": "Ana 2", "email": "ANA@corp.example", "client_id": str(x)},
        headers=admin,
    )
    assert again.status_code == 409, again.text


async def test_the_people_audit_names_the_person_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-audit")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    person = await _new(api_env, admin, x, "Auditada")
    r = await api_env.client.post(f"{API}/{person}/logins", json={"login": "aud"}, headers=admin)
    assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id).where(
                    AuditEvent.org_id == org, AuditEvent.target_id == person
                )
            )
        ).all()
    assert {(a, c) for a, c in rows} == {("people.created", x), ("people.login_added", x)}


# --------------------------------------------------------------------------- #
# A person tied to an account follows the account (C4, `its_person_account_guard`)
# --------------------------------------------------------------------------- #


async def test_a_contact_s_person_is_its_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-contact")
    x, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x, "cliente@x.example")
    admin = await _admin(api_env, org)
    assert (await api_env.client.get(API, headers=admin)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        client = (
            await s.execute(select(Person.client_id).where(Person.user_id == contact))
        ).scalar_one()
    assert client == x


async def test_an_account_is_linked_only_to_a_person_of_its_own_client(api_env: ApiEnv) -> None:
    """The sync links an account to the person holding its address -- in the
    account's client. Y's person with X's contact's address is somebody else:
    it stays unlinked, and the contact gets a person of its own in X."""
    org = await api_env.create_org("p-link-sync")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_person = await _new(api_env, admin, y, "Homónimo", "shared@corp.example")
    contact = await _contact(api_env, org, x, "shared@corp.example")
    assert (await api_env.client.get(API, headers=admin)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(Person.id, Person.client_id, Person.user_id).where(
                    Person.org_id == org, Person.email == "shared@corp.example"
                )
            )
        ).all()
    by_client = {c: (i, u) for i, c, u in rows}
    assert by_client[y] == (UUID(y_person), None)
    assert by_client[x][1] == contact


async def test_a_narrowed_technician_mirrors_the_accounts_of_its_own_clients_only(
    api_env: ApiEnv,
) -> None:
    """The sync under a scope of X: X's contact gets its person; the staff
    accounts' people (internal client) wait for someone who sees it."""
    org = await api_env.create_org("p-sync-x")
    x, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x, "c@x.example")
    x_tech = await everything_on(api_env, org, x)
    listing = await api_env.client.get(API, headers=x_tech)
    assert listing.status_code == 200, listing.text
    async with AsyncSession(api_env.admin_engine) as s:
        people = (
            await s.execute(select(Person.user_id, Person.client_id).where(Person.org_id == org))
        ).all()
    assert [(u, c) for u, c in people] == [(contact, x)]
    assert [p["user_id"] for p in listing.json()] == [str(contact)]


async def test_the_database_holds_a_person_to_its_account_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-guard")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x, "g@x.example")
    staff = await _user_id(api_env, await api_env.create_user(org))

    async def insert(client: UUID, user: UUID) -> None:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Person(org_id=org, client_id=client, display_name="g", user_id=user))
            await s.flush()

    with pytest.raises(DBAPIError, match="contact's person belongs to the contact's client"):
        await insert(y, contact)
    with pytest.raises(DBAPIError, match="staff member's person belongs to the internal client"):
        await insert(x, staff)
    await insert(x, contact)
    await insert(org, staff)


async def test_linking_an_account_of_another_client_is_refused(api_env: ApiEnv) -> None:
    org = await api_env.create_org("p-link")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x, "l@x.example")
    staff = await _user_id(api_env, await api_env.create_user(org))
    admin = await _admin(api_env, org)
    y_person = await _new(api_env, admin, y, "Y")
    x_person = await _new(api_env, admin, x, "X")

    async def link(person: str, user: UUID) -> int:
        r = await api_env.client.put(
            f"{API}/{person}/account", json={"user_id": str(user)}, headers=admin
        )
        return r.status_code

    assert await link(y_person, contact) == 422
    assert await link(x_person, staff) == 422
    assert await link(x_person, contact) == 200


async def test_a_new_person_takes_only_an_account_of_its_own_client(api_env: ApiEnv) -> None:
    """`create_person` links an account holding the same address -- only one
    the holder rule allows (a contact of that client; staff in the internal
    client)."""
    org = await api_env.create_org("p-autolink")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x, "same@corp.example")
    admin = await _admin(api_env, org)
    in_y = await api_env.client.post(
        API,
        json={"display_name": "Y", "email": "same@corp.example", "client_id": str(y)},
        headers=admin,
    )
    in_x = await api_env.client.post(
        API,
        json={"display_name": "X", "email": "same@corp.example", "client_id": str(x)},
        headers=admin,
    )
    assert (in_y.status_code, in_x.status_code) == (201, 201), (in_y.text, in_x.text)
    assert in_y.json()["user_id"] is None
    assert in_x.json()["user_id"] == str(contact)


# --------------------------------------------------------------------------- #
# Who uses a device: the device's client
# --------------------------------------------------------------------------- #


async def test_inventory_resolves_a_sign_in_within_the_device_s_client(api_env: ApiEnv) -> None:
    from app.modules.people.service import observe_login

    org = await api_env.create_org("p-observe")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _new(api_env, admin, x, "Maria X", "maria.garcia@corp.example")
    maria_y = await _new(api_env, admin, y, "Maria Y", "maria.garcia@corp.example")
    device = await _device(api_env, org, y, y_site)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        link = await observe_login(
            s, org_id=org, device_id=device, client_id=y, login="AzureAD\\MariaGarcia"
        )
        assert link.person_id == UUID(maria_y)


async def test_a_narrowed_technician_cannot_name_another_client_s_person(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("p-narrow")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_person = await _new(api_env, admin, y, "Y")
    device = await _device(api_env, org, x, x_site)
    x_tech = await everything_on(api_env, org, x)
    assert (await api_env.client.get(f"{API}/{y_person}", headers=x_tech)).status_code == 404
    r = await api_env.client.put(
        f"{API}/devices/{device}", json={"person_id": y_person}, headers=x_tech
    )
    assert r.status_code == 404, r.text


async def test_the_person_page_reads_its_sections_with_the_person_s_client(
    api_env: ApiEnv,
) -> None:
    """`detail.py:190` asked which codes the viewer holds on EVERY client, so
    a technician of X saw X's person with licences, seats and tickets hidden."""
    org = await api_env.create_org("p-detail")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    person = await _new(api_env, admin, x, "Detalle")
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get(f"{API}/{person}", headers=x_tech)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["person"]["client_id"] == str(x)
    assert body["licences"] == [] and body["seats"] == [] and body["tickets"] == []


async def test_the_fleet_list_shows_a_holder_only_where_the_viewer_reads_people(
    api_env: ApiEnv,
) -> None:
    """`devices.py:403`: `people.read` on X shows X's holders, and neither
    shows nor searches Y's."""
    org = await api_env.create_org("p-fleet")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    xavier = await _new(api_env, admin, x, "Xavier Holder")
    yolanda = await _new(api_env, admin, y, "Yolanda Holder")
    x_device = await _device(api_env, org, x, x_site, "x-pc")
    y_device = await _device(api_env, org, y, y_site, "y-pc")
    for device, person in ((x_device, xavier), (y_device, yolanda)):
        put = await api_env.client.put(
            f"{API}/devices/{device}", json={"person_id": person}, headers=admin
        )
        assert put.status_code == 200, put.text

    sub = await api_env.create_user(org)
    await _grant(api_env, org, sub, ("devices.read",), None)
    await _grant(api_env, org, sub, ("people.read",), x)
    viewer = await api_env.headers(org, sub)

    listing = await api_env.client.get("/api/v1/devices", headers=viewer)
    assert listing.status_code == 200, listing.text
    holders = {i["hostname"]: i["assigned_to"] for i in listing.json()["items"]}
    assert holders == {
        "x-pc": {"id": xavier, "display_name": "Xavier Holder"},
        "y-pc": None,
    }
    hidden = (await api_env.client.get("/api/v1/devices?q=yolanda", headers=viewer)).json()
    assert hidden["items"] == [] and hidden["total"] == 0
    found = (await api_env.client.get("/api/v1/devices?q=xavier", headers=viewer)).json()
    assert [i["hostname"] for i in found["items"]] == ["x-pc"]


async def _ticket_in(
    env: ApiEnv, org: UUID, client: UUID, requester: UUID, subject: str
) -> UUID:
    from app.models import Ticket, TicketCategory, TicketQueue

    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        queue = TicketQueue(
            org_id=org, key=f"q-{uuid4().hex[:8]}", name="Q", created_by=requester
        )
        s.add(queue)
        await s.flush()
        category = TicketCategory(
            org_id=org, key=f"c-{uuid4().hex[:8]}", name="C", type="incident",
            default_queue_id=queue.id, created_by=requester,
        )
        s.add(category)
        await s.flush()
        ticket = Ticket(
            org_id=org, client_id=client, number=f"T-{uuid4().hex[:8]}", type="incident",
            category_id=category.id, queue_id=queue.id, status="new", subject=subject,
            requester_id=requester, origin="technician", created_by=requester,
        )
        s.add(ticket)
        await s.commit()
        return ticket.id


async def test_the_person_page_shows_only_tickets_whose_client_the_viewer_reads(
    api_env: ApiEnv,
) -> None:
    """Final review B1 Important 1 (`detail.py:194,221`): the tickets section
    was gated on `ticket.read` held on the PERSON's client and its rows were
    filtered only by the `people.read` scope. A staff person is the internal
    client's (C4) but raises tickets in any client, so `people.read` +
    `ticket.read` on the internal client and `people.read` alone on X showed
    X's ticket number and subject -- while the ticket itself is a 404. The
    leaver checklist's open-ticket count (`checklists.py:362`) had the same
    shape, as a number."""
    org = await api_env.create_org("p-tickets")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    staff = await _user_id(api_env, await api_env.create_user(org))
    assert (await api_env.client.get(API, headers=admin)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        person = (
            await s.execute(select(Person.id).where(Person.user_id == staff))
        ).scalar_one()
    ticket = await _ticket_in(api_env, org, x, staff, "X's confidential subject")
    opened = await api_env.client.post(
        f"{API}/{person}/checklists", json={"kind": "leaver"}, headers=admin
    )
    assert opened.status_code == 201, opened.text

    sub = await api_env.create_user(org)
    await _grant(api_env, org, sub, ("people.read", "ticket.read"), org)
    await _grant(api_env, org, sub, ("people.read",), x)
    viewer = await api_env.headers(org, sub)
    direct = await api_env.client.get(f"/api/v1/service-desk/tickets/{ticket}", headers=viewer)
    assert direct.status_code == 404, direct.text

    def tickets_step(body: dict[str, object]) -> dict[str, object]:
        checklist = body["checklist"]
        assert isinstance(checklist, dict)
        return next(s for s in checklist["steps"] if s["key"] == "tickets")

    page = await api_env.client.get(f"{API}/{person}", headers=viewer)
    assert page.status_code == 200, page.text
    assert page.json()["tickets"] == []
    assert "confidential" not in page.text
    assert tickets_step(page.json())["checked"] is True

    await _grant(api_env, org, sub, ("ticket.read",), x)
    page = await api_env.client.get(f"{API}/{person}", headers=viewer)
    assert page.status_code == 200, page.text
    assert [t["ticket_id"] for t in page.json()["tickets"]] == [str(ticket)]
    assert tickets_step(page.json())["checked"] is False

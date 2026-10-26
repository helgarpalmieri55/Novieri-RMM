"""Tickets belong to a client (MSP clients spec §2.4, §3.2; plan 2026-10-26
Tasks 2 and 3). Two customer clients X and Y in one organisation, and the
client as the ONLY thing that tells their rows apart (spec §5 test 4)."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session, portal_session
from app.models import Device, Ticket, TicketComment, User
from app.modules.portal.schema import PortalTicketCreate
from app.modules.portal.tickets import create_my_ticket
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

API = "/api/v1/service-desk"


async def _user_id(env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(User.id).where(User.external_subject == sub))).scalar_one()


async def _desk(env: ApiEnv, org: UUID) -> dict[str, Any]:
    """An every-client admin, a queue and an incident category."""
    sub = await env.create_user(org, "org-admin")
    admin = await env.headers(org, sub)
    queue = await env.client.post(
        f"{API}/queues", json={"key": f"q{uuid4().hex[:6]}", "name": "Helpdesk"}, headers=admin
    )
    category = await env.client.post(
        f"{API}/categories",
        json={"key": f"c{uuid4().hex[:6]}", "name": "HW", "type": "incident",
              "requires_approval": False, "default_queue_id": queue.json()["id"]},
        headers=admin,
    )
    return {
        "admin": admin, "admin_id": await _user_id(env, sub),
        "queue": queue.json()["id"], "category": category.json()["id"],
    }


def _body(desk: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "incident", "category_id": desk["category"], "queue_id": desk["queue"],
        "subject": "printer", "requester_id": str(desk["admin_id"]), **extra,
    }


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="pc", os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def _contact(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        u = User(org_id=org, external_subject=uuid4().hex, email=f"c-{uuid4().hex[:8]}@x.test",
                 kind="contact", client_id=client)
        s.add(u)
        await s.commit()
        return u.id


async def test_a_ticket_for_a_device_takes_the_device_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-dev")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, device_id=str(device)), headers=desk["admin"]
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)


async def test_a_client_that_contradicts_the_device_is_refused(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-contra")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, device_id=str(device), client_id=str(y)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_a_ticket_without_device_needs_a_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-none")
    desk = await _desk(api_env, org)
    r = await api_env.client.post(f"{API}/tickets", json=_body(desk), headers=desk["admin"])
    assert r.status_code == 422, r.text
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(org)), headers=desk["admin"]
    )
    assert r.status_code == 201 and r.json()["client_id"] == str(org)


async def test_the_unassigned_client_takes_no_ticket_from_a_technician(api_env: ApiEnv) -> None:
    """C10: Unassigned is the inbox of what has no owner, not a client a
    technician files work under."""
    org = await api_env.create_org("t2-unassigned")
    desk = await _desk(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(unassigned)), headers=desk["admin"]
    )
    assert r.status_code == 422, r.text


async def test_a_requester_contact_must_belong_to_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-req")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_contact = await _contact(api_env, org, y)
    r = await api_env.client.post(
        f"{API}/tickets",
        json=_body(desk, client_id=str(x), requester_id=str(y_contact)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_an_assignee_must_see_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-assign")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, y)
    async with AsyncSession(api_env.admin_engine) as s:
        y_tech = (
            await s.execute(
                text(
                    "SELECT u.id FROM users u JOIN user_roles r ON r.user_id = u.id "
                    "WHERE r.client_id = :y"
                ),
                {"y": y},
            )
        ).scalar_one()
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(x), assigned_to=str(y_tech)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_a_technician_limited_to_x_neither_sees_nor_files_y_tickets(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("t2-narrow")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(y)), headers=desk["admin"]
        )
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)

    listed = await api_env.client.get(f"{API}/tickets", headers=x_tech)
    assert listed.status_code == 200
    assert y_ticket not in {t["id"] for t in listed.json()["items"]}
    opened = await api_env.client.get(f"{API}/tickets/{y_ticket}", headers=x_tech)
    assert opened.status_code == 404
    refused = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(y)), headers=x_tech
    )
    assert refused.status_code == 404, refused.text


async def test_a_comment_inherits_the_ticket_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-child")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "hola", "visibility": "public"},
        headers=desk["admin"],
    )
    assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        clients = set(
            (
                await s.execute(
                    select(TicketComment.client_id).where(TicketComment.ticket_id == UUID(ticket))
                )
            ).scalars()
        )
    assert clients == {x}


async def test_the_database_refuses_a_device_of_another_client(api_env: ApiEnv) -> None:
    """`its_ticket_client_guard`, below the service's own check: under scope
    all nothing else would stop it."""
    org = await api_env.create_org("t2-guard")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    y_device = await _device(api_env, org, y, y_site)
    with pytest.raises(DBAPIError, match="ticket device belongs to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Ticket(
                org_id=org, client_id=x, number=f"T-{uuid4().hex[:8]}", type="incident",
                category_id=UUID(desk["category"]), queue_id=UUID(desk["queue"]), status="new",
                subject="s", requester_id=desk["admin_id"], device_id=y_device,
                origin="technician", created_by=desk["admin_id"],
            ))
            await s.flush()


async def test_a_staff_portal_ticket_lands_in_the_internal_client(
    api_env: ApiEnv, portal_engine
) -> None:
    """C5: contacts do not sign in yet (project 2); a staff member filing a
    ticket of their own through the portal files it under Novieri's own IT."""
    org = await api_env.create_org("t2-portal")
    desk = await _desk(api_env, org)
    async with portal_session(
        org, desk["admin_id"], engine=portal_engine, clients=ClientScope.all()
    ) as s:
        detail = await create_my_ticket(
            s, org_id=org, requester_id=desk["admin_id"],
            data=PortalTicketCreate(category_id=UUID(desk["category"]), subject="mine"),
        )
    async with AsyncSession(api_env.admin_engine) as s:
        client = (
            await s.execute(select(Ticket.client_id).where(Ticket.id == detail.id))
        ).scalar_one()
    assert client == org


# --------------------------------------------------------------------------- #
# Beyond the brief's list: each write path that names another row, the list
# filter, what the ticket's audit and events say, and the database guard's
# requester clause.
# --------------------------------------------------------------------------- #


async def _ticket_of(env: ApiEnv, desk: dict[str, Any], client: UUID) -> str:
    r = await env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(client)), headers=desk["admin"]
    )
    assert r.status_code == 201, r.text
    ticket_id: str = r.json()["id"]
    return ticket_id


async def _tech_of(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    """The technician `everything_on` just made for `client`."""
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                text(
                    "SELECT u.id FROM users u JOIN user_roles r ON r.user_id = u.id "
                    "WHERE r.client_id = :c"
                ),
                {"c": client},
            )
        ).scalar_one()


async def test_a_ticket_takes_no_device_of_another_client_afterwards(api_env: ApiEnv) -> None:
    """`update_ticket`: the device a ticket gains later is its client's too."""
    org = await api_env.create_org("t2-patch")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    ticket = await _ticket_of(api_env, desk, x)
    y_device = await _device(api_env, org, y, y_site)
    refused = await api_env.client.patch(
        f"{API}/tickets/{ticket}", json={"device_id": str(y_device)}, headers=desk["admin"]
    )
    assert refused.status_code == 422, refused.text
    x_device = await _device(api_env, org, x, x_site)
    taken = await api_env.client.patch(
        f"{API}/tickets/{ticket}", json={"device_id": str(x_device)}, headers=desk["admin"]
    )
    assert taken.status_code == 200, taken.text


async def test_a_ticket_is_reassigned_only_to_someone_who_sees_its_client(
    api_env: ApiEnv,
) -> None:
    """`assign_ticket`: the same rule as on creation."""
    org = await api_env.create_org("t2-reassign")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = await _ticket_of(api_env, desk, x)
    await everything_on(api_env, org, y)
    await everything_on(api_env, org, x)
    y_tech, x_tech = await _tech_of(api_env, org, y), await _tech_of(api_env, org, x)
    refused = await api_env.client.post(
        f"{API}/tickets/{ticket}/assign", json={"assigned_to": str(y_tech)},
        headers=desk["admin"],
    )
    assert refused.status_code == 422, refused.text
    assigned = await api_env.client.post(
        f"{API}/tickets/{ticket}/assign", json={"assigned_to": str(x_tech)},
        headers=desk["admin"],
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned_to"] == str(x_tech)


async def test_a_contact_is_never_an_assignee(api_env: ApiEnv) -> None:
    """Not even one holding `ticket.read` on its own client: an assignee is a
    technician, and a contact is somebody the work is done for."""
    from app.models import Permission, Role, RolePermission, UserRole

    org = await api_env.create_org("t2-contact-assignee")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    x_contact = await _contact(api_env, org, x)
    async with AsyncSession(api_env.admin_engine) as s:
        role = Role(org_id=org, name=f"readers-{uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        read = (
            await s.execute(select(Permission.id).where(Permission.code == "ticket.read"))
        ).scalar_one()
        s.add(RolePermission(role_id=role.id, permission_id=read))
        s.add(UserRole(org_id=org, user_id=x_contact, role_id=role.id, client_id=x))
        await s.commit()
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(x), assigned_to=str(x_contact)),
        headers=desk["admin"],
    )
    assert r.status_code == 422, r.text


async def test_a_requester_contact_of_the_ticket_s_client_is_accepted(api_env: ApiEnv) -> None:
    """The positive half of the requester rule: a contact files under its own
    client."""
    org = await api_env.create_org("t2-req-ok")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    x_contact = await _contact(api_env, org, x)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(x), requester_id=str(x_contact)),
        headers=desk["admin"],
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)


async def test_an_archived_client_takes_no_new_ticket(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-archived")
    desk = await _desk(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    r = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(archived)), headers=desk["admin"]
    )
    assert r.status_code == 409, r.text


async def test_the_list_narrows_to_one_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t2-filter")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_ticket = await _ticket_of(api_env, desk, x)
    await _ticket_of(api_env, desk, y)
    listed = await api_env.client.get(
        f"{API}/tickets", params={"client_id": str(x)}, headers=desk["admin"]
    )
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()["items"]] == [x_ticket]
    assert listed.json()["total"] == 1


async def test_the_ticket_s_audit_and_events_name_its_client(api_env: ApiEnv) -> None:
    """Task 1 gave audit events and domain events a client; a ticket's carry
    the ticket's (plan Global Constraints, "Eventos y auditoría")."""
    from app.models import AuditEvent, DomainEvent

    org = await api_env.create_org("t2-events")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    ticket = await _ticket_of(api_env, desk, x)
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "hola", "visibility": "public"},
        headers=desk["admin"],
    )
    assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        audited = dict(
            (
                await s.execute(
                    select(AuditEvent.action, AuditEvent.client_id).where(
                        AuditEvent.target_type == "ticket", AuditEvent.target_id == ticket
                    )
                )
            ).all()
        )
        published = dict(
            (
                await s.execute(
                    select(DomainEvent.event_type, DomainEvent.client_id).where(
                        DomainEvent.subject_id == UUID(ticket)
                    )
                )
            ).all()
        )
    assert audited == {"ticket.created": x, "ticket.comment_added": x}
    assert published == {"ticket.comment_added": x}


async def test_a_portal_scope_without_the_internal_client_files_nothing(
    api_env: ApiEnv, portal_engine
) -> None:
    """C5's other half: the staff member's client is the internal one, and a
    portal session narrowed away from it has nowhere to file -- a 404 like
    everything else the portal cannot see, never a policy violation (500)."""
    from app.modules.portal.errors import PortalNotFoundError

    org = await api_env.create_org("t2-portal-narrow")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    with pytest.raises(PortalNotFoundError):
        async with portal_session(
            org, desk["admin_id"], engine=portal_engine, clients=ClientScope.only([x])
        ) as s:
            await create_my_ticket(
                s, org_id=org, requester_id=desk["admin_id"],
                data=PortalTicketCreate(category_id=UUID(desk["category"]), subject="mine"),
            )
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.execute(select(Ticket.id).where(Ticket.org_id == org))).all() == []


async def test_the_database_refuses_a_contact_of_another_client_as_requester(
    api_env: ApiEnv,
) -> None:
    """`its_ticket_client_guard`'s requester clause, under scope all."""
    org = await api_env.create_org("t2-guard-req")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_contact = await _contact(api_env, org, y)
    with pytest.raises(DBAPIError, match="ticket requester is a contact of another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Ticket(
                org_id=org, client_id=x, number=f"T-{uuid4().hex[:8]}", type="incident",
                category_id=UUID(desk["category"]), queue_id=UUID(desk["queue"]), status="new",
                subject="s", requester_id=y_contact, origin="technician",
                created_by=desk["admin_id"],
            ))
            await s.flush()


async def test_an_approver_role_held_on_another_client_decides_nothing(
    api_env: ApiEnv,
) -> None:
    """research-1 §I10: the approver role must be held on the ticket's client
    (or on every client). Held on Y it does not approve X's request; held on
    X it does."""
    from app.models import Role, TicketCategory, UserRole
    from app.modules.service_desk.approvals import decide_approval
    from app.modules.service_desk.errors import TicketValidationError

    org = await api_env.create_org("t2-approver")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    approver = await _user_id(api_env, await api_env.create_user(org, "org-admin"))
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org, name=f"approvers-{uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        s.add(UserRole(org_id=org, user_id=approver, role_id=role.id, client_id=y))
        category = TicketCategory(
            org_id=org, key=f"r{uuid4().hex[:6]}", name="Access", type="request",
            requires_approval=True, approver_role_id=role.id,
            default_queue_id=UUID(desk["queue"]), created_by=desk["admin_id"],
        )
        s.add(category)
        await s.commit()
    r = await api_env.client.post(
        f"{API}/tickets",
        json=_body(desk, type="request", category_id=str(category.id), client_id=str(x)),
        headers=desk["admin"],
    )
    assert r.status_code == 201 and r.json()["status"] == "pending_approval", r.text
    ticket = UUID(r.json()["id"])

    with pytest.raises(TicketValidationError, match="role required"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await decide_approval(s, org, approver, ticket, approve=True)

    async with AsyncSession(api_env.admin_engine) as s:
        s.add(UserRole(org_id=org, user_id=approver, role_id=role.id, client_id=x))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        decided = await decide_approval(s, org, approver, ticket, approve=True)
        assert decided.state == "approved"


async def test_the_database_refuses_a_requester_the_session_cannot_see(
    api_env: ApiEnv,
) -> None:
    """The guard reads the requester under the session's own RLS: a
    technician limited to X cannot name Y's contact even by its id."""
    org = await api_env.create_org("t2-guard-hidden")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    y_contact = await _contact(api_env, org, y)
    with pytest.raises(DBAPIError, match="ticket requester is not visible"):
        async with org_session(
            org, clients=ClientScope.only([x]), engine=api_env.app_engine
        ) as s:
            s.add(Ticket(
                org_id=org, client_id=x, number=f"T-{uuid4().hex[:8]}", type="incident",
                category_id=UUID(desk["category"]), queue_id=UUID(desk["queue"]), status="new",
                subject="s", requester_id=y_contact, origin="technician",
                created_by=desk["admin_id"],
            ))
            await s.flush()

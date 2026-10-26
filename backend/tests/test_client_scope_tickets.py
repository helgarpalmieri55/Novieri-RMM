"""Tickets belong to a client (MSP clients spec §2.4, §3.2; plan 2026-10-26
Tasks 2 and 3). Two customer clients X and Y in one organisation, and the
client as the ONLY thing that tells their rows apart (spec §5 test 4)."""

import asyncio
from datetime import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session, portal_session
from app.models import Device, SlaPolicy, Ticket, TicketComment, TicketSla, User
from app.modules.portal.schema import PortalTicketCreate
from app.modules.portal.tickets import create_my_ticket
from app.modules.sla.policies import resolve_policy
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv
from tests.test_sla_migration import _seed_calendar, _seed_user

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


async def test_a_technician_limited_to_x_files_and_edits_x_tickets(api_env: ApiEnv) -> None:
    """Task 2 review: the narrowed-RLS path. `its_ticket_client_guard` reads
    `users` and `devices` under the session's own scope, so a technician
    limited to X files a ticket of X without a device and one with an X
    device, and moves the second to another X device."""
    org = await api_env.create_org("t2-narrow-ok")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    plain = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=x_tech
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["client_id"] == str(x)
    first, second = await _device(api_env, org, x, x_site), await _device(api_env, org, x, x_site)
    with_device = await api_env.client.post(
        f"{API}/tickets", json=_body(desk, device_id=str(first)), headers=x_tech
    )
    assert with_device.status_code == 201, with_device.text
    assert with_device.json()["client_id"] == str(x)
    patched = await api_env.client.patch(
        f"{API}/tickets/{with_device.json()['id']}", json={"device_id": str(second)},
        headers=x_tech,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["device_id"] == str(second)


# --------------------------------------------------------------------------- #
# Task 3: the SLA of a ticket's client (plan 2026-10-26 D2), moving a ticket
# between clients (spec §2.4, C9, C10) and notices inside the client
# (research-1 §I9).
# --------------------------------------------------------------------------- #

_ALWAYS = [(d, time(0, 0), time(0, 0)) for d in range(7)]


async def _policy(env: ApiEnv, org: UUID, client: UUID | None, hours: int, **level: Any) -> UUID:
    """One active policy of `client` (None: a global one) on a 24/7 calendar."""
    calendar = await _seed_calendar(env, org, windows=_ALWAYS)
    actor = await _seed_user(env, org)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        p = SlaPolicy(
            org_id=org, client_id=client, active=True, calendar_id=calendar,
            response_target_minutes=hours * 60, resolution_target_minutes=hours * 120,
            created_by=actor, **level,
        )
        s.add(p)
        await s.commit()
        return p.id


async def test_a_client_with_policies_of_its_own_uses_only_those(api_env: ApiEnv) -> None:
    """D2. Under scope all -- the email poller and the sweeps run there, and
    RLS then shows every client's policies -- so the client predicate in the
    code is what is tested (spec §5 test 4)."""
    org = await api_env.create_org("t3-sla")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    queue = UUID(desk["queue"])
    global_specific = await _policy(api_env, org, None, 8, queue_id=queue, priority="high")
    x_catch_all = await _policy(api_env, org, x, 1)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await resolve_policy(s, org, client_id=x, queue_id=queue, priority="high")
        for_y = await resolve_policy(s, org, client_id=y, queue_id=queue, priority="high")
    assert for_x is not None and for_x.id == x_catch_all
    assert for_y is not None and for_y.id == global_specific


async def test_a_client_without_policies_falls_back_to_the_global_ones(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-sla-fallback")
    x, _ = await client_with_site(api_env, org)
    global_catch_all = await _policy(api_env, org, None, 4)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = await resolve_policy(s, org, client_id=x, queue_id=None, priority=None)
    assert found is not None and found.id == global_catch_all


async def test_another_client_s_lone_policy_is_never_applied(api_env: ApiEnv) -> None:
    """Under scope all, Y's only policy is visible -- and is not X's SLA. With
    no policy of its own and no global one, X has no SLA (spec §3)."""
    org = await api_env.create_org("t3-sla-other")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await _policy(api_env, org, y, 2)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await resolve_policy(s, org, client_id=x, queue_id=None, priority=None) is None


async def test_an_inactive_policy_of_the_client_does_not_shadow_the_global_ones(
    api_env: ApiEnv,
) -> None:
    """D2 counts a client's ACTIVE policies: a retired one is history and
    leaves the client on the global rules."""
    org = await api_env.create_org("t3-sla-retired")
    x, _ = await client_with_site(api_env, org)
    global_catch_all = await _policy(api_env, org, None, 4)
    retired = await _policy(api_env, org, x, 1)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("UPDATE sla_policy SET active = false WHERE id = :p"), {"p": retired})
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = await resolve_policy(s, org, client_id=x, queue_id=None, priority=None)
    assert found is not None and found.id == global_catch_all


async def test_a_ticket_s_sla_comes_from_its_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-sla-ticket")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    await _policy(api_env, org, None, 8)
    x_policy = await _policy(api_env, org, x, 1)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    async with AsyncSession(api_env.admin_engine) as s:
        policy = (
            await s.execute(select(TicketSla.policy_id).where(TicketSla.ticket_id == UUID(ticket)))
        ).scalar_one()
    assert policy == x_policy


async def test_a_client_policy_is_created_through_the_api(api_env: ApiEnv) -> None:
    """`POST /sla/policies` with `client_id`: a policy of that client, and a
    second one at the same level beside the global one (0231's per-client
    keys). A technician limited to X creates X's; Y's is a 404 to them."""
    org = await api_env.create_org("t3-sla-api")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    calendar = await _seed_calendar(api_env, org, windows=_ALWAYS)
    body = {"calendar_id": str(calendar), "response_target_minutes": 30,
            "resolution_target_minutes": 60}
    global_ = await api_env.client.post("/api/v1/sla/policies", json=body, headers=desk["admin"])
    assert global_.status_code == 201, global_.text
    assert global_.json()["client_id"] is None
    x_tech = await everything_on(api_env, org, x)
    own = await api_env.client.post(
        "/api/v1/sla/policies", json={**body, "client_id": str(x)}, headers=x_tech
    )
    assert own.status_code == 201, own.text
    assert own.json()["client_id"] == str(x)
    hidden = await api_env.client.post(
        "/api/v1/sla/policies", json={**body, "client_id": str(y)}, headers=x_tech
    )
    assert hidden.status_code == 404, hidden.text
    refused = await api_env.client.post("/api/v1/sla/policies", json=body, headers=x_tech)
    assert refused.status_code == 403, refused.text  # a global row needs every client

    archived, _ = await client_with_site(api_env, org, client_archived=True)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    for client, expected in ((archived, 409), (unassigned, 422)):
        r = await api_env.client.post(
            "/api/v1/sla/policies", json={**body, "client_id": str(client)},
            headers=desk["admin"],
        )
        assert r.status_code == expected, (client, r.text)
    listed = await api_env.client.get("/api/v1/sla/policies", headers=x_tech)
    assert {p["client_id"] for p in listed.json()} == {None, str(x)}


async def test_a_client_policy_s_audit_names_its_client(api_env: ApiEnv) -> None:
    """Created, changed and deleted by a technician of X, X's policy is
    audited under X; a global one under no client."""
    from app.models import AuditEvent

    org = await api_env.create_org("t3-sla-audit")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    calendar = await _seed_calendar(api_env, org, windows=_ALWAYS)
    body = {"calendar_id": str(calendar), "response_target_minutes": 30,
            "resolution_target_minutes": 60}
    global_ = await api_env.client.post("/api/v1/sla/policies", json=body, headers=desk["admin"])
    x_tech = await everything_on(api_env, org, x)
    own = (
        await api_env.client.post(
            "/api/v1/sla/policies", json={**body, "client_id": str(x)}, headers=x_tech
        )
    ).json()["id"]
    patched = await api_env.client.patch(
        f"/api/v1/sla/policies/{own}", json={"response_target_minutes": 20}, headers=x_tech
    )
    assert patched.status_code == 200, patched.text
    deleted = await api_env.client.delete(f"/api/v1/sla/policies/{own}", headers=x_tech)
    assert deleted.status_code == 204, deleted.text
    async with AsyncSession(api_env.admin_engine) as s:
        audited = set(
            (
                await s.execute(
                    select(AuditEvent.target_id, AuditEvent.action, AuditEvent.client_id).where(
                        AuditEvent.org_id == org, AuditEvent.target_type == "sla_policy"
                    )
                )
            ).all()
        )
    assert audited == {
        (global_.json()["id"], "sla.policy_created", None),
        (own, "sla.policy_created", x),
        (own, "sla.policy_updated", x),
        (own, "sla.policy_deleted", x),
    }


async def _ticket_in(env: ApiEnv, desk: dict[str, Any], client: UUID, **extra: Any) -> str:
    r = await env.client.post(
        f"{API}/tickets", json=_body(desk, client_id=str(client), **extra), headers=desk["admin"]
    )
    assert r.status_code == 201, r.text
    ticket_id: str = r.json()["id"]
    return ticket_id


async def _move(env: ApiEnv, ticket: str, client: UUID, headers: dict[str, str]) -> Any:
    return await env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(client)}, headers=headers
    )


async def test_moving_a_ticket_moves_its_children(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-move")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "uno", "visibility": "public"},
        headers=desk["admin"],
    )
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=desk["admin"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["client_id"] == str(y)
    async with AsyncSession(api_env.admin_engine) as s:
        for table in ("ticket_comment", "ticket_status_history"):
            clients = set(
                (
                    await s.execute(
                        text(f"SELECT client_id FROM {table} WHERE ticket_id = :t"),
                        {"t": UUID(ticket)},
                    )
                ).scalars()
            )
            assert clients == {y}, table


async def test_a_move_needs_the_permission_on_both_clients(api_env: ApiEnv) -> None:
    org = await api_env.create_org("t3-move-perm")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, client_id=str(x)), headers=desk["admin"]
        )
    ).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=x_tech
    )
    assert r.status_code == 404, r.text  # Y is invisible to them: not a 403 oracle


async def test_a_ticket_with_a_device_does_not_move(api_env: ApiEnv) -> None:
    """C9: the device's client is immutable and the ticket must agree."""
    org = await api_env.create_org("t3-move-dev")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    ticket = (
        await api_env.client.post(
            f"{API}/tickets", json=_body(desk, device_id=str(device)), headers=desk["admin"]
        )
    ).json()["id"]
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/move", json={"client_id": str(y)}, headers=desk["admin"]
    )
    assert r.status_code == 409, r.text


async def test_the_source_must_be_visible_too(api_env: ApiEnv) -> None:
    """The other half of "both clients": a technician of Y cannot pull X's
    ticket into Y. X's ticket does not exist for them -- a 404."""
    org = await api_env.create_org("t3-move-src")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = await _ticket_in(api_env, desk, x)
    y_tech = await everything_on(api_env, org, y)
    r = await _move(api_env, ticket, y, y_tech)
    assert r.status_code == 404, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(Ticket.client_id).where(Ticket.id == UUID(ticket)))
        ).scalar_one() == x


async def test_a_technician_on_both_clients_moves_whatever_the_header_narrows(
    api_env: ApiEnv,
) -> None:
    """`X-Client-Id` narrows what the console lists, not where a ticket may
    go: holding `ticket.write` on X and on Y is what decides (spec §2.4). The
    move is audited with both clients."""
    from app.models import AuditEvent, UserRole

    org = await api_env.create_org("t3-move-both")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = await _ticket_in(api_env, desk, x)
    tech = await everything_on(api_env, org, x)
    tech_id = await _tech_of(api_env, org, x)
    async with AsyncSession(api_env.admin_engine) as s:
        role = (
            await s.execute(select(UserRole.role_id).where(UserRole.user_id == tech_id))
        ).scalar_one()
        s.add(UserRole(org_id=org, user_id=tech_id, role_id=role, client_id=y))
        await s.commit()
    r = await _move(api_env, ticket, y, {**tech, "X-Client-Id": str(x)})
    assert r.status_code == 200, r.text
    assert r.json()["client_id"] == str(y)
    async with AsyncSession(api_env.admin_engine) as s:
        moved = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "ticket.moved", AuditEvent.target_id == ticket
                )
            )
        ).scalar_one()
    assert moved.client_id == y
    assert moved.actor_id == tech_id
    assert moved.meta["from"] == str(x) and moved.meta["to"] == str(y)


async def test_a_requester_contact_keeps_the_ticket_in_its_client(api_env: ApiEnv) -> None:
    """research-1 §E2: a contact's ticket stays with the contact's client,
    or its emailed reply would comment on another client's ticket."""
    org = await api_env.create_org("t3-move-contact")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    contact = await _contact(api_env, org, x)
    ticket = await _ticket_in(api_env, desk, x, requester_id=str(contact))
    r = await _move(api_env, ticket, y, desk["admin"])
    assert r.status_code == 422, r.text


async def test_nothing_moves_into_unassigned_or_an_archived_client(api_env: ApiEnv) -> None:
    """C10: Unassigned is the inbox, never a destination (422); an archived
    client takes nothing new (409)."""
    org = await api_env.create_org("t3-move-dest")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    ticket = await _ticket_in(api_env, desk, x)
    assert (await _move(api_env, ticket, unassigned, desk["admin"])).status_code == 422
    assert (await _move(api_env, ticket, archived, desk["admin"])).status_code == 409
    nowhere = await _move(api_env, ticket, uuid4(), desk["admin"])
    assert nowhere.status_code == 404, nowhere.text


async def test_a_ticket_of_an_alert_stays_with_its_client(api_env: ApiEnv) -> None:
    """research-1 §E6: an alert's ticket -- by origin, or linked from a
    `health_alert` -- belongs to the alert's device's client."""
    from app.models import HealthAlert
    from tests.health_fixtures import seed_check, seed_profile

    org = await api_env.create_org("t3-move-alert")
    desk = await _desk(api_env, org)
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        by_origin = Ticket(
            org_id=org, client_id=x, number=f"T-{uuid4().hex[:8]}", type="incident",
            category_id=UUID(desk["category"]), queue_id=UUID(desk["queue"]), status="new",
            subject="alert", origin="alert", source_alert_id=uuid4(),
        )
        s.add(by_origin)
        await s.commit()
    r = await _move(api_env, str(by_origin.id), y, desk["admin"])
    assert r.status_code == 409, r.text

    linked = await _ticket_in(api_env, desk, x)
    profile = await seed_profile(api_env.admin_engine, org)
    check = await seed_check(api_env.admin_engine, org, profile)
    device = await _device(api_env, org, x, x_site)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(HealthAlert(
            org_id=org, device_id=device, check_id=check, profile_id=profile,
            check_type="cpu.busy_pct", severity="warning", ticket_id=UUID(linked),
        ))
        await s.commit()
    r = await _move(api_env, linked, y, desk["admin"])
    assert r.status_code == 409, r.text


async def test_an_assignee_who_cannot_see_the_destination_is_unassigned(
    api_env: ApiEnv,
) -> None:
    """A ticket is never left with someone who cannot open it; the audit
    names who was taken off it. An assignee who sees the destination stays."""
    from app.models import AuditEvent

    org = await api_env.create_org("t3-move-assignee")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, x)
    x_tech = await _tech_of(api_env, org, x)
    ticket = await _ticket_in(api_env, desk, x, assigned_to=str(x_tech))
    r = await _move(api_env, ticket, y, desk["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["assigned_to"] is None
    async with AsyncSession(api_env.admin_engine) as s:
        metadata = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.action == "ticket.moved", AuditEvent.target_id == ticket
                )
            )
        ).scalar_one()
    assert metadata["unassigned"] == str(x_tech)

    kept = await _ticket_in(api_env, desk, x, assigned_to=str(desk["admin_id"]))
    r = await _move(api_env, kept, y, desk["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["assigned_to"] == str(desk["admin_id"])


async def test_a_moved_ticket_takes_its_new_client_s_sla(api_env: ApiEnv) -> None:
    """D2 after a move: the promise is re-resolved among the destination's
    policies (`recompute_for_retriage`), and the SLA row moves with it."""
    org = await api_env.create_org("t3-move-sla")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_policy = await _policy(api_env, org, x, 8)
    y_policy = await _policy(api_env, org, y, 1)
    ticket = await _ticket_in(api_env, desk, x)

    async def sla() -> tuple[UUID, UUID, str]:
        async with AsyncSession(api_env.admin_engine) as s:
            row = (
                await s.execute(select(TicketSla).where(TicketSla.ticket_id == UUID(ticket)))
            ).scalar_one()
            return row.policy_id, row.client_id, row.policy_source

    assert await sla() == (x_policy, x, "initial")
    assert (await _move(api_env, ticket, y, desk["admin"])).status_code == 200
    assert await sla() == (y_policy, y, "retriage")
    assert (await _moved_audit(api_env, ticket))["sla"] is None


async def _moved_audit(env: ApiEnv, ticket: str) -> dict[str, Any]:
    from app.models import AuditEvent

    async with AsyncSession(env.admin_engine) as s:
        meta: dict[str, Any] = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.action == "ticket.moved", AuditEvent.target_id == ticket
                )
            )
        ).scalar_one()
    return meta


async def _sla_rows(env: ApiEnv, ticket: str) -> list[UUID]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(TicketSla.policy_id).where(TicketSla.ticket_id == UUID(ticket))
                )
            ).scalars()
        )


async def test_a_move_to_a_client_with_no_policy_voids_the_sla(api_env: ApiEnv) -> None:
    """Review fix round 1. X has a policy of its own; Y has none and there is
    no global one, so nothing resolves for Y (D2). The moved ticket has NO SLA
    -- its row is deleted, never left naming X's policy -- and the move's
    audit says so (spec §3: an absent SLA is visibly absent)."""
    org = await api_env.create_org("t3-move-void")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_policy = await _policy(api_env, org, x, 8)
    ticket = await _ticket_in(api_env, desk, x)
    assert await _sla_rows(api_env, ticket) == [x_policy]
    assert (await _move(api_env, ticket, y, desk["admin"])).status_code == 200
    assert await _sla_rows(api_env, ticket) == []
    assert (await _moved_audit(api_env, ticket))["sla"] == "voided"


async def test_a_technician_of_the_new_client_works_a_ticket_whose_sla_was_voided(
    api_env: ApiEnv,
) -> None:
    """The clock was running when the ticket moved. A technician limited to Y
    then opens it, parks it on `pending` (pause), takes it off (resume) and
    replies -- with X's policy left on the row, `resume` read a policy that is
    invisible to them (`_load_policy_in_org`) and failed."""
    org = await api_env.create_org("t3-move-void-work")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await _policy(api_env, org, x, 8)
    ticket = await _ticket_in(api_env, desk, x)
    assert (await _move(api_env, ticket, y, desk["admin"])).status_code == 200
    y_tech = await everything_on(api_env, org, y)
    for to_status in ("open", "pending", "open"):
        r = await api_env.client.post(
            f"{API}/tickets/{ticket}/status", json={"to_status": to_status}, headers=y_tech
        )
        assert r.status_code == 200, (to_status, r.text)
    r = await api_env.client.post(
        f"{API}/tickets/{ticket}/comments", json={"body": "ya", "visibility": "public"},
        headers=y_tech,
    )
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("child", ["comment", "attachment"])
async def test_a_child_racing_a_move_lands_in_the_new_client(
    api_env: ApiEnv, child: str
) -> None:
    """research-1 §I5. `its_client_from_parent` reads the ticket without a
    lock, and the move's AFTER trigger cannot see a row not yet committed:
    without the ticket lock in `add_comment`/`add_attachment`, a comment or
    attachment written while the move is in flight keeps the old client for
    good.

    The writer holds the ticket it read BEFORE the move -- as an emailed
    reply does (`intake.process_message` resolves the ticket by its token,
    then comments) -- so its audit row and its event name the new client only
    if the ticket is re-read once the move has committed."""
    from moto import mock_aws

    from app.core.storage import ObjectStore
    from app.models import AuditEvent, DomainEvent, TicketAttachment
    from app.modules.service_desk.threads import add_attachment, add_comment
    from app.modules.service_desk.tickets import get_ticket, move_ticket

    org = await api_env.create_org(f"t3-race-{child}")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    ticket = UUID(await _ticket_in(api_env, desk, x))
    read, moved, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def mover() -> None:
        await read.wait()
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await move_ticket(s, org, desk["admin_id"], ticket, to_client_id=y)
            moved.set()
            await release.wait()

    async def writer(store: ObjectStore) -> None:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            held = await get_ticket(s, org, ticket)  # held: the identity map is weak
            assert held.client_id == x
            read.set()
            await moved.wait()
            if child == "comment":
                await add_comment(s, org, ticket, body="late", author_id=desk["admin_id"])
            else:
                await add_attachment(
                    s, org, desk["admin_id"], ticket, filename="late.txt", data=b"late",
                    store=store,
                )
            assert held.client_id == y

    with mock_aws():
        store = ObjectStore(api_env.settings)
        tasks = [asyncio.create_task(mover()), asyncio.create_task(writer(store))]
        await moved.wait()
        await asyncio.sleep(0.5)  # the row reaches the database while the move is open
        release.set()
        await asyncio.gather(*tasks)

    model = TicketComment if child == "comment" else TicketAttachment
    action = "ticket.comment_added" if child == "comment" else "ticket.attachment_uploaded"
    async with AsyncSession(api_env.admin_engine) as s:
        clients = set(
            (await s.execute(select(model.client_id).where(model.ticket_id == ticket))).scalars()
        )
        audited = (
            await s.execute(
                select(AuditEvent.client_id).where(
                    AuditEvent.action == action, AuditEvent.target_id == str(ticket)
                )
            )
        ).scalar_one()
        published = set(
            (
                await s.execute(
                    select(DomainEvent.client_id).where(
                        DomainEvent.event_type == "ticket.comment_added",
                        DomainEvent.subject_id == ticket,
                    )
                )
            ).scalars()
        )
    assert clients == {y}
    assert audited == y
    assert published == ({y} if child == "comment" else set())


async def _queue_rule(
    env: ApiEnv, org: UUID, desk: dict[str, Any], kind: str, members: list[UUID]
) -> None:
    from app.models import NotificationChannel, NotificationRule, QueueMember

    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        for member in members:
            s.add(QueueMember(
                org_id=org, queue_id=UUID(desk["queue"]), user_id=member,
                created_by=desk["admin_id"],
            ))
        channel = NotificationChannel(
            org_id=org, kind="email", name=f"mail-{kind}", config={},
            created_by=desk["admin_id"],
        )
        s.add(channel)
        await s.flush()
        s.add(NotificationRule(
            org_id=org, event_type="ticket.assigned", channel_id=channel.id,
            recipient_kind=kind, active=True, created_by=desk["admin_id"],
        ))
        await s.commit()


def _assigned(org: UUID, client: UUID | None, desk: dict[str, Any], **payload: Any) -> Any:
    from app.models import DomainEvent

    return DomainEvent(
        org_id=org, client_id=client, event_type="ticket.assigned", subject_type="ticket",
        subject_id=uuid4(), payload={"queue_id": desk["queue"], **payload},
        actor_user_id=None,
    )


async def _emails(env: ApiEnv, *users: UUID) -> list[str]:
    async with AsyncSession(env.admin_engine) as s:
        rows = dict((await s.execute(select(User.id, User.email).where(User.id.in_(users)))).all())
    return [rows[u] for u in users]


async def test_queue_members_outside_the_ticket_s_client_get_no_notice(
    api_env: ApiEnv,
) -> None:
    """research-1 §I9: a queue is the organisation's (G), its members are
    not all of every client's. A member with no role in the ticket's client
    is dropped from the recipients."""
    from app.models import DomainEvent, NotificationChannel, NotificationRule, QueueMember
    from app.modules.notification.routing import resolve_recipients

    org = await api_env.create_org("t3-notify")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, y)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        y_tech, y_email = (
            await s.execute(
                text(
                    "SELECT u.id, u.email FROM users u JOIN user_roles r ON r.user_id = u.id "
                    "WHERE r.client_id = :y"
                ),
                {"y": y},
            )
        ).one()
        s.add(QueueMember(
            org_id=org, queue_id=UUID(desk["queue"]), user_id=y_tech,
            created_by=desk["admin_id"],
        ))
        channel = NotificationChannel(
            org_id=org, kind="email", name="mail", config={}, created_by=desk["admin_id"]
        )
        s.add(channel)
        await s.flush()
        s.add(NotificationRule(
            org_id=org, event_type="ticket.assigned", channel_id=channel.id,
            recipient_kind="queue_members", active=True, created_by=desk["admin_id"],
        ))
        await s.commit()
    event = DomainEvent(
        org_id=org, client_id=x, event_type="ticket.assigned", subject_type="ticket",
        subject_id=uuid4(), payload={"queue_id": desk["queue"]}, actor_user_id=None,
    )
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        recipients = await resolve_recipients(s, event=event)
    assert y_email not in {r.address for r in recipients}


async def test_queue_members_who_see_the_client_are_still_told(api_env: ApiEnv) -> None:
    """The filter's other side, on the same queue: an all-clients member and
    a member with a role on X hear about X's ticket and Y's member does not;
    an event with no client (global) still reaches all three."""
    from app.modules.notification.routing import resolve_recipients

    org = await api_env.create_org("t3-notify-ok")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, x)
    await everything_on(api_env, org, y)
    x_tech, y_tech = await _tech_of(api_env, org, x), await _tech_of(api_env, org, y)
    await _queue_rule(api_env, org, desk, "queue_members", [desk["admin_id"], x_tech, y_tech])
    admin_email, x_email, y_email = await _emails(api_env, desk["admin_id"], x_tech, y_tech)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = {r.address for r in await resolve_recipients(s, event=_assigned(org, x, desk))}
        for_all = {
            r.address for r in await resolve_recipients(s, event=_assigned(org, None, desk))
        }
    assert for_x == {admin_email, x_email}
    assert for_all == {admin_email, x_email, y_email}


async def test_a_single_recipient_outside_the_client_is_dropped(api_env: ApiEnv) -> None:
    """`assignee` and `requester` too: a technician of Y is not told about
    X's ticket, a contact of Y neither, and a contact of X is."""
    from app.modules.notification.routing import resolve_recipients

    org = await api_env.create_org("t3-notify-one")
    desk = await _desk(api_env, org)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await everything_on(api_env, org, y)
    y_tech = await _tech_of(api_env, org, y)
    x_contact, y_contact = await _contact(api_env, org, x), await _contact(api_env, org, y)
    await _queue_rule(api_env, org, desk, "assignee", [])
    await _queue_rule(api_env, org, desk, "requester", [])
    x_contact_email, = await _emails(api_env, x_contact)

    async def told(**payload: Any) -> set[str]:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            event = _assigned(org, x, desk, **payload)
            return {r.address for r in await resolve_recipients(s, event=event)}

    assert await told(assignee_id=str(y_tech)) == set()
    assert await told(requester_id=str(y_contact)) == set()
    assert await told(requester_id=str(x_contact)) == {x_contact_email}

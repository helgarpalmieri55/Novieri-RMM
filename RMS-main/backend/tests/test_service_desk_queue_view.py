"""The ticket queue's SLA column, due-soonest sort and scope switch.

`GET /service-desk/tickets` carries each ticket's SLA summary so the queue can
answer "which ticket breaches next?" without a request per row, sorts by the
earliest outstanding deadline on request, and narrows by scope (mine,
unassigned, open, all) server-side.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Ticket, User
from app.modules.sla.models import SlaCalendar, SlaPolicy, TicketSla
from tests.conftest import ApiEnv

API = "/api/v1/service-desk"


async def _user_id(api_env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _setup(api_env: ApiEnv, slug: str) -> dict[str, Any]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    admin_id = await _user_id(api_env, sub)
    queue = (
        await api_env.client.post(
            f"{API}/queues", json={"key": "helpdesk", "name": "Helpdesk"}, headers=headers
        )
    ).json()
    category = (
        await api_env.client.post(
            f"{API}/categories",
            json={
                "key": "hardware",
                "name": "Hardware",
                "type": "incident",
                "default_queue_id": queue["id"],
            },
            headers=headers,
        )
    ).json()
    return {
        "org_id": org_id,
        "headers": headers,
        "admin_id": admin_id,
        "queue": queue,
        "category": category,
    }


async def _ticket(api_env: ApiEnv, env: dict[str, Any], subject: str, **extra: Any) -> str:
    payload = {
        "type": "incident",
        "category_id": env["category"]["id"],
        "queue_id": env["queue"]["id"],
        "subject": subject,
        "requester_id": str(env["admin_id"]),
        **extra,
    }
    resp = await api_env.client.post(f"{API}/tickets", json=payload, headers=env["headers"])
    assert resp.status_code == 201, resp.text
    ticket_id: str = resp.json()["id"]
    return ticket_id


async def _policy(api_env: ApiEnv, org_id: UUID, creator: UUID) -> tuple[UUID, UUID]:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        calendar = SlaCalendar(
            org_id=org_id, name="Office hours", timezone="Europe/London", created_by=creator
        )
        session.add(calendar)
        await session.flush()
        policy = SlaPolicy(
            org_id=org_id,
            calendar_id=calendar.id,
            response_target_minutes=60,
            resolution_target_minutes=480,
            created_by=creator,
            # Inactive so no ticket created later starts its own clock; the
            # rows below are written by hand with known deadlines.
            active=False,
        )
        session.add(policy)
        await session.commit()
        return policy.id, calendar.id


async def _clock(
    api_env: ApiEnv,
    org_id: UUID,
    ticket_id: str,
    policy: tuple[UUID, UUID],
    *,
    response_due: datetime,
    resolution_due: datetime,
    **stops: datetime,
) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        session.add(
            TicketSla(
                org_id=org_id,
                ticket_id=UUID(ticket_id),
                policy_id=policy[0],
                calendar_id=policy[1],
                response_due_at=response_due,
                resolution_due_at=resolution_due,
                policy_source="initial",
                **stops,
            )
        )
        await session.commit()


async def _set_status(api_env: ApiEnv, ticket_id: str, status: str) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            update(Ticket).where(Ticket.id == UUID(ticket_id)).values(status=status)
        )
        await session.commit()


def _by_subject(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["subject"]: item for item in body["items"]}


def _instant(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


# --------------------------------------------------------------------------- #
# The SLA summary on every row
# --------------------------------------------------------------------------- #


async def test_each_row_carries_its_sla_summary(api_env: ApiEnv) -> None:
    env = await _setup(api_env, "sd-due")
    org_id = env["org_id"]
    policy = await _policy(api_env, org_id, env["admin_id"])
    now = datetime.now(UTC)

    running = await _ticket(api_env, env, "Running")
    answered = await _ticket(api_env, env, "Answered")
    breached = await _ticket(api_env, env, "Breached")
    paused = await _ticket(api_env, env, "Paused")
    await _ticket(api_env, env, "No SLA")

    await _clock(
        api_env, org_id, running, policy,
        response_due=now + timedelta(hours=2), resolution_due=now + timedelta(hours=8),
    )
    await _clock(
        api_env, org_id, answered, policy,
        response_due=now - timedelta(hours=1), resolution_due=now + timedelta(hours=1),
        response_met_at=now - timedelta(hours=2),
    )
    await _clock(
        api_env, org_id, breached, policy,
        response_due=now - timedelta(minutes=30), resolution_due=now + timedelta(hours=4),
        response_breached_at=now - timedelta(minutes=29),
    )
    await _clock(
        api_env, org_id, paused, policy,
        response_due=now + timedelta(minutes=5), resolution_due=now + timedelta(hours=3),
        paused_at=now - timedelta(minutes=10),
    )

    resp = await api_env.client.get(f"{API}/tickets", headers=env["headers"])
    assert resp.status_code == 200, resp.text
    rows = _by_subject(resp.json())

    assert rows["No SLA"]["sla"] is None

    sla = rows["Running"]["sla"]
    assert sla["response_state"] == "running"
    assert sla["resolution_state"] == "running"
    assert _instant(sla["next_due_at"]) == now + timedelta(hours=2)
    assert sla["next_due_target"] == "response"
    assert sla["breached"] is False
    assert sla["paused"] is False

    # A met response leaves the resolution clock as the next deadline.
    sla = rows["Answered"]["sla"]
    assert sla["response_state"] == "met"
    assert sla["next_due_target"] == "resolution"
    assert _instant(sla["next_due_at"]) == now + timedelta(hours=1)

    sla = rows["Breached"]["sla"]
    assert sla["response_state"] == "breached"
    assert sla["breached"] is True
    assert _instant(sla["next_due_at"]) == now - timedelta(minutes=30)
    assert sla["next_due_target"] == "response"

    # A stopped clock has no meaningful countdown.
    sla = rows["Paused"]["sla"]
    assert sla["paused"] is True
    assert sla["response_state"] == "paused"
    assert sla["next_due_at"] is None

    # The detail route carries the same summary.
    one = await api_env.client.get(f"{API}/tickets/{running}", headers=env["headers"])
    assert one.json()["sla"]["next_due_target"] == "response"


async def test_a_voided_clock_has_no_deadline(api_env: ApiEnv) -> None:
    env = await _setup(api_env, "sd-void")
    policy = await _policy(api_env, env["org_id"], env["admin_id"])
    now = datetime.now(UTC)
    cancelled = await _ticket(api_env, env, "Cancelled")
    await _clock(
        api_env, env["org_id"], cancelled, policy,
        response_due=now - timedelta(hours=1), resolution_due=now + timedelta(hours=1),
        voided_at=now,
    )
    resp = await api_env.client.get(f"{API}/tickets", headers=env["headers"])
    sla = _by_subject(resp.json())["Cancelled"]["sla"]
    assert sla["response_state"] == "void"
    assert sla["next_due_at"] is None
    assert sla["breached"] is False


async def test_the_sla_summary_costs_the_same_queries_for_any_page_size(
    api_env: ApiEnv,
) -> None:
    """No N+1: a page of six rows runs exactly as many statements as a page
    of two."""
    env = await _setup(api_env, "sd-n1")
    policy = await _policy(api_env, env["org_id"], env["admin_id"])
    now = datetime.now(UTC)
    for n in range(6):
        ticket_id = await _ticket(api_env, env, f"T{n}")
        await _clock(
            api_env, env["org_id"], ticket_id, policy,
            response_due=now + timedelta(hours=n + 1), resolution_due=now + timedelta(days=1),
        )

    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    sync_engine = api_env.app_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", count)
    try:
        for sort in ("newest", "due"):
            statements.clear()
            small = await api_env.client.get(
                f"{API}/tickets?limit=2&sort={sort}", headers=env["headers"]
            )
            small_count = len(statements)
            statements.clear()
            large = await api_env.client.get(
                f"{API}/tickets?limit=6&sort={sort}", headers=env["headers"]
            )
            large_count = len(statements)
            assert small.status_code == large.status_code == 200
            assert len(large.json()["items"]) == 6
            assert all(item["sla"] is not None for item in large.json()["items"])
            assert small_count == large_count, (sort, statements)
    finally:
        event.remove(sync_engine, "before_cursor_execute", count)


# --------------------------------------------------------------------------- #
# Sort by due soonest
# --------------------------------------------------------------------------- #


async def test_sort_by_due_puts_the_earliest_outstanding_deadline_first(
    api_env: ApiEnv,
) -> None:
    env = await _setup(api_env, "sd-sort")
    org_id = env["org_id"]
    policy = await _policy(api_env, org_id, env["admin_id"])
    now = datetime.now(UTC)

    # Created in this order, so newest-first is the reverse of it.
    in_two_hours = await _ticket(api_env, env, "In two hours")
    overdue = await _ticket(api_env, env, "Overdue")
    await _ticket(api_env, env, "No SLA")
    paused = await _ticket(api_env, env, "Paused")
    in_one_hour = await _ticket(api_env, env, "Resolution in one hour")
    all_met = await _ticket(api_env, env, "All met")

    await _clock(
        api_env, org_id, in_two_hours, policy,
        response_due=now + timedelta(hours=2), resolution_due=now + timedelta(hours=9),
    )
    await _clock(
        api_env, org_id, overdue, policy,
        response_due=now - timedelta(hours=1), resolution_due=now + timedelta(hours=5),
        response_breached_at=now,
    )
    await _clock(
        api_env, org_id, paused, policy,
        response_due=now - timedelta(hours=3), resolution_due=now + timedelta(hours=1),
        paused_at=now,
    )
    # The response was met long ago, so its (earlier) deadline no longer counts.
    await _clock(
        api_env, org_id, in_one_hour, policy,
        response_due=now - timedelta(hours=5), resolution_due=now + timedelta(hours=1),
        response_met_at=now - timedelta(hours=6),
    )
    await _clock(
        api_env, org_id, all_met, policy,
        response_due=now - timedelta(hours=5), resolution_due=now - timedelta(hours=1),
        response_met_at=now - timedelta(hours=6), resolution_met_at=now - timedelta(hours=2),
    )

    resp = await api_env.client.get(f"{API}/tickets?sort=due", headers=env["headers"])
    assert resp.status_code == 200, resp.text
    order = [item["subject"] for item in resp.json()["items"]]
    # Deadlines first, soonest first; then everything without one, newest first.
    assert order == [
        "Overdue",
        "Resolution in one hour",
        "In two hours",
        "All met",
        "Paused",
        "No SLA",
    ]

    newest = await api_env.client.get(f"{API}/tickets", headers=env["headers"])
    assert [item["subject"] for item in newest.json()["items"]][0] == "All met"


async def test_an_unknown_sort_is_refused(api_env: ApiEnv) -> None:
    env = await _setup(api_env, "sd-sort-bad")
    resp = await api_env.client.get(f"{API}/tickets?sort=oldest", headers=env["headers"])
    assert resp.status_code == 422


async def test_the_due_sort_ignores_another_orgs_clock_where_rls_is_not_the_thing_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    """The join's own `org_id` predicate, against an engine that bypasses RLS:
    a foreign org's `ticket_sla` row pointing at this org's ticket must not
    give it a deadline."""
    from app.models import Organization
    from app.modules.service_desk.models import TicketCategory, TicketQueue
    from app.modules.service_desk.tickets import list_tickets
    from app.modules.sla.views import ticket_sla_summaries

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        orgs = []
        for key in ("mine", "theirs"):
            org = Organization(name=key, slug=f"sd-due-rls-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs.append(org.id)
        mine, theirs = orgs
        creator = User(org_id=mine, external_subject=f"sub-{uuid4().hex[:10]}", email="m@t.l")
        other = User(org_id=theirs, external_subject=f"sub-{uuid4().hex[:10]}", email="t@t.l")
        session.add_all([creator, other])
        await session.flush()
        queue = TicketQueue(org_id=mine, key="general", name="General", created_by=creator.id)
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=mine, key="hw", name="Hardware", type="incident", created_by=creator.id
        )
        session.add(category)
        await session.flush()
        tickets = []
        for n in range(2):
            ticket = Ticket(
                org_id=mine,
                number=f"INC-{n}-{uuid4().hex[:6]}",
                type="incident",
                category_id=category.id,
                queue_id=queue.id,
                subject=f"T{n}",
                description="",
                status="open",
                priority="normal",
                origin="technician",
                requester_id=creator.id,
                created_by=creator.id,
            )
            session.add(ticket)
            await session.flush()
            tickets.append(ticket)
        calendar = SlaCalendar(
            org_id=theirs, name="Theirs", timezone="UTC", created_by=other.id
        )
        session.add(calendar)
        await session.flush()
        policy = SlaPolicy(
            org_id=theirs,
            calendar_id=calendar.id,
            response_target_minutes=1,
            resolution_target_minutes=2,
            created_by=other.id,
            active=False,
        )
        session.add(policy)
        await session.flush()
        # The OLDER ticket gets a foreign clock; if it counted, it would sort first.
        now = datetime.now(UTC)
        session.add(
            TicketSla(
                org_id=theirs,
                ticket_id=tickets[0].id,
                policy_id=policy.id,
                calendar_id=calendar.id,
                response_due_at=now - timedelta(days=1),
                resolution_due_at=now,
                policy_source="initial",
            )
        )
        await session.flush()

        rows = await list_tickets(session, mine, sort="due")
        assert [t.subject for t in rows] == ["T1", "T0"]
        assert await ticket_sla_summaries(session, mine, [t.id for t in tickets]) == {}
        assert len(await ticket_sla_summaries(session, theirs, [tickets[0].id])) == 1
        await session.rollback()


# --------------------------------------------------------------------------- #
# Scope: mine / unassigned / open / all
# --------------------------------------------------------------------------- #


async def test_scope_narrows_the_list_and_its_total_server_side(api_env: ApiEnv) -> None:
    env = await _setup(api_env, "sd-scope")
    other_sub = await api_env.create_user(env["org_id"], "technician")
    other_id = await _user_id(api_env, other_sub)

    await _ticket(api_env, env, "Mine open", assigned_to=str(env["admin_id"]))
    mine_closed = await _ticket(api_env, env, "Mine closed", assigned_to=str(env["admin_id"]))
    await _ticket(api_env, env, "Unassigned open")
    unassigned_cancelled = await _ticket(api_env, env, "Unassigned cancelled")
    await _ticket(api_env, env, "Theirs open", assigned_to=str(other_id))
    await _set_status(api_env, mine_closed, "closed")
    await _set_status(api_env, unassigned_cancelled, "cancelled")

    async def subjects(scope: str) -> tuple[set[str], int]:
        resp = await api_env.client.get(
            f"{API}/tickets", params={"scope": scope}, headers=env["headers"]
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return {item["subject"] for item in body["items"]}, body["total"]

    assert await subjects("mine") == ({"Mine open"}, 1)
    assert await subjects("unassigned") == ({"Unassigned open"}, 1)
    assert await subjects("open") == ({"Mine open", "Unassigned open", "Theirs open"}, 3)
    assert (await subjects("all"))[1] == 5


async def test_scope_mine_means_the_caller_and_never_a_body_supplied_user(
    api_env: ApiEnv,
) -> None:
    env = await _setup(api_env, "sd-scope-me")
    tech_sub = await api_env.create_user(env["org_id"], "technician")
    tech_headers = await api_env.headers(env["org_id"], tech_sub)
    tech_id = await _user_id(api_env, tech_sub)
    await _ticket(api_env, env, "For the admin", assigned_to=str(env["admin_id"]))
    await _ticket(api_env, env, "For the tech", assigned_to=str(tech_id))

    resp = await api_env.client.get(f"{API}/tickets?scope=mine", headers=tech_headers)
    assert [item["subject"] for item in resp.json()["items"]] == ["For the tech"]

    clash = await api_env.client.get(
        f"{API}/tickets",
        params={"scope": "mine", "assigned_to": str(env["admin_id"])},
        headers=tech_headers,
    )
    assert clash.status_code == 400, clash.text


@pytest.mark.parametrize("scope", ["everything", "MINE"])
async def test_an_unknown_scope_is_refused(api_env: ApiEnv, scope: str) -> None:
    env = await _setup(api_env, f"sd-scope-{scope.lower()}")
    resp = await api_env.client.get(
        f"{API}/tickets", params={"scope": scope}, headers=env["headers"]
    )
    assert resp.status_code == 422

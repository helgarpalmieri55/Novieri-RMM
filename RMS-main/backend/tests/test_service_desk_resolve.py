"""`POST /service-desk/tickets/{id}/resolve`: an optional reply and the move to
resolved, in one transaction.

The console used to post the reply and then walk the status one call at a
time, so a refused second call left a reply sent and a ticket still open.
Here every step runs in one session: if any of them fails, nothing is kept.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, DomainEvent, Ticket, TicketComment, TicketStatusHistory
from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import (
    RESOLVE_PATHS,
    can_transition,
    resolve_path,
)
from tests.conftest import ApiEnv

API = "/api/v1/service-desk"


# --------------------------------------------------------------------------- #
# The path, as pure lifecycle logic.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ticket_type", "status", "expected"),
    [
        ("incident", "new", ("open", "resolved")),
        ("incident", "open", ("resolved",)),
        ("incident", "pending", ("resolved",)),
        ("request", "new", ("in_progress", "fulfilled")),
        ("request", "in_progress", ("fulfilled",)),
    ],
)
def test_resolve_path_opens_a_new_ticket_first(
    ticket_type: str, status: str, expected: tuple[str, ...]
) -> None:
    assert resolve_path(ticket_type, status) == expected


@pytest.mark.parametrize(
    ("ticket_type", "status"),
    [
        ("incident", "resolved"),
        ("incident", "closed"),
        ("incident", "cancelled"),
        ("request", "pending_approval"),
        ("request", "fulfilled"),
        ("request", "closed"),
        ("request", "rejected"),
        ("request", "cancelled"),
    ],
)
def test_there_is_no_resolve_path_from_a_finished_or_gated_status(
    ticket_type: str, status: str
) -> None:
    """`pending_approval` is excluded on purpose: only an approval decision may
    move a request out of it."""
    assert resolve_path(ticket_type, status) is None


def test_every_step_of_every_resolve_path_is_a_legal_transition() -> None:
    for ticket_type, paths in RESOLVE_PATHS.items():
        for start, steps in paths.items():
            current = start
            for step in steps:
                assert can_transition(ticket_type, current, step), (ticket_type, current, step)
                current = step
            assert current in {"resolved", "fulfilled"}


# --------------------------------------------------------------------------- #
# The route.
# --------------------------------------------------------------------------- #


async def _user_id(api_env: ApiEnv, sub: str) -> UUID:
    from app.models import User

    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _setup(
    api_env: ApiEnv, slug: str, *, ticket_type: str = "incident"
) -> tuple[UUID, dict[str, str], dict[str, Any]]:
    org_id = await api_env.create_org(slug)
    admin_sub = await api_env.create_user(org_id, "org-admin")
    admin = await api_env.headers(org_id, admin_sub)
    admin_id = await _user_id(api_env, admin_sub)
    queue = await api_env.client.post(
        f"{API}/queues", json={"key": "helpdesk", "name": "Helpdesk"}, headers=admin
    )
    category = await api_env.client.post(
        f"{API}/categories",
        json={
            "key": "general",
            "name": "General",
            "type": ticket_type,
            "default_queue_id": queue.json()["id"],
        },
        headers=admin,
    )
    ticket = await api_env.client.post(
        f"{API}/tickets",
        json={
            "type": ticket_type,
            "category_id": category.json()["id"],
            "queue_id": queue.json()["id"],
            "subject": "Laptop will not boot",
            "requester_id": str(admin_id),
        },
        headers=admin,
    )
    assert ticket.status_code == 201, ticket.text
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    return org_id, tech, ticket.json()


async def _state(api_env: ApiEnv, org_id: UUID, ticket_id: str) -> dict[str, Any]:
    """Everything a resolve writes, read as the admin role so RLS hides nothing."""
    async with AsyncSession(api_env.admin_engine) as session:
        ticket = await session.get(Ticket, UUID(ticket_id))
        assert ticket is not None

        async def count(model: Any, *where: Any) -> int:
            return int(
                (await session.execute(select(func.count()).select_from(model).where(*where)))
                .scalar_one()
            )

        return {
            "status": ticket.status,
            "resolved_at": ticket.resolved_at,
            "comments": await count(TicketComment, TicketComment.ticket_id == ticket.id),
            "history": [
                (h.from_status, h.to_status)
                for h in (
                    await session.execute(
                        select(TicketStatusHistory)
                        .where(TicketStatusHistory.ticket_id == ticket.id)
                        .order_by(TicketStatusHistory.created_at, TicketStatusHistory.id)
                    )
                ).scalars()
            ],
            "audit": sorted(
                a
                for a in (
                    await session.execute(
                        select(AuditEvent.action).where(
                            AuditEvent.org_id == org_id,
                            AuditEvent.target_id == ticket_id,
                            AuditEvent.action.in_(
                                ["ticket.comment_added", "ticket.status_changed"]
                            ),
                        )
                    )
                ).scalars()
            ),
            "events": await count(
                DomainEvent,
                DomainEvent.org_id == org_id,
                DomainEvent.subject_id == ticket.id,
                DomainEvent.event_type != "ticket.created",
            ),
        }


async def test_reply_and_resolve_opens_a_new_incident_first_in_one_call(api_env) -> None:
    org_id, tech, ticket = await _setup(api_env, "sd-resolve")
    resp = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve",
        json={"reply": {"body": "Reseated the RAM; it boots now.", "visibility": "public"}},
        headers=tech,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"

    state = await _state(api_env, org_id, ticket["id"])
    assert state["status"] == "resolved" and state["resolved_at"] is not None
    assert state["comments"] == 1
    assert state["history"][-2:] == [("new", "open"), ("open", "resolved")]
    assert state["audit"] == [
        "ticket.comment_added",
        "ticket.status_changed",
        "ticket.status_changed",
    ]


async def test_resolve_without_a_reply_and_with_an_internal_note(api_env) -> None:
    org_id, tech, ticket = await _setup(api_env, "sd-resolve-plain")
    plain = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve", json={}, headers=tech
    )
    assert plain.status_code == 200, plain.text
    assert (await _state(api_env, org_id, ticket["id"]))["comments"] == 0

    org_id, tech, ticket = await _setup(api_env, "sd-resolve-note")
    noted = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve",
        json={"reply": {"body": "Fixed on site.", "visibility": "internal"}},
        headers=tech,
    )
    assert noted.status_code == 200, noted.text
    comments = await api_env.client.get(f"{API}/tickets/{ticket['id']}/comments", headers=tech)
    assert [c["visibility"] for c in comments.json()] == ["internal"]


async def test_a_request_is_fulfilled(api_env) -> None:
    org_id, tech, ticket = await _setup(api_env, "sd-resolve-req", ticket_type="request")
    assert ticket["status"] == "in_progress"
    resp = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve",
        json={"reply": {"body": "Installed."}},
        headers=tech,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "fulfilled"


async def test_a_ticket_that_cannot_be_resolved_is_400_and_keeps_no_reply(api_env) -> None:
    org_id, tech, ticket = await _setup(api_env, "sd-resolve-done")
    first = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve", json={}, headers=tech
    )
    assert first.status_code == 200
    before = await _state(api_env, org_id, ticket["id"])

    again = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve",
        json={"reply": {"body": "Resolving twice"}},
        headers=tech,
    )
    assert again.status_code == 400
    assert await _state(api_env, org_id, ticket["id"]) == before


async def test_a_failure_after_the_reply_commits_nothing(api_env, monkeypatch) -> None:
    """The reply and the first transition (new -> open) both run, then the
    move to resolved is refused. The reply, the history row, the audit events
    and the outbox events must all be gone with it."""
    import app.modules.service_desk.resolve as resolve_module

    org_id, tech, ticket = await _setup(api_env, "sd-resolve-fail")
    before = await _state(api_env, org_id, ticket["id"])
    real_change_status = resolve_module.change_status
    steps_run: list[str] = []

    async def fail_on_resolved(*args: Any, **kwargs: Any) -> Any:
        if kwargs["to_status"] == "resolved":
            raise TicketValidationError("refused for the test")
        result = await real_change_status(*args, **kwargs)
        steps_run.append(kwargs["to_status"])
        return result

    monkeypatch.setattr(resolve_module, "change_status", fail_on_resolved)
    resp = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve",
        json={"reply": {"body": "This reply must not survive.", "visibility": "public"}},
        headers=tech,
    )
    assert resp.status_code == 400, resp.text
    assert steps_run == ["open"], "the failure must come after the reply and the first step"
    assert await _state(api_env, org_id, ticket["id"]) == before


async def test_an_unexpected_error_after_the_reply_also_commits_nothing(
    api_env, monkeypatch
) -> None:
    import app.modules.service_desk.resolve as resolve_module

    org_id, tech, ticket = await _setup(api_env, "sd-resolve-boom")
    before = await _state(api_env, org_id, ticket["id"])

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(resolve_module, "change_status", boom)
    with pytest.raises(RuntimeError):
        await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/resolve",
            json={"reply": {"body": "Nor this one."}},
            headers=tech,
        )
    assert await _state(api_env, org_id, ticket["id"]) == before


async def test_resolve_needs_ticket_write_and_stays_in_the_org(api_env) -> None:
    org_id, _tech, ticket = await _setup(api_env, "sd-resolve-rbac")
    for role in ("read-only", "auditor", "requester"):
        headers = await api_env.headers(org_id, await api_env.create_user(org_id, role))
        denied = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/resolve", json={}, headers=headers
        )
        assert denied.status_code == 403, role

    other_org = await api_env.create_org("sd-resolve-other")
    outsider = await api_env.headers(other_org, await api_env.create_user(other_org, "technician"))
    missing = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/resolve", json={}, headers=outsider
    )
    assert missing.status_code == 404
    assert (await _state(api_env, org_id, ticket["id"]))["status"] == "new"

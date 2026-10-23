"""Service Desk REST API tests (docs/modules/service-desk/spec.md §9, §12).

Mirrors `tests/test_budget_api.py`'s style and the invariants it exercises:
server-side RBAC on every route (invariant #6), `TicketValidationError`
routing to 4xx never 500 -- `TicketNotFoundError` (a subclass) -> 404, every
other validation error -> 400 (invariant #5), the audited-download vs
non-audited-read split (invariant #4: attachment download writes
`ticket.attachment_downloaded` exactly once per call; list/detail/comment-
list reads write nothing), and route ordering (`/service-desk/attachments/
{attachment_id}` resolves to the attachment handler, not a ticket route).
Uses the `api_env` in-process ASGI harness.

Spec §0.6 is this module's defining RBAC fact -- the INVERSE of Budget's:
technician holds `ticket.read`/`write`/`assign` and is denied only
`ticket.approve`/`ticket.admin`. `test_technician_access_matches_spec_grants`
iterates every route in the module (declared == covered, derived from the
router so a route added later without a permission entry fails the suite)
and asserts technician is let through on the granted routes and 403'd on
the withheld two.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Permission,
    Role,
    RolePermission,
    Ticket,
    TicketApproval,
    TicketAttachment,
    User,
    UserRole,
)
from tests.conftest import ApiEnv

API = "/api/v1/service-desk"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


async def _user_id(api_env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _org_admin(api_env: ApiEnv, slug: str = "sd-api") -> tuple[UUID, dict[str, str], UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    user_id = await _user_id(api_env, sub)
    return org_id, headers, user_id


async def _role_user(
    api_env: ApiEnv, org_id: UUID, *role_names: str
) -> tuple[dict[str, str], UUID]:
    sub = await api_env.create_user(org_id, *role_names)
    headers = await api_env.headers(org_id, sub)
    user_id = await _user_id(api_env, sub)
    return headers, user_id


async def _user_with_permissions(
    api_env: ApiEnv, org_id: UUID, *codes: str
) -> tuple[dict[str, str], UUID]:
    """Create a user whose ONLY permissions are `codes`, via a bespoke
    org-scoped role -- needed for the self-approval test (a requester who
    ALSO holds `ticket.approve`, isolated from `ticket.write`), mirroring
    `test_budget_api.py`'s identical helper."""
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        session.add(user)
        await session.flush()
        role = Role(org_id=org_id, name=f"custom-{uuid4().hex[:8]}")
        session.add(role)
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
        user_id = user.id
    headers = await api_env.headers(org_id, sub)
    return headers, user_id


async def _create_queue(
    api_env: ApiEnv, headers: dict[str, str], *, key: str = "helpdesk", name: str = "Helpdesk"
) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"{API}/queues", json={"key": key, "name": name}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_category(
    api_env: ApiEnv,
    headers: dict[str, str],
    *,
    key: str = "hardware",
    name: str = "Hardware",
    type: str = "incident",
    requires_approval: bool = False,
    default_queue_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "name": name,
        "type": type,
        "requires_approval": requires_approval,
    }
    if default_queue_id is not None:
        payload["default_queue_id"] = default_queue_id
    resp = await api_env.client.post(f"{API}/categories", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_ticket(
    api_env: ApiEnv,
    headers: dict[str, str],
    *,
    category_id: str,
    queue_id: str,
    requester_id: UUID,
    type: str = "incident",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type,
        "category_id": category_id,
        "queue_id": queue_id,
        "subject": "Laptop won't boot",
        "requester_id": str(requester_id),
    }
    payload.update(overrides)
    resp = await api_env.client.post(f"{API}/tickets", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _audit_count(api_env: ApiEnv, org_id: UUID, action: str) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == action))
        ).scalars().all()
        return len(rows)


async def _setup_incident(
    api_env: ApiEnv, slug: str = "sd-inc"
) -> tuple[UUID, dict[str, str], UUID, dict[str, Any], dict[str, Any]]:
    """org-admin + a helpdesk queue + a no-approval incident category."""
    org_id, admin_headers, admin_id = await _org_admin(api_env, slug)
    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, type="incident", default_queue_id=queue["id"]
    )
    return org_id, admin_headers, admin_id, queue, category


# --------------------------------------------------------------------------- #
# RBAC completeness derived from the router, and technician's positive/
# negative access (spec §0.6, this module's defining assertion -- the
# inverse of Budget's).
# --------------------------------------------------------------------------- #


async def test_technician_access_matches_spec_grants(api_env: ApiEnv) -> None:
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, admin_headers, admin_id = await _org_admin(api_env, "sd-tech")
    tech_headers, tech_id = await _role_user(api_env, org_id, "technician")

    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, type="incident", default_queue_id=queue["id"]
    )
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    comment = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/comments",
        json={"body": "hello"},
        headers=admin_headers,
    )
    assert comment.status_code == 201, comment.text

    mock = mock_aws()
    mock.start()
    api_env.app.state.object_store = ObjectStore(api_env.settings)
    attachment = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/attachments?filename=note.txt",
        content=b"attachment-bytes",
        headers=admin_headers,
    )
    assert attachment.status_code == 201, attachment.text
    attachment_id = attachment.json()["id"]
    canned = await api_env.client.post(
        f"{API}/canned-responses", json={"title": "Hi", "body": "Hello"}, headers=admin_headers
    )
    assert canned.status_code == 201, canned.text
    canned_id = canned.json()["id"]

    # Each entry: (method, route TEMPLATE exactly as `router.routes` reports
    # it, concrete path/query, request kwargs, whether TECHNICIAN is granted
    # this route per spec §0.6/§8.2). Technician holds read/write/assign
    # (`True`) and is denied approve/admin (`False`) -- both directions are
    # asserted, the inverse-of-Budget behavior spec §0.6 calls out.
    routes: list[tuple[str, str, str, dict[str, Any], bool]] = [
        ("GET", "/service-desk/queues", f"{API}/queues", {}, True),
        (
            "POST", "/service-desk/queues", f"{API}/queues",
            {"json": {"key": "sec2", "name": "Security 2"}}, False,
        ),
        (
            "PATCH", "/service-desk/queues/{queue_id}", f"{API}/queues/{queue['id']}",
            {"json": {"name": "Renamed"}}, False,
        ),
        ("GET", "/service-desk/categories", f"{API}/categories", {}, True),
        (
            "POST", "/service-desk/categories", f"{API}/categories",
            {"json": {"key": "misc2", "name": "Misc 2", "type": "incident"}}, False,
        ),
        (
            "PATCH", "/service-desk/categories/{category_id}",
            f"{API}/categories/{category['id']}", {"json": {"name": "Renamed"}}, False,
        ),
        ("GET", "/service-desk/tickets", f"{API}/tickets", {}, True),
        (
            "POST", "/service-desk/tickets", f"{API}/tickets",
            {
                "json": {
                    "type": "incident", "category_id": category["id"],
                    "queue_id": queue["id"], "subject": "Printer jam",
                    "requester_id": str(admin_id),
                }
            },
            True,
        ),
        ("GET", "/service-desk/tickets/{ticket_id}", f"{API}/tickets/{ticket['id']}", {}, True),
        (
            "PATCH", "/service-desk/tickets/{ticket_id}", f"{API}/tickets/{ticket['id']}",
            {"json": {"priority": "high"}}, True,
        ),
        (
            "GET", "/service-desk/tickets/{ticket_id}/history",
            f"{API}/tickets/{ticket['id']}/history", {}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/status",
            f"{API}/tickets/{ticket['id']}/status", {"json": {"to_status": "open"}}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/assign",
            f"{API}/tickets/{ticket['id']}/assign", {"json": {"assigned_to": str(tech_id)}}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/approve",
            f"{API}/tickets/{ticket['id']}/approve", {"json": {}}, False,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/reject",
            f"{API}/tickets/{ticket['id']}/reject", {"json": {"reason": "no"}}, False,
        ),
        (
            "GET", "/service-desk/tickets/{ticket_id}/comments",
            f"{API}/tickets/{ticket['id']}/comments", {}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/comments",
            f"{API}/tickets/{ticket['id']}/comments", {"json": {"body": "tech note"}}, True,
        ),
        (
            "GET", "/service-desk/tickets/{ticket_id}/attachments",
            f"{API}/tickets/{ticket['id']}/attachments", {}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/attachments",
            f"{API}/tickets/{ticket['id']}/attachments?filename=b.txt",
            {"content": b"more-bytes"}, True,
        ),
        (
            "GET", "/service-desk/attachments/{attachment_id}",
            f"{API}/attachments/{attachment_id}", {}, True,
        ),
        (
            "POST", "/service-desk/tickets/{ticket_id}/resolve",
            f"{API}/tickets/{ticket['id']}/resolve", {"json": {"reply": {"body": "done"}}}, True,
        ),
        ("GET", "/service-desk/canned-responses", f"{API}/canned-responses", {}, True),
        (
            "POST", "/service-desk/canned-responses", f"{API}/canned-responses",
            {"json": {"title": "Tech", "body": "x"}}, False,
        ),
        (
            "PATCH", "/service-desk/canned-responses/{response_id}",
            f"{API}/canned-responses/{canned_id}", {"json": {"title": "Tech"}}, False,
        ),
        (
            "DELETE", "/service-desk/canned-responses/{response_id}",
            f"{API}/canned-responses/{canned_id}", {}, False,
        ),
    ]

    for method, _template, path, kwargs, granted in routes:
        resp = await api_env.client.request(method, path, headers=tech_headers, **kwargs)
        if granted:
            assert resp.status_code != 403, (
                f"{method} {path} wrongly denied to technician ({resp.status_code}) {resp.text}"
            )
        else:
            assert resp.status_code == 403, (
                f"{method} {path} leaked to technician ({resp.status_code})"
            )

    # Derive the actual route set FROM the router, so a route added to
    # api.py later without a matching entry above fails this assertion
    # instead of leaving the suite green with an ungated route (the
    # Phase-11/12-review lesson, `test_budget_api.py`'s identical pattern).
    from app.modules.service_desk.api import router

    declared = {(m, r.path) for r in router.routes for m in r.methods if m != "HEAD"}
    covered = {(method, template) for method, template, _path, _kwargs, _granted in routes}
    assert declared == covered, f"route(s) not RBAC-tested: {declared - covered}"

    mock.stop()


async def test_auditor_and_read_only_get_read_but_not_write(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id = await _org_admin(api_env, "sd-ro")
    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, type="incident", default_queue_id=queue["id"]
    )
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    for role in ("auditor", "read-only"):
        headers, _uid = await _role_user(api_env, org_id, role)
        ok = await api_env.client.get(f"{API}/tickets/{ticket['id']}", headers=headers)
        assert ok.status_code == 200, f"{role}: {ok.text}"
        denied = await api_env.client.post(
            f"{API}/tickets", json={"type": "incident", "category_id": category["id"],
            "subject": "x", "requester_id": str(admin_id)}, headers=headers,
        )
        assert denied.status_code == 403, f"{role}: {denied.text}"


# --------------------------------------------------------------------------- #
# POST /queues, POST /categories with a non-zero sort_order -- exactly one
# audit event per create request (whole-branch security review, minor
# finding).
# --------------------------------------------------------------------------- #


async def test_create_queue_with_sort_order_writes_one_audit_event_not_two(
    api_env: ApiEnv,
) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-qsort")
    resp = await api_env.client.post(
        f"{API}/queues",
        json={"key": "ops", "name": "Ops", "sort_order": 5},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sort_order"] == 5
    assert await _audit_count(api_env, org_id, "ticket.queue_created") == 1
    assert await _audit_count(api_env, org_id, "ticket.queue_updated") == 0


async def test_create_category_with_sort_order_writes_one_audit_event_not_two(
    api_env: ApiEnv,
) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-csort")
    resp = await api_env.client.post(
        f"{API}/categories",
        json={"key": "misc", "name": "Misc", "type": "incident", "sort_order": 5},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sort_order"] == 5
    assert await _audit_count(api_env, org_id, "ticket.category_created") == 1
    assert await _audit_count(api_env, org_id, "ticket.category_updated") == 0


# --------------------------------------------------------------------------- #
# Ticket lifecycle happy path -- create/read/update/status/assign/comment/
# attachment, exercised through the routes.
# --------------------------------------------------------------------------- #


async def test_ticket_crud_status_assign_comment_attachment(api_env: ApiEnv) -> None:
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-crud")

    created = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    assert created["status"] == "new"
    assert created["has_attachments"] is False
    assert "org_id" not in created  # invariant #5

    got = await api_env.client.get(f"{API}/tickets/{created['id']}", headers=admin_headers)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == created["id"]

    patched = await api_env.client.patch(
        f"{API}/tickets/{created['id']}", json={"priority": "urgent"}, headers=admin_headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["priority"] == "urgent"
    assert patched.json()["subject"] == created["subject"]  # untouched fields survive

    status_resp = await api_env.client.post(
        f"{API}/tickets/{created['id']}/status",
        json={"to_status": "open"},
        headers=admin_headers,
    )
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["status"] == "open"

    assign_resp = await api_env.client.post(
        f"{API}/tickets/{created['id']}/assign",
        json={"assigned_to": str(admin_id)},
        headers=admin_headers,
    )
    assert assign_resp.status_code == 200, assign_resp.text
    assert assign_resp.json()["assigned_to"] == str(admin_id)

    comment_resp = await api_env.client.post(
        f"{API}/tickets/{created['id']}/comments",
        json={"body": "Looked into it", "visibility": "internal"},
        headers=admin_headers,
    )
    assert comment_resp.status_code == 201, comment_resp.text
    assert "org_id" not in comment_resp.json()

    comments = await api_env.client.get(
        f"{API}/tickets/{created['id']}/comments", headers=admin_headers
    )
    assert comments.status_code == 200, comments.text
    assert len(comments.json()) == 1

    mock = mock_aws()
    mock.start()
    api_env.app.state.object_store = ObjectStore(api_env.settings)
    upload = await api_env.client.post(
        f"{API}/tickets/{created['id']}/attachments?filename=screenshot.png",
        content=b"fake-png-bytes",
        headers=admin_headers,
    )
    assert upload.status_code == 201, upload.text
    assert "object_key" not in upload.json()  # invariant #5 (mirrors ExpenseOut)

    listed_attachments = await api_env.client.get(
        f"{API}/tickets/{created['id']}/attachments", headers=admin_headers
    )
    assert listed_attachments.status_code == 200, listed_attachments.text
    assert len(listed_attachments.json()) == 1

    detail_after_upload = await api_env.client.get(
        f"{API}/tickets/{created['id']}", headers=admin_headers
    )
    assert detail_after_upload.json()["has_attachments"] is True

    download = await api_env.client.get(
        f"{API}/attachments/{upload.json()['id']}", headers=admin_headers
    )
    assert download.status_code == 200, download.text
    assert download.content == b"fake-png-bytes"
    assert download.headers["content-disposition"] == 'attachment; filename="screenshot.png"'
    mock.stop()


# --------------------------------------------------------------------------- #
# GET /service-desk/tickets/{ticket_id}/history (task 13's G1 fix -- the
# route `StatusHistoryOut` was defined and schema-tested for but never
# mounted).
# --------------------------------------------------------------------------- #


async def test_ticket_history_route_returns_rows_oldest_first(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-history")
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )

    for to_status, reason in (("open", "started work"), ("resolved", "fixed it")):
        resp = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/status",
            json={"to_status": to_status, "reason": reason},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    history = await api_env.client.get(
        f"{API}/tickets/{ticket['id']}/history", headers=admin_headers
    )
    assert history.status_code == 200, history.text
    rows = history.json()
    assert [(r["from_status"], r["to_status"]) for r in rows] == [
        (None, "new"),
        ("new", "open"),
        ("open", "resolved"),
    ]
    assert rows[1]["reason"] == "started work"
    assert rows[2]["reason"] == "fixed it"
    assert rows[0]["actor_type"] == "user"
    assert "org_id" not in rows[0]  # invariant #5


async def test_ticket_history_route_denies_caller_without_ticket_read(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(
        api_env, "sd-history-rbac"
    )
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    no_read_headers, _uid = await _user_with_permissions(api_env, org_id)

    resp = await api_env.client.get(
        f"{API}/tickets/{ticket['id']}/history", headers=no_read_headers
    )
    assert resp.status_code == 403, resp.text


async def test_list_tickets_filters_by_type(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-filter")
    request_category = await _create_category(
        api_env, admin_headers, key="software_request", name="Software request",
        type="request", default_queue_id=queue["id"],
    )
    await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    await _create_ticket(
        api_env, admin_headers, category_id=request_category["id"], queue_id=queue["id"],
        requester_id=admin_id, type="request", subject="Need a laptop",
    )

    incidents = await api_env.client.get(f"{API}/tickets?type=incident", headers=admin_headers)
    assert incidents.status_code == 200, incidents.text
    # `{items, total}` since closeout rec. 10 -- see the paged-envelope block
    # at the end of this file for why the bare list could not stay.
    assert all(t["type"] == "incident" for t in incidents.json()["items"])
    assert len(incidents.json()["items"]) == 1

    requests = await api_env.client.get(f"{API}/tickets?type=request", headers=admin_headers)
    assert requests.status_code == 200, requests.text
    assert len(requests.json()["items"]) == 1
    assert requests.json()["items"][0]["status"] == "in_progress"  # no approval required


async def test_list_tickets_route_forwards_all_ten_filter_params(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 (whole-branch review, round B), extended by C1 (round C) to cover
    `q`: nulling every filter EXCEPT `type` (`status`, `queue_id`,
    `assigned_to`, `requester_id`, `device_id`, `priority`, `q`) and forcing
    `limit=200`/`offset=0` left the suite GREEN -- only `type` was ever
    proven to reach `tickets.list_tickets`. A dropped kwarg here would
    silently return the org's entire ticket list, capped at 200 rather than
    the caller's own `limit`, to every filtered UI query (My Work, the
    default view). The service-layer filters themselves are well covered
    (`test_service_desk_tickets.py`); this is the ROUTE's own job --
    forwarding every one of the ten kwargs unchanged -- proven by
    monkeypatching `tickets.list_tickets` itself (as imported into `api.py`)
    to capture exactly what the route calls it with, for a request that
    supplies all ten at once."""
    import app.modules.service_desk.api as sd_api

    org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-list-fwd")

    captured: dict[str, Any] = {}

    async def fake_list_tickets(session: Any, org_id_arg: UUID, **kwargs: Any) -> list[Any]:
        captured["org_id"] = org_id_arg
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sd_api, "list_tickets", fake_list_tickets)

    queue_uuid = uuid4()
    assigned_uuid = uuid4()
    requester_uuid = uuid4()
    device_uuid = uuid4()

    resp = await api_env.client.get(
        f"{API}/tickets",
        params={
            "type": "incident",
            "status": "open",
            "queue_id": str(queue_uuid),
            "assigned_to": str(assigned_uuid),
            "requester_id": str(requester_uuid),
            "device_id": str(device_uuid),
            "priority": "high",
            "q": "printer",
            "scope": "unassigned",
            "sort": "due",
            "limit": 7,
            "offset": 3,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert captured == {
        "org_id": org_id,
        "type": "incident",
        "status": "open",
        "queue_id": queue_uuid,
        "assigned_to": assigned_uuid,
        "requester_id": requester_uuid,
        "device_id": device_uuid,
        "priority": "high",
        "q": "printer",
        "unassigned": True,
        "open_only": True,
        "sort": "due",
        "limit": 7,
        "offset": 3,
    }


async def test_list_tickets_route_rejects_a_search_term_over_the_bound(
    api_env: ApiEnv,
) -> None:
    """C1 fix: `q` is bound at the schema boundary (`MAX_SEARCH_Q`, mirroring
    every other bounded string in this module, Phase-11/12 lesson) -- an
    oversized value is rejected as a typed 422 before it ever reaches
    `tickets.list_tickets`, rather than reaching Postgres unbounded."""
    from app.modules.service_desk.schema import MAX_SEARCH_Q

    _org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-list-q-bound")

    resp = await api_env.client.get(
        f"{API}/tickets",
        params={"q": "x" * (MAX_SEARCH_Q + 1)},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Approval workflow (spec §4) -- self-approval prohibition, approve, reject.
# --------------------------------------------------------------------------- #


async def test_self_approval_is_rejected(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id = await _org_admin(api_env, "sd-selfapp")
    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, key="access_request", name="Access request", type="request",
        requires_approval=True, default_queue_id=queue["id"],
    )
    # A user who is BOTH the requester and holds ticket.approve -- the
    # scenario spec §4 explicitly calls out ("plausible for a small org's
    # admin") and the single most important behavior this test proves.
    requester_headers, requester_id = await _user_with_permissions(
        api_env, org_id, "ticket.read", "ticket.write", "ticket.approve"
    )
    ticket = await _create_ticket(
        api_env, requester_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=requester_id, type="request", subject="Need admin access",
    )
    assert ticket["status"] == "pending_approval"

    self_approve = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/approve", json={}, headers=requester_headers
    )
    assert self_approve.status_code == 400, self_approve.text
    assert self_approve.status_code != 500


async def test_technician_cannot_bypass_approval_via_generic_status_route(
    api_env: ApiEnv,
) -> None:
    """A1 regression, verified by exploit (whole-branch security review).

    The reported exploit shape exactly: a technician (holds `ticket.write`,
    correctly 403'd from the dedicated `/approve` route since technician is
    never granted `ticket.approve`, spec §0.6) files an `access_request` --
    "grant me domain admin" -- with themselves as requester. It lands on
    `pending_approval`. They then try the generic `POST .../status
    {"to_status": "in_progress"}` instead of the dedicated route, hoping to
    self-approve by skipping `ticket.approve`, the self-approval prohibition,
    `approver_role_id` membership, and the audit trail entirely.

    Must get a 4xx, never 200 -- and `ticket_approval` must still show
    `state="pending"`, `decided_by=None` afterwards: nobody ever decided
    this request, even though the ticket originally reached `pending_approval`
    for real."""
    org_id, admin_headers, admin_id = await _org_admin(api_env, "sd-a1-bypass")
    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, key="access_request", name="Access request", type="request",
        requires_approval=True, default_queue_id=queue["id"],
    )
    tech_headers, tech_id = await _role_user(api_env, org_id, "technician")

    ticket = await _create_ticket(
        api_env, tech_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=tech_id, type="request", subject="Grant me domain admin",
    )
    assert ticket["status"] == "pending_approval"

    approve_attempt = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/approve", json={}, headers=tech_headers
    )
    assert approve_attempt.status_code == 403, approve_attempt.text

    status_bypass = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/status",
        json={"to_status": "in_progress"},
        headers=tech_headers,
    )
    assert status_bypass.status_code != 200, status_bypass.text
    assert 400 <= status_bypass.status_code < 500, status_bypass.text

    reject_bypass = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/status",
        json={"to_status": "rejected"},
        headers=tech_headers,
    )
    assert reject_bypass.status_code != 200, reject_bypass.text
    assert 400 <= reject_bypass.status_code < 500, reject_bypass.text

    # Withdrawing your own request is still a legitimate `ticket.write`
    # action -- the fix must not have collaterally broken this edge, which
    # stays on the generic path deliberately (module docstring).
    cancel = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/status",
        json={"to_status": "cancelled"},
        headers=tech_headers,
    )
    assert cancel.status_code == 200, cancel.text

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        approval = (
            await session.execute(
                select(TicketApproval).where(TicketApproval.ticket_id == UUID(ticket["id"]))
            )
        ).scalar_one()
    assert approval.state == "pending"
    assert approval.decided_by is None


async def test_approve_and_reject_flows(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id = await _org_admin(api_env, "sd-approve")
    queue = await _create_queue(api_env, admin_headers)
    category = await _create_category(
        api_env, admin_headers, key="equipment_request", name="Equipment request",
        type="request", requires_approval=True, default_queue_id=queue["id"],
    )
    approver_headers, _approver_id = await _user_with_permissions(
        api_env, org_id, "ticket.read", "ticket.approve"
    )

    to_approve = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id, type="request", subject="Need a monitor",
    )
    before = await _audit_count(api_env, org_id, "ticket.approved")
    approved = await api_env.client.post(
        f"{API}/tickets/{to_approve['id']}/approve", json={}, headers=approver_headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "in_progress"
    assert await _audit_count(api_env, org_id, "ticket.approved") == before + 1

    to_reject = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id, type="request", subject="Need a second monitor",
    )
    empty_reason = await api_env.client.post(
        f"{API}/tickets/{to_reject['id']}/reject", json={}, headers=approver_headers
    )
    assert empty_reason.status_code == 400, empty_reason.text  # spec §4: reject needs a reason

    before_rejected = await _audit_count(api_env, org_id, "ticket.rejected")
    rejected = await api_env.client.post(
        f"{API}/tickets/{to_reject['id']}/reject",
        json={"reason": "budget frozen"},
        headers=approver_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert await _audit_count(api_env, org_id, "ticket.rejected") == before_rejected + 1

    # Deciding twice is a typed 400, never a 500.
    again = await api_env.client.post(
        f"{API}/tickets/{to_reject['id']}/approve", json={}, headers=approver_headers
    )
    assert again.status_code == 400, again.text

    # Approving an incident (never approvable, spec §0.4) is a typed 400.
    incident_category = await _create_category(
        api_env, admin_headers, type="incident", default_queue_id=queue["id"]
    )
    incident = await _create_ticket(
        api_env, admin_headers, category_id=incident_category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    incident_approve = await api_env.client.post(
        f"{API}/tickets/{incident['id']}/approve", json={}, headers=approver_headers
    )
    assert incident_approve.status_code == 400, incident_approve.text


# --------------------------------------------------------------------------- #
# Error mapping: TicketNotFoundError -> 404, every other TicketValidationError
# -> 400, no path returns 500 (spec §9, invariant #5). Covers the approval,
# transition, and attachment-sanitizer branches explicitly.
# --------------------------------------------------------------------------- #


async def test_nonexistent_ids_are_404_not_500(api_env: ApiEnv) -> None:
    _org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-404")
    missing = str(uuid4())

    for method, path, kwargs in [
        ("GET", f"{API}/tickets/{missing}", {}),
        ("PATCH", f"{API}/tickets/{missing}", {"json": {"priority": "low"}}),
        ("POST", f"{API}/tickets/{missing}/status", {"json": {"to_status": "open"}}),
        ("POST", f"{API}/tickets/{missing}/assign", {"json": {}}),
        ("POST", f"{API}/tickets/{missing}/approve", {"json": {}}),
        ("POST", f"{API}/tickets/{missing}/reject", {"json": {"reason": "x"}}),
        ("GET", f"{API}/tickets/{missing}/comments", {}),
        ("POST", f"{API}/tickets/{missing}/comments", {"json": {"body": "x"}}),
        ("GET", f"{API}/tickets/{missing}/history", {}),
        ("GET", f"{API}/tickets/{missing}/attachments", {}),
        (
            "POST", f"{API}/tickets/{missing}/attachments?filename=a.txt",
            {"content": b"data"},
        ),
        ("GET", f"{API}/attachments/{missing}", {}),
        ("PATCH", f"{API}/queues/{missing}", {"json": {"name": "x"}}),
        ("PATCH", f"{API}/categories/{missing}", {"json": {"name": "x"}}),
    ]:
        resp = await api_env.client.request(method, path, headers=admin_headers, **kwargs)
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code} {resp.text}"
        assert resp.status_code != 500


async def test_illegal_transition_is_400_not_500(api_env: ApiEnv) -> None:
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-illegal")
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    # `new -> closed` is not a legal incident transition (spec §3.1: closed
    # is only reachable from resolved).
    resp = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/status",
        json={"to_status": "closed"},
        headers=admin_headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.status_code != 500


async def test_attachment_sanitizer_rejects_bad_filename_as_400(api_env: ApiEnv) -> None:
    """B5 (whole-branch review, round B) -- DOCUMENTED ARTIFACT, not fixed
    (one of this round's two sanctioned documentation-only exceptions,
    alongside `test_close_change_is_idempotent` in
    `test_service_desk_adapter.py`).

    The terminal-ticket portion below (cancel, then re-upload) LOOKS like it
    pins `upload_attachment_route`'s own `if ticket.status in
    TERMINAL_STATUSES: raise _http_error(...)` pre-check. It does not: this
    test never sets `app.state.object_store`, so deleting that pre-check
    makes the route fall through to `_object_store(request)` -- whose
    docstring says construction itself "hits the object store to ensure the
    bucket exists" -- which then makes a REAL network call with the test's
    fake AWS credentials and raises an unhandled
    `botocore.exceptions.ClientError: InvalidAccessKeyId` (verified
    empirically: deleting the pre-check turns this test's terminal-ticket
    assertion into that uncaught error, not a 400) rather than the 400 this
    test asserts. The kill is real, but it comes from the object-store
    construction failing outright, not from the route's own guard.

    With a real (moto-mocked) store wired in first, the mutation would
    SURVIVE: `threads.add_attachment` re-checks the identical
    `TERMINAL_STATUSES` gate itself, strictly BEFORE any object-storage
    write, so the service layer alone still produces the same typed 400 even
    with the route's pre-check gone. The route's pre-check is therefore
    redundant with the service-layer guard -- its only genuine purpose is
    ORDERING: skip reading the request body and constructing the object
    store (a real S3 round trip on first use) for an upload already known to
    fail, not a second authority on legality. Stated plainly here rather than
    wiring a store to chase a test that would then assert nothing new."""
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-sanit")
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    # Note: `sanitize_attachment_filename`'s threat model is path traversal,
    # CRLF header injection, and non-ASCII header-encoding failures -- NOT
    # Windows reserved device names (`con`, `nul`, ...), which are a distinct
    # concern this sanitizer was never built to cover, so they are not
    # included here.
    for bad_filename in ("../../etc/passwd", "..", "a" * 300 + ".txt"):
        resp = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/attachments?filename={bad_filename}",
            content=b"data",
            headers=admin_headers,
        )
        assert resp.status_code == 400, f"{bad_filename!r}: {resp.status_code} {resp.text}"
        assert resp.status_code != 500

    # A terminal ticket accepts no new attachments either (spec §6, §3.3) --
    # cancel it, then confirm a well-formed upload is still a typed 400, not
    # a 500, and that the sanitizer error and the terminal-ticket error are
    # both routed identically (both are plain `TicketValidationError`, never
    # a `TicketNotFoundError`).
    cancel = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/status",
        json={"to_status": "cancelled"},
        headers=admin_headers,
    )
    assert cancel.status_code == 200, cancel.text
    terminal_upload = await api_env.client.post(
        f"{API}/tickets/{ticket['id']}/attachments?filename=fine.txt",
        content=b"data",
        headers=admin_headers,
    )
    assert terminal_upload.status_code == 400, terminal_upload.text


async def test_upload_ordering_rejects_nonexistent_ticket_before_size_matters(
    api_env: ApiEnv,
) -> None:
    """Carried-forward requirement #1: the ticket is loaded and validated
    BEFORE the request body is read. This in-process ASGI test can only
    observe the OUTCOME (a 404, not a hang, a 500, or a successful upload
    against a ticket that was never created) -- it cannot observe memory
    behavior directly, but a nonexistent/cross-org ticket id must never
    reach `add_attachment` at all."""
    _org_id, admin_headers, _admin_id = await _org_admin(api_env, "sd-upload-order")
    missing_ticket = str(uuid4())
    resp = await api_env.client.post(
        f"{API}/tickets/{missing_ticket}/attachments?filename=a.txt",
        content=b"x" * 1024,
        headers=admin_headers,
    )
    assert resp.status_code == 404, resp.text

    # Cross-org: a real ticket id, but requested with a DIFFERENT org's
    # bearer token -- indistinguishable from nonexistent (invariant #5).
    other_org_id, other_headers, other_admin_id = await _org_admin(api_env, "sd-upload-order-2")
    other_queue = await _create_queue(api_env, other_headers)
    other_category = await _create_category(
        api_env, other_headers, type="incident", default_queue_id=other_queue["id"]
    )
    other_ticket = await _create_ticket(
        api_env, other_headers, category_id=other_category["id"], queue_id=other_queue["id"],
        requester_id=other_admin_id,
    )
    cross_org = await api_env.client.post(
        f"{API}/tickets/{other_ticket['id']}/attachments?filename=a.txt",
        content=b"x" * 1024,
        headers=admin_headers,  # the FIRST org's token, not other_headers
    )
    assert cross_org.status_code == 404, cross_org.text


# --------------------------------------------------------------------------- #
# Audit split (invariant #4, spec §8.3's no-audit-on-view rule): a list call
# writes nothing; an attachment download writes exactly one event per call;
# a status change writes exactly one.
# --------------------------------------------------------------------------- #


async def test_audit_split_list_download_status(api_env: ApiEnv) -> None:
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-audit")
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        upload = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/attachments?filename=log.txt",
            content=b"log-bytes",
            headers=admin_headers,
        )
        assert upload.status_code == 201, upload.text
        attachment_id = upload.json()["id"]

        # `GET /queues`/`GET /categories` lazily seed an org's defaults on
        # FIRST use (spec §2: "seeded per org on first use"), and that seed
        # writes its own one-off `ticket.queues_seeded`/`ticket.
        # categories_seeded` system event exactly when it inserts something
        # -- `_setup_incident` created a queue/category that collide with
        # only ONE of each default's keys ("helpdesk"/"hardware"), so the
        # other defaults are still missing here. Warm the seed up front so
        # the read-only assertion below measures a STEADY STATE (every
        # default already present, so the second round of calls inserts
        # nothing and audits nothing), not the one-time bootstrap.
        warm_queues = await api_env.client.get(f"{API}/queues", headers=admin_headers)
        assert warm_queues.status_code == 200, warm_queues.text
        warm_categories = await api_env.client.get(f"{API}/categories", headers=admin_headers)
        assert warm_categories.status_code == 200, warm_categories.text

        # Plain reads write nothing (list tickets, get ticket, list comments,
        # list attachments, list queues, list categories).
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            before_total = len((await session.execute(select(AuditEvent))).scalars().all())

        for method, path in [
            ("GET", f"{API}/tickets"),
            ("GET", f"{API}/tickets/{ticket['id']}"),
            ("GET", f"{API}/tickets/{ticket['id']}/comments"),
            ("GET", f"{API}/tickets/{ticket['id']}/attachments"),
            ("GET", f"{API}/queues"),
            ("GET", f"{API}/categories"),
        ]:
            r = await api_env.client.request(method, path, headers=admin_headers)
            assert r.status_code == 200, f"{method} {path}: {r.text}"

        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            after_total = len((await session.execute(select(AuditEvent))).scalars().all())
        assert after_total == before_total, "a read-only route wrote an audit event"

        # Download writes exactly one `ticket.attachment_downloaded` event PER
        # CALL -- `threads.get_attachment` itself is silent (spec §8.3), so
        # this route is the only writer of that action.
        before_downloads = await _audit_count(api_env, org_id, "ticket.attachment_downloaded")
        d1 = await api_env.client.get(f"{API}/attachments/{attachment_id}", headers=admin_headers)
        assert d1.status_code == 200, d1.text
        assert await _audit_count(api_env, org_id, "ticket.attachment_downloaded") == (
            before_downloads + 1
        )
        # B2 (whole-branch review, round B): attribution was never asserted
        # for this event either -- `actor_type="user"` -> `"system"` and
        # `actor_id=ctx.user_id` -> `None` both survived with the suite
        # green, since only the COUNT was checked above.
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            download_event = (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.attachment_downloaded",
                    )
                    .order_by(AuditEvent.id.desc())
                    .limit(1)
                )
            ).scalar_one()
        assert download_event.actor_type == "user"
        assert download_event.actor_id == admin_id
        d2 = await api_env.client.get(f"{API}/attachments/{attachment_id}", headers=admin_headers)
        assert d2.status_code == 200, d2.text
        assert await _audit_count(api_env, org_id, "ticket.attachment_downloaded") == (
            before_downloads + 2
        )

        # A status change writes exactly one `ticket.status_changed` event.
        before_status = await _audit_count(api_env, org_id, "ticket.status_changed")
        status_resp = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/status",
            json={"to_status": "open"},
            headers=admin_headers,
        )
        assert status_resp.status_code == 200, status_resp.text
        assert await _audit_count(api_env, org_id, "ticket.status_changed") == before_status + 1


# --------------------------------------------------------------------------- #
# Route ordering (the Phase-11/12 lesson, carried forward as requirement
# #4): `/service-desk/attachments/{attachment_id}` resolves to the
# attachment-download handler -- proven by its distinct response shape
# (raw bytes + Content-Disposition), never a ticket-route's JSON body.
# --------------------------------------------------------------------------- #


async def test_attachment_route_resolves_before_ticket_routes(api_env: ApiEnv) -> None:
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-order")
    ticket = await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        upload = await api_env.client.post(
            f"{API}/tickets/{ticket['id']}/attachments?filename=readme.txt",
            content=b"hello-world",
            headers=admin_headers,
        )
        assert upload.status_code == 201, upload.text
        attachment_id = upload.json()["id"]

        resp = await api_env.client.get(
            f"{API}/attachments/{attachment_id}", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == b"hello-world"
        assert resp.headers["content-type"] == "application/octet-stream"
        assert "content-disposition" in resp.headers

        # A random UUID against the SAME endpoint 404s as an attachment
        # lookup (not, say, silently matching some other route).
        resp_missing = await api_env.client.get(
            f"{API}/attachments/{uuid4()}", headers=admin_headers
        )
        assert resp_missing.status_code == 404, resp_missing.text


# --------------------------------------------------------------------------- #
# `_tickets_out`'s `has_attachments` org predicate (B4, whole-branch review,
# round B) -- every other org predicate in this module has a BYPASSRLS test;
# this one has ridden on RLS alone.
# --------------------------------------------------------------------------- #


async def test_tickets_out_has_attachments_org_predicate_is_exercised(
    api_env: ApiEnv, admin_engine: AsyncEngine
) -> None:
    """`_tickets_out`'s own `TicketAttachment.org_id == org_id` predicate (the
    batched `has_attachments` lookup) has never been exercised on a raw
    BYPASSRLS session -- unlike every other org predicate in this module
    (`list_tickets`, `list_status_history`, `resolve_*`, ...), which all have
    one. Inside `org_session`, RLS alone would already hide a foreign-org
    attachment row, so a test written there would pass even with this
    predicate deleted, proving RLS works and nothing about the explicit
    predicate (spec §8.1's defence-in-depth requirement).

    Foreign-stamp an attachment row onto org_a's own ticket with org_b's
    `org_id` -- a shape `threads.add_attachment`'s own org-scoped insert never
    produces -- directly on a raw admin session, then call `_tickets_out`
    itself (bypassing RLS, bypassing the route) and confirm `has_attachments`
    still reads `False`: only this predicate, not RLS, stands in the way
    (mutation-verified, see `.git/sdd/fix-B-report.md`)."""
    from app.modules.service_desk.api import _tickets_out

    org_a, admin_headers_a, admin_id_a = await _org_admin(api_env, "sd-attpred-a")
    org_b, _admin_headers_b, admin_id_b = await _org_admin(api_env, "sd-attpred-b")
    queue = await _create_queue(api_env, admin_headers_a)
    category = await _create_category(
        api_env, admin_headers_a, type="incident", default_queue_id=queue["id"]
    )
    ticket = await _create_ticket(
        api_env, admin_headers_a, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id_a,
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket_row = (
            await session.execute(select(Ticket).where(Ticket.id == UUID(ticket["id"])))
        ).scalar_one()

        foreign_attachment = TicketAttachment(
            org_id=org_b,
            ticket_id=ticket_row.id,
            object_key=f"{org_b}/service-desk/tickets/{ticket_row.id}/foreign",
            filename="foreign.txt",
            byte_size=3,
            uploaded_by=admin_id_b,
        )
        session.add(foreign_attachment)
        await session.flush()

        [out] = await _tickets_out(session, org_a, [ticket_row])

    assert out.has_attachments is False


# --------------------------------------------------------------------------- #
# The paged envelope (audit
# `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 10 --
# finding B46 / auditor A's "hard-coded limit=200 with no pagination control").
#
# `GET /service-desk/tickets` returned a bare `list[TicketOut]`, so a console
# holding a full page could not tell "these are all the tickets" from "these
# are the first 200 of an unknown number". `TicketListPage` said so in a
# comment and refused to invent "showing X of Y" -- correctly, because there
# was no Y. This is the Y, in the `{items, total}` shape `15ed5de` and
# `787461f` established for exactly this reason.
# --------------------------------------------------------------------------- #


async def test_the_ticket_list_is_an_envelope_whose_total_counts_past_the_page(
    api_env: ApiEnv,
) -> None:
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-page")
    for n in range(3):
        await _create_ticket(
            api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
            requester_id=admin_id, subject=f"Ticket {n}",
        )

    page = await api_env.client.get(f"{API}/tickets?limit=2", headers=admin_headers)
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3, "the count must survive the limit, or the page cannot say so"

    tail = await api_env.client.get(f"{API}/tickets?limit=2&offset=2", headers=admin_headers)
    assert len(tail.json()["items"]) == 1
    assert tail.json()["total"] == 3


async def test_the_total_counts_the_filtered_set_and_not_the_whole_queue(
    api_env: ApiEnv,
) -> None:
    """A count taken before the WHERE clause is a number that contradicts the
    rows beside it, which is the /reports nine-zeros defect with a different
    sign. Every filter the route forwards has to reach the count too."""
    org_id, admin_headers, admin_id, queue, category = await _setup_incident(api_env, "sd-pagef")
    request_category = await _create_category(
        api_env, admin_headers, key="software_request", name="Software request",
        type="request", default_queue_id=queue["id"],
    )
    await _create_ticket(
        api_env, admin_headers, category_id=category["id"], queue_id=queue["id"],
        requester_id=admin_id,
    )
    await _create_ticket(
        api_env, admin_headers, category_id=request_category["id"], queue_id=queue["id"],
        requester_id=admin_id, type="request", subject="Need a laptop",
    )

    requests = await api_env.client.get(f"{API}/tickets?type=request", headers=admin_headers)
    assert requests.json()["total"] == 1
    assert len(requests.json()["items"]) == 1

    # "Need a laptop", not "laptop": the incident's default subject is
    # "Laptop won't boot", and `q` is a case-insensitive substring on both
    # rows -- a search that matched everything would prove nothing.
    searched = await api_env.client.get(f"{API}/tickets?q=Need+a", headers=admin_headers)
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["subject"] == "Need a laptop"


async def test_a_neighbours_ticket_is_in_neither_the_items_nor_the_total(
    api_env: ApiEnv,
) -> None:
    """A tenant-scoped page carrying an org-wide count is still a leak -- it
    discloses how much work another tenant is carrying, and it makes this
    org's own page look truncated when it is complete."""
    org_a, a_headers, a_id, a_queue, a_category = await _setup_incident(api_env, "sd-page-a")
    _org_b, b_headers, b_id, b_queue, b_category = await _setup_incident(api_env, "sd-page-b")
    await _create_ticket(
        api_env, b_headers, category_id=b_category["id"], queue_id=b_queue["id"],
        requester_id=b_id, subject="Their ticket",
    )
    await _create_ticket(
        api_env, a_headers, category_id=a_category["id"], queue_id=a_queue["id"],
        requester_id=a_id, subject="My ticket",
    )

    mine = await api_env.client.get(f"{API}/tickets", headers=a_headers)
    assert [t["subject"] for t in mine.json()["items"]] == ["My ticket"]
    assert mine.json()["total"] == 1


async def test_the_ticket_count_is_tenant_scoped_where_rls_is_not_the_thing_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    """`count_tickets`' OWN `org_id` predicate, pinned against an engine that
    ignores RLS.

    The route runs through `org_session` on a NOBYPASSRLS role, where Postgres
    rejects a cross-tenant row whether or not the query asked it to -- so
    deleting `WHERE org_id` from the counter turns none of the three tests
    above red (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`).
    `its_admin` carries BYPASSRLS, so below the SQL is the only thing scoping
    anything, and a count is precisely where an org-wide answer hides behind a
    correctly-filtered page. `theirs` is asserted too, so this cannot pass by
    counting nothing.
    """
    from app.models import Organization
    from app.modules.service_desk.models import TicketCategory, TicketQueue
    from app.modules.service_desk.tickets import count_tickets

    orgs: dict[str, UUID] = {}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for key, count in (("mine", 1), ("theirs", 2)):
            org = Organization(name=key, slug=f"sd-cnt-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs[key] = org.id
            creator = User(
                org_id=org.id,
                external_subject=f"sub-{uuid4().hex[:10]}",
                email=f"{key}@test.local",
            )
            session.add(creator)
            await session.flush()
            queue = TicketQueue(
                org_id=org.id, key="general", name="General", created_by=creator.id
            )
            session.add(queue)
            await session.flush()
            category = TicketCategory(
                org_id=org.id,
                key="hardware",
                name="Hardware",
                type="incident",
                created_by=creator.id,
            )
            session.add(category)
            await session.flush()
            for n in range(count):
                session.add(
                    Ticket(
                        org_id=org.id,
                        number=f"{key[:2].upper()}-{n}-{uuid4().hex[:6]}",
                        type="incident",
                        category_id=category.id,
                        queue_id=queue.id,
                        subject=f"{key} {n}",
                        description="",
                        status="open",
                        priority="normal",
                        # `ck_ticket_human_attribution`: any origin but
                        # `alert` must carry both a requester and an author.
                        origin="technician",
                        requester_id=creator.id,
                        created_by=creator.id,
                    )
                )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        assert await count_tickets(session, orgs["mine"]) == 1
        assert await count_tickets(session, orgs["theirs"]) == 2
        assert await count_tickets(session, orgs["mine"], type="request") == 0

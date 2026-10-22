"""Canned responses: a small per-org library of saved replies.

Anyone who reads tickets can list them (to insert one into a reply); only
`ticket.admin` may create, edit or delete them, and every change is audited.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, User
from tests.conftest import ApiEnv

API = "/api/v1/service-desk/canned-responses"


async def _org(api_env: ApiEnv, slug: str) -> tuple[UUID, dict[str, str], dict[str, str]]:
    org_id = await api_env.create_org(slug)
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    return org_id, admin, tech


async def _audit(api_env: ApiEnv, org_id: UUID, action: str) -> list[AuditEvent]:
    async with org_session(org_id, engine=api_env.app_engine) as session:
        return list(
            (
                await session.execute(select(AuditEvent).where(AuditEvent.action == action))
            ).scalars().all()
        )


async def _create(api_env: ApiEnv, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"title": "Restart first", "body": "Please restart and tell us if it persists."}
    payload.update(body)
    resp = await api_env.client.post(API, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    created: dict[str, Any] = resp.json()
    return created


async def test_create_list_update_delete_round_trip_and_audit(api_env: ApiEnv) -> None:
    org_id, admin, tech = await _org(api_env, "canned-crud")

    created = await _create(api_env, admin)
    assert created["title"] == "Restart first"
    assert "org_id" not in created

    # A technician reads the library to insert from it.
    listed = await api_env.client.get(API, headers=tech)
    assert listed.status_code == 200, listed.text
    assert [r["title"] for r in listed.json()] == ["Restart first"]

    updated = await api_env.client.patch(
        f"{API}/{created['id']}", json={"body": "Restart, then reply here."}, headers=admin
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["body"] == "Restart, then reply here."
    assert updated.json()["title"] == "Restart first"

    deleted = await api_env.client.delete(f"{API}/{created['id']}", headers=admin)
    assert deleted.status_code == 204, deleted.text
    assert (await api_env.client.get(API, headers=admin)).json() == []

    for action in (
        "ticket.canned_response_created",
        "ticket.canned_response_updated",
        "ticket.canned_response_deleted",
    ):
        events = await _audit(api_env, org_id, action)
        assert len(events) == 1, action
        assert events[0].target_id == created["id"]
        # The title identifies it in the log; the body is not copied there.
        assert events[0].meta.get("title") == "Restart first"
        assert "body" not in events[0].meta


async def test_the_library_is_sorted_by_title(api_env: ApiEnv) -> None:
    _org_id, admin, _tech = await _org(api_env, "canned-sort")
    for title in ("Password reset", "Access granted", "Mailbox full"):
        await _create(api_env, admin, title=title)
    listed = await api_env.client.get(API, headers=admin)
    assert [r["title"] for r in listed.json()] == [
        "Access granted",
        "Mailbox full",
        "Password reset",
    ]


async def test_only_ticket_admin_may_change_the_library(api_env: ApiEnv) -> None:
    org_id, admin, tech = await _org(api_env, "canned-rbac")
    created = await _create(api_env, admin)
    body = {"title": "Mine", "body": "x"}

    assert (await api_env.client.post(API, json=body, headers=tech)).status_code == 403
    assert (
        await api_env.client.patch(f"{API}/{created['id']}", json=body, headers=tech)
    ).status_code == 403
    assert (await api_env.client.delete(f"{API}/{created['id']}", headers=tech)).status_code == 403

    # Without ticket.read there is nothing to read either.
    nobody = await api_env.headers(org_id, await api_env.create_user(org_id))
    assert (await api_env.client.get(API, headers=nobody)).status_code == 403


async def test_another_orgs_responses_are_invisible_and_untouchable(api_env: ApiEnv) -> None:
    _mine, my_admin, _ = await _org(api_env, "canned-a")
    _theirs, their_admin, _ = await _org(api_env, "canned-b")
    theirs = await _create(api_env, their_admin, title="Theirs")

    assert (await api_env.client.get(API, headers=my_admin)).json() == []
    patch = await api_env.client.patch(
        f"{API}/{theirs['id']}", json={"title": "Stolen"}, headers=my_admin
    )
    assert patch.status_code == 404
    delete = await api_env.client.delete(f"{API}/{theirs['id']}", headers=my_admin)
    assert delete.status_code == 404
    assert [r["title"] for r in (await api_env.client.get(API, headers=their_admin)).json()] == [
        "Theirs"
    ]


async def test_a_duplicate_title_is_a_400_not_a_500(api_env: ApiEnv) -> None:
    _org_id, admin, _ = await _org(api_env, "canned-dup")
    await _create(api_env, admin, title="Same")
    dup = await api_env.client.post(API, json={"title": "same", "body": "b"}, headers=admin)
    assert dup.status_code == 400, dup.text

    other = await _create(api_env, admin, title="Other")
    rename = await api_env.client.patch(
        f"{API}/{other['id']}", json={"title": "SAME"}, headers=admin
    )
    assert rename.status_code == 400, rename.text


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "body": "x"},
        {"title": "   ", "body": "x"},
        {"title": "x" * 101, "body": "x"},
        {"title": "ok", "body": ""},
        {"title": "ok", "body": "x" * 20_001},
        {"title": "ok", "body": "x", "org_id": str(uuid4())},
    ],
)
async def test_bounds_are_enforced_at_the_schema(api_env: ApiEnv, body: dict[str, Any]) -> None:
    _org_id, admin, _ = await _org(api_env, f"canned-bound-{uuid4().hex[:4]}")
    resp = await api_env.client.post(API, json=body, headers=admin)
    assert resp.status_code == 422, resp.text


async def test_the_table_has_forced_rls_and_a_tenant_policy(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        flags = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'ticket_canned_response'"
                )
            )
        ).one()
        policies = (
            await conn.execute(
                text(
                    "SELECT policyname FROM pg_policies"
                    " WHERE tablename = 'ticket_canned_response'"
                )
            )
        ).scalars().all()
    assert tuple(flags) == (True, True)
    assert "tenant_isolation" in policies


async def test_rls_refuses_a_row_written_for_another_org(api_env: ApiEnv) -> None:
    """The database, not only the service, keeps a row in its own org."""
    from app.modules.service_desk.models import TicketCannedResponse

    mine = await api_env.create_org("canned-rls-a")
    theirs = await api_env.create_org("canned-rls-b")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=mine, external_subject=f"sub-{uuid4().hex[:8]}", email="a@t.l")
        session.add(user)
        await session.commit()
        user_id = user.id
    with pytest.raises(Exception, match="row-level security"):
        async with org_session(mine, engine=api_env.app_engine) as session:
            session.add(
                TicketCannedResponse(org_id=theirs, title="x", body="y", created_by=user_id)
            )
            await session.flush()

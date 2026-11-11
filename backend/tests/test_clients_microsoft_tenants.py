"""A client's Microsoft tenants (plan decision D35, B2-10): edited with
`clients.manage` on the client, kept sorted and unique, audited as text, and
never a personal-account tenant or the unassigned client's."""

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Client
from tests.entra_support import CONSUMERS


async def _client(env: Any) -> tuple[Any, dict[str, str], str]:
    org = await env.create_org("ms-tenants")
    sub = await env.create_user(org, "org-admin")
    headers = await env.headers(org, sub)
    made = await env.client.post("/api/v1/clients", headers=headers, json={"name": "Acme"})
    assert made.status_code == 201, made.text
    return org, headers, made.json()["id"]


async def test_tenants_are_kept_sorted_unique_and_audited_as_text(api_env: Any) -> None:
    org, headers, client_id = await _client(api_env)
    one, two = sorted([uuid4(), uuid4()], key=str)
    patched = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(two), str(one), str(two)]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["microsoft_tenants"] == [str(one), str(two)]
    async with AsyncSession(api_env.admin_engine) as s:
        event = (
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org, AuditEvent.action == "client.updated")
                    .order_by(AuditEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        stored = await s.scalar(select(Client.microsoft_tenants).where(Client.id == client_id))
    assert event is not None
    assert event.meta["changed"]["microsoft_tenants"] == {
        "before": [],
        "after": [str(one), str(two)],
    }
    assert stored == [one, two]


async def _patched(
    env: Any, org: Any, headers: dict[str, str], client_id: str, tenants: list[Any]
) -> tuple[list[str], list[Any], dict[str, Any]]:
    """One PATCH of the list: what it answered, what is stored, and what the
    newest `client.updated` row says changed."""
    patched = await env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(t) for t in tenants]},
    )
    assert patched.status_code == 200, patched.text
    async with AsyncSession(env.admin_engine) as s:
        meta = (
            (
                await s.execute(
                    select(AuditEvent.meta)
                    .where(AuditEvent.org_id == org, AuditEvent.action == "client.updated")
                    .order_by(AuditEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        stored = await s.scalar(select(Client.microsoft_tenants).where(Client.id == client_id))
    assert meta is not None
    return patched.json()["microsoft_tenants"], list(stored or []), meta


async def test_a_list_is_replaced_and_audited_before_and_after(api_env: Any) -> None:
    """Fix round 1, item 5: a non-empty list changed to another."""
    org, headers, client_id = await _client(api_env)
    one, two, three = sorted([uuid4(), uuid4(), uuid4()], key=str)
    await _patched(api_env, org, headers, client_id, [one, two])
    answered, stored, meta = await _patched(api_env, org, headers, client_id, [three, one])
    assert answered == [str(one), str(three)]
    assert stored == [one, three]
    assert meta["changed"]["microsoft_tenants"] == {
        "before": [str(one), str(two)],
        "after": [str(one), str(three)],
    }


async def test_a_list_is_cleared_and_audited_before_and_after(api_env: Any) -> None:
    """Fix round 1, item 5: a non-empty list cleared to `[]` (an empty list is
    a value, not "leave it out")."""
    org, headers, client_id = await _client(api_env)
    one, two = sorted([uuid4(), uuid4()], key=str)
    await _patched(api_env, org, headers, client_id, [one, two])
    answered, stored, meta = await _patched(api_env, org, headers, client_id, [])
    assert (answered, stored) == ([], [])
    assert meta["changed"]["microsoft_tenants"] == {
        "before": [str(one), str(two)],
        "after": [],
    }


async def test_a_personal_account_tenant_is_refused_by_name(api_env: Any) -> None:
    """The refusal's own sentence, not `extra="forbid"`'s: before the field
    exists the PATCH is a 422 too, so the `detail` is what tells them apart."""
    _org, headers, client_id = await _client(api_env)
    refused = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(CONSUMERS)]},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "a personal Microsoft account tenant cannot sign anyone in here"
    )


async def test_at_most_twenty(api_env: Any) -> None:
    _org, headers, client_id = await _client(api_env)
    twenty = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(uuid4()) for _ in range(20)]},
    )
    assert twenty.status_code == 200, twenty.text
    refused = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(uuid4()) for _ in range(21)]},
    )
    assert refused.status_code == 422


async def test_the_unassigned_client_takes_no_tenants(api_env: Any) -> None:
    """Decision 7: nobody is that client's contact, so no tenant signs anyone
    in there. Refused by its own sentence, as its email domains are."""
    org, headers, _client_id = await _client(api_env)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = await s.scalar(
            select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
        )
    refused = await api_env.client.patch(
        f"/api/v1/clients/{unassigned}",
        headers=headers,
        json={"microsoft_tenants": [str(uuid4())]},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "the unassigned client takes no Microsoft tenants"

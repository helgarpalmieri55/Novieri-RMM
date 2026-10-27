"""Seats and licence allocations carry the person (people spec §0.4)."""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Person
from tests.client_scope_support import internal_client
from tests.conftest import ApiEnv


async def _person(api_env: ApiEnv, org_id: UUID, name: str, email: str | None) -> UUID:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        person = Person(
            org_id=org_id, client_id=org_id, display_name=name, email=email, source="operator"
        )
        s.add(person)
        await s.flush()
        return person.id


async def _subscription(api_env: ApiEnv, headers: dict[str, str]) -> dict[str, Any]:
    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={"client_id": await internal_client(api_env, headers), "service_name": "Slack",
              "billing_cycle": "monthly", "cost_per_cycle": 5,
              "currency": "USD", "seats": 5},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_a_seat_links_to_the_person_holding_its_address(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("plink")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    maria = await _person(api_env, org_id, "Maria Garcia", "Maria.Garcia@corp.example")
    sub = await _subscription(api_env, headers)

    matched = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Maria", "assignee_email": "maria.garcia@CORP.example"},
        headers=headers,
    )
    assert matched.status_code == 201, matched.text
    assert matched.json()["person_id"] == str(maria)

    stranger = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Who", "assignee_email": "who@elsewhere.example"},
        headers=headers,
    )
    assert stranger.status_code == 201
    assert stranger.json()["person_id"] is None


async def test_a_seat_can_name_the_person_directly(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("plink")
    other_org = await api_env.create_org("plink")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    ann = await _person(api_env, org_id, "Ann", "ann@corp.example")
    foreign = await _person(api_env, other_org, "Eve", "eve@other.example")
    sub = await _subscription(api_env, headers)

    ok = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Ann", "assignee_email": "ann@corp.example",
              "person_id": str(ann)},
        headers=headers,
    )
    assert ok.status_code == 201 and ok.json()["person_id"] == str(ann)

    cross = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Eve", "assignee_email": "eve@other.example",
              "person_id": str(foreign)},
        headers=headers,
    )
    assert cross.status_code == 400


async def _licence(api_env: ApiEnv, headers: dict[str, str]) -> dict[str, Any]:
    product = await api_env.client.post(
        "/api/v1/licenses/products", json={"name": "Office"}, headers=headers
    )
    assert product.status_code == 201, product.text
    licence = await api_env.client.post(
        "/api/v1/licenses",
        json={"client_id": await internal_client(api_env, headers),
              "product_id": product.json()["id"], "type": "per_user", "entitlements": 5},
        headers=headers,
    )
    assert licence.status_code == 201, licence.text
    body: dict[str, Any] = licence.json()
    return body


async def test_a_licence_can_be_allocated_to_a_person(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("plink")
    other_org = await api_env.create_org("plink")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    maria = await _person(api_env, org_id, "Maria", "maria@corp.example")
    foreign = await _person(api_env, other_org, "Eve", None)
    licence = await _licence(api_env, headers)

    resp = await api_env.client.post(
        f"/api/v1/licenses/{licence['id']}/allocations",
        json={"target_type": "person", "target_id": str(maria)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["person_id"] == str(maria)

    cross = await api_env.client.post(
        f"/api/v1/licenses/{licence['id']}/allocations",
        json={"target_type": "person", "target_id": str(foreign)},
        headers=headers,
    )
    assert cross.status_code == 400


async def test_a_licence_for_an_account_is_carried_to_its_person(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("plink")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    licence = await _licence(api_env, headers)
    # The people list mirrors accounts, so the technician now has a person.
    listing = await api_env.client.get("/api/v1/people", headers=headers)
    assert listing.status_code == 200, listing.text
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        person = (
            await s.execute(select(Person).where(Person.email == f"{tech}@test.local"))
        ).scalar_one()
        user_id = person.user_id

    resp = await api_env.client.post(
        f"/api/v1/licenses/{licence['id']}/allocations",
        json={"target_type": "user", "target_id": str(user_id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["person_id"] == str(person.id)

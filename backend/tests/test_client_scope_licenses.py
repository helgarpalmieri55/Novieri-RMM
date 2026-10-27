"""Licenses and subscriptions belong to a client (plan 2026-10-26 Task 6)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    Device,
    DomainEvent,
    InstalledSoftware,
    LicenseAllocation,
    Person,
    SeatAssignment,
    User,
)
from app.modules.licenses.reporting import compliance_summary
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _product(env: ApiEnv, admin: dict[str, str], name: str) -> str:
    r = await env.client.post(
        "/api/v1/licenses/products",
        json={"name": name, "match_rules": [{"type": "exact", "pattern": name}]},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _license(
    env: ApiEnv, admin: dict[str, str], product: str, client: UUID, seats: int, **extra: Any
) -> str:
    r = await env.client.post(
        "/api/v1/licenses",
        json={"product_id": product, "client_id": str(client), "entitlements": seats,
              "type": "per_device", "notes": f"L {uuid4().hex[:4]}", **extra},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _subscription(
    env: ApiEnv, headers: dict[str, str], client: UUID, **extra: Any
) -> Any:
    return await env.client.post(
        "/api/v1/subscriptions",
        json={"service_name": "M365", "client_id": str(client), "seats": 10,
              "billing_cycle": "monthly", "cost_per_cycle": "10.00", "currency": "USD",
              **extra},
        headers=headers,
    )


async def _installed(env: ApiEnv, org: UUID, client: UUID, site: UUID, name: str) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="h", os_family="windows")
        s.add(d)
        await s.flush()
        s.add(InstalledSoftware(org_id=org, device_id=d.id, name=name, version="1"))
        await s.commit()
        return d.id


async def _user_id(env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _contact(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        user = User(
            org_id=org, external_subject=uuid4().hex, email=f"c-{uuid4().hex[:8]}@test.local",
            kind="contact", client_id=client,
        )
        s.add(user)
        await s.commit()
        return user.id


async def _person(env: ApiEnv, org: UUID, client: UUID, email: str | None = None) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        person = Person(org_id=org, client_id=client, display_name="P", email=email)
        s.add(person)
        await s.commit()
        return person.id


async def _audit_clients(env: ApiEnv, org: UUID, action: str) -> list[UUID | None]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent.client_id).where(
                        AuditEvent.org_id == org, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_a_license_is_its_client_s_and_needs_one(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-create")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Office")
    license_id = await _license(api_env, admin, product, x, 5)
    got = await api_env.client.get(f"/api/v1/licenses/{license_id}", headers=admin)
    assert got.json()["client_id"] == str(x)
    missing = await api_env.client.post(
        "/api/v1/licenses",
        json={"product_id": product, "entitlements": 1, "type": "perpetual"},
        headers=admin,
    )
    assert missing.status_code == 422
    assert "client_id" in missing.text


async def test_an_allocation_cannot_name_another_client_s_device(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-alloc")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    sub = await api_env.create_user(org, "org-admin")
    admin = await api_env.headers(org, sub)
    actor = await _user_id(api_env, sub)
    product = await _product(api_env, admin, "Visio")
    license_id = await _license(api_env, admin, product, x, 5)
    y_device = await _installed(api_env, org, y, y_site, "Visio")
    x_device = await _installed(api_env, org, x, x_site, "Visio")
    r = await api_env.client.post(
        f"/api/v1/licenses/{license_id}/allocations",
        json={"target_type": "device", "target_id": str(y_device)}, headers=admin,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "the allocation target belongs to another client"
    with pytest.raises(DBAPIError, match="allocation target belongs to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(LicenseAllocation(org_id=org, license_id=UUID(license_id),
                                    target_type="device", target_id=y_device,
                                    created_by=actor))
            await s.flush()
    # THE TRIGGER ORDER, the positive control: inserted without a client, X's
    # device on X's license passes only if `its_client_from_parent` filled the
    # client in BEFORE the guard compared it (Postgres fires BEFORE triggers
    # by name). Guard first, it would compare X with NULL and refuse.
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        own = LicenseAllocation(org_id=org, license_id=UUID(license_id),
                                target_type="device", target_id=x_device, created_by=actor)
        s.add(own)
        await s.flush()
        assert own.client_id == x


async def test_compliance_is_counted_per_client(api_env: ApiEnv) -> None:
    """Two clients, one product: X holds 1 seat and installs it twice (a
    breach); Y holds 5 and installs it once. An org-wide sum (6 vs 3) would
    hide X's breach. Under scope all: the code's predicate is tested."""
    org = await api_env.create_org("l-compliance")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Acrobat")
    await _license(api_env, admin, product, x, 1)
    await _license(api_env, admin, product, y, 5)
    for _ in range(2):
        await _installed(api_env, org, x, x_site, "Acrobat")
    await _installed(api_env, org, y, y_site, "Acrobat")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await compliance_summary(s, org, client_id=x)
        for_y = await compliance_summary(s, org, client_id=y)
    row_x = next(r for r in for_x if str(r["product_id"]) == product)
    row_y = next(r for r in for_y if str(r["product_id"]) == product)
    assert (row_x["entitled"], row_x["installed"], row_x["breach"]) == (1, 2, True)
    assert (row_y["entitled"], row_y["installed"], row_y["breach"]) == (5, 1, False)
    assert row_x["client_id"] == x and row_y["client_id"] == y


async def test_a_seat_by_email_finds_the_person_in_the_subscription_s_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("s-seat")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    people = {}
    for client in (x, y):
        r = await api_env.client.post(
            "/api/v1/people",
            json={"display_name": "Eva", "email": "eva@shared.test", "client_id": str(client)},
            headers=admin,
        )
        people[client] = r.json()["id"]
    sub = await _subscription(api_env, admin, y)
    assert sub.status_code == 201, sub.text
    assert sub.json()["client_id"] == str(y)
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub.json()['id']}/seats",
        json={"assignee_name": "Eva", "assignee_email": "eva@shared.test"}, headers=admin,
    )
    assert seat.status_code == 201, seat.text
    async with AsyncSession(api_env.admin_engine) as s:
        person = (
            await s.execute(select(SeatAssignment.person_id).where(
                SeatAssignment.id == UUID(seat.json()["id"])))
        ).scalar_one()
    assert str(person) == people[y]


async def test_ignoring_a_name_for_x_does_not_hide_it_for_y(api_env: ApiEnv) -> None:
    from app.modules.licenses.service import review_queue

    org = await api_env.create_org("l-ignore")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    await _installed(api_env, org, x, x_site, "Toolbar")
    await _installed(api_env, org, y, y_site, "Toolbar")
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        "/api/v1/licenses/review-queue/ignore", json={"name": "Toolbar", "client_id": str(x)},
        headers=x_tech,
    )
    assert r.status_code in (200, 201), r.text
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        queue = await review_queue(s, org)
    toolbar = [row for row in queue if row["name"] == "Toolbar"]
    assert toolbar and toolbar[0]["device_count"] == 1


# --------------------------------------------------------------------------- #
# Creating a license or a subscription: the client named in the body
# --------------------------------------------------------------------------- #


async def test_the_client_of_a_new_license_or_subscription_is_checked(api_env: ApiEnv) -> None:
    """`client_for_new_rows`: an archived client is a 409, another
    organisation's is a 404, a client outside a technician's scope a 404 (never
    a 403), and "Sin asignar" a 422 (C10)."""
    org = await api_env.create_org("l-refusals")
    other = await api_env.create_org("l-refusals-other")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    foreign, _ = await client_with_site(api_env, other)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Refusals")
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    x_tech = await everything_on(api_env, org, x)

    def license_body(client: UUID) -> dict[str, Any]:
        return {"product_id": product, "client_id": str(client), "entitlements": 1,
                "type": "perpetual"}

    for headers, client, expected in [
        (admin, unassigned, 422), (admin, archived, 409), (admin, foreign, 404),
        (x_tech, y, 404), (x_tech, x, 201),
    ]:
        r = await api_env.client.post("/api/v1/licenses", json=license_body(client),
                                      headers=headers)
        assert r.status_code == expected, (client, r.text)
        s = await _subscription(api_env, headers, client)
        assert s.status_code == expected, (client, s.text)


async def test_license_and_subscription_events_carry_their_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-audit")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Audited")
    license_id = await _license(api_env, admin, product, x, 2)
    device = await _installed(api_env, org, x, x_site, "Audited")
    allocation = await api_env.client.post(
        f"/api/v1/licenses/{license_id}/allocations",
        json={"target_type": "device", "target_id": str(device)}, headers=admin,
    )
    assert allocation.status_code == 201, allocation.text
    released = await api_env.client.post(
        f"/api/v1/allocations/{allocation.json()['id']}/release", headers=admin
    )
    assert released.status_code == 200, released.text
    patched = await api_env.client.patch(
        f"/api/v1/licenses/{license_id}", json={"notes": "n"}, headers=admin
    )
    assert patched.status_code == 200, patched.text
    keyed = await _license(api_env, admin, product, x, 1, key="K-1")
    revealed = await api_env.client.get(f"/api/v1/licenses/{keyed}/key", headers=admin)
    assert revealed.status_code == 200, revealed.text
    deleted = await api_env.client.delete(f"/api/v1/licenses/{keyed}", headers=admin)
    assert deleted.status_code == 204, deleted.text
    sub = await _subscription(api_env, admin, x, renews_at=date.today().isoformat())
    sub_id = sub.json()["id"]
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub_id}/seats",
        json={"assignee_name": "A", "assignee_email": "a@x.test"}, headers=admin,
    )
    assert seat.status_code == 201, seat.text
    freed = await api_env.client.post(f"/api/v1/seats/{seat.json()['id']}/release", headers=admin)
    assert freed.status_code == 200, freed.text
    renewed = await api_env.client.post(f"/api/v1/subscriptions/{sub_id}/renew", headers=admin)
    assert renewed.status_code == 200, renewed.text
    cancelled = await api_env.client.post(f"/api/v1/subscriptions/{sub_id}/cancel", headers=admin)
    assert cancelled.status_code == 200, cancelled.text
    # Every license/subscription/allocation/seat event of this org: one row
    # each, all of them X's. `license.created` twice: two licenses.
    for action, rows in (
        ("license.created", 2), ("license.allocated", 1), ("license.allocation_released", 1),
        ("license.updated", 1), ("license.key_revealed", 1), ("license.deleted", 1),
        ("subscription.created", 1), ("subscription.seat_assigned", 1),
        ("subscription.seat_released", 1), ("subscription.renewed", 1),
        ("subscription.cancelled", 1),
    ):
        assert await _audit_clients(api_env, org, action) == [x] * rows, action


# --------------------------------------------------------------------------- #
# Allocations: who may hold a client's license
# --------------------------------------------------------------------------- #


async def test_who_may_hold_a_license(api_env: ApiEnv) -> None:
    """A device or a person of the license's client; a contact of it; a
    staff member only for an internal-client license (C4). Each refusal is a
    422 with the same sentence, and the database says the same under scope all."""
    org = await api_env.create_org("l-holders")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    sub = await api_env.create_user(org, "org-admin")
    admin = await api_env.headers(org, sub)
    staff = await _user_id(api_env, sub)
    product = await _product(api_env, admin, "Holders")
    x_license = await _license(api_env, admin, product, x, 10)
    internal_license = await _license(api_env, admin, product, org, 10)
    x_contact = await _contact(api_env, org, x)
    y_contact = await _contact(api_env, org, y)
    x_person = await _person(api_env, org, x)
    y_person = await _person(api_env, org, y)

    async def allocate(license_id: str, kind: str, target: UUID) -> Any:
        return await api_env.client.post(
            f"/api/v1/licenses/{license_id}/allocations",
            json={"target_type": kind, "target_id": str(target)}, headers=admin,
        )

    for license_id, kind, target, expected in [
        (x_license, "user", x_contact, 201),
        (x_license, "person", x_person, 201),
        (x_license, "user", y_contact, 422),
        (x_license, "person", y_person, 422),
        (x_license, "user", staff, 422),
        (internal_license, "user", staff, 201),
        (internal_license, "user", x_contact, 422),
    ]:
        r = await allocate(license_id, kind, target)
        assert r.status_code == expected, (kind, target, r.text)

    with pytest.raises(DBAPIError, match="allocation target belongs to another client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(LicenseAllocation(org_id=org, license_id=UUID(x_license), target_type="user",
                                    target_id=staff, created_by=staff))
            await s.flush()
    x_device = await _installed(api_env, org, x, x_site, "z")
    with pytest.raises(DBAPIError, match="fk_license_allocation_person_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(LicenseAllocation(org_id=org, license_id=UUID(x_license), target_type="device",
                                    target_id=x_device, person_id=y_person, created_by=staff))
            await s.flush()


async def test_a_technician_of_x_cannot_allocate_y_s_device(api_env: ApiEnv) -> None:
    """Under X's scope Y's device is invisible: refused as unknown, never
    allocated, and nothing about it leaks beyond "not found"."""
    org = await api_env.create_org("l-alloc-narrow")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Narrow")
    license_id = await _license(api_env, admin, product, x, 5)
    y_device = await _installed(api_env, org, y, y_site, "Narrow")
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"/api/v1/licenses/{license_id}/allocations",
        json={"target_type": "device", "target_id": str(y_device)}, headers=x_tech,
    )
    unknown = await api_env.client.post(
        f"/api/v1/licenses/{license_id}/allocations",
        json={"target_type": "device", "target_id": str(uuid4())}, headers=x_tech,
    )
    # Exactly what an id that exists nowhere gets: no oracle.
    assert (r.status_code, unknown.status_code) == (400, 400), (r.text, unknown.text)
    async with AsyncSession(api_env.admin_engine) as s:
        held = (
            await s.execute(
                select(LicenseAllocation.id).where(LicenseAllocation.target_id == y_device)
            )
        ).all()
    assert held == []


# --------------------------------------------------------------------------- #
# Seats and the license a subscription refers to
# --------------------------------------------------------------------------- #


async def test_a_seat_s_named_person_must_be_of_the_subscription_s_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("s-seat-person")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    sub = (await _subscription(api_env, admin, x)).json()["id"]
    y_person = await _person(api_env, org, y)
    x_person = await _person(api_env, org, x)
    refused = await api_env.client.post(
        f"/api/v1/subscriptions/{sub}/seats",
        json={"assignee_name": "Y", "assignee_email": "y@y.test", "person_id": str(y_person)},
        headers=admin,
    )
    assert refused.status_code == 422, refused.text
    ok = await api_env.client.post(
        f"/api/v1/subscriptions/{sub}/seats",
        json={"assignee_name": "X", "assignee_email": "x@x.test", "person_id": str(x_person)},
        headers=admin,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["person_id"] == str(x_person)


async def test_linking_a_seat_files_the_new_person_in_the_seat_s_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("s-link")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    sub = (await _subscription(api_env, admin, x)).json()["id"]
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub}/seats",
        json={"assignee_name": "Nuevo", "assignee_email": "nuevo@x.test"}, headers=admin,
    )
    assert seat.json()["person_id"] is None
    linked = await api_env.client.post(
        f"/api/v1/people/unmatched/seats/{seat.json()['id']}/link", json={}, headers=admin
    )
    assert linked.status_code == 200, linked.text
    async with AsyncSession(api_env.admin_engine) as s:
        person = (
            await s.execute(
                select(Person).join(SeatAssignment, SeatAssignment.person_id == Person.id)
                .where(SeatAssignment.id == UUID(seat.json()["id"]))
            )
        ).scalar_one()
    assert person.client_id == x
    assert await _audit_clients(api_env, org, "people.seat_linked") == [x]

    other = await api_env.client.post(
        f"/api/v1/subscriptions/{sub}/seats",
        json={"assignee_name": "Otro", "assignee_email": "otro@x.test"}, headers=admin,
    )
    y_person = await _person(api_env, org, y)
    refused = await api_env.client.post(
        f"/api/v1/people/unmatched/seats/{other.json()['id']}/link",
        json={"person_id": str(y_person)}, headers=admin,
    )
    assert refused.status_code == 422, refused.text


async def test_a_subscription_refers_only_to_a_license_of_its_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("s-ref")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Ref")
    x_license = await _license(api_env, admin, product, x, 1)
    y_license = await _license(api_env, admin, product, y, 1)
    refused = await _subscription(api_env, admin, x, license_ref=y_license)
    assert refused.status_code == 422, refused.text
    made = await _subscription(api_env, admin, x, license_ref=x_license)
    assert made.status_code == 201, made.text
    changed = await api_env.client.patch(
        f"/api/v1/subscriptions/{made.json()['id']}", json={"license_ref": y_license},
        headers=admin,
    )
    assert changed.status_code == 422, changed.text


# --------------------------------------------------------------------------- #
# Reports: one row per client, nothing summed across clients
# --------------------------------------------------------------------------- #


async def test_the_reports_are_per_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("l-reports")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Reported")
    soon = (date.today() + timedelta(days=5)).isoformat()
    for client, cost in ((x, "100.00"), (y, "40.00")):
        await _license(api_env, admin, product, client, 1, cost=cost, currency="USD",
                       renews_at=soon)
        made = await _subscription(api_env, admin, client, cost_per_cycle=cost)
        assert made.status_code == 201, made.text
    await _installed(api_env, org, x, x_site, "Reported")
    await _installed(api_env, org, x, x_site, "Reported")
    await _installed(api_env, org, y, y_site, "Reported")

    compliance = (
        await api_env.client.get("/api/v1/licenses/compliance", headers=admin)
    ).json()
    mine = {r["client_id"]: r for r in compliance if r["product_id"] == product}
    assert (mine[str(x)]["entitled"], mine[str(x)]["installed"]) == (1, 2)
    assert (mine[str(y)]["entitled"], mine[str(y)]["installed"]) == (1, 1)

    costs = (await api_env.client.get("/api/v1/licenses/costs", headers=admin)).json()
    by_client = {r["client_id"]: r["total_cost"] for r in costs if r["product_id"] == product}
    assert by_client == {str(x): "100.00", str(y): "40.00"}

    renewals = (await api_env.client.get("/api/v1/licenses/renewals", headers=admin)).json()
    assert {r["client_id"] for r in renewals if r["product_id"] == product} == {str(x), str(y)}

    spend = (await api_env.client.get("/api/v1/subscriptions/spend", headers=admin)).json()
    totals = {r["client_id"]: r["monthly_total"] for r in spend["by_currency"]}
    assert totals == {str(x): "100.00", str(y): "40.00"}
    assert {r["client_id"] for r in spend["by_vendor"]} == {str(x), str(y)}

    x_tech = await everything_on(api_env, org, x)
    narrowed = (
        await api_env.client.get("/api/v1/licenses/compliance", headers=x_tech)
    ).json()
    assert {r["client_id"] for r in narrowed} == {str(x)}


async def _statements_for(env: ApiEnv, headers: dict[str, str], url: str) -> int:
    """How many SQL statements the app's own engine runs to answer `url`."""
    count = 0

    def tick(*_: Any) -> None:
        nonlocal count
        count += 1

    engine = env.app.state.app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", tick)
    try:
        r = await env.client.get(url, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    assert r.status_code == 200, r.text
    return count


async def test_the_compliance_report_does_not_query_per_product(api_env: ApiEnv) -> None:
    """Review M1: the report used to run two queries per client per product
    (`/licenses` reads it on every page load). Its statement count may grow
    with clients -- one install load each -- but not with the catalogue: two
    clients (the internal one and X), the same count for one product as for
    five."""
    org = await api_env.create_org("l-fanout")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    first = await _product(api_env, admin, "Fan 0")
    for client in (org, x):
        await _license(api_env, admin, first, client, 1)
    await _installed(api_env, org, x, x_site, "Fan 0")
    await _installed(api_env, org, org, org, "Fan 0")
    url = "/api/v1/licenses/compliance"
    await api_env.client.get(url, headers=admin)  # warm whatever a first request caches
    one = await _statements_for(api_env, admin, url)
    for n in range(1, 5):
        product = await _product(api_env, admin, f"Fan {n}")
        await _license(api_env, admin, product, x, 1)
        await _installed(api_env, org, x, x_site, f"Fan {n}")
    rows = (await api_env.client.get(url, headers=admin)).json()
    assert len(rows) == 2 * 5  # two clients x five products: the report is all there
    five = await _statements_for(api_env, admin, url)
    assert five == one, f"{one} statements for one product, {five} for five"


async def test_the_daily_sweep_snapshots_each_client(
    api_env: ApiEnv, pg_cluster: Any, tmp_path: Path
) -> None:
    """One snapshot per active client per product: X's breach is X's, and Y
    and the internal client each hold their own (empty) position. "Sin
    asignar" has no sites, so no devices and no licenses (C10): no row."""
    from app.core.config import Settings, override_settings
    from app.models import LicenseComplianceSnapshot
    from app.worker import licenses_compliance_sweep_all_orgs

    org = await api_env.create_org("l-sweep")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await client_with_site(api_env, org, client_archived=True)
    admin = await _admin(api_env, org)
    product = await _product(api_env, admin, "Swept")
    await _license(api_env, admin, product, x, 1)
    await _installed(api_env, org, x, x_site, "Swept")
    await _installed(api_env, org, x, x_site, "Swept")
    stamp = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
    )
    with override_settings(settings):
        await licenses_compliance_sweep_all_orgs(stamp)
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(LicenseComplianceSnapshot.client_id, LicenseComplianceSnapshot.entitled,
                       LicenseComplianceSnapshot.installed, LicenseComplianceSnapshot.delta)
                .where(LicenseComplianceSnapshot.org_id == org,
                       LicenseComplianceSnapshot.product_id == UUID(product),
                       LicenseComplianceSnapshot.computed_at == stamp)
            )
        ).all()
    assert sorted(tuple(r) for r in rows) == sorted([(x, 1, 2, -1), (y, 0, 0, 0), (org, 0, 0, 0)])


# --------------------------------------------------------------------------- #
# Alerts and the ignore list
# --------------------------------------------------------------------------- #


async def test_a_subscription_alert_is_its_client_s(api_env: ApiEnv) -> None:
    from app.modules.subscriptions.alerts import emit_due_alerts

    org = await api_env.create_org("s-alert")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    soon = (date.today() + timedelta(days=3)).isoformat()
    sub = await _subscription(api_env, admin, x, renews_at=soon)
    assert sub.status_code == 201, sub.text
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await emit_due_alerts(s, org, today=date.today(), window_days=30) == 1
    async with AsyncSession(api_env.admin_engine) as s:
        event_clients = (
            await s.execute(
                select(DomainEvent.client_id).where(
                    DomainEvent.org_id == org,
                    DomainEvent.subject_id == UUID(sub.json()["id"]),
                )
            )
        ).scalars().all()
    assert event_clients == [x]


async def test_the_ignore_list_by_client(api_env: ApiEnv) -> None:
    """Without a client the row is global and needs every client (403 for
    X's technician, as before); with one, the client is loaded like any new
    row's -- Y's is a 404 for X's technician, "Sin asignar" a 422 -- and a
    repeat is the same row."""
    org = await api_env.create_org("l-ignore-refusals")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    admin = await _admin(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    url = "/api/v1/licenses/review-queue/ignore"
    assert (await api_env.client.post(url, json={"name": "G"}, headers=x_tech)).status_code == 403
    assert (
        await api_env.client.post(url, json={"name": "G", "client_id": str(y)}, headers=x_tech)
    ).status_code == 404
    assert (
        await api_env.client.post(
            url, json={"name": "G", "client_id": str(unassigned)}, headers=admin
        )
    ).status_code == 422
    first = await api_env.client.post(url, json={"name": "G", "client_id": str(x)}, headers=x_tech)
    again = await api_env.client.post(url, json={"name": "G", "client_id": str(x)}, headers=x_tech)
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["client_id"] == str(x)
    global_ = await api_env.client.post(url, json={"name": "G"}, headers=admin)
    assert global_.status_code == 200, global_.text
    assert global_.json()["id"] != first.json()["id"]
    assert global_.json()["client_id"] is None
    assert await _audit_clients(api_env, org, "license.software_ignored") == [x, x, None]

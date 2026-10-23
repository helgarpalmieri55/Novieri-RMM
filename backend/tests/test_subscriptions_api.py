"""Subscriptions REST API tests (docs/modules/subscriptions/spec.md §9, §12).

Mirrors `tests/test_licenses_api.py`'s style and the invariants it exercises:
server-side RBAC on every route (invariant #6), `SubscriptionValidationError`
routing to 4xx never 500 -- `SubscriptionNotFoundError` (a subclass) -> 404,
every other validation error -> 400 (invariant #5, cross-org ids never leak,
never crash), the audited-export vs non-audited-read split (invariant #4:
`GET /subscriptions/export` writes `subscription.export`; plain list/detail/
report reads write nothing), and route ordering (the static report paths must
resolve before `/subscriptions/{id}` captures them as an id). Uses the
`api_env` in-process ASGI harness (mirrors `tests/test_bcp_api.py`).
"""

from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent
from tests.conftest import ApiEnv


async def _create_vendor(
    api_env: ApiEnv, headers: dict[str, str], name: str = "Acme Corp"
) -> dict[str, Any]:
    resp = await api_env.client.post("/api/v1/vendors", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_subscription(
    api_env: ApiEnv,
    headers: dict[str, str],
    *,
    service_name: str = "Slack",
    billing_cycle: str = "monthly",
    cost_per_cycle: float = 100,
    currency: str = "USD",
    seats: int = 1,
    vendor_id: str | None = None,
    renews_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "service_name": service_name,
        "billing_cycle": billing_cycle,
        "cost_per_cycle": cost_per_cycle,
        "currency": currency,
        "seats": seats,
    }
    if vendor_id is not None:
        payload["vendor_id"] = vendor_id
    if renews_at is not None:
        payload["renews_at"] = renews_at
    resp = await api_env.client.post("/api/v1/subscriptions", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _audit_count(api_env: ApiEnv, org_id: UUID, action: str) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == action))
        ).scalars().all()
        return len(rows)


# --------------------------------------------------------------------------- #
# POST /subscriptions -- RBAC
# --------------------------------------------------------------------------- #


async def test_create_subscription_ok_for_technician_forbidden_for_read_only(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    ro_headers = await api_env.headers(org_id, ro)

    sub = await _create_subscription(api_env, tech_headers)
    assert sub["service_name"] == "Slack"
    assert sub["status"] == "active"
    assert "org_id" not in sub

    forbidden = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "Other",
            "billing_cycle": "monthly",
            "cost_per_cycle": 10,
            "currency": "USD",
        },
        headers=ro_headers,
    )
    assert forbidden.status_code == 403


async def test_list_and_get_subscription_ok_for_read(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    ro_headers = await api_env.headers(org_id, ro)

    created = await _create_subscription(api_env, tech_headers)

    listing = await api_env.client.get("/api/v1/subscriptions", headers=ro_headers)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    detail = await api_env.client.get(
        f"/api/v1/subscriptions/{created['id']}", headers=ro_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == created["id"]

    forbidden_patch = await api_env.client.patch(
        f"/api/v1/subscriptions/{created['id']}", json={"plan": "pro"}, headers=ro_headers
    )
    assert forbidden_patch.status_code == 403

    patched = await api_env.client.patch(
        f"/api/v1/subscriptions/{created['id']}", json={"plan": "pro"}, headers=tech_headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["plan"] == "pro"


# --------------------------------------------------------------------------- #
# Seats: assign to the cap then 400; release frees a slot; double-release 400
# --------------------------------------------------------------------------- #


async def test_seat_assign_release_pool_enforcement(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(api_env, headers, seats=1)

    first = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Alice", "assignee_email": "alice@example.com"},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["assignee_email"] == "alice@example.com"

    second = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/seats",
        json={"assignee_name": "Bob", "assignee_email": "bob@example.com"},
        headers=headers,
    )
    assert second.status_code == 400, second.text

    listing = await api_env.client.get(
        f"/api/v1/subscriptions/{sub['id']}/seats", headers=headers
    )
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    released = await api_env.client.post(
        f"/api/v1/seats/{first.json()['id']}/release", json={}, headers=headers
    )
    assert released.status_code == 200, released.text
    assert released.json()["released_at"] is not None

    released_again = await api_env.client.post(
        f"/api/v1/seats/{first.json()['id']}/release", json={}, headers=headers
    )
    assert released_again.status_code == 400, released_again.text

    released_unknown = await api_env.client.post(
        f"/api/v1/seats/{uuid4()}/release", json={}, headers=headers
    )
    assert released_unknown.status_code == 404


# --------------------------------------------------------------------------- #
# Lifecycle: cancel + renew
# --------------------------------------------------------------------------- #


async def test_cancel_and_renew_subscription(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(
        api_env, headers, billing_cycle="monthly", renews_at="2026-01-31"
    )

    renewed = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/renew", headers=headers
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["renews_at"] == "2026-02-28"

    cancelled = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/cancel", headers=headers
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    cancel_again = await api_env.client.post(
        f"/api/v1/subscriptions/{sub['id']}/cancel", headers=headers
    )
    assert cancel_again.status_code == 400, cancel_again.text

    events = await api_env.client.get(
        f"/api/v1/subscriptions/{sub['id']}/events", headers=headers
    )
    assert events.status_code == 200, events.text
    types = {e["type"] for e in events.json()}
    assert {"created", "renewal", "cancel"} <= types


# --------------------------------------------------------------------------- #
# GET /subscriptions/spend, /renewals, /idle-seats -- OK for read
# --------------------------------------------------------------------------- #


async def test_reports_ok_for_read(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    ro_headers = await api_env.headers(org_id, ro)

    await _create_subscription(api_env, tech_headers)
    # The renewal calendar excludes rows with a null `renews_at`, so the default
    # helper subscription alone would make /renewals return an empty list and
    # any assertion on it vacuous. Add one renewing inside the default 30-day
    # window so the report has a real row to verify.
    renewing_at = date.today() + timedelta(days=10)
    await _create_subscription(
        api_env,
        tech_headers,
        service_name="Figma",
        renews_at=renewing_at.isoformat(),
    )

    spend = await api_env.client.get("/api/v1/subscriptions/spend", headers=ro_headers)
    assert spend.status_code == 200, spend.text
    body = spend.json()
    assert set(body) == {"by_currency", "by_vendor", "by_category", "by_owner"}
    assert body["by_currency"][0]["currency"] == "USD"

    renewals = await api_env.client.get("/api/v1/subscriptions/renewals", headers=ro_headers)
    assert renewals.status_code == 200, renewals.text
    # Assert the row SHAPE, not just the status code: a 200 alone would also be
    # produced by a route that returned the wrong report entirely.
    assert set(renewals.json()[0]) == {
        "subscription_id",
        "service_name",
        "renews_at",
        "notice_deadline",
        "days_to_notice",
        "days_to_renewal",
        "action_required",
    }

    idle = await api_env.client.get("/api/v1/subscriptions/idle-seats", headers=ro_headers)
    assert idle.status_code == 200, idle.text
    assert idle.json()[0]["idle"] == 1

    forbidden = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "x",
            "billing_cycle": "monthly",
            "cost_per_cycle": 1,
            "currency": "USD",
        },
        headers=ro_headers,
    )
    assert forbidden.status_code == 403


# --------------------------------------------------------------------------- #
# Route ordering: /subscriptions/spend must resolve to the report route, not
# be captured as a /subscriptions/{id} path parameter.
# --------------------------------------------------------------------------- #


async def test_subscriptions_spend_resolves_to_report_route_not_id(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/subscriptions/spend", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"by_currency", "by_vendor", "by_category", "by_owner"}


# --------------------------------------------------------------------------- #
# Cross-org vendor_id on POST /subscriptions -- 400 not 500 (invariant #5)
# --------------------------------------------------------------------------- #


async def test_create_subscription_cross_org_vendor_id_is_400_not_500(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    other_org_id = await api_env.create_org("sub-other")
    tech = await api_env.create_user(org_id, "technician")
    other_tech = await api_env.create_user(other_org_id, "technician")

    other_vendor = await _create_vendor(api_env, await api_env.headers(other_org_id, other_tech))

    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "vendor_id": other_vendor["id"],
            "service_name": "CrossOrg",
            "billing_cycle": "monthly",
            "cost_per_cycle": 10,
            "currency": "USD",
        },
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# Missing ids -- 404 not 500
# --------------------------------------------------------------------------- #


async def test_missing_subscription_id_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    missing = uuid4()

    assert (
        await api_env.client.get(f"/api/v1/subscriptions/{missing}", headers=headers)
    ).status_code == 404
    assert (
        await api_env.client.patch(
            f"/api/v1/subscriptions/{missing}", json={"plan": "x"}, headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.post(
            f"/api/v1/subscriptions/{missing}/cancel", headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.post(
            f"/api/v1/subscriptions/{missing}/renew", headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.get(
            f"/api/v1/subscriptions/{missing}/events", headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.get(
            f"/api/v1/subscriptions/{missing}/seats", headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.post(
            f"/api/v1/subscriptions/{missing}/seats",
            json={"assignee_name": "x", "assignee_email": "x@example.com"},
            headers=headers,
        )
    ).status_code == 404


# --------------------------------------------------------------------------- #
# GET /subscriptions/export -- audited; GET /subscriptions (list) -- not.
# --------------------------------------------------------------------------- #


async def test_export_writes_audit_list_writes_none(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    await _create_subscription(api_env, headers)

    before_export = await _audit_count(api_env, org_id, "subscription.export")

    listing = await api_env.client.get("/api/v1/subscriptions", headers=headers)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    after_list = await _audit_count(api_env, org_id, "subscription.export")
    assert after_list == before_export, "GET /subscriptions must never write subscription.export"

    export = await api_env.client.get("/api/v1/subscriptions/export", headers=headers)
    assert export.status_code == 200, export.text
    assert len(export.json()) == 1
    assert "org_id" not in export.json()[0]

    after_export = await _audit_count(api_env, org_id, "subscription.export")
    assert after_export == before_export + 1


# --------------------------------------------------------------------------- #
# currency length -- 422 at the schema boundary (the Phase-11 lesson)
# --------------------------------------------------------------------------- #


async def test_create_subscription_currency_too_long_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "BadCurrency",
            "billing_cycle": "monthly",
            "cost_per_cycle": 10,
            "currency": "USDD",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Unbounded schema fields (I4) -- 422 at the boundary, never a Postgres
# 22001/22003 surfacing as a 500 (there is no global exception handler).
# --------------------------------------------------------------------------- #


async def test_create_subscription_over_long_service_name_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "x" * 300,  # column is varchar(255)
            "billing_cycle": "monthly",
            "cost_per_cycle": 10,
            "currency": "USD",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_create_subscription_oversized_cost_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "HugeCost",
            "billing_cycle": "monthly",
            "cost_per_cycle": 1e13,  # column is numeric(14, 2)
            "currency": "USD",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_create_subscription_negative_seats_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "service_name": "NegativeSeats",
            "billing_cycle": "monthly",
            "cost_per_cycle": 10,
            "currency": "USD",
            "seats": -5,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_patch_subscription_over_long_service_name_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(api_env, headers)

    resp = await api_env.client.patch(
        f"/api/v1/subscriptions/{sub['id']}",
        json={"service_name": "x" * 300},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# GET /subscriptions/renewals?within_days=N (I1, spec §7) -- the window must be
# caller-controlled; a caller asking for 90 days must not silently get 30.
# --------------------------------------------------------------------------- #


async def test_renewals_within_days_widens_the_window(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    far = date.today() + timedelta(days=60)
    sub = await _create_subscription(
        api_env, headers, service_name="FarRenewal", renews_at=far.isoformat()
    )

    default_window = await api_env.client.get(
        "/api/v1/subscriptions/renewals", headers=headers
    )
    assert default_window.status_code == 200, default_window.text
    assert sub["id"] not in {row["subscription_id"] for row in default_window.json()}

    wide_window = await api_env.client.get(
        "/api/v1/subscriptions/renewals?within_days=90", headers=headers
    )
    assert wide_window.status_code == 200, wide_window.text
    assert sub["id"] in {row["subscription_id"] for row in wide_window.json()}


async def test_renewals_within_days_out_of_range_is_422(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)

    for bad in ("0", "-1", "366", "abc"):
        resp = await api_env.client.get(
            f"/api/v1/subscriptions/renewals?within_days={bad}", headers=headers
        )
        assert resp.status_code == 422, f"within_days={bad}: {resp.text}"


# --------------------------------------------------------------------------- #
# PATCH explicit null (I2): "omitted" and "explicitly null" must be
# distinguishable -- clearing a nullable column must work, and a null on a
# NOT NULL column must be a 400, never a 500.
# --------------------------------------------------------------------------- #


async def test_patch_explicit_null_clears_renews_at_and_leaves_the_calendar(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    soon = date.today() + timedelta(days=10)
    sub = await _create_subscription(
        api_env, headers, service_name="Renewing", renews_at=soon.isoformat()
    )

    before = await api_env.client.get("/api/v1/subscriptions/renewals", headers=headers)
    assert before.status_code == 200, before.text
    assert sub["id"] in {row["subscription_id"] for row in before.json()}

    cleared = await api_env.client.patch(
        f"/api/v1/subscriptions/{sub['id']}", json={"renews_at": None}, headers=headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["renews_at"] is None

    after = await api_env.client.get("/api/v1/subscriptions/renewals", headers=headers)
    assert after.status_code == 200, after.text
    assert sub["id"] not in {row["subscription_id"] for row in after.json()}


async def test_patch_explicit_null_on_non_nullable_field_is_400_not_500(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(api_env, headers)

    for field in (
        "service_name",
        "billing_cycle",
        "cost_per_cycle",
        "currency",
        "seats",
        "status",
        "auto_renew",
        "notice_days",
        "plan",
        "category",
    ):
        resp = await api_env.client.patch(
            f"/api/v1/subscriptions/{sub['id']}", json={field: None}, headers=headers
        )
        assert resp.status_code == 400, f"{field}: {resp.status_code} {resp.text}"


async def test_patch_omitted_field_is_left_untouched(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(api_env, headers, service_name="Slack", seats=3)

    patched = await api_env.client.patch(
        f"/api/v1/subscriptions/{sub['id']}", json={"plan": "pro"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["plan"] == "pro"
    assert body["service_name"] == "Slack"
    assert body["seats"] == 3
    assert body["currency"] == "USD"
    assert body["status"] == "active"


# --------------------------------------------------------------------------- #
# Seat floor (M3): reducing `seats` below the active-assignment count is a 400.
# --------------------------------------------------------------------------- #


async def test_patch_seats_below_active_assignments_is_400(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sub")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    sub = await _create_subscription(api_env, headers, seats=3)

    for n in range(3):
        assigned = await api_env.client.post(
            f"/api/v1/subscriptions/{sub['id']}/seats",
            json={"assignee_name": f"P{n}", "assignee_email": f"p{n}@example.com"},
            headers=headers,
        )
        assert assigned.status_code == 201, assigned.text

    resp = await api_env.client.patch(
        f"/api/v1/subscriptions/{sub['id']}", json={"seats": 1}, headers=headers
    )
    assert resp.status_code == 400, resp.text

    idle = await api_env.client.get("/api/v1/subscriptions/idle-seats", headers=headers)
    assert idle.status_code == 200, idle.text
    row = next(r for r in idle.json() if r["subscription_id"] == sub["id"])
    assert row["seats"] == 3
    assert row["idle"] == 0

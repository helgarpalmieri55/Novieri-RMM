"""Budget REST API tests (docs/modules/budget/spec.md §9, §12).

Mirrors `tests/test_subscriptions_api.py`'s style and the invariants it
exercises: server-side RBAC on every route (invariant #6),
`BudgetValidationError` routing to 4xx never 500 -- `BudgetNotFoundError` (a
subclass) -> 404, every other validation error -> 400 (invariant #5,
cross-org ids never leak, never crash), the audited-export vs
non-audited-read split (invariant #4: both `GET .../export` variants write
`budget.export`; plain list/detail/report reads write nothing), and route
ordering (the static `.../export`, `.../export/odoo`, `.../materialize`
segments must resolve before `/budget/expenses/{id}` captures them as an
id; `/budget/forecast` must resolve before any budget/expense `{id}` route).
Uses the `api_env` in-process ASGI harness.

Spec §0.5 is this module's defining RBAC fact: technician holds NONE of the
four budget permissions (every prior module granted technician read) --
`test_technician_is_denied_every_budget_route` iterates every route in the
module and asserts a uniform 403. Unlike change_mgmt/bcp, which split their
two-tier permissions (`change.request`/`change.approve`,
`bcp.manage`/`bcp.approve`) across roles where technician already holds the
"do" half, no built-in role isolates `expense.record` from `expense.confirm`
for budget -- `_user_with_permissions` builds an org-scoped role granting
exactly the permissions under test, so the record/confirm split can be
proven independently of any built-in role's shape.
"""

import csv
import io
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Permission, Role, RolePermission, User, UserRole
from app.modules.budget.budgets import (
    close_budget,
    create_budget,
    get_budget,
    list_budgets,
)
from app.modules.budget.errors import BudgetNotFoundError
from app.modules.budget.refdata import DEFAULT_CATEGORIES
from tests.client_scope_support import internal_client
from tests.conftest import ApiEnv

API = "/api/v1/budget"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


async def _org_admin(api_env: ApiEnv, slug: str = "budget") -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org(slug)
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    return org_id, headers


async def _user_with_permissions(api_env: ApiEnv, org_id: UUID, *codes: str) -> str:
    """Create a user whose ONLY permissions are `codes`, via a bespoke
    org-scoped role -- needed because budget's expense.record/expense.confirm
    split (spec §0.5) has no built-in role that isolates one from the other."""
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
    return sub


async def _create_category(
    api_env: ApiEnv,
    headers: dict[str, str],
    *,
    key: str = "hardware",
    name: str = "Hardware",
    sort_order: int = 0,
) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"{API}/categories",
        json={
            "client_id": await internal_client(api_env, headers),
            "key": key,
            "name": name,
            "sort_order": sort_order,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_cost_center(
    api_env: ApiEnv, headers: dict[str, str], *, code: str = "CC1", name: str = "Cost Center 1"
) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"{API}/cost-centers",
        json={"client_id": await internal_client(api_env, headers), "code": code, "name": name},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_budget(
    api_env: ApiEnv,
    headers: dict[str, str],
    *,
    fiscal_year: int = 2026,
    name: str = "FY26",
    granularity: str = "month",
) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"{API}/budgets",
        json={
            "client_id": await internal_client(api_env, headers),
            "fiscal_year": fiscal_year,
            "name": name,
            "granularity": granularity,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_expense(
    api_env: ApiEnv, headers: dict[str, str], category_id: str, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_id": await internal_client(api_env, headers),
        "expense_date": "2026-01-15",
        "amount": "100.00",
        "currency": "USD",
        "category_id": category_id,
    }
    payload.update(overrides)
    resp = await api_env.client.post(f"{API}/expenses", json=payload, headers=headers)
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
# Spec §0.5 -- the module-defining RBAC assertion.
# --------------------------------------------------------------------------- #


async def test_technician_is_denied_every_budget_route(api_env: ApiEnv) -> None:
    org_id, admin_headers = await _org_admin(api_env, "budget-tech")
    tech = await api_env.create_user(org_id, "technician")
    tech_headers = await api_env.headers(org_id, tech)

    category = await _create_category(api_env, admin_headers)
    cost_center = await _create_cost_center(api_env, admin_headers)
    fx = await api_env.client.post(
        f"{API}/fx-rates",
        json={"from_currency": "EUR", "rate": "1.10", "effective_date": "2026-01-01"},
        headers=admin_headers,
    )
    assert fx.status_code == 201, fx.text
    fx_rate_id = fx.json()["id"]
    budget = await _create_budget(api_env, admin_headers)
    lines_resp = await api_env.client.put(
        f"{API}/budgets/{budget['id']}/lines",
        json={
            "lines": [
                {"category_id": category["id"], "period_index": 1, "planned_amount": "100.00"}
            ]
        },
        headers=admin_headers,
    )
    assert lines_resp.status_code == 200, lines_resp.text
    line_id = lines_resp.json()[0]["id"]
    expense = await _create_expense(api_env, admin_headers, category["id"])

    # Each entry's second element is the route TEMPLATE exactly as
    # `router.routes` reports it (no `/api/v1` prefix -- that's added at
    # `include_router` time, not part of the router itself); the third is the
    # concrete request path/query this test actually sends. P3 of the
    # post-review controller fix (.git/sdd/task-12-report.md): asserting
    # `declared == covered` below on the TEMPLATE set, not just running the
    # concrete requests, is what makes a future 34th route fail this test
    # instead of silently shipping ungated -- a hand-maintained list alone
    # stays green even when it falls out of sync with the router.
    # Every request names the internal client (plan 2026-10-26 Task 8), so
    # each 403 below is RBAC's, not a missing client's.
    client = {"client_id": str(org_id)}
    routes: list[tuple[str, str, str, dict[str, Any]]] = [
        ("GET", "/budget/settings", f"{API}/settings", {"params": client}),
        (
            "PUT",
            "/budget/settings",
            f"{API}/settings",
            {"params": client, "json": {"base_currency": "EUR"}},
        ),
        ("GET", "/budget/categories", f"{API}/categories", {}),
        (
            "POST",
            "/budget/categories",
            f"{API}/categories",
            {"json": {**client, "key": "svc2", "name": "Services 2"}},
        ),
        (
            "PATCH",
            "/budget/categories/{category_id}",
            f"{API}/categories/{category['id']}",
            {"json": {"name": "Renamed"}},
        ),
        ("GET", "/budget/cost-centers", f"{API}/cost-centers", {}),
        (
            "POST",
            "/budget/cost-centers",
            f"{API}/cost-centers",
            {"json": {**client, "code": "CC2", "name": "Cost 2"}},
        ),
        (
            "PATCH",
            "/budget/cost-centers/{cost_center_id}",
            f"{API}/cost-centers/{cost_center['id']}",
            {"json": {"name": "Renamed"}},
        ),
        ("GET", "/budget/fx-rates", f"{API}/fx-rates", {}),
        (
            "POST",
            "/budget/fx-rates",
            f"{API}/fx-rates",
            {"json": {"from_currency": "GBP", "rate": "1.30", "effective_date": "2026-01-01"}},
        ),
        (
            "PATCH",
            "/budget/fx-rates/{fx_rate_id}",
            f"{API}/fx-rates/{fx_rate_id}",
            {"json": {"rate": "1.40"}},
        ),
        ("GET", "/budget/budgets", f"{API}/budgets", {}),
        (
            "POST",
            "/budget/budgets",
            f"{API}/budgets",
            {"json": {**client, "fiscal_year": 2028, "name": "FY28", "granularity": "month"}},
        ),
        ("GET", "/budget/budgets/{budget_id}", f"{API}/budgets/{budget['id']}", {}),
        (
            "PATCH",
            "/budget/budgets/{budget_id}",
            f"{API}/budgets/{budget['id']}",
            {"json": {"name": "Renamed"}},
        ),
        (
            "POST",
            "/budget/budgets/{budget_id}/close",
            f"{API}/budgets/{budget['id']}/close",
            {},
        ),
        (
            "GET",
            "/budget/budgets/{budget_id}/lines",
            f"{API}/budgets/{budget['id']}/lines",
            {},
        ),
        (
            "PUT",
            "/budget/budgets/{budget_id}/lines",
            f"{API}/budgets/{budget['id']}/lines",
            {"json": {"lines": []}},
        ),
        (
            "DELETE",
            "/budget/budgets/{budget_id}/lines/{line_id}",
            f"{API}/budgets/{budget['id']}/lines/{line_id}",
            {},
        ),
        (
            "GET",
            "/budget/budgets/{budget_id}/variance",
            f"{API}/budgets/{budget['id']}/variance",
            {},
        ),
        (
            "GET",
            "/budget/budgets/{budget_id}/position",
            f"{API}/budgets/{budget['id']}/position",
            {},
        ),
        ("GET", "/budget/expenses", f"{API}/expenses", {}),
        (
            "POST",
            "/budget/expenses",
            f"{API}/expenses",
            {
                "json": {
                    **client,
                    "expense_date": "2026-01-15",
                    "amount": "50.00",
                    "currency": "USD",
                    "category_id": category["id"],
                }
            },
        ),
        ("GET", "/budget/expenses/{expense_id}", f"{API}/expenses/{expense['id']}", {}),
        (
            "PATCH",
            "/budget/expenses/{expense_id}",
            f"{API}/expenses/{expense['id']}",
            {"json": {"description": "y"}},
        ),
        (
            "POST",
            "/budget/expenses/{expense_id}/confirm",
            f"{API}/expenses/{expense['id']}/confirm",
            {"json": {}},
        ),
        (
            "POST",
            "/budget/expenses/{expense_id}/void",
            f"{API}/expenses/{expense['id']}/void",
            {},
        ),
        (
            "POST",
            "/budget/expenses/materialize",
            f"{API}/expenses/materialize",
            {"json": {**client, "from_date": "2026-01-01", "to_date": "2026-01-31"}},
        ),
        (
            "POST",
            "/budget/expenses/{expense_id}/attachment",
            f"{API}/expenses/{expense['id']}/attachment?filename=a.pdf",
            {"content": b"attachment-bytes"},
        ),
        (
            "GET",
            "/budget/expenses/{expense_id}/attachment",
            f"{API}/expenses/{expense['id']}/attachment",
            {},
        ),
        ("GET", "/budget/forecast", f"{API}/forecast", {"params": client}),
        ("GET", "/budget/expenses/export", f"{API}/expenses/export", {"params": client}),
        (
            "GET",
            "/budget/expenses/export/odoo",
            f"{API}/expenses/export/odoo",
            {"params": client},
        ),
    ]

    for method, _template, path, kwargs in routes:
        resp = await api_env.client.request(method, path, headers=tech_headers, **kwargs)
        assert resp.status_code == 403, f"{method} {path} leaked to technician ({resp.status_code})"

    # P3: derive the actual route set FROM the router rather than trusting
    # the hand-maintained list above stayed in sync -- a 34th route added to
    # `api.py` without a matching entry here now fails this assertion instead
    # of leaving the suite green with an ungated route.
    from app.modules.budget.api import router

    declared = {(m, r.path) for r in router.routes for m in r.methods if m != "HEAD"}
    covered = {(method, template) for method, template, _path, _kwargs in routes}
    assert declared == covered, f"route(s) not RBAC-tested: {declared - covered}"


# --------------------------------------------------------------------------- #
# expense.record vs expense.confirm -- neither subsumes the other.
# --------------------------------------------------------------------------- #


async def test_expense_record_cannot_confirm(api_env: ApiEnv) -> None:
    org_id, admin_headers = await _org_admin(api_env, "budget-rec")
    category = await _create_category(api_env, admin_headers)
    expense = await _create_expense(api_env, admin_headers, category["id"])

    recorder_sub = await _user_with_permissions(api_env, org_id, "budget.read", "expense.record")
    recorder_headers = await api_env.headers(org_id, recorder_sub)

    created = await _create_expense(api_env, recorder_headers, category["id"])
    assert created["status"] == "expected"

    confirm = await api_env.client.post(
        f"{API}/expenses/{expense['id']}/confirm", json={}, headers=recorder_headers
    )
    assert confirm.status_code == 403, confirm.text


async def test_expense_confirm_cannot_create(api_env: ApiEnv) -> None:
    org_id, admin_headers = await _org_admin(api_env, "budget-conf")
    category = await _create_category(api_env, admin_headers)
    expense = await _create_expense(api_env, admin_headers, category["id"])

    confirmer_sub = await _user_with_permissions(
        api_env, org_id, "budget.read", "expense.confirm"
    )
    confirmer_headers = await api_env.headers(org_id, confirmer_sub)

    create = await api_env.client.post(
        f"{API}/expenses",
        json={
            "client_id": str(org_id),
            "expense_date": "2026-01-15",
            "amount": "10.00",
            "currency": "USD",
            "category_id": category["id"],
        },
        headers=confirmer_headers,
    )
    assert create.status_code == 403, create.text

    confirmed = await api_env.client.post(
        f"{API}/expenses/{expense['id']}/confirm", json={}, headers=confirmer_headers
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# Cross-org / invalid-input mapping -- 404/422, never 500.
# --------------------------------------------------------------------------- #


async def test_cross_org_budget_id_is_404_not_500(api_env: ApiEnv) -> None:
    org_a, headers_a = await _org_admin(api_env, "budget-xa")
    org_b, headers_b = await _org_admin(api_env, "budget-xb")
    budget_b = await _create_budget(api_env, headers_b)

    resp = await api_env.client.get(f"{API}/budgets/{budget_b['id']}", headers=headers_a)
    assert resp.status_code == 404, resp.text

    resp2 = await api_env.client.get(f"{API}/budgets/{uuid4()}", headers=headers_a)
    assert resp2.status_code == 404, resp2.text


async def test_invalid_granularity_is_422_not_500(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-gran")
    resp = await api_env.client.post(
        f"{API}/budgets",
        json={
            "client_id": str(org_id),
            "fiscal_year": 2026,
            "name": "FYBad",
            "granularity": "year",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "granularity" in resp.text


# --------------------------------------------------------------------------- #
# Invariant #4: exports audit, list does not.
# --------------------------------------------------------------------------- #


async def test_export_is_audited_and_list_is_not(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-exp")
    category = await _create_category(api_env, headers)
    await _create_expense(api_env, headers, category["id"])

    before = await _audit_count(api_env, org_id, "budget.export")

    listing = await api_env.client.get(f"{API}/expenses", headers=headers)
    assert listing.status_code == 200, listing.text
    assert await _audit_count(api_env, org_id, "budget.export") == before, (
        "GET /budget/expenses must never write budget.export"
    )

    client = {"client_id": str(org_id)}
    export = await api_env.client.get(f"{API}/expenses/export", params=client, headers=headers)
    assert export.status_code == 200, export.text
    assert await _audit_count(api_env, org_id, "budget.export") == before + 1

    odoo_export = await api_env.client.get(
        f"{API}/expenses/export/odoo", params=client, headers=headers
    )
    assert odoo_export.status_code == 200, odoo_export.text
    assert await _audit_count(api_env, org_id, "budget.export") == before + 2


# --------------------------------------------------------------------------- #
# CSV formula-injection escaping (deploy/README.md points an ERP import job
# at the Odoo export -- this is the most exposed of the five hardened
# modules) -- escaped per column, never per row, and a negative amount must
# survive completely raw in BOTH exports (spec: budget carries signed
# money; a credit note or refund is a legitimate negative figure, not an
# attack).
# --------------------------------------------------------------------------- #


async def test_exports_escape_free_text_columns_but_leave_amount_and_date_raw(
    api_env: ApiEnv,
) -> None:
    org_id, headers = await _org_admin(api_env, "budget-csv-inj")
    category = await _create_category(
        api_env, headers, key="svc", name='=HYPERLINK("http://evil","x")'
    )
    cost_center = await _create_cost_center(api_env, headers, code="CC9", name="+1+1")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        admin_user_id = (
            await session.execute(select(User.id).where(User.org_id == org_id))
        ).scalars().first()
        from app.modules.licenses.models import Vendor

        vendor = Vendor(org_id=org_id, name="-50 Corp", created_by=admin_user_id)
        session.add(vendor)
        await session.commit()
        vendor_id = str(vendor.id)

    # A credit note: a legitimate NEGATIVE amount, plus free-text columns
    # each seeded with a different dangerous leading character (`=`, `+`,
    # `-`, `@`).
    expense = await _create_expense(
        api_env,
        headers,
        category["id"],
        amount="-100.00",
        cost_center_id=cost_center["id"],
        vendor_id=vendor_id,
        description="@SUM(A1:A9)",
        invoice_ref="-DROP-CREDIT-9001",
        status="confirmed",
    )
    assert expense["amount"] == "-100.00"

    client = {"client_id": str(org_id)}
    export = await api_env.client.get(f"{API}/expenses/export", params=client, headers=headers)
    assert export.status_code == 200, export.text
    rows = list(csv.reader(io.StringIO(export.text)))
    header = rows[0]
    data_row = next(r for r in rows[1:] if r[header.index("id")] == expense["id"])

    def _cell(col: str) -> str:
        return data_row[header.index(col)]

    # Free-text columns: each escaped -- a leading quote neutralizes the
    # formula without altering the underlying text.
    assert _cell("category") == '\'=HYPERLINK("http://evil","x")'
    assert _cell("cost_center") == "'+1+1"
    assert _cell("vendor") == "'-50 Corp"
    assert _cell("description") == "'@SUM(A1:A9)"
    assert _cell("invoice_ref") == "'-DROP-CREDIT-9001"
    # Money/date/id/enum columns: raw, unescaped -- this is the proof a
    # legitimate negative amount is never mangled.
    assert _cell("amount") == "-100.00"
    assert _cell("amount_base") == "-100.00"
    assert _cell("expense_date") == "2026-01-15"
    assert _cell("status") == "confirmed"

    odoo_export = await api_env.client.get(
        f"{API}/expenses/export/odoo", params=client, headers=headers
    )
    assert odoo_export.status_code == 200, odoo_export.text
    odoo_rows = list(csv.reader(io.StringIO(odoo_export.text)))
    odoo_header = odoo_rows[0]
    odoo_row = next(
        r
        for r in odoo_rows[1:]
        if r[odoo_header.index("reference")] == "'-DROP-CREDIT-9001"
    )

    def _odoo_cell(col: str) -> str:
        return odoo_row[odoo_header.index(col)]

    assert _odoo_cell("reference") == "'-DROP-CREDIT-9001"
    assert _odoo_cell("category") == '\'=HYPERLINK("http://evil","x")'
    assert _odoo_cell("cost_center") == "'+1+1"
    assert _odoo_cell("vendor") == "'-50 Corp"
    assert _odoo_cell("description") == "'@SUM(A1:A9)"
    # The pinned Odoo contract (spec §7) is read by an ERP importer, not
    # opened in Excel -- `amount`/`amount_base` must round-trip as PLAIN,
    # machine-parseable negative numbers, never prefixed.
    assert _odoo_cell("amount") == "-100.00"
    assert _odoo_cell("amount_base") == "-100.00"
    assert _odoo_cell("date") == "2026-01-15"


# --------------------------------------------------------------------------- #
# Route ordering (the Phase-11/12 lesson).
# --------------------------------------------------------------------------- #


async def test_read_only_user_seeding_categories_is_not_attributed_to_them(
    api_env: ApiEnv,
) -> None:
    """N1: `auditor` (migration 0020) is granted exactly `budget.read`. On a
    fresh org, that reader's very first `GET /budget/categories` is exactly
    what trips the lazy seed (C1) -- and the seed must never be recorded as
    THAT reader creating the org's expense taxonomy. Zero
    `budget.category_created` events, exactly one system-attributed
    `budget.categories_seeded`; a second call adds neither."""
    org_id, _admin_headers = await _org_admin(api_env, "budget-ro-seed")
    reader = await api_env.create_user(org_id, "auditor")
    reader_headers = await api_env.headers(org_id, reader)

    # The seed is per client (plan 2026-10-26 Task 8): a read that names the
    # internal client seeds its defaults.
    client = {"client_id": str(org_id)}
    first = await api_env.client.get(f"{API}/categories", params=client, headers=reader_headers)
    assert first.status_code == 200, first.text
    assert len(first.json()) == len(DEFAULT_CATEGORIES) == 7

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        created = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.category_created",
                )
            )
        ).scalars().all()
        seeded = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.categories_seeded",
                )
            )
        ).scalars().all()

    assert created == [], "a budget.read-only reader must never appear as the creator"
    assert len(seeded) == 1
    assert seeded[0].actor_type == "system"
    assert seeded[0].actor_id is None

    second = await api_env.client.get(f"{API}/categories", params=client, headers=reader_headers)
    assert second.status_code == 200, second.text
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        seeded_after = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.categories_seeded",
                )
            )
        ).scalars().all()
    assert len(seeded_after) == 1, "the second call inserts nothing and must audit nothing"


async def test_static_paths_resolve_before_the_id_route(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-ord")
    client = {"client_id": str(org_id)}

    export = await api_env.client.get(f"{API}/expenses/export", params=client, headers=headers)
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")

    odoo = await api_env.client.get(f"{API}/expenses/export/odoo", params=client, headers=headers)
    assert odoo.status_code == 200, odoo.text

    materialize = await api_env.client.post(
        f"{API}/expenses/materialize",
        json={**client, "from_date": "2026-01-01", "to_date": "2026-01-31"},
        headers=headers,
    )
    assert materialize.status_code == 200, materialize.text
    assert set(materialize.json()) == {"created", "skipped_existing", "skipped_unconvertible"}


async def test_forecast_route_resolves(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-fc")
    resp = await api_env.client.get(
        f"{API}/forecast", params={"client_id": str(org_id)}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"rows", "unconvertible_count", "unconvertible_by_currency"}


async def test_forecast_months_out_of_range_is_422(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-fcb")
    for bad in ("0", "61", "-1", "abc"):
        resp = await api_env.client.get(
            f"{API}/forecast?months={bad}&client_id={org_id}", headers=headers
        )
        assert resp.status_code == 422, f"months={bad}: {resp.text}"
        assert "months" in resp.text, resp.text


# --------------------------------------------------------------------------- #
# Materialization idempotency through the HTTP route (spec §6.3).
# --------------------------------------------------------------------------- #


async def test_materialize_is_idempotent_through_the_api(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-mat")

    # No manual `seed_default_categories` call: `materialize` lazily seeds
    # the org's default categories itself (C1 fix) -- see
    # `test_brand_new_org_materializes_variance_and_forecast_without_manual_seeding`
    # below for the dedicated regression test.

    sub = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "client_id": str(org_id),
            "service_name": "Materialized Co",
            "billing_cycle": "monthly",
            "cost_per_cycle": 42,
            "currency": "USD",
            "renews_at": "2026-01-20",
        },
        headers=headers,
    )
    assert sub.status_code == 201, sub.text

    body = {"client_id": str(org_id), "from_date": "2026-01-01", "to_date": "2026-01-31"}
    first = await api_env.client.post(f"{API}/expenses/materialize", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1

    second = await api_env.client.post(f"{API}/expenses/materialize", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["created"] == 0
    assert second.json()["skipped_existing"] == 1


async def test_brand_new_org_materializes_variance_and_forecast_without_manual_seeding(
    api_env: ApiEnv,
) -> None:
    """C1: before the lazy-seed fix, `refdata.seed_default_categories` was
    called by NOTHING in production -- only tests -- so `materialize`,
    `variance`, and `forecast` 400'd for every real org with a subscription
    or license renewing in scope, and no route could create a system-keyed
    category to recover. This is the test that would have caught it: a
    brand-new org, with NO prior category setup and no direct call to
    `seed_default_categories` anywhere in this test, must be able to
    materialize a renewing subscription and read variance/forecast through
    the API alone -- and end up with exactly the seven system-keyed
    categories.
    """
    org_id, headers = await _org_admin(api_env, "budget-lazyseed")

    sub = await api_env.client.post(
        "/api/v1/subscriptions",
        json={
            "client_id": str(org_id),
            "service_name": "Lazy Seed Co",
            "billing_cycle": "monthly",
            "cost_per_cycle": 42,
            "currency": "USD",
            "renews_at": "2026-01-20",
        },
        headers=headers,
    )
    assert sub.status_code == 201, sub.text

    materialize_resp = await api_env.client.post(
        f"{API}/expenses/materialize",
        json={"client_id": str(org_id), "from_date": "2026-01-01", "to_date": "2026-01-31"},
        headers=headers,
    )
    assert materialize_resp.status_code == 200, materialize_resp.text
    assert materialize_resp.json()["created"] == 1

    budget = await _create_budget(api_env, headers, fiscal_year=2026)
    variance_resp = await api_env.client.get(
        f"{API}/budgets/{budget['id']}/variance", headers=headers
    )
    assert variance_resp.status_code == 200, variance_resp.text

    forecast_resp = await api_env.client.get(
        f"{API}/forecast?months=1&client_id={org_id}", headers=headers
    )
    assert forecast_resp.status_code == 200, forecast_resp.text

    categories_resp = await api_env.client.get(f"{API}/categories", headers=headers)
    assert categories_resp.status_code == 200, categories_resp.text
    categories = categories_resp.json()
    system_keys = {c["system_key"] for c in categories if c["system_key"] is not None}
    assert system_keys == {key for key, _name in DEFAULT_CATEGORIES}
    assert len(categories) == len(DEFAULT_CATEGORIES) == 7


# --------------------------------------------------------------------------- #
# Settings + base-currency mixed-basis guard (spec §3).
# --------------------------------------------------------------------------- #


async def test_settings_lifecycle_and_base_currency_mixed_basis_guard(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-settings")
    client = {"client_id": str(org_id)}

    initial = await api_env.client.get(f"{API}/settings", params=client, headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["base_currency"] == "USD"
    assert initial.json()["fiscal_year_start_month"] == 1
    assert initial.json()["updated_by"] is None

    category = await _create_category(api_env, headers)
    # An expense with a resolvable rate (base -> base is exactly 1, no
    # lookup) carries a non-NULL amount_base -- the mixed-basis guard
    # trigger.
    converted = await _create_expense(api_env, headers, category["id"], currency="USD")
    assert converted["amount_base"] == "100.00"

    blocked = await api_env.client.put(
        f"{API}/settings", params=client, json={"base_currency": "EUR"}, headers=headers
    )
    assert blocked.status_code == 400, blocked.text

    allowed = await api_env.client.put(
        f"{API}/settings",
        params=client,
        json={"base_currency": "EUR", "acknowledge_mixed_basis": True},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["base_currency"] == "EUR"

    # Not retroactive: the earlier expense keeps its original amount_base.
    unchanged = await api_env.client.get(
        f"{API}/expenses/{converted['id']}", headers=headers
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["amount_base"] == "100.00"

    fiscal = await api_env.client.put(
        f"{API}/settings", params=client, json={"fiscal_year_start_month": 4}, headers=headers
    )
    assert fiscal.status_code == 200, fiscal.text
    assert fiscal.json()["fiscal_year_start_month"] == 4
    assert fiscal.json()["updated_by"] is not None


# --------------------------------------------------------------------------- #
# Category / cost-center / FX-rate CRUD, and the explicit-null 400 rule.
# --------------------------------------------------------------------------- #


async def test_category_crud_and_explicit_null_on_non_nullable_is_400(api_env: ApiEnv) -> None:
    _, headers = await _org_admin(api_env, "budget-cat")

    created = await _create_category(api_env, headers, key="travel", name="Travel", sort_order=5)
    assert created["sort_order"] == 5
    assert created["system_key"] is None

    patched = await api_env.client.patch(
        f"{API}/categories/{created['id']}",
        json={"name": "Travel & Expenses", "active": False},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Travel & Expenses"
    assert patched.json()["active"] is False

    for field in ("name", "active", "sort_order"):
        bad = await api_env.client.patch(
            f"{API}/categories/{created['id']}", json={field: None}, headers=headers
        )
        assert bad.status_code == 400, f"{field}: {bad.status_code} {bad.text}"

    missing = await api_env.client.patch(
        f"{API}/categories/{uuid4()}", json={"name": "x"}, headers=headers
    )
    assert missing.status_code == 404, missing.text


async def test_cost_center_crud_and_explicit_null_on_non_nullable_is_400(
    api_env: ApiEnv,
) -> None:
    _, headers = await _org_admin(api_env, "budget-cc")

    created = await _create_cost_center(api_env, headers, code="OPS", name="Operations")
    patched = await api_env.client.patch(
        f"{API}/cost-centers/{created['id']}",
        json={"external_ref": "gl-4000"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["external_ref"] == "gl-4000"

    for field in ("name", "active"):
        bad = await api_env.client.patch(
            f"{API}/cost-centers/{created['id']}", json={field: None}, headers=headers
        )
        assert bad.status_code == 400, f"{field}: {bad.status_code} {bad.text}"


async def test_fx_rate_crud_and_explicit_null_is_400(api_env: ApiEnv) -> None:
    _, headers = await _org_admin(api_env, "budget-fx")

    created = await api_env.client.post(
        f"{API}/fx-rates",
        json={"from_currency": "JPY", "rate": "0.0067", "effective_date": "2026-01-01"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["to_currency"] == "USD"

    patched = await api_env.client.patch(
        f"{API}/fx-rates/{created.json()['id']}", json={"rate": "0.0070"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["rate"] == "0.0070"

    bad = await api_env.client.patch(
        f"{API}/fx-rates/{created.json()['id']}", json={"rate": None}, headers=headers
    )
    assert bad.status_code == 400, bad.text

    duplicate = await api_env.client.post(
        f"{API}/fx-rates",
        json={"from_currency": "JPY", "rate": "0.0068", "effective_date": "2026-01-01"},
        headers=headers,
    )
    assert duplicate.status_code == 400, duplicate.text


# --------------------------------------------------------------------------- #
# Budget + lines lifecycle, including the closed-budget freeze (spec §4).
# --------------------------------------------------------------------------- #


async def test_budget_and_lines_lifecycle(api_env: ApiEnv) -> None:
    _, headers = await _org_admin(api_env, "budget-lines")
    category = await _create_category(api_env, headers)
    budget = await _create_budget(api_env, headers)

    put = await api_env.client.put(
        f"{API}/budgets/{budget['id']}/lines",
        json={
            "lines": [
                {"category_id": category["id"], "period_index": 1, "planned_amount": "500.00"}
            ]
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    line_id = put.json()[0]["id"]

    listing = await api_env.client.get(f"{API}/budgets/{budget['id']}/lines", headers=headers)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    deleted = await api_env.client.delete(
        f"{API}/budgets/{budget['id']}/lines/{line_id}", headers=headers
    )
    assert deleted.status_code == 204, deleted.text

    empty = await api_env.client.get(f"{API}/budgets/{budget['id']}/lines", headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json() == []

    closed = await api_env.client.post(
        f"{API}/budgets/{budget['id']}/close", headers=headers
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    frozen = await api_env.client.put(
        f"{API}/budgets/{budget['id']}/lines",
        json={
            "lines": [
                {"category_id": category["id"], "period_index": 2, "planned_amount": "1.00"}
            ]
        },
        headers=headers,
    )
    assert frozen.status_code == 400, frozen.text


async def test_variance_and_position_shapes(api_env: ApiEnv) -> None:
    _, headers = await _org_admin(api_env, "budget-var")
    category = await _create_category(api_env, headers)
    budget = await _create_budget(api_env, headers)
    await api_env.client.put(
        f"{API}/budgets/{budget['id']}/lines",
        json={
            "lines": [
                {"category_id": category["id"], "period_index": 1, "planned_amount": "200.00"}
            ]
        },
        headers=headers,
    )
    await _create_expense(
        api_env, headers, category["id"], expense_date="2026-01-10", status="confirmed"
    )

    variance = await api_env.client.get(
        f"{API}/budgets/{budget['id']}/variance", headers=headers
    )
    assert variance.status_code == 200, variance.text
    assert set(variance.json()) == {"rows", "unconvertible_count", "unconvertible_by_currency"}
    row = next(r for r in variance.json()["rows"] if r["period_index"] == 1)
    assert row["planned"] == "200.00"
    assert row["actual"] == "100.00"

    position = await api_env.client.get(
        f"{API}/budgets/{budget['id']}/position?as_of=2026-01-31", headers=headers
    )
    assert position.status_code == 200, position.text
    assert position.json()["ytd_actual"] == "100.00"


# --------------------------------------------------------------------------- #
# Expense lifecycle + the load-bearing exclude_unset PATCH semantics.
# --------------------------------------------------------------------------- #


async def test_expense_lifecycle_create_patch_confirm_void(api_env: ApiEnv) -> None:
    _, headers = await _org_admin(api_env, "budget-exp-life")
    category = await _create_category(api_env, headers)
    cost_center = await _create_cost_center(api_env, headers)

    created = await _create_expense(
        api_env, headers, category["id"], cost_center_id=cost_center["id"]
    )
    assert created["status"] == "expected"
    assert created["cost_center_id"] == cost_center["id"]

    patched = await api_env.client.patch(
        f"{API}/expenses/{created['id']}",
        json={"description": "conference travel"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["description"] == "conference travel"
    # Omitted field left untouched.
    assert patched.json()["cost_center_id"] == cost_center["id"]

    cleared = await api_env.client.patch(
        f"{API}/expenses/{created['id']}", json={"cost_center_id": None}, headers=headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cost_center_id"] is None

    non_nullable_bad = await api_env.client.patch(
        f"{API}/expenses/{created['id']}", json={"amount": None}, headers=headers
    )
    assert non_nullable_bad.status_code == 400, non_nullable_bad.text

    confirmed = await api_env.client.post(
        f"{API}/expenses/{created['id']}/confirm",
        json={"amount": "123.45"},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["amount"] == "123.45"
    assert confirmed.json()["confirmed_by"] is not None

    re_confirm = await api_env.client.post(
        f"{API}/expenses/{created['id']}/confirm", json={}, headers=headers
    )
    assert re_confirm.status_code == 400, re_confirm.text

    other = await _create_expense(api_env, headers, category["id"])
    voided = await api_env.client.post(f"{API}/expenses/{other['id']}/void", headers=headers)
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "void"

    void_again = await api_env.client.post(f"{API}/expenses/{other['id']}/void", headers=headers)
    assert void_again.status_code == 400, void_again.text

    confirm_void = await api_env.client.post(
        f"{API}/expenses/{other['id']}/confirm", json={}, headers=headers
    )
    assert confirm_void.status_code == 400, confirm_void.text


# --------------------------------------------------------------------------- #
# convert_for's two failure modes (context item #7): unresolvable rate is
# NOT an error; overflow IS a 400, never a 500.
# --------------------------------------------------------------------------- #


async def test_unresolvable_rate_creates_expense_with_null_amount_base(
    api_env: ApiEnv,
) -> None:
    _, headers = await _org_admin(api_env, "budget-unresolv")
    category = await _create_category(api_env, headers)

    expense = await _create_expense(api_env, headers, category["id"], currency="COP")
    assert expense["status"] == "expected"
    assert expense["fx_rate"] is None
    assert expense["amount_base"] is None


async def test_fx_overflow_is_400_not_500(api_env: ApiEnv) -> None:
    org_id, headers = await _org_admin(api_env, "budget-overflow")
    category = await _create_category(api_env, headers)

    rate = await api_env.client.post(
        f"{API}/fx-rates",
        json={"from_currency": "XXX", "rate": "2", "effective_date": "2026-01-01"},
        headers=headers,
    )
    assert rate.status_code == 201, rate.text

    resp = await api_env.client.post(
        f"{API}/expenses",
        json={
            "client_id": str(org_id),
            "expense_date": "2026-01-15",
            "amount": "900000000000.00",
            "currency": "XXX",
            "category_id": category["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# Attachment upload/download (spec §5, §8): download is audited, upload is
# audited too (no double-audit -- this is this route's own mutation).
#
# P1-P4 of the post-review controller fix (.git/sdd/task-12-report.md): a
# malicious `filename` must never reach the object-storage key or a response
# header; oversized bodies are rejected before they can OOM the process; a
# voided expense may never receive an attachment.
# --------------------------------------------------------------------------- #


async def test_attachment_upload_download_roundtrip_and_audit(api_env: ApiEnv) -> None:
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, headers = await _org_admin(api_env, "budget-attach")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    with mock_aws():
        # Pin a moto-backed store so the route's lazy `_object_store` builder
        # never constructs a fresh, unmocked one mid-request (mirrors
        # `tests/test_remote_api.py::test_recording_download_ok_and_audited`).
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        missing = await api_env.client.get(
            f"{API}/expenses/{expense['id']}/attachment", headers=headers
        )
        assert missing.status_code == 404, missing.text

        before_upload = await _audit_count(api_env, org_id, "expense.attachment_uploaded")
        before_download = await _audit_count(api_env, org_id, "expense.attachment_downloaded")

        upload = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=invoice.pdf",
            content=b"%PDF-fake-bytes",
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        # P5: the raw object-storage key is never returned; only the boolean
        # fact and the sanitized display name are.
        assert upload.json()["has_attachment"] is True
        assert "invoice_object_key" not in upload.json()
        assert upload.json()["invoice_filename"] == "invoice.pdf"
        assert (
            await _audit_count(api_env, org_id, "expense.attachment_uploaded")
            == before_upload + 1
        )
        assert (
            await _audit_count(api_env, org_id, "expense.attachment_downloaded")
            == before_download
        )

        download = await api_env.client.get(
            f"{API}/expenses/{expense['id']}/attachment", headers=headers
        )
        assert download.status_code == 200, download.text
        assert download.content == b"%PDF-fake-bytes"
        # P1(c): the header is built from the sanitized STORED name.
        assert download.headers["content-disposition"] == 'attachment; filename="invoice.pdf"'
        assert (
            await _audit_count(api_env, org_id, "expense.attachment_downloaded")
            == before_download + 1
        )

        # Replacing the attachment (P4) -- the new bytes/filename win, and no
        # second, orphaned object is left reachable under any key this module
        # exposes.
        replaced = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=invoice-v2.pdf",
            content=b"second-version-bytes",
            headers=headers,
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["invoice_filename"] == "invoice-v2.pdf"

        redownload = await api_env.client.get(
            f"{API}/expenses/{expense['id']}/attachment", headers=headers
        )
        assert redownload.status_code == 200, redownload.text
        assert redownload.content == b"second-version-bytes"
        assert (
            redownload.headers["content-disposition"]
            == 'attachment; filename="invoice-v2.pdf"'
        )


async def test_attachment_upload_rejects_crlf_traversal_empty_and_overlong_filenames(
    api_env: ApiEnv,
) -> None:
    """P1 -- the security tests the fix is FOR. None of these four values may
    ever be persisted or reach a header; each must be rejected 400 before
    touching the object store."""
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org_id, headers = await _org_admin(api_env, "budget-attach-bad")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        # CRLF: decodes to a value containing a bare `\r\n` -- if this ever
        # reached a `Content-Disposition` header unsanitized, h11 raises
        # `LocalProtocolError: Illegal header value` on the NEXT download (an
        # unrecoverable 500, since the poisoned key would already be on the
        # row). It must never get that far.
        crlf = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment"
            "?filename=a%0d%0aX-Evil%3A%201.pdf",
            content=b"bytes",
            headers=headers,
        )
        assert crlf.status_code == 400, crlf.text

        # `..` traversal: must never be silently reduced to a trailing path
        # segment and accepted.
        traversal = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=../../../etc/passwd",
            content=b"bytes",
            headers=headers,
        )
        assert traversal.status_code == 400, traversal.text

        # Empty filename.
        empty = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=",
            content=b"bytes",
            headers=headers,
        )
        assert empty.status_code == 400, empty.text

        # Over-length (300 chars, comfortably past the 255 cap).
        overlong = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename={'a' * 300}",
            content=b"bytes",
            headers=headers,
        )
        assert overlong.status_code == 400, overlong.text

        # None of the above may have been stored: the expense still reports
        # no attachment, and download still 404s.
        detail = await api_env.client.get(f"{API}/expenses/{expense['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["has_attachment"] is False

        still_missing = await api_env.client.get(
            f"{API}/expenses/{expense['id']}/attachment", headers=headers
        )
        assert still_missing.status_code == 404, still_missing.text


async def test_attachment_upload_accepts_a_normal_filename_end_to_end(api_env: ApiEnv) -> None:
    """A legitimate filename is unaffected by the P1 sanitizer and round-trips
    through upload -> download with the expected header."""
    from moto import mock_aws

    from app.core.storage import ObjectStore

    _, headers = await _org_admin(api_env, "budget-attach-ok")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        upload = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=Q3 Invoice - Final.pdf",
            content=b"normal-bytes",
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["invoice_filename"] == "Q3 Invoice - Final.pdf"

        download = await api_env.client.get(
            f"{API}/expenses/{expense['id']}/attachment", headers=headers
        )
        assert download.status_code == 200, download.text
        assert download.content == b"normal-bytes"
        assert (
            download.headers["content-disposition"]
            == 'attachment; filename="Q3 Invoice - Final.pdf"'
        )


async def test_attachment_upload_oversized_body_is_413(api_env: ApiEnv) -> None:
    """P2 -- an unbounded `await request.body()` could OOM the control plane;
    a body past the documented 10 MiB cap must be rejected, not buffered."""
    from moto import mock_aws

    from app.core.storage import ObjectStore
    from app.modules.budget.api import _MAX_ATTACHMENT_BYTES

    _, headers = await _org_admin(api_env, "budget-attach-big")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        oversized = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=big.pdf",
            content=b"x" * (_MAX_ATTACHMENT_BYTES + 1),
            headers=headers,
        )
        assert oversized.status_code == 413, oversized.text

        detail = await api_env.client.get(f"{API}/expenses/{expense['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["has_attachment"] is False


async def test_attachment_upload_oversized_chunked_body_is_413_via_streaming_guard(
    api_env: ApiEnv,
) -> None:
    """The OTHER half of P2's guard, and the one that actually matters
    against a hostile client. `test_attachment_upload_oversized_body_is_413`
    above posts a plain `bytes` body, which httpx always sends with a
    declared `Content-Length` -- so that test only ever exercises
    `reject_oversized_content_length`'s HEADER fast-path, never
    `read_bounded_body`'s streaming enforcement. `Content-Length` is
    client-supplied, and a genuinely chunked request
    (`Transfer-Encoding: chunked`) carries none at all, so the header check
    simply returns without rejecting anything. Posting an async-generator
    body makes httpx negotiate chunked transfer with no `Content-Length`
    (verified: `ASGITransport` receives `transfer-encoding: chunked` and no
    `content-length` header at all), forcing the request through the
    streaming path -- this proves `read_bounded_body` independently rejects
    an oversized body with no declared length."""
    from moto import mock_aws

    from app.core.storage import ObjectStore
    from app.modules.budget.api import _MAX_ATTACHMENT_BYTES

    _, headers = await _org_admin(api_env, "budget-attach-chunked")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    async def _chunks():
        chunk = b"x" * (1024 * 1024)
        sent = 0
        while sent <= _MAX_ATTACHMENT_BYTES:
            yield chunk
            sent += len(chunk)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        oversized = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=big.pdf",
            content=_chunks(),
            headers=headers,
        )
        assert oversized.status_code == 413, oversized.text

        detail = await api_env.client.get(f"{API}/expenses/{expense['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["has_attachment"] is False


async def test_attachment_upload_rejected_on_a_void_expense(api_env: ApiEnv) -> None:
    """P4 -- upload bypassed the expense state machine; a voided expense must
    never receive an attachment the audit trail says was retracted."""
    from moto import mock_aws

    from app.core.storage import ObjectStore

    _, headers = await _org_admin(api_env, "budget-attach-void")
    category = await _create_category(api_env, headers)
    expense = await _create_expense(api_env, headers, category["id"])

    voided = await api_env.client.post(f"{API}/expenses/{expense['id']}/void", headers=headers)
    assert voided.status_code == 200, voided.text

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        upload = await api_env.client.post(
            f"{API}/expenses/{expense['id']}/attachment?filename=invoice.pdf",
            content=b"bytes",
            headers=headers,
        )
        assert upload.status_code == 400, upload.text



async def test_the_budget_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    api_env: ApiEnv, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    `test_cross_org_budget_id_is_404_not_500` above drives the route, i.e.
    `org_session` on the NOBYPASSRLS `app_user` role, where `budget`'s forced
    `tenant_isolation` policy removes the neighbour's row before
    `Budget.org_id == org_id` is consulted -- it is green with that predicate
    deleted, and 404-rather-than-500 is what it pins. That is worth pinning and
    it is not the filter. `its_admin` carries BYPASSRLS (`tests/conftest.py`),
    so here the filter is the only scoping.

    BOTH ORGS HOLD `(2026, "Ops")`, WHICH IS THE POINT. The uniqueness
    constraint is `(org_id, fiscal_year, name)`, so two tenants running the
    same fiscal year with the same budget name is an ordinary state, and every
    assertion below would read identically for the wrong row if the predicate
    went missing. `create_budget`'s duplicate pre-check is asserted for the
    same reason: without its org predicate, org B naming a budget org A already
    has would be told the name is taken by a row it cannot see.

    `close_budget` is included because it is a WRITE that resolves through
    `get_budget`: a miss there does not disclose a neighbour's budget, it
    closes it, and a closed budget refuses further expense records.

    FALSIFIED: deleting `Budget.org_id == org_id` from `get_budget` returns org
    B's budget to org A and lets `close_budget` close it; deleting it from
    `list_budgets` returns both orgs' rows -- and, asked for org B's internal
    client (its id is org B's), org B's budget: the client filter alone does
    not exclude it, so only the org predicate does (plan 2026-10-26 Task 8,
    lesson 2). `create_budget`'s duplicate pre-check is per client since Task
    8, and its client is loaded org-scoped first (`client_for_new_rows`), so
    org A cannot reach org B's names through it at all.
    """
    org_a, _headers_a = await _org_admin(api_env, "budget-pred-a")
    org_b, _headers_b = await _org_admin(api_env, "budget-pred-b")
    async with AsyncSession(api_env.admin_engine) as session:
        actor_a = (
            await session.execute(select(User.id).where(User.org_id == org_a).limit(1))
        ).scalar_one()
        actor_b = (
            await session.execute(select(User.id).where(User.org_id == org_b).limit(1))
        ).scalar_one()

    budgets: dict[str, UUID] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        budget = await create_budget(
            bypassrls_session,
            org_id,
            actor_id, client_id=org_id,
            fiscal_year=2026,
            name="Ops",
            granularity="annual",
        )
        budgets[key] = budget.id
    await bypassrls_session.commit()

    assert [b.id for b in await list_budgets(bypassrls_session, org_a)] == [budgets["a"]]
    assert [b.id for b in await list_budgets(bypassrls_session, org_b)] == [budgets["b"]]
    assert await list_budgets(bypassrls_session, org_a, client_id=org_b) == []

    with pytest.raises(BudgetNotFoundError):
        await get_budget(bypassrls_session, org_a, budgets["b"])

    with pytest.raises(BudgetNotFoundError):
        await close_budget(bypassrls_session, org_a, actor_a, budgets["b"])
    await bypassrls_session.rollback()

    theirs = await get_budget(bypassrls_session, org_b, budgets["b"])
    assert theirs.status != "closed", "org A closed a neighbour tenant's budget"

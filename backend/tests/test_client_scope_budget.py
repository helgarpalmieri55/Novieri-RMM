"""Budget per client (plan 2026-10-26 Task 8).

Budget settings, expense categories, cost centres, budgets and their lines,
and expenses belong to a client, under the database's client isolation
(migration 0238); budget is an optional module per client (Task 7's
`require_module`). Each client has its own base currency and fiscal year; a
budget line's and an expense's category and cost centre are the row's
client's; what an expense links to is the expense's client's; the renewal
feed files each client's subscriptions and licenses under that client alone.
`fx_rate` stays the organisation's (G).
"""

import asyncio
import calendar
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.router_inspect import flatten
from app.models import (
    AuditEvent,
    Budget,
    BudgetLine,
    BudgetSettings,
    ChangeRequest,
    Client,
    CostCenter,
    Device,
    Expense,
    ExpenseCategory,
    License,
    SoftwareProduct,
    Subscription,
    User,
)
from app.modules.budget.budgets import list_budgets
from app.modules.budget.expenses import list_expenses
from app.modules.budget.refdata import (
    category_by_system_key,
    list_categories,
    list_cost_centers,
    seed_default_categories,
)
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

B = "/api/v1/budget"
MODULE_OFF = "this client does not use that module"
NO_SUCH_CLIENT = "no such client"

#: Every table of the family (migration 0238).
FAMILY = (BudgetSettings, CostCenter, ExpenseCategory, Budget, BudgetLine, Expense)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _budget_on(env: ApiEnv, admin: dict[str, str], client: UUID) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": ["budget"]}, headers=admin
    )
    assert r.status_code == 200, r.text


async def _budget_off(env: ApiEnv, admin: dict[str, str], client: UUID) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": []}, headers=admin
    )
    assert r.status_code == 200, r.text


async def _user_id(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.org_id == org).limit(1))
        ).scalar_one()


async def _post(env: ApiEnv, path: str, body: dict[str, Any], headers: dict[str, str]) -> str:
    r = await env.client.post(f"{B}{path}", json=body, headers=headers)
    assert r.status_code == 201, (path, r.text)
    return str(r.json()["id"])


async def _category(
    env: ApiEnv, headers: dict[str, str], client: UUID, key: str = "hw"
) -> str:
    return await _post(
        env, "/categories", {"client_id": str(client), "key": key, "name": key.upper()}, headers
    )


async def _cost_center(
    env: ApiEnv, headers: dict[str, str], client: UUID, code: str = "IT"
) -> str:
    return await _post(
        env, "/cost-centers", {"client_id": str(client), "code": code, "name": code}, headers
    )


async def _budget(
    env: ApiEnv, headers: dict[str, str], client: UUID, name: str = "TI", year: int = 2027
) -> str:
    return await _post(
        env,
        "/budgets",
        {"client_id": str(client), "fiscal_year": year, "name": name, "granularity": "month"},
        headers,
    )


def _expense_body(client: UUID, category: str, **extra: Any) -> dict[str, Any]:
    return {
        "client_id": str(client), "category_id": category, "amount": "100.00",
        "currency": "USD", "expense_date": "2027-01-10", "description": "d", **extra,
    }


async def _expense(
    env: ApiEnv, headers: dict[str, str], client: UUID, category: str, **extra: Any
) -> str:
    return await _post(env, "/expenses", _expense_body(client, category, **extra), headers)


async def _seed(env: ApiEnv, *rows: Any) -> list[UUID]:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()
    return [row.id for row in rows]


async def _subscription(
    env: ApiEnv, org: UUID, client: UUID, actor: UUID, *, renews_at: date, name: str = "SaaS"
) -> UUID:
    [sub] = await _seed(env, Subscription(
        org_id=org, client_id=client, service_name=name, billing_cycle="annual",
        cost_per_cycle=Decimal("120.00"), currency="USD", renews_at=renews_at,
        created_by=actor,
    ))
    return sub


async def _license(
    env: ApiEnv, org: UUID, client: UUID, actor: UUID, *, renews_at: date
) -> UUID:
    [product] = await _seed(env, SoftwareProduct(
        org_id=org, name=f"Suite {uuid4().hex[:6]}", created_by=actor
    ))
    [lic] = await _seed(env, License(
        org_id=org, client_id=client, product_id=product, type="volume", entitlements=1,
        cost=Decimal("50.00"), currency="USD", renews_at=renews_at, created_by=actor,
    ))
    return lic


async def _rows(env: ApiEnv, org: UUID) -> dict[str, int]:
    """How many rows of each table of the family the organisation holds."""
    counts: dict[str, int] = {}
    async with AsyncSession(env.admin_engine) as s:
        for model in FAMILY:
            counts[model.__tablename__] = (
                await s.execute(
                    select(func.count()).select_from(model).where(model.org_id == org)
                )
            ).scalar_one()
    return counts


async def _audit(env: ApiEnv, org: UUID) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org).order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The brief's tests (bodies are the real schemas, `budget/schema.py`)
# --------------------------------------------------------------------------- #


async def test_each_client_has_its_own_base_currency(api_env: ApiEnv) -> None:
    org = await api_env.create_org("b-settings")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    r = await api_env.client.put(
        f"{B}/settings", params={"client_id": str(x)},
        json={"base_currency": "EUR", "fiscal_year_start_month": 7}, headers=admin,
    )
    assert r.status_code == 200, r.text
    internal = await api_env.client.get(f"{B}/settings", params={"client_id": str(org)},
                                        headers=admin)
    mine = await api_env.client.get(f"{B}/settings", params={"client_id": str(x)},
                                    headers=admin)
    assert (mine.json()["base_currency"], mine.json()["fiscal_year_start_month"]) == ("EUR", 7)
    assert mine.json()["client_id"] == str(x)
    assert (internal.json()["base_currency"], internal.json()["fiscal_year_start_month"]) == (
        "USD", 1,
    )
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(BudgetSettings.client_id, BudgetSettings.base_currency).where(
                    BudgetSettings.org_id == org
                )
            )
        ).all()
    assert rows == [(x, "EUR")]


async def test_reading_a_client_without_budget_writes_nothing(api_env: ApiEnv) -> None:
    """research-5 §I: `get_settings`, `list_categories`, `variance` and
    `forecast` seeded rows on READ. For a client without the module they
    must not -- and `get_settings` writes nothing for anyone: a client's
    settings row exists once somebody sets it."""
    org = await api_env.create_org("b-pure")
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _subscription(api_env, org, y, await _user_id(api_env, org), renews_at=date.today())

    categories = await api_env.client.get(
        f"{B}/categories", params={"client_id": str(y)}, headers=admin
    )
    settings = await api_env.client.get(f"{B}/settings", params={"client_id": str(y)},
                                        headers=admin)
    forecast = await api_env.client.get(f"{B}/forecast", params={"client_id": str(y)},
                                        headers=admin)
    internal = await api_env.client.get(f"{B}/settings", params={"client_id": str(org)},
                                        headers=admin)
    assert categories.status_code == 200 and categories.json() == [], categories.text
    assert settings.status_code == 200, settings.text
    assert settings.json()["base_currency"] == "USD"
    assert settings.json()["updated_by"] is None and settings.json()["updated_at"] is None
    # A client without budget has no forecast: its subscription is not projected.
    assert forecast.status_code == 200 and forecast.json()["rows"] == [], forecast.text
    assert internal.status_code == 200 and internal.json()["base_currency"] == "USD"
    assert await _rows(api_env, org) == {m.__tablename__: 0 for m in FAMILY}
    assert [e.action for e in await _audit(api_env, org) if e.action.startswith("budget.")] == []


async def test_a_budget_line_cannot_use_another_client_s_category(api_env: ApiEnv) -> None:
    org = await api_env.create_org("b-line")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    y_category = await _category(api_env, admin, y)
    y_center = await _cost_center(api_env, admin, y)
    x_category = await _category(api_env, admin, x)
    budget = await _budget(api_env, admin, x)
    for line in (
        {"category_id": y_category, "planned_amount": "10.00", "period_index": 1},
        {"category_id": x_category, "cost_center_id": y_center, "planned_amount": "10.00",
         "period_index": 1},
    ):
        r = await api_env.client.put(
            f"{B}/budgets/{budget}/lines", json={"lines": [line]}, headers=admin
        )
        assert r.status_code == 422, r.text
    assert (await _rows(api_env, org))["budget_line"] == 0


async def test_every_budget_write_refuses_a_client_without_the_module(api_env: ApiEnv) -> None:
    """The route list is read from the app (as in tests/test_client_modules.py).
    The FX table is the organisation's (G) and stays every-client config.
    Each case was run once with the module ON, and none answers 404 there."""
    org = await api_env.create_org("b-off")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    category = await _category(api_env, admin, x, "sw")
    center = await _cost_center(api_env, admin, x)
    budget = await _budget(api_env, admin, x)
    lines = (await api_env.client.put(
        f"{B}/budgets/{budget}/lines",
        json={"lines": [{"category_id": category, "planned_amount": "1.00", "period_index": 1}]},
        headers=admin)).json()
    expense = await _expense(api_env, admin, x, category)
    await _budget_off(api_env, admin, x)
    before = await _rows(api_env, org)

    cases: dict[tuple[str, str], tuple[str, Any]] = {
        ("PUT", f"{B}/settings"): (f"{B}/settings?client_id={x}", {"base_currency": "EUR"}),
        ("POST", f"{B}/categories"): (
            f"{B}/categories", {"client_id": str(x), "key": "k", "name": "n"}),
        ("PATCH", f"{B}/categories/{{category_id}}"): (
            f"{B}/categories/{category}", {"name": "m"}),
        ("POST", f"{B}/cost-centers"): (
            f"{B}/cost-centers", {"client_id": str(x), "code": "C", "name": "n"}),
        ("PATCH", f"{B}/cost-centers/{{cost_center_id}}"): (
            f"{B}/cost-centers/{center}", {"name": "m"}),
        ("POST", f"{B}/budgets"): (f"{B}/budgets", {
            "client_id": str(x), "fiscal_year": 2028, "name": "n", "granularity": "month"}),
        ("PATCH", f"{B}/budgets/{{budget_id}}"): (f"{B}/budgets/{budget}", {"name": "m"}),
        ("POST", f"{B}/budgets/{{budget_id}}/close"): (f"{B}/budgets/{budget}/close", None),
        ("PUT", f"{B}/budgets/{{budget_id}}/lines"): (
            f"{B}/budgets/{budget}/lines", {"lines": []}),
        ("DELETE", f"{B}/budgets/{{budget_id}}/lines/{{line_id}}"): (
            f"{B}/budgets/{budget}/lines/{lines[0]['id']}", None),
        ("POST", f"{B}/expenses/materialize"): (f"{B}/expenses/materialize", {
            "client_id": str(x), "from_date": "2027-01-01", "to_date": "2027-01-31"}),
        ("POST", f"{B}/expenses"): (
            f"{B}/expenses", _expense_body(x, category, expense_date="2027-01-11")),
        ("PATCH", f"{B}/expenses/{{expense_id}}"): (
            f"{B}/expenses/{expense}", {"description": "e"}),
        ("POST", f"{B}/expenses/{{expense_id}}/confirm"): (
            f"{B}/expenses/{expense}/confirm", {}),
        ("POST", f"{B}/expenses/{{expense_id}}/void"): (f"{B}/expenses/{expense}/void", None),
        ("POST", f"{B}/expenses/{{expense_id}}/attachment"): (
            f"{B}/expenses/{expense}/attachment?filename=a.pdf", None),
    }
    global_config = {("POST", f"{B}/fx-rates"), ("PATCH", f"{B}/fx-rates/{{fx_rate_id}}")}
    routes = {
        (method, path)
        for path, route in flatten(api_env.app.router)
        for method in getattr(route, "methods", None) or set()
        if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith(B)
    }
    assert routes - global_config == set(cases), sorted((routes - global_config) ^ set(cases))
    for (method, _), (url, body) in cases.items():
        if url.endswith("filename=a.pdf"):
            r = await api_env.client.post(url, content=b"%PDF-1.4", headers=admin)
        else:
            r = await api_env.client.request(method, url, json=body, headers=admin)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        assert r.json()["detail"] == MODULE_OFF, (method, url, r.text)
    assert await _rows(api_env, org) == before
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(Budget.name, Budget.status).where(Budget.id == UUID(budget)))
        ).one() == ("TI", "draft")
        assert (
            await s.execute(
                select(Expense.description, Expense.status, Expense.invoice_object_key).where(
                    Expense.id == UUID(expense)
                )
            )
        ).one() == ("d", "expected", None)
        assert (await s.execute(select(ExpenseCategory.name).where(
            ExpenseCategory.id == UUID(category)))).scalar_one() == "SW"
        assert (await s.execute(select(CostCenter.name).where(
            CostCenter.id == UUID(center)))).scalar_one() == "IT"


# --------------------------------------------------------------------------- #
# Creating, and who is answered what
# --------------------------------------------------------------------------- #


async def test_a_budget_row_names_a_client_it_may_file_under(api_env: ApiEnv) -> None:
    """The create rules every new row's client follows (`client_for_new_rows`):
    none named 422, invisible or another organisation's 404, archived 409,
    "Sin asignar" 422 (C10). A refused create writes no row of any table of
    the family, and audits nothing."""
    org = await api_env.create_org("b-create")
    other = await api_env.create_org("b-create-other")
    admin = await _admin(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    # The internal client's category: the client check answers before it is read.
    category = await _category(api_env, admin, org)
    before = await _rows(api_env, org)
    audit_before = len(await _audit(api_env, org))

    bodies = {
        "/categories": lambda c: {"client_id": str(c), "key": "k", "name": "n"},
        "/cost-centers": lambda c: {"client_id": str(c), "code": "C", "name": "n"},
        "/budgets": lambda c: {"client_id": str(c), "fiscal_year": 2027, "name": "n",
                               "granularity": "month"},
        "/expenses": lambda c: _expense_body(c, category),
        "/expenses/materialize": lambda c: {"client_id": str(c), "from_date": "2027-01-01",
                                            "to_date": "2027-01-31"},
    }
    for path, body in bodies.items():
        missing = body(org)
        del missing["client_id"]
        r = await api_env.client.post(f"{B}{path}", json=missing, headers=admin)
        assert r.status_code == 422 and "client_id" in r.text, (path, r.text)
        for client, expected in ((other, 404), (uuid4(), 404), (archived, 409),
                                 (unassigned, 422)):
            r = await api_env.client.post(f"{B}{path}", json=body(client), headers=admin)
            assert r.status_code == expected, (path, client, r.status_code, r.text)
            if expected == 404:
                assert r.json()["detail"] == NO_SUCH_CLIENT, (path, r.text)
    assert await _rows(api_env, org) == before
    assert await _rows(api_env, other) == {m.__tablename__: 0 for m in FAMILY}
    assert len(await _audit(api_env, org)) == audit_before


async def test_rbac_answers_before_the_module_does(api_env: ApiEnv) -> None:
    """A person without the permission is told so (403) whether or not the
    client uses the module: the module lookup runs after RBAC, in the
    service."""
    org = await api_env.create_org("b-rbac")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    budget = await _budget(api_env, admin, x)
    category = await _category(api_env, admin, x)
    await _budget_off(api_env, admin, x)
    auditor = await api_env.headers(org, await api_env.create_user(org, "auditor"))
    for method, url, body in (
        ("PUT", f"{B}/settings?client_id={x}", {"base_currency": "EUR"}),
        ("POST", f"{B}/budgets", {"client_id": str(x), "fiscal_year": 2028, "name": "n",
                                  "granularity": "month"}),
        ("PATCH", f"{B}/budgets/{budget}", {"name": "n"}),
        ("POST", f"{B}/expenses", _expense_body(x, category)),
    ):
        r = await api_env.client.request(method, url, json=body, headers=auditor)
        assert r.status_code == 403, (method, url, r.status_code, r.text)


async def test_an_expense_s_category_centre_and_link_are_its_client_s(api_env: ApiEnv) -> None:
    """What an expense points at is of the expense's client: another client's
    category, cost centre, subscription, license, device or change is a 422
    (on create and on edit); a missing one stays the 404 it was. Nothing is
    written by a refusal."""
    org = await api_env.create_org("b-refs")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    actor = await _user_id(api_env, org)
    x_category = await _category(api_env, admin, x)
    y_category = await _category(api_env, admin, y)
    y_center = await _cost_center(api_env, admin, y)
    links: dict[str, dict[UUID, UUID]] = {"subscription": {}, "license": {}, "device": {},
                                          "change": {}}
    for client, site in ((x, x_site), (y, y_site)):
        links["subscription"][client] = await _subscription(
            api_env, org, client, actor, renews_at=date(2027, 3, 1))
        links["license"][client] = await _license(
            api_env, org, client, actor, renews_at=date(2027, 3, 1))
        [links["device"][client]] = await _seed(api_env, Device(
            org_id=org, client_id=client, site_id=site, hostname="h", os_family="windows"))
        [links["change"][client]] = await _seed(api_env, ChangeRequest(
            org_id=org, client_id=client, type="normal", title="t", description="d",
            risk="low", affected=[], created_by=actor))
    expense = await _expense(api_env, admin, x, x_category)
    before = await _rows(api_env, org)

    refused = [
        _expense_body(x, y_category),
        _expense_body(x, x_category, cost_center_id=y_center),
        *(
            _expense_body(x, x_category, link_type=kind, link_id=str(ids[y]))
            for kind, ids in links.items()
        ),
    ]
    for body in refused:
        r = await api_env.client.post(f"{B}/expenses", json=body, headers=admin)
        assert r.status_code == 422, (body, r.status_code, r.text)
    for patch in (
        {"category_id": y_category},
        {"cost_center_id": y_center},
        *({"link_type": kind, "link_id": str(ids[y])} for kind, ids in links.items()),
    ):
        r = await api_env.client.patch(f"{B}/expenses/{expense}", json=patch, headers=admin)
        assert r.status_code == 422, (patch, r.status_code, r.text)
    assert await _rows(api_env, org) == before
    async with AsyncSession(api_env.admin_engine) as s:
        row = (await s.execute(select(Expense).where(Expense.id == UUID(expense)))).scalar_one()
        assert (str(row.category_id), row.cost_center_id, row.link_id) == (x_category, None, None)

    # Its own client's are accepted; a missing one is the 404 it always was.
    for kind, ids in links.items():
        r = await api_env.client.post(
            f"{B}/expenses",
            json=_expense_body(x, x_category, link_type=kind, link_id=str(ids[x])),
            headers=admin,
        )
        assert r.status_code == 201, (kind, r.text)
        missing = _expense_body(x, x_category, link_type=kind, link_id=str(uuid4()))
        r = await api_env.client.post(f"{B}/expenses", json=missing, headers=admin)
        assert r.status_code == 404, (kind, r.text)


async def test_natural_keys_are_per_client(api_env: ApiEnv) -> None:
    """Two clients may each have a "hw" category, an "IT" cost centre and a
    2027 "TI" budget, and each gets its own default categories; inside one
    client they stay unique."""
    org = await api_env.create_org("b-keys")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
        await _category(api_env, admin, client)
        await _cost_center(api_env, admin, client)
        await _budget(api_env, admin, client)
    for path, body in (
        ("/categories", {"client_id": str(x), "key": "hw", "name": "again"}),
        ("/cost-centers", {"client_id": str(x), "code": "IT", "name": "again"}),
        ("/budgets", {"client_id": str(x), "fiscal_year": 2027, "name": "TI",
                      "granularity": "month"}),
    ):
        r = await api_env.client.post(f"{B}{path}", json=body, headers=admin)
        assert r.status_code == 400, (path, r.text)
    for client in (x, y):
        r = await api_env.client.get(
            f"{B}/categories", params={"client_id": str(client)}, headers=admin
        )
        assert r.status_code == 200, r.text
        system = sorted(c["system_key"] for c in r.json() if c["system_key"] is not None)
        assert len(system) == 7, r.json()
        assert {c["client_id"] for c in r.json()} == {str(client)}


# --------------------------------------------------------------------------- #
# Reads: per client, and the reads that need one
# --------------------------------------------------------------------------- #


async def test_lists_filter_by_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("b-lists")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    made: dict[UUID, dict[str, str]] = {}
    for client in (x, y):
        await _budget_on(api_env, admin, client)
        category = await _category(api_env, admin, client)
        made[client] = {
            "categories": category,
            "cost-centers": await _cost_center(api_env, admin, client),
            "budgets": await _budget(api_env, admin, client),
            "expenses": await _expense(api_env, admin, client, category),
        }
    for path in ("categories", "cost-centers", "budgets", "expenses"):
        r = await api_env.client.get(f"{B}/{path}", params={"client_id": str(x)}, headers=admin)
        assert r.status_code == 200, r.text
        ids = {row["id"] for row in r.json() if row.get("system_key") is None}
        assert ids == {made[x][path]}, (path, r.json())
        assert {row["client_id"] for row in r.json()} == {str(x)}
        everyone = await api_env.client.get(f"{B}/{path}", headers=admin)
        assert {made[x][path], made[y][path]} <= {row["id"] for row in everyone.json()}


async def test_the_reads_that_need_a_client(api_env: ApiEnv) -> None:
    """Settings, the forecast and both exports are one client's: each client
    has its own base currency and fiscal year (research-5 §I2), and summing
    across clients would mix currencies. Without a client: 422. A client this
    session cannot see: 404 "no such client". The exports of a client without
    budget: the module's 404."""
    org = await api_env.create_org("b-need")
    other = await api_env.create_org("b-need-other")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    reads = (
        f"{B}/settings", f"{B}/forecast", f"{B}/expenses/export", f"{B}/expenses/export/odoo"
    )
    for path in reads:
        r = await api_env.client.get(path, headers=admin)
        assert r.status_code == 422 and "client_id" in r.text, (path, r.text)
        for invisible in (other, uuid4()):
            r = await api_env.client.get(path, params={"client_id": str(invisible)},
                                         headers=admin)
            assert r.status_code == 404, (path, r.text)
            assert r.json()["detail"] == NO_SUCH_CLIENT, (path, r.text)
    r = await api_env.client.put(f"{B}/settings", params={"client_id": str(other)},
                                 json={"base_currency": "EUR"}, headers=admin)
    assert (r.status_code, r.json()["detail"]) == (404, NO_SUCH_CLIENT), r.text
    for path in reads[2:]:
        r = await api_env.client.get(path, params={"client_id": str(x)}, headers=admin)
        assert (r.status_code, r.json()["detail"]) == (404, MODULE_OFF), (path, r.text)
    # Nothing was exported, so nothing was audited as one.
    assert "budget.export" not in {e.action for e in await _audit(api_env, org)}


async def test_reports_and_exports_count_only_their_client(api_env: ApiEnv) -> None:
    """Two clients alike in everything but their client: the same category
    key, the same dates, the same amounts. A budget's variance and position,
    and a client's exports, see that client's expenses alone."""
    org = await api_env.create_org("b-reports")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    budgets: dict[UUID, str] = {}
    for client, amount in ((x, "100.00"), (y, "7.00")):
        await _budget_on(api_env, admin, client)
        category = await _category(api_env, admin, client)
        budgets[client] = await _budget(api_env, admin, client)
        await _expense(api_env, admin, client, category, amount=amount, status="confirmed",
                       description=f"spend {amount}")
    variance = await api_env.client.get(f"{B}/budgets/{budgets[x]}/variance", headers=admin)
    assert variance.status_code == 200, variance.text
    assert [row["actual"] for row in variance.json()["rows"]] == ["100.00"]
    position = await api_env.client.get(
        f"{B}/budgets/{budgets[x]}/position", params={"as_of": "2027-12-31"}, headers=admin
    )
    assert position.json()["ytd_actual"] == "100.00", position.text
    for path in ("/expenses/export", "/expenses/export/odoo"):
        r = await api_env.client.get(f"{B}{path}", params={"client_id": str(x)}, headers=admin)
        assert r.status_code == 200, r.text
        assert "spend 100.00" in r.text and "spend 7.00" not in r.text, r.text


async def test_the_feed_files_each_client_s_costs_under_that_client(api_env: ApiEnv) -> None:
    """A subscription or license of Y never feeds X's budget: X's forecast and
    X's materialization project X's records alone, and every materialized
    expense is X's. The two clients' records are alike in all but client."""
    org = await api_env.create_org("b-feed")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id(api_env, org)
    when = date.today()
    month_end = date(when.year, when.month, calendar.monthrange(when.year, when.month)[1])
    records: dict[UUID, set[str]] = {}
    for client in (x, y):
        await _budget_on(api_env, admin, client)
        records[client] = {
            str(await _subscription(api_env, org, client, actor, renews_at=when)),
            str(await _license(api_env, org, client, actor, renews_at=when)),
        }

    forecast = await api_env.client.get(
        f"{B}/forecast", params={"client_id": str(x), "months": 1}, headers=admin
    )
    assert forecast.status_code == 200, forecast.text
    assert len(forecast.json()["rows"]) == 2, forecast.json()

    r = await api_env.client.post(
        f"{B}/expenses/materialize",
        json={"client_id": str(x), "from_date": when.isoformat(),
              "to_date": month_end.isoformat()},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["created"] == 2, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        fed = (
            await s.execute(
                select(Expense.client_id, Expense.link_id, ExpenseCategory.client_id)
                .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
                .where(Expense.org_id == org)
            )
        ).all()
    assert {(c, str(link), cat) for c, link, cat in fed} == {(x, rec, x) for rec in records[x]}

    # Y's turn writes Y's, and X's materialization did not take Y's slots.
    r = await api_env.client.post(
        f"{B}/expenses/materialize",
        json={"client_id": str(y), "from_date": when.isoformat(),
              "to_date": month_end.isoformat()},
        headers=admin,
    )
    assert r.json()["created"] == 2, r.text


async def test_another_client_s_expense_never_books_this_client_s_projection(
    api_env: ApiEnv,
) -> None:
    """The forecast's "already booked" guard (`_materialized_keys`) counts the
    client's own expenses: a row of Y that names X's subscription (planted;
    the application refuses one, 422) does not mark X's renewal as booked."""
    org = await api_env.create_org("b-booked")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    actor = await _user_id(api_env, org)
    today = date.today()
    sub_x = await _subscription(api_env, org, x, actor, renews_at=today)
    y_category = await _category(api_env, admin, y, "saas")
    await _seed(api_env, Expense(
        org_id=org, client_id=y, category_id=UUID(y_category), expense_date=today,
        amount=Decimal("120.00"), currency="USD", source="subscription",
        link_type="subscription", link_id=sub_x, period_key=f"{today:%Y-%m}",
        status="expected", entered_by=actor,
    ))
    r = await api_env.client.get(
        f"{B}/forecast", params={"client_id": str(x), "months": 1}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert [row["materialized"] for row in r.json()["rows"]] == [False], r.json()


async def test_variance_of_a_client_that_turned_budget_off_writes_and_projects_nothing(
    api_env: ApiEnv,
) -> None:
    """A budget outlives its client's module being switched off. Reading its
    variance then seeds no categories (research-5 §I: reads used to) and
    projects nothing -- that client has no forecast -- while what was
    recorded still shows."""
    org = await api_env.create_org("b-var-off")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    actor = await _user_id(api_env, org)
    budget = await _budget(api_env, admin, x, year=date.today().year)
    category = await _category(api_env, admin, x)
    await _expense(api_env, admin, x, category, expense_date=date.today().isoformat(),
                   status="confirmed")
    await _subscription(api_env, org, x, actor, renews_at=date.today())
    await _budget_off(api_env, admin, x)

    r = await api_env.client.get(f"{B}/budgets/{budget}/variance", headers=admin)
    assert r.status_code == 200, r.text
    assert [(row["actual"], row["projected"]) for row in r.json()["rows"]] == [
        ("100.00", "0.00")
    ]
    async with AsyncSession(api_env.admin_engine) as s:
        seeded = (
            await s.execute(
                select(func.count()).select_from(ExpenseCategory).where(
                    ExpenseCategory.client_id == x, ExpenseCategory.system_key.is_not(None)
                )
            )
        ).scalar_one()
    assert seeded == 0


async def test_a_technician_of_one_client_sees_and_writes_that_client_alone(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("b-tech")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_rows: dict[str, str] = {}
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    y_category = await _category(api_env, admin, y)
    y_rows["budget"] = await _budget(api_env, admin, y)
    y_rows["expense"] = await _expense(api_env, admin, y, y_category)
    tech = await everything_on(api_env, org, x)

    x_category = await _category(api_env, tech, x)
    await _budget(api_env, tech, x)
    await _expense(api_env, tech, x, x_category)
    for path in ("categories", "cost-centers", "budgets", "expenses"):
        r = await api_env.client.get(f"{B}/{path}", headers=tech)
        assert r.status_code == 200, r.text
        assert {row["client_id"] for row in r.json()} <= {str(x)}, (path, r.json())
    for url in (f"{B}/budgets/{y_rows['budget']}", f"{B}/expenses/{y_rows['expense']}"):
        assert (await api_env.client.get(url, headers=tech)).status_code == 404, url
    r = await api_env.client.post(f"{B}/budgets", json={
        "client_id": str(y), "fiscal_year": 2027, "name": "n", "granularity": "month"},
        headers=tech)
    assert (r.status_code, r.json()["detail"]) == (404, NO_SUCH_CLIENT), r.text
    r = await api_env.client.get(f"{B}/settings", params={"client_id": str(y)}, headers=tech)
    assert (r.status_code, r.json()["detail"]) == (404, NO_SUCH_CLIENT), r.text


async def test_the_client_filters_keep_their_org_predicates(
    api_env: ApiEnv, bypassrls_session: AsyncSession
) -> None:
    """Lesson 2 of the Task 6 review: next to each new client predicate the
    org predicate must still bear the load. On the BYPASSRLS session nothing
    else filters, and org A asks for ORG B's internal client (its id is org
    B's): the client predicate matches org B's rows, so only the org
    predicate keeps them out."""
    org_a = await api_env.create_org("b-org-a")
    await api_env.create_user(org_a, "org-admin")
    org_b = await api_env.create_org("b-org-b")
    admin_b = await _admin(api_env, org_b)
    category = await _category(api_env, admin_b, org_b)
    await _cost_center(api_env, admin_b, org_b)
    await _budget(api_env, admin_b, org_b)
    await _expense(api_env, admin_b, org_b, category)
    actor_b = await _user_id(api_env, org_b)
    await seed_default_categories(bypassrls_session, org_b, actor_b, client_id=org_b)
    await bypassrls_session.commit()
    actor_a = await _user_id(api_env, org_a)

    session = bypassrls_session
    assert await list_categories(session, org_a, actor_a, client_id=org_b) == []
    assert await list_cost_centers(session, org_a, client_id=org_b) == []
    assert await list_budgets(session, org_a, client_id=org_b) == []
    assert await list_expenses(session, org_a, client_id=org_b) == []
    assert (
        await category_by_system_key(session, org_a, "hardware", client_id=org_b)
    ) is None
    # The positive control: org B's own view of the same filter has rows.
    assert await list_expenses(session, org_b, client_id=org_b) != []


async def test_the_database_holds_lines_and_expenses_to_their_client_s_references(
    api_env: ApiEnv,
) -> None:
    """Migration 0238's composite keys, below the application and the RLS: a
    foreign key ignores RLS, so even under scope all (here the BYPASSRLS
    admin engine) a line of X's budget or an expense of X cannot name Y's
    category or cost centre."""
    org = await api_env.create_org("b-fk")
    await api_env.create_user(org, "org-admin")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    [budget_x, category_x, category_y, center_y] = await _seed(
        api_env,
        Budget(org_id=org, client_id=x, fiscal_year=2027, name="TI", granularity="month",
               created_by=actor),
        ExpenseCategory(org_id=org, client_id=x, key="hw", name="hw", created_by=actor),
        ExpenseCategory(org_id=org, client_id=y, key="hw", name="hw", created_by=actor),
        CostCenter(org_id=org, client_id=y, code="IT", name="IT", created_by=actor),
    )
    refused = [
        BudgetLine(org_id=org, budget_id=budget_x, category_id=category_y, period_index=1,
                   planned_amount=1, created_by=actor),
        BudgetLine(org_id=org, budget_id=budget_x, category_id=category_x,
                   cost_center_id=center_y, period_index=1, planned_amount=1,
                   created_by=actor),
        Expense(org_id=org, client_id=x, category_id=category_y, expense_date=date(2027, 1, 1),
                amount=1, currency="USD", entered_by=actor),
        Expense(org_id=org, client_id=x, category_id=category_x, cost_center_id=center_y,
                expense_date=date(2027, 1, 1), amount=1, currency="USD", entered_by=actor),
    ]
    for row in refused:
        with pytest.raises(
            IntegrityError, match=r"fk_(budget_line|expense)_(category|cost_center)_client"
        ):
            await _seed(api_env, row)
    # The positive control: X's own category, no cost centre.
    await _seed(api_env, BudgetLine(org_id=org, budget_id=budget_x, category_id=category_x,
                                    period_index=1, planned_amount=1, created_by=actor))


# --------------------------------------------------------------------------- #
# Settings and FX
# --------------------------------------------------------------------------- #


async def test_a_settings_change_that_changes_nothing_writes_nothing(api_env: ApiEnv) -> None:
    """A client's settings row is made by the first change to it, and that
    change is audited. A PUT that names no field changes nothing: it answers
    the defaults and writes no row -- an unaudited write would be worse."""
    org = await api_env.create_org("b-noop")
    admin = await _admin(api_env, org)
    r = await api_env.client.put(f"{B}/settings", params={"client_id": str(org)}, json={},
                                 headers=admin)
    assert r.status_code == 200 and r.json()["updated_at"] is None, r.text
    assert (await _rows(api_env, org))["budget_settings"] == 0


async def test_an_fx_rate_goes_to_the_internal_base_currency_unless_named(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("b-fx")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    for client, currency in ((org, "EUR"), (x, "COP")):
        r = await api_env.client.put(f"{B}/settings", params={"client_id": str(client)},
                                     json={"base_currency": currency}, headers=admin)
        assert r.status_code == 200, r.text
    implied = await api_env.client.post(f"{B}/fx-rates", json={
        "from_currency": "USD", "rate": "0.9", "effective_date": "2027-01-01"}, headers=admin)
    named = await api_env.client.post(f"{B}/fx-rates", json={
        "from_currency": "USD", "to_currency": "COP", "rate": "4000",
        "effective_date": "2027-01-01"}, headers=admin)
    assert implied.status_code == 201 and implied.json()["to_currency"] == "EUR", implied.text
    assert named.status_code == 201 and named.json()["to_currency"] == "COP", named.text
    # X converts with the COP rate, into its own base currency.
    category = await _category(api_env, admin, x)
    r = await api_env.client.post(f"{B}/expenses", json=_expense_body(x, category),
                                  headers=admin)
    assert (r.json()["fx_rate"], r.json()["amount_base"]) == ("4000.00000000", "400000.00")


async def test_a_base_currency_change_is_held_back_by_that_client_s_expenses_alone(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("b-guard")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _budget_on(api_env, admin, client)
    await _expense(api_env, admin, y, await _category(api_env, admin, y))  # converted, USD
    r = await api_env.client.put(f"{B}/settings", params={"client_id": str(x)},
                                 json={"base_currency": "EUR"}, headers=admin)
    assert r.status_code == 200, r.text
    r = await api_env.client.put(f"{B}/settings", params={"client_id": str(y)},
                                 json={"base_currency": "EUR"}, headers=admin)
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------- #
# Audit and locks
# --------------------------------------------------------------------------- #


async def test_every_budget_audit_event_carries_its_client(api_env: ApiEnv) -> None:
    """Every audited budget action, on client X: each event names X. The FX
    rates are the organisation's and name none."""
    org = await api_env.create_org("b-audit")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    actor = await _user_id(api_env, org)
    await _subscription(api_env, org, x, actor, renews_at=date(2027, 1, 5))

    r = await api_env.client.put(f"{B}/settings", params={"client_id": str(x)},
                                 json={"base_currency": "USD", "fiscal_year_start_month": 1},
                                 headers=admin)
    assert r.status_code == 200, r.text
    await api_env.client.get(f"{B}/categories", params={"client_id": str(x)}, headers=admin)
    category = await _category(api_env, admin, x)
    await api_env.client.patch(f"{B}/categories/{category}", json={"name": "n"}, headers=admin)
    center = await _cost_center(api_env, admin, x)
    await api_env.client.patch(f"{B}/cost-centers/{center}", json={"name": "n"}, headers=admin)
    budget = await _budget(api_env, admin, x)
    await api_env.client.patch(f"{B}/budgets/{budget}", json={"name": "m"}, headers=admin)
    lines = (await api_env.client.put(
        f"{B}/budgets/{budget}/lines",
        json={"lines": [{"category_id": category, "planned_amount": "1.00", "period_index": 1}]},
        headers=admin)).json()
    await api_env.client.delete(f"{B}/budgets/{budget}/lines/{lines[0]['id']}", headers=admin)
    await api_env.client.post(f"{B}/budgets/{budget}/close", headers=admin)
    expense = await _expense(api_env, admin, x, category)
    await api_env.client.patch(f"{B}/expenses/{expense}", json={"description": "e"},
                               headers=admin)
    await api_env.client.post(f"{B}/expenses/{expense}/confirm", json={}, headers=admin)
    other = await _expense(api_env, admin, x, category)
    await api_env.client.post(f"{B}/expenses/{other}/void", headers=admin)
    r = await api_env.client.post(
        f"{B}/expenses/materialize",
        json={"client_id": str(x), "from_date": "2027-01-01", "to_date": "2027-01-31"},
        headers=admin,
    )
    assert r.json()["created"] == 1, r.text
    for path in ("/expenses/export", "/expenses/export/odoo"):
        r = await api_env.client.get(f"{B}{path}", params={"client_id": str(x)}, headers=admin)
        assert r.status_code == 200, r.text
    await api_env.client.post(f"{B}/fx-rates", json={
        "from_currency": "EUR", "rate": "1.1", "effective_date": "2027-01-01"}, headers=admin)

    budget_actions = {
        "budget.base_currency_changed", "budget.fiscal_year_start_changed",
        "budget.categories_seeded", "budget.category_created", "budget.category_updated",
        "budget.cost_center_created", "budget.cost_center_updated", "budget.created",
        "budget.updated", "budget.lines_updated", "budget.line_deleted", "budget.closed",
        "expense.created", "expense.updated", "expense.confirmed", "expense.voided",
        "budget.expenses_materialized", "budget.export",
    }
    events = [e for e in await _audit(api_env, org)
              if e.action.startswith(("budget.", "expense."))]
    assert budget_actions <= {e.action for e in events}
    wrong = sorted(
        (e.action, e.client_id) for e in events
        if e.action not in {"budget.fx_rate_created", "budget.fx_rate_updated"}
        and e.client_id != x
    )
    assert not wrong, wrong
    assert {e.client_id for e in events if e.action.startswith("budget.fx_rate")} == {None}


async def test_the_attachment_audits_carry_their_client(api_env: ApiEnv) -> None:
    """Upload and download, each audited with the expense's client."""
    from moto import mock_aws

    from app.core.storage import ObjectStore

    org = await api_env.create_org("b-attach")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    expense = await _expense(api_env, admin, x, await _category(api_env, admin, x))
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        r = await api_env.client.post(f"{B}/expenses/{expense}/attachment?filename=a.pdf",
                                      content=b"%PDF-1.4", headers=admin)
        assert r.status_code == 200, r.text
        r = await api_env.client.get(f"{B}/expenses/{expense}/attachment", headers=admin)
        assert r.status_code == 200, r.text
    events = {e.action: e.client_id for e in await _audit(api_env, org)
              if e.action.startswith("expense.attachment")}
    assert events == {"expense.attachment_uploaded": x, "expense.attachment_downloaded": x}


async def test_a_budget_write_takes_the_client_before_the_audit_chain(api_env: ApiEnv) -> None:
    """The module guard reads the client FOR SHARE before anything is written
    (Task 7): with an archive holding the client, an edit waits BEFORE the
    audit lock, the archive takes that lock and commits, and the edit then
    goes through -- no deadlock."""
    org = await api_env.create_org("b-lock")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _budget_on(api_env, admin, x)
    budget = await _budget(api_env, admin, x)

    async with AsyncSession(api_env.admin_engine) as archive, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await archive.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await archive.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        edit = asyncio.create_task(
            api_env.client.patch(f"{B}/budgets/{budget}", json={"name": "m"}, headers=admin)
        )
        for _ in range(400):
            await asyncio.sleep(0.025)
            waiting = (
                await observer.execute(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted AND pid <> :me"),
                    {"me": me},
                )
            ).scalar_one()
            await observer.rollback()
            if waiting:
                break
        else:
            edit.cancel()
            pytest.fail("the edit never waited on the archive's client lock")
        await archive.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await archive.commit()
    r = await edit
    assert r.status_code == 200, r.text

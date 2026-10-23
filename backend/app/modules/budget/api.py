"""Budget REST API (docs/modules/budget/spec.md §9, §8, §5, §7).

Routes are all `/budget`-prefixed; ``app.modules.budget.register`` mounts this
router under ``/api/v1``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

Like ``licenses``/``subscriptions``/``bcp``/``change_mgmt``, this module
dispatches no agent jobs -- ``register(app)`` mounts this router only, no job
type registration and no gateway edit (spec §10: control-plane only, no
worker).

Every ``BudgetValidationError`` raised by the service layer is translated to
an HTTP 4xx here -- it must never surface as a 500, and the translation is by
TYPE, not by matching exception-message text. ``BudgetNotFoundError`` (a
``BudgetValidationError`` subclass, raised for a missing/cross-org budget,
category, cost center, FX rate, or expense) becomes a 404; every other
``BudgetValidationError`` -- an invalid granularity/period_index, a
cross-org/nonexistent reference, a closed-budget edit, an FX overflow from
``fx.convert``, a materialization/report window that names a missing
``system_key`` category, an over-long materialization window, or a
currency-mismatched merge -- becomes a 400.

**No double-auditing (invariant #4).** The service layer already audits every
mutation it performs (budget/line/category/cost-center/FX-rate/settings
writes; expense create/update/confirm/void; materialization). This file adds
exactly two audited READS that the service layer has no way to audit itself,
because they are pure route-level concerns: both expense exports
(``budget.export``) and attachment download (``expense.attachment_downloaded``).
Attachment upload is a mutation with no dedicated service function (there is
no object-storage-aware service module for it), so this file audits it
directly (``expense.attachment_uploaded``) -- the same "the mutation audits
itself, once" rule, just performed here instead of one layer down. Every
other read -- list, detail, variance, position, forecast, the confirmation
queue -- writes nothing (the Phase-9 no-audit-on-view lesson).

**Route-ordering caution (the Phase-11/12 lesson).** ``/budget/expenses/export``,
``/budget/expenses/export/odoo``, and ``/budget/expenses/materialize`` are
declared before ``GET|PATCH /budget/expenses/{expense_id}``, so FastAPI never
tries to parse one of those literal segments as an expense id.
``/budget/forecast`` is declared early, before the budget/expense ``{id}``
sections, per the same discipline.

RBAC (spec §8, §9): reads -> ``budget.read``; budget/category/cost-center/
FX/settings writes -> ``budget.manage``; expense create/edit + materialize
(and attachment upload, which is part of editing an expense) ->
``expense.record``; confirm/void -> ``expense.confirm``; attachment download
is explicitly ``budget.read``-gated per spec §5/§8. Technician holds none of
the four permissions (spec §0.5) -- the module-defining assertion, tested by
iterating every route in ``tests/test_budget_api.py``.
"""

import csv
import io
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AuthContext, require_permission
from app.core.attachments import sanitize_attachment_filename as _sanitize_attachment_filename
from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import (
    Budget,
    BudgetLine,
    BudgetSettings,
    CostCenter,
    Expense,
    ExpenseCategory,
    FxRate,
)
from app.modules.budget.budgets import (
    close_budget,
    create_budget,
    delete_line,
    get_budget,
    list_budgets,
    put_lines,
    update_budget,
)
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.expenses import (
    confirm_expense,
    create_expense,
    get_expense,
    list_expenses,
    reject_if_void,
    update_expense,
    void_expense,
)
from app.modules.budget.feeds import materialize
from app.modules.budget.fx import get_settings
from app.modules.budget.rates import (
    create_rate,
    list_rates,
    set_base_currency,
    set_fiscal_year_start,
    update_rate,
)
from app.modules.budget.refdata import (
    create_category,
    create_cost_center,
    list_categories,
    list_cost_centers,
    update_category,
    update_cost_center,
)
from app.modules.budget.reporting import export_rows, forecast, odoo_export_rows, position, variance
from app.modules.budget.schema import (
    BudgetCreate,
    BudgetLineOut,
    BudgetLinesPut,
    BudgetOut,
    BudgetUpdate,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    CostCenterCreate,
    CostCenterOut,
    CostCenterUpdate,
    ExpenseConfirm,
    ExpenseCreate,
    ExpenseOut,
    ExpenseUpdate,
    ForecastRow,
    FxRateCreate,
    FxRateOut,
    FxRateUpdate,
    MaterializeRequest,
    PositionOut,
    SettingsOut,
    SettingsUpdate,
    VarianceRow,
)

router = APIRouter(tags=["budget"])


def _http_error(exc: BudgetValidationError) -> HTTPException:
    """Translate a `BudgetValidationError` to an HTTP 4xx -- it must never
    become a 500.

    Routing is by TYPE, never by matching exception-message prose.
    `BudgetNotFoundError` -- raised for "the resource named in the URL path
    doesn't exist under this org" (a budget, category, cost center, FX rate,
    or expense) -- becomes a 404. Every other `BudgetValidationError` is a
    400 -- including an invalid granularity/period_index, a
    cross-org/nonexistent reference, a closed-budget edit, an FX overflow, a
    missing `system_key` category, an over-long materialization/forecast
    window, and a currency-mismatched merge.
    """
    if isinstance(exc, BudgetNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use -- mirrors
    `app.modules.backup.api._object_store`/`app.modules.remote.api._object_store`.
    Construction hits the object store to ensure the bucket exists, so it
    stays out of app startup; attachment upload/download is the only
    budget-module path that needs it."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


# --------------------------------------------------------------------------- #
# Response-out builders -- no `org_id` field anywhere (invariant #5).
# --------------------------------------------------------------------------- #


def _settings_out(row: BudgetSettings) -> SettingsOut:
    return SettingsOut(
        base_currency=row.base_currency,
        fiscal_year_start_month=row.fiscal_year_start_month,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _category_out(row: ExpenseCategory) -> CategoryOut:
    return CategoryOut(
        id=row.id,
        key=row.key,
        name=row.name,
        system_key=row.system_key,
        active=row.active,
        sort_order=row.sort_order,
        created_at=row.created_at,
    )


def _cost_center_out(row: CostCenter) -> CostCenterOut:
    return CostCenterOut(
        id=row.id,
        code=row.code,
        name=row.name,
        external_ref=row.external_ref,
        active=row.active,
        created_at=row.created_at,
    )


def _fx_rate_out(row: FxRate) -> FxRateOut:
    return FxRateOut(
        id=row.id,
        from_currency=row.from_currency,
        to_currency=row.to_currency,
        rate=row.rate,
        effective_date=row.effective_date,
        created_at=row.created_at,
    )


def _budget_out(row: Budget) -> BudgetOut:
    return BudgetOut(
        id=row.id,
        fiscal_year=row.fiscal_year,
        name=row.name,
        granularity=row.granularity,
        status=row.status,
        created_at=row.created_at,
    )


def _budget_line_out(row: BudgetLine) -> BudgetLineOut:
    return BudgetLineOut(
        id=row.id,
        budget_id=row.budget_id,
        category_id=row.category_id,
        cost_center_id=row.cost_center_id,
        period_index=row.period_index,
        planned_amount=row.planned_amount,
        created_at=row.created_at,
    )


def _expense_out(row: Expense) -> ExpenseOut:
    return ExpenseOut(
        id=row.id,
        expense_date=row.expense_date,
        amount=row.amount,
        currency=row.currency,
        fx_rate=row.fx_rate,
        amount_base=row.amount_base,
        vendor_id=row.vendor_id,
        category_id=row.category_id,
        cost_center_id=row.cost_center_id,
        description=row.description,
        source=row.source,
        link_type=row.link_type,
        link_id=row.link_id,
        period_key=row.period_key,
        invoice_ref=row.invoice_ref,
        # P5: the raw object-storage URI is never returned to a caller --
        # only whether an attachment exists and its sanitized display name.
        has_attachment=row.invoice_object_key is not None,
        invoice_filename=row.invoice_filename,
        status=row.status,
        entered_by=row.entered_by,
        created_at=row.created_at,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        voided_by=row.voided_by,
        voided_at=row.voided_at,
    )


# --------------------------------------------------------------------------- #
# Local response wrappers for the reporting/materialize routes -- these are
# route-level shapes, not service-layer contracts, so they live here rather
# than in schema.py (mirrors `app.modules.patch.api.ScriptRunResponse`-style
# locally-defined response models).
# --------------------------------------------------------------------------- #


class VarianceOut(BaseModel):
    rows: list[VarianceRow]
    unconvertible_count: int
    unconvertible_by_currency: dict[str, str]


class ForecastOut(BaseModel):
    rows: list[ForecastRow]
    unconvertible_count: int
    unconvertible_by_currency: dict[str, str]


class MaterializeOut(BaseModel):
    created: int
    skipped_existing: int
    skipped_unconvertible: int


# CSV column orders -- pinned exactly to `reporting.export_rows`/
# `reporting.odoo_export_rows`'s dict key order. The Odoo order is the spec
# §7 verbatim contract; changing either list changes the wire contract.
_EXPORT_COLUMNS: list[str] = [
    "id", "expense_date", "amount", "currency", "fx_rate", "amount_base",
    "vendor_id", "vendor", "category_id", "category", "cost_center_id",
    "cost_center", "description", "source", "link_type", "link_id",
    "period_key", "invoice_ref", "status", "entered_by", "created_at",
    "confirmed_by", "confirmed_at",
]
_ODOO_EXPORT_COLUMNS: list[str] = [
    "date", "reference", "vendor", "category", "cost_center", "description",
    "amount", "currency", "amount_base", "base_currency", "link_type", "link_id",
]

# CSV formula-injection escaping (`app.core.csv_export.escape_formula`) is
# applied PER COLUMN, never per row. Only a column that can actually carry
# operator-supplied free text is a candidate for a formula payload -- a
# vendor/category/cost-center NAME, a free-text `description`, or an
# `invoice_ref` the operator typed in. Every other column here (`id`,
# `expense_date`/`date`, `amount`/`amount_base`, `currency`/`base_currency`,
# every `*_id` column, `link_type`, `status`, `source`, `entered_by`/
# `confirmed_by`, every `*_at` timestamp, `period_key`) is a Decimal/date/
# UUID/fixed-enum value the SERVER itself formatted -- never operator text --
# so it cannot carry a formula in the first place, and escaping it would only
# corrupt it for a machine reader.
#
# This distinction is load-bearing for the Odoo export specifically:
# `deploy/README.md` tells operators to point an ERP import job at
# `GET /budget/expenses/export/odoo`, and spec §7 pins that column contract.
# An ERP importer is a plain-text/machine CSV reader, not Excel -- it has no
# concept of "a leading `'` means force-text", so escaping `amount`/
# `amount_base` would hand it the literal string `'-100.00` for every credit
# note or negative figure: an import failure at best, a silently wrong
# imported figure at worst. Leaving those columns unescaped is correct
# precisely because a `Decimal` the server stringified can never begin with
# `=`, `+`, `-`, or `@` for any reason other than "the amount is genuinely
# negative" -- there is no injection surface there to guard.
_EXPORT_ESCAPED_COLUMNS: frozenset[str] = frozenset(
    {"vendor", "category", "cost_center", "description", "invoice_ref"}
)
_ODOO_EXPORT_ESCAPED_COLUMNS: frozenset[str] = frozenset(
    {"reference", "vendor", "category", "cost_center", "description"}
)


def _csv_response(
    rows: list[dict[str, Any]], columns: list[str], escaped_columns: frozenset[str]
) -> PlainTextResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(
            [
                ""
                if row.get(col) is None
                else (escape_formula(row[col]) if col in escaped_columns else row[col])
                for col in columns
            ]
        )
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv")


# --------------------------------------------------------------------------- #
# GET /budget/forecast -- static, declared early (before any budget/expense
# `{id}`-shaped route), spec §6.2/§9's route-ordering discipline.
# --------------------------------------------------------------------------- #


@router.get("/budget/forecast", response_model=ForecastOut)
async def forecast_route(
    request: Request,
    months: int = Query(default=12, ge=1, le=60),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> dict[str, Any]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            return await forecast(session, ctx.org_id, ctx.user_id, months=months)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/PUT /budget/settings
# --------------------------------------------------------------------------- #


@router.get("/budget/settings", response_model=SettingsOut)
async def get_settings_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> SettingsOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        settings = await get_settings(session, ctx.org_id)
        return _settings_out(settings)


@router.put("/budget/settings", response_model=SettingsOut)
async def update_settings_route(
    body: SettingsUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> SettingsOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            if body.base_currency is not None:
                await set_base_currency(
                    session,
                    ctx.org_id,
                    ctx.user_id,
                    base_currency=body.base_currency,
                    acknowledge_mixed_basis=body.acknowledge_mixed_basis,
                )
            if body.fiscal_year_start_month is not None:
                await set_fiscal_year_start(
                    session, ctx.org_id, ctx.user_id, start_month=body.fiscal_year_start_month
                )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        settings = await get_settings(session, ctx.org_id)
        return _settings_out(settings)


# --------------------------------------------------------------------------- #
# GET/POST /budget/categories, PATCH /budget/categories/{category_id}
# --------------------------------------------------------------------------- #


@router.get("/budget/categories", response_model=list[CategoryOut])
async def list_categories_route(
    request: Request,
    include_inactive: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[CategoryOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_categories(
            session, ctx.org_id, ctx.user_id, include_inactive=include_inactive
        )
        return [_category_out(r) for r in rows]


@router.post(
    "/budget/categories", status_code=status.HTTP_201_CREATED, response_model=CategoryOut
)
async def create_category_route(
    body: CategoryCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> CategoryOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            cat = await create_category(
                session, ctx.org_id, ctx.user_id, key=body.key, name=body.name
            )
            # `create_category` has no `sort_order` parameter (only
            # `seed_default_categories` sets fields beyond key/name at
            # creation) -- a non-default `sort_order` is applied as an
            # immediate follow-up edit through the same editable-field path
            # `PATCH /budget/categories/{id}` uses, rather than duplicating
            # that logic here.
            if body.sort_order != 0:
                cat = await update_category(
                    session, ctx.org_id, ctx.user_id, cat.id, {"sort_order": body.sort_order}
                )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _category_out(cat)


@router.patch("/budget/categories/{category_id}", response_model=CategoryOut)
async def update_category_route(
    category_id: UUID,
    body: CategoryUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> CategoryOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            cat = await update_category(
                session,
                ctx.org_id,
                ctx.user_id,
                category_id,
                body.model_dump(exclude_unset=True),
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _category_out(cat)


# --------------------------------------------------------------------------- #
# GET/POST /budget/cost-centers, PATCH /budget/cost-centers/{cost_center_id}
# --------------------------------------------------------------------------- #


@router.get("/budget/cost-centers", response_model=list[CostCenterOut])
async def list_cost_centers_route(
    request: Request,
    include_inactive: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[CostCenterOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_cost_centers(session, ctx.org_id, include_inactive=include_inactive)
        return [_cost_center_out(r) for r in rows]


@router.post(
    "/budget/cost-centers", status_code=status.HTTP_201_CREATED, response_model=CostCenterOut
)
async def create_cost_center_route(
    body: CostCenterCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> CostCenterOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            cc = await create_cost_center(
                session,
                ctx.org_id,
                ctx.user_id,
                code=body.code,
                name=body.name,
                external_ref=body.external_ref,
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _cost_center_out(cc)


@router.patch("/budget/cost-centers/{cost_center_id}", response_model=CostCenterOut)
async def update_cost_center_route(
    cost_center_id: UUID,
    body: CostCenterUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> CostCenterOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            cc = await update_cost_center(
                session,
                ctx.org_id,
                ctx.user_id,
                cost_center_id,
                body.model_dump(exclude_unset=True),
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _cost_center_out(cc)


# --------------------------------------------------------------------------- #
# GET/POST /budget/fx-rates, PATCH /budget/fx-rates/{fx_rate_id}
# --------------------------------------------------------------------------- #


@router.get("/budget/fx-rates", response_model=list[FxRateOut])
async def list_fx_rates_route(
    request: Request,
    from_currency: str | None = Query(default=None, min_length=3, max_length=3),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[FxRateOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_rates(session, ctx.org_id, from_currency=from_currency)
        return [_fx_rate_out(r) for r in rows]


@router.post(
    "/budget/fx-rates", status_code=status.HTTP_201_CREATED, response_model=FxRateOut
)
async def create_fx_rate_route(
    body: FxRateCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> FxRateOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            fx = await create_rate(
                session,
                ctx.org_id,
                ctx.user_id,
                from_currency=body.from_currency,
                rate=body.rate,
                effective_date=body.effective_date,
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _fx_rate_out(fx)


@router.patch("/budget/fx-rates/{fx_rate_id}", response_model=FxRateOut)
async def update_fx_rate_route(
    fx_rate_id: UUID,
    body: FxRateUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> FxRateOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            fx = await update_rate(
                session, ctx.org_id, ctx.user_id, fx_rate_id, body.model_dump(exclude_unset=True)
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _fx_rate_out(fx)


# --------------------------------------------------------------------------- #
# GET/POST /budget/budgets, GET/PATCH /budget/budgets/{budget_id},
# POST /budget/budgets/{budget_id}/close
# --------------------------------------------------------------------------- #


@router.get("/budget/budgets", response_model=list[BudgetOut])
async def list_budgets_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[BudgetOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_budgets(session, ctx.org_id)
        return [_budget_out(r) for r in rows]


@router.post("/budget/budgets", status_code=status.HTTP_201_CREATED, response_model=BudgetOut)
async def create_budget_route(
    body: BudgetCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> BudgetOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            budget = await create_budget(
                session,
                ctx.org_id,
                ctx.user_id,
                fiscal_year=body.fiscal_year,
                name=body.name,
                granularity=body.granularity,
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _budget_out(budget)


@router.get("/budget/budgets/{budget_id}", response_model=BudgetOut)
async def get_budget_route(
    budget_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> BudgetOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            budget = await get_budget(session, ctx.org_id, budget_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _budget_out(budget)


@router.patch("/budget/budgets/{budget_id}", response_model=BudgetOut)
async def update_budget_route(
    budget_id: UUID,
    body: BudgetUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> BudgetOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            budget = await update_budget(
                session, ctx.org_id, ctx.user_id, budget_id, body.model_dump(exclude_unset=True)
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _budget_out(budget)


@router.post("/budget/budgets/{budget_id}/close", response_model=BudgetOut)
async def close_budget_route(
    budget_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> BudgetOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            budget = await close_budget(session, ctx.org_id, ctx.user_id, budget_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _budget_out(budget)


# --------------------------------------------------------------------------- #
# GET/PUT /budget/budgets/{budget_id}/lines,
# DELETE /budget/budgets/{budget_id}/lines/{line_id}
# --------------------------------------------------------------------------- #


@router.get("/budget/budgets/{budget_id}/lines", response_model=list[BudgetLineOut])
async def list_lines_route(
    budget_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[BudgetLineOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await get_budget(session, ctx.org_id, budget_id)  # 404 if cross-org/missing
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        rows = (
            (
                await session.execute(
                    select(BudgetLine)
                    .where(BudgetLine.org_id == ctx.org_id, BudgetLine.budget_id == budget_id)
                    .order_by(BudgetLine.period_index, BudgetLine.category_id)
                )
            )
            .scalars()
            .all()
        )
        return [_budget_line_out(r) for r in rows]


@router.put("/budget/budgets/{budget_id}/lines", response_model=list[BudgetLineOut])
async def put_lines_route(
    budget_id: UUID,
    body: BudgetLinesPut,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> list[BudgetLineOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            lines = await put_lines(session, ctx.org_id, ctx.user_id, budget_id, body.lines)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return [_budget_line_out(r) for r in lines]


@router.delete(
    "/budget/budgets/{budget_id}/lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_line_route(
    budget_id: UUID,
    line_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_line(session, ctx.org_id, ctx.user_id, budget_id, line_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /budget/budgets/{budget_id}/variance, /position
# --------------------------------------------------------------------------- #


@router.get("/budget/budgets/{budget_id}/variance", response_model=VarianceOut)
async def variance_route(
    budget_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> dict[str, Any]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            return await variance(session, ctx.org_id, budget_id, ctx.user_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None


@router.get("/budget/budgets/{budget_id}/position", response_model=PositionOut)
async def position_route(
    budget_id: UUID,
    request: Request,
    as_of: date | None = Query(default=None),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> dict[str, Any]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            return await position(session, ctx.org_id, budget_id, as_of=as_of or date.today())
        except BudgetValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /budget/expenses/{export,export/odoo}, POST /budget/expenses/materialize
# -- static, declared BEFORE GET|PATCH /budget/expenses/{expense_id} (the
# Phase-11/12 route-ordering lesson).
# --------------------------------------------------------------------------- #


@router.get("/budget/expenses/export")
async def export_expenses_route(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> PlainTextResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await export_rows(session, ctx.org_id, date_from=date_from, date_to=date_to)

        # An explicit, audited export (invariant #4) -- one of this module's
        # two route-level audited reads; plain list/detail/variance/position/
        # forecast/confirmation-queue reads write nothing (the Phase-9
        # no-audit-on-view lesson).
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="budget.export",
            target_type="expense_export",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows), "format": "csv"},
        )

        return _csv_response(rows, _EXPORT_COLUMNS, _EXPORT_ESCAPED_COLUMNS)


@router.get("/budget/expenses/export/odoo")
async def export_expenses_odoo_route(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    # I2 fix: defaults to CONFIRMED rows only -- this is the export the
    # README tells you to point an ERP import job at, and the pinned Odoo
    # contract has no `status` column, so an unconfirmed `expected` row
    # would book into Odoo indistinguishable from a real, asserted cost.
    # Explicit opt-in for a caller that genuinely wants everything.
    include_unconfirmed: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> PlainTextResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await odoo_export_rows(
            session,
            ctx.org_id,
            date_from=date_from,
            date_to=date_to,
            include_unconfirmed=include_unconfirmed,
        )

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="budget.export",
            target_type="expense_export",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows), "format": "odoo_csv"},
        )

        return _csv_response(rows, _ODOO_EXPORT_COLUMNS, _ODOO_EXPORT_ESCAPED_COLUMNS)


@router.post("/budget/expenses/materialize", response_model=MaterializeOut)
async def materialize_route(
    body: MaterializeRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("expense.record")),
) -> dict[str, int]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            return await materialize(
                session, ctx.org_id, ctx.user_id, from_date=body.from_date, to_date=body.to_date
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/POST /budget/expenses, GET/PATCH /budget/expenses/{expense_id},
# POST /budget/expenses/{expense_id}/confirm, /void
# --------------------------------------------------------------------------- #


@router.get("/budget/expenses", response_model=list[ExpenseOut])
async def list_expenses_route(
    request: Request,
    status_: str | None = Query(default=None, alias="status"),
    category_id: UUID | None = Query(default=None),
    cost_center_id: UUID | None = Query(default=None),
    vendor_id: UUID | None = Query(default=None),
    source: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> list[ExpenseOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_expenses(
            session,
            ctx.org_id,
            status=status_,
            category_id=category_id,
            cost_center_id=cost_center_id,
            vendor_id=vendor_id,
            source=source,
            date_from=date_from,
            date_to=date_to,
        )
        return [_expense_out(r) for r in rows]


@router.post(
    "/budget/expenses", status_code=status.HTTP_201_CREATED, response_model=ExpenseOut
)
async def create_expense_route(
    body: ExpenseCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("expense.record")),
) -> ExpenseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await create_expense(session, ctx.org_id, ctx.user_id, body)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _expense_out(expense)


@router.get("/budget/expenses/{expense_id}", response_model=ExpenseOut)
async def get_expense_route(
    expense_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> ExpenseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await get_expense(session, ctx.org_id, expense_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _expense_out(expense)


@router.patch("/budget/expenses/{expense_id}", response_model=ExpenseOut)
async def update_expense_route(
    expense_id: UUID,
    body: ExpenseUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("expense.record")),
) -> ExpenseOut:
    # `exclude_unset=True` is load-bearing, not a micro-optimization: without
    # it every optional key is present with a `None` default, so "field
    # omitted" and "field explicitly set to null" are indistinguishable.
    # `update_expense` relies on key PRESENCE to decide what to touch, and
    # rejects an explicit `null` on a non-nullable column with a typed 400.
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await update_expense(
                session,
                ctx.org_id,
                ctx.user_id,
                expense_id,
                body.model_dump(exclude_unset=True),
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _expense_out(expense)


@router.post("/budget/expenses/{expense_id}/confirm", response_model=ExpenseOut)
async def confirm_expense_route(
    expense_id: UUID,
    body: ExpenseConfirm,
    request: Request,
    ctx: AuthContext = Depends(require_permission("expense.confirm")),
) -> ExpenseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await confirm_expense(
                session, ctx.org_id, ctx.user_id, expense_id, amount=body.amount
            )
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _expense_out(expense)


@router.post("/budget/expenses/{expense_id}/void", response_model=ExpenseOut)
async def void_expense_route(
    expense_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("expense.confirm")),
) -> ExpenseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await void_expense(session, ctx.org_id, ctx.user_id, expense_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        return _expense_out(expense)


# --------------------------------------------------------------------------- #
# POST/GET /budget/expenses/{expense_id}/attachment (spec §5, §8).
#
# No multipart form dependency is installed in this project, so upload takes
# the raw request body as bytes (mirrors no established precedent in this
# codebase for HTTP file upload; download mirrors
# `app.modules.remote.api.get_recording`'s `Response(content=..., media_type=
# "application/octet-stream")` pattern exactly). The object key is stored on
# `expense.invoice_object_key`; attachment bytes are never returned inline in
# a list response (spec §5) -- only this dedicated download route serves them.
#
# P1 of the post-review controller fix (.git/sdd/task-12-report.md): the
# client-supplied `filename` NEVER reaches the object-storage key (that key is
# derived entirely server-side, mirroring
# `app.modules.deploy.api.upload_package_artifact`) and never reaches a
# response header unsanitized -- it is sanitized once, on upload, into
# `expense.invoice_filename` (migration 0021), and every later header is built
# from that stored, already-sanitized value.
# --------------------------------------------------------------------------- #

# P2: an ample cap for an invoice -- any `expense.record` holder could
# otherwise OOM the control plane with an unbounded request body. Enforced by
# `app.core.http.reject_oversized_content_length` (fast-path, declared
# `Content-Length`) AND `read_bounded_body` (the actual bytes read off the
# wire, which a chunked request or a lying header cannot evade) -- both
# helpers are shared with `app.modules.deploy.api.upload_package_artifact`,
# which needs the identical protection at a much larger cap (installers, not
# invoices).
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB

# P1(b): the sanitizer's actual logic (the ASCII-only allow-list, the
# `fullmatch` anchoring, and the reasons both are load-bearing) now lives in
# `app.core.attachments` -- EXTRACTED, not copied, once Service Desk needed
# the identical hardening for `ticket_attachment`
# (docs/modules/service-desk/spec.md §2). This wrapper only translates the
# shared function's plain `ValueError` into this module's own
# `BudgetValidationError`, so every existing `except BudgetValidationError`
# call site (and this module's route-level 400 translation) is unaffected by
# the move.
def _sanitize_invoice_filename(filename: str) -> str:
    """Sanitize the client-supplied display filename BEFORE it ever reaches
    the object-storage key or a response header (P1) -- see
    `app.core.attachments.sanitize_attachment_filename` for the full
    reasoning (reject outright rather than store-and-hope, `os.path.
    basename` defence in depth, the ASCII-only `fullmatch` allow-list, the
    255-char cap). Every rejection here is a typed `BudgetValidationError`,
    which the route translates to a 400."""
    try:
        return _sanitize_attachment_filename(filename)
    except ValueError as exc:
        raise BudgetValidationError(str(exc)) from exc


@router.post("/budget/expenses/{expense_id}/attachment", response_model=ExpenseOut)
async def upload_attachment_route(
    expense_id: UUID,
    request: Request,
    filename: str = Query(max_length=4096),
    ctx: AuthContext = Depends(require_permission("expense.record")),
) -> ExpenseOut:
    # Cheapest checks first, before any DB/session/object-store work.
    reject_oversized_content_length(request, _MAX_ATTACHMENT_BYTES)
    try:
        invoice_filename = _sanitize_invoice_filename(filename)
    except BudgetValidationError as exc:
        raise _http_error(exc) from None

    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await get_expense(session, ctx.org_id, expense_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None

        # P4: a voided expense is terminal -- reuse the same guard
        # `update_expense` uses rather than duplicating the condition.
        try:
            reject_if_void(expense)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None

        data = await read_bounded_body(request, _MAX_ATTACHMENT_BYTES)

        store = _object_store(request)
        # A replacing upload is a plain overwrite: the key is a fixed
        # per-expense slot (P1(a)), so `put_object` replaces in place
        # atomically and no orphan can exist. Deleting first would be strictly
        # worse -- it opens a window where a failing put leaves
        # `invoice_object_key` pointing at an object that no longer exists
        # (the session rolls back, the object-store delete does not), turning
        # a recoverable "replace failed, old file intact" into "old file
        # destroyed, every download 500s with NoSuchKey".
        ref = await store.put_budget_attachment(ctx.org_id, expense_id, data)
        expense.invoice_object_key = ref
        expense.invoice_filename = invoice_filename
        await session.flush()

        # Attachment upload is a mutation with no dedicated service function
        # (there is no object-storage-aware budget service module) -- this
        # route audits it directly, the same "the mutation audits itself,
        # once" rule the service layer follows everywhere else, just applied
        # one layer up here instead of one layer down.
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="expense.attachment_uploaded",
            target_type="expense",
            target_id=str(expense_id),
            metadata={"filename": invoice_filename},
        )
        await session.flush()
        return _expense_out(expense)


@router.get("/budget/expenses/{expense_id}/attachment")
async def download_attachment_route(
    expense_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("budget.read")),
) -> Response:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            expense = await get_expense(session, ctx.org_id, expense_id)
        except BudgetValidationError as exc:
            raise _http_error(exc) from None
        if expense.invoice_object_key is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no attachment for this expense"))
        ref = expense.invoice_object_key
        # P1(c): the header is built from the SANITIZED STORED value, never
        # by re-deriving a name from the object key (which no longer carries
        # one at all, per P1(a)).
        filename = expense.invoice_filename or "attachment"

        # The second (and last) route-level audited read (spec §5: "download
        # is budget.read-gated and audited").
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="expense.attachment_downloaded",
            target_type="expense",
            target_id=str(expense_id),
            metadata={},
        )

    # P1(d): defence in depth -- `get_budget_attachment` itself asserts `ref`
    # is under this caller's org prefix before it will read it.
    data = await _object_store(request).get_budget_attachment(ref, ctx.org_id)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

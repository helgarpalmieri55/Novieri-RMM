# IT Budget & Expense (Phase 13) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plan IT budgets, record and categorize IT spend, track actuals against plan, and turn the license/subscription renewals the platform already knows about into a committed-spend forecast and confirmable expected expenses.

**Architecture:** A control-plane-only module at `backend/app/modules/budget/`, mirroring `app/modules/subscriptions/` file-for-file (`models.py`, `schema.py`, `errors.py`, service layer, `reporting.py`, `api.py`, plus two pure helper modules `periods.py` and `fx.py`). Six new RLS-scoped tables in migration `0020`. **No agent capability, no job types, no gateway edit, no Celery task** — the forecast is computed at read time and expected expenses materialize only on an explicit operator action.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed, async), Pydantic v2, Alembic, PostgreSQL with Row-Level Security; React + TypeScript (strict) + Vite + TanStack Query on the frontend; pytest / mypy / ruff / vitest as gates.

## Global Constraints

These apply to **every** task. They are copied from `docs/modules/budget/spec.md` and `CLAUDE.md`.

- **Authoritative spec:** `docs/modules/budget/spec.md`. Where this plan and the spec disagree, the spec wins — raise the conflict rather than silently picking one.
- **Tenancy (invariant #5):** every query carries an explicit `org_id` predicate **in addition to** RLS. Every cross-reference (`vendor_id`, `category_id`, `cost_center_id`, `budget_id`, advisory `link_id`) is resolved `WHERE org_id AND id`, so another org's id is indistinguishable from a nonexistent one.
- **Audit (invariant #4):** every privileged mutation calls `audit_service.record`. **No audit on view** — lists, detail, variance, position, forecast, and the confirmation queue write nothing. Only the two exports and attachment download audit a read.
- **RBAC (invariant #6):** every route gated server-side with `require_permission`. `org_id`/`actor_id` come from `AuthContext`, never from the request body or path.
- **Errors:** `BudgetNotFoundError(BudgetValidationError)`. The API maps by `isinstance`, checking the **subclass first**, never by matching message text, never 500.
- **Money:** `Decimal` in Python, `Numeric(14,2)` at rest, JSON **strings** on the wire, `string` in TypeScript. No float arithmetic anywhere. `rate` is `Numeric(18,8)`.
- **Schema bounds (Phase-11/12 lesson):** every string `max_length`; every money field `max_digits=14, decimal_places=2`; `rate` `max_digits=18, decimal_places=8, gt=0`; every integer bounded on **both** ends. Nothing may reach Postgres as an out-of-range `DataError` → 500.
- **Tests first.** Write the failing test, run it, watch it fail for the stated reason, then implement. A test that has never failed is not evidence.
- **Tenancy tests are load-bearing or they are theatre.** Cross-org tests run on a raw `AsyncSession(admin_engine)` (`its_admin` is BYPASSRLS — `tests/conftest.py:111-117`) so the application-level `org_id` predicate is the sole guard. A test written under `org_session(..., engine=app_engine)` is hollow: RLS masks the other org and the predicate is never exercised. **Phase 12 wrote correct tenancy tests, verified them, then deleted them before committing** — mutation-verify each one (remove the predicate, watch the test fail, restore) and report the failure output.
- **Gates per task:** `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m mypy app/modules/budget/`, `.venv/bin/python -m ruff check app/ tests/`. Frontend tasks additionally `npm test`, `npm run typecheck`, `npm run build`. Run from `backend/` and `frontend/` respectively.
- **Commit trailers**, exactly:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013e3wJiCmGprugRA82mA4nF
  ```
- **Do not push.** The controller pushes.

## File Structure

| File | Responsibility |
|---|---|
| `backend/migrations/versions/0020_budget.py` | Six tables, RLS, four permissions + role grants |
| `backend/app/modules/budget/__init__.py` | `register(app)` — mounts the router only |
| `backend/app/modules/budget/errors.py` | `BudgetValidationError`, `BudgetNotFoundError` |
| `backend/app/modules/budget/models.py` | Six ORM models |
| `backend/app/modules/budget/schema.py` | Pydantic request/response models, all bounded |
| `backend/app/modules/budget/periods.py` | **Pure** fiscal-period math — no DB, no async |
| `backend/app/modules/budget/fx.py` | Rate resolution + conversion |
| `backend/app/modules/budget/refdata.py` | Settings, categories, cost centers |
| `backend/app/modules/budget/rates.py` | FX-rate CRUD + base-currency change |
| `backend/app/modules/budget/budgets.py` | Budget + budget_line service |
| `backend/app/modules/budget/expenses.py` | Expense CRUD, confirm, void, attachment |
| `backend/app/modules/budget/feeds.py` | Projection + idempotent materialization |
| `backend/app/modules/budget/reporting.py` | Variance, position, forecast, exports |
| `backend/app/modules/budget/api.py` | Routes, RBAC gating, error mapping |
| `frontend/src/modules/budget/` | Planner, expenses, confirmation queue, dashboard, admin |

Services are split by entity rather than lumped into one `service.py`: Subscriptions' single service file reached ~700 lines with three entities, and Budget has six. Files that change together stay together.

---

### Task 1: Migration 0020 — tables, RLS, RBAC

**Files:**
- Create: `backend/migrations/versions/0020_budget.py`
- Test: `backend/tests/test_budget_migration.py`

**Interfaces:**
- Consumes: migration `0019` as `down_revision` (confirm the current head first).
- Produces: tables `budget_settings`, `cost_center`, `expense_category`, `budget`, `budget_line`, `fx_rate`, `expense`; permissions `budget.read`, `budget.manage`, `expense.record`, `expense.confirm`.

- [ ] **Step 1: Confirm the current migration head**

Run: `cd backend && .venv/bin/python -m alembic heads`
Expected: `0019 (head)`. If it is not `0019`, use whatever it reports as `down_revision` and say so in the commit message.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_budget_migration.py
"""Migration 0020: six budget tables + RLS + the four budget permissions.

Mirrors tests/test_subscriptions_migration.py. RLS is asserted through the
*app* engine (its_app is NOT bypassrls) -- an admin-engine query would pass
even with no policy at all and prove nothing.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

BUDGET_TABLES = [
    "budget_settings",
    "cost_center",
    "expense_category",
    "budget",
    "budget_line",
    "fx_rate",
    "expense",
]


@pytest.mark.parametrize("table", BUDGET_TABLES)
async def test_table_has_rls_enabled_and_forced(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity is True, f"{table} missing ENABLE ROW LEVEL SECURITY"
    assert row.relforcerowsecurity is True, f"{table} missing FORCE ROW LEVEL SECURITY"


@pytest.mark.parametrize("table", BUDGET_TABLES)
async def test_table_has_tenant_isolation_policy(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = :t"),
                {"t": table},
            )
        ).scalars().all()
    assert "tenant_isolation" in names


async def test_budget_permissions_are_seeded(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text("SELECT name FROM permissions WHERE name LIKE 'budget.%' "
                     "OR name LIKE 'expense.%'")
            )
        ).scalars().all()
    assert set(names) == {
        "budget.read",
        "budget.manage",
        "expense.record",
        "expense.confirm",
    }


async def test_technician_is_granted_no_budget_permission(admin_engine) -> None:
    """Spec §0.5 / §8: the first module where technician gets nothing --
    budget and expense data is sensitive even inside IT."""
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text(
                    "SELECT p.name FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'technician' AND r.org_id IS NULL "
                    "AND (p.name LIKE 'budget.%' OR p.name LIKE 'expense.%')"
                )
            )
        ).scalars().all()
    assert names == []


async def test_auditor_gets_read_only(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text(
                    "SELECT p.name FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'auditor' AND r.org_id IS NULL "
                    "AND (p.name LIKE 'budget.%' OR p.name LIKE 'expense.%')"
                )
            )
        ).scalars().all()
    assert names == ["budget.read"]
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_migration.py -q`
Expected: FAIL — the tables do not exist, so `.one()` raises `NoResultFound`.

- [ ] **Step 4: Write the migration**

Follow `migrations/versions/0019_subscriptions.py` exactly for style: raw `op.execute` DDL, a module-level `_rls(table)` helper, `PERMISSIONS` and `ROLE_GRANTS` constants.

```python
# backend/migrations/versions/0020_budget.py
"""Budget: settings, cost centers, categories, budgets, lines, FX rates, expenses.

docs/modules/budget/spec.md §2 (data model) and §8 (security invariants).

Revision ID: 0020
Revises: 0019
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("budget.read", "budget", "read"),
    ("budget.manage", "budget", "manage"),
    ("expense.record", "expense", "record"),
    ("expense.confirm", "expense", "confirm"),
]
_ALL_BUDGET = ["budget.read", "budget.manage", "expense.record", "expense.confirm"]
# Spec §0.5: technician and read-only are deliberately absent -- budget data is
# sensitive inside IT, and this is the first module where technician gets nothing.
ROLE_GRANTS = {
    "super-admin": _ALL_BUDGET,
    "org-admin": _ALL_BUDGET,
    "auditor": ["budget.read"],
}


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid) "
        "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)"
    )
```

`upgrade()` creates, in dependency order: `budget_settings`, `cost_center`, `expense_category`, `budget`, `budget_line`, `fx_rate`, `expense` — column-for-column as spec §2 lists them. Then indexes, then `_rls(t)` for each, then the permission/grant seed (copy the seeding block verbatim from `0019_subscriptions.py`).

Three details that are easy to get wrong and are individually tested later:

```sql
-- budget_line: TWO partial unique indexes. A single index over a nullable
-- cost_center_id enforces nothing for the NULL case (Postgres treats NULLs as
-- distinct), silently permitting unlimited duplicate org-wide lines.
CREATE UNIQUE INDEX uq_budget_line_cc
    ON budget_line (org_id, budget_id, category_id, cost_center_id, period_index)
    WHERE cost_center_id IS NOT NULL;
CREATE UNIQUE INDEX uq_budget_line_nocc
    ON budget_line (org_id, budget_id, category_id, period_index)
    WHERE cost_center_id IS NULL;

-- expense: the materialization idempotency guard. Voided rows stay under the
-- constraint on purpose -- a deliberately voided entry must never resurrect.
CREATE UNIQUE INDEX uq_expense_autofeed
    ON expense (org_id, source, link_id, period_key)
    WHERE source <> 'manual';

-- fx_rate: a rate must be positive.
ALTER TABLE fx_rate ADD CONSTRAINT ck_fx_rate_positive CHECK (rate > 0);
```

`budget_settings` uses `org_id uuid PRIMARY KEY REFERENCES organizations(id)` (one row per org, so `org_id` is the PK) and still gets `_rls`.

`downgrade()` drops the tables in reverse order and deletes the seeded permissions. Use PostgreSQL-valid syntax — the Phase-9 lesson was a downgrade that never ran.

- [ ] **Step 5: Run the test and watch it pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_migration.py -q`
Expected: PASS (17 tests: 7+7 parametrized RLS/policy, plus 3).

- [ ] **Step 6: Run the full gates**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check app/ tests/`
Expected: the whole suite passes. A migration change re-runs every module's schema assertions, so a failure elsewhere means 0020 broke something.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0020_budget.py backend/tests/test_budget_migration.py
git commit -m "feat(budget): migration 0020 — six tables + RLS + RBAC"
```

---

### Task 2: ORM models

**Files:**
- Create: `backend/app/modules/budget/__init__.py`, `backend/app/modules/budget/models.py`
- Modify: `backend/app/models/__init__.py` (export the six models)
- Test: `backend/tests/test_budget_models.py`

**Interfaces:**
- Consumes: `app.models.base.Base`, `uuid7_pk` (`app/modules/subscriptions/models.py:23` shows the import).
- Produces: `BudgetSettings`, `CostCenter`, `ExpenseCategory`, `Budget`, `BudgetLine`, `FxRate`, `Expense` — importable from `app.models`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_budget_models.py
"""ORM models map cleanly onto migration 0020's tables."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Budget, Expense, ExpenseCategory, FxRate


async def test_expense_round_trips_with_decimal_money(api_env) -> None:
    org_id = await api_env.create_org("budget")
    sub = await api_env.create_user(org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        user_id = (
            await session.execute(
                select(__import__("app.models", fromlist=["User"]).User.id).where(
                    __import__("app.models", fromlist=["User"]).User.external_subject == sub
                )
            )
        ).scalar_one()
        cat = ExpenseCategory(
            org_id=org_id, key="hardware", name="Hardware", system_key="hardware",
            created_by=user_id,
        )
        session.add(cat)
        await session.flush()
        exp = Expense(
            org_id=org_id,
            expense_date=date(2026, 3, 1),
            amount=Decimal("1234.56"),
            currency="USD",
            category_id=cat.id,
            source="manual",
            status="confirmed",
            entered_by=user_id,
        )
        session.add(exp)
        await session.flush()
        loaded = (
            await session.execute(select(Expense).where(Expense.id == exp.id))
        ).scalar_one()
    assert loaded.amount == Decimal("1234.56")
    assert isinstance(loaded.amount, Decimal)
    assert loaded.amount_base is None
    assert loaded.fx_rate is None
```

Add equivalent round-trip tests for `Budget` (with `granularity="month"`, `status="draft"`) and `FxRate` (`rate=Decimal("4100.50000000")`, asserting the 8-dp scale survives).

- [ ] **Step 2: Run it and watch it fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Budget' from 'app.models'`.

- [ ] **Step 3: Write the models**

Mirror `app/modules/subscriptions/models.py` exactly: `Mapped[...]` annotations, `mapped_column`, `Index` entries in `__table_args__`, `uuid7_pk()` for `id`, `created_at` via `mapped_column(server_default=func.now())`.

```python
class Expense(Base):
    __tablename__ = "expense"
    __table_args__ = (
        Index("ix_expense_org_date", "org_id", "expense_date"),
        Index("ix_expense_org_status", "org_id", "status"),
        Index("ix_expense_org_category_date", "org_id", "category_id", "expense_date"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    expense_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    amount_base: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendor.id"), default=None)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("expense_category.id"))
    cost_center_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_center.id"), default=None
    )
    description: Mapped[str] = mapped_column(String(1000), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    link_type: Mapped[str | None] = mapped_column(String(16), default=None)
    link_id: Mapped[UUID | None] = mapped_column(default=None)
    period_key: Mapped[str | None] = mapped_column(String(16), default=None)
    invoice_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    invoice_object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(10), default="expected")
    entered_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    voided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    voided_at: Mapped[datetime | None] = mapped_column(default=None)
```

`link_id` deliberately has **no** `ForeignKey` — it is an advisory reference, matching `Subscription.license_ref` (`app/modules/subscriptions/models.py:50`).

Write `__init__.py` mirroring `app/modules/subscriptions/__init__.py`: a docstring stating control-plane-only, and `register(app)` that imports the router and calls `app.include_router(router, prefix="/api/v1")`. Leave the import of `api` until Task 12 creates it — for now `register` may be defined but the module is not yet added to `register_all`.

- [ ] **Step 4: Run the test and watch it pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_models.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m mypy app/modules/budget/
.venv/bin/python -m ruff check app/ tests/
git add backend/app/modules/budget/ backend/app/models/__init__.py backend/tests/test_budget_models.py
git commit -m "feat(budget): ORM models"
```

---

### Task 3: Schemas + typed errors

**Files:**
- Create: `backend/app/modules/budget/errors.py`, `backend/app/modules/budget/schema.py`
- Test: `backend/tests/test_budget_schema.py`

**Interfaces:**
- Produces: `BudgetValidationError`, `BudgetNotFoundError`; request/response models `SettingsUpdate`, `CategoryCreate`, `CategoryUpdate`, `CostCenterCreate`, `CostCenterUpdate`, `FxRateCreate`, `BudgetCreate`, `BudgetUpdate`, `BudgetLineInput`, `BudgetLinesPut`, `ExpenseCreate`, `ExpenseUpdate`, `ExpenseConfirm`, `MaterializeRequest`, and the read models `ExpenseOut`, `BudgetOut`, `VarianceRow`, `PositionOut`, `ForecastRow`.

- [ ] **Step 1: Write the failing test**

Every bound gets a test. This is the module where an unbounded field is a 500 waiting to happen — Phase 12 shipped four of them.

```python
# backend/tests/test_budget_schema.py
"""Schema-boundary bounds (spec §8). Every one of these, unbounded, is a
reachable 500: the value passes Pydantic, reaches asyncpg, and raises an
unhandled DataError. Phase 11 hit it with currency; Phase 12 hit it with
strings, cost precision, and int32 counts.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.schema import BudgetCreate, ExpenseCreate, FxRateCreate


def test_not_found_is_a_validation_error_subclass() -> None:
    assert issubclass(BudgetNotFoundError, BudgetValidationError)


@pytest.mark.parametrize("bad", ["USDD", "US", ""])
def test_expense_rejects_bad_currency_length(bad: str) -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(amount=Decimal("1"), currency=bad, category_id=_UUID, expense_date=_DATE)


def test_expense_rejects_amount_beyond_numeric_precision() -> None:
    # numeric(14,2) holds at most 12 integer digits
    with pytest.raises(ValidationError):
        ExpenseCreate(
            amount=Decimal("1234567890123.00"), currency="USD",
            category_id=_UUID, expense_date=_DATE,
        )


def test_expense_accepts_amount_at_precision_limit() -> None:
    exp = ExpenseCreate(
        amount=Decimal("999999999999.99"), currency="USD",
        category_id=_UUID, expense_date=_DATE,
    )
    assert exp.amount == Decimal("999999999999.99")


def test_fx_rate_rejects_zero_and_negative() -> None:
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationError):
            FxRateCreate(from_currency="COP", rate=bad, effective_date=_DATE)


@pytest.mark.parametrize("bad", [1999, 2101])
def test_budget_rejects_out_of_range_fiscal_year(bad: int) -> None:
    with pytest.raises(ValidationError):
        BudgetCreate(fiscal_year=bad, name="FY", granularity="month")


def test_budget_rejects_out_of_domain_granularity() -> None:
    with pytest.raises(ValidationError):
        BudgetCreate(fiscal_year=2026, name="FY", granularity="weekly")
```

Define `_UUID = uuid4()` and `_DATE = date(2026, 1, 1)` at module level.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.budget.errors`.

- [ ] **Step 3: Write errors.py and schema.py**

`errors.py` mirrors `app/modules/subscriptions/errors.py` verbatim in shape:

```python
class BudgetValidationError(Exception):
    """Bad request -- invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class BudgetNotFoundError(BudgetValidationError):
    """A budget-module row was not found in this org (maps to HTTP 404).
    Subclass so existing `except BudgetValidationError` handlers still catch
    it; the API routes 404 by isinstance."""
```

`schema.py` uses shared `Field` constants so create and update models can never drift apart on their bounds — the drift Phase 12 hit where `currency` was guarded and four sibling strings were not:

```python
_MAX_TEXT = 255
_MAX_DESC = 1000
_MONEY_DIGITS = 14
_MONEY_PLACES = 2
_RATE_DIGITS = 18
_RATE_PLACES = 8

_Money = Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
_OptionalMoney = Field(default=None, max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
_Currency = Field(min_length=3, max_length=3)
_OptionalText = Field(default=None, max_length=_MAX_TEXT)

Granularity = Literal["month", "quarter"]
BudgetStatus = Literal["draft", "active", "closed"]
ExpenseStatus = Literal["expected", "confirmed", "void"]
ExpenseSource = Literal["manual", "subscription", "license"]
LinkType = Literal["subscription", "license", "device", "change"]


class ExpenseCreate(BaseModel):
    expense_date: date
    amount: Decimal = _Money
    currency: str = _Currency
    category_id: UUID
    cost_center_id: UUID | None = None
    vendor_id: UUID | None = None
    description: str = Field(default="", max_length=_MAX_DESC)
    invoice_ref: str | None = Field(default=None, max_length=128)
    link_type: LinkType | None = None
    link_id: UUID | None = None
    status: Literal["expected", "confirmed"] = "confirmed"


class FxRateCreate(BaseModel):
    from_currency: str = _Currency
    rate: Decimal = Field(max_digits=_RATE_DIGITS, decimal_places=_RATE_PLACES, gt=0)
    effective_date: date


class BudgetCreate(BaseModel):
    fiscal_year: int = Field(ge=2000, le=2100)
    name: str = Field(max_length=_MAX_TEXT)
    granularity: Granularity
```

`ExpenseUpdate` uses `X | None = None` for every field and is applied with `exclude_unset=True` plus an explicit-null rejection pass (Task 9) — the Phase-12 PATCH lesson.

Money fields in **response** models are serialized as strings. Follow whatever `app/modules/subscriptions/schema.py` does for `SubscriptionOut` (read it) and match it exactly rather than inventing a second convention.

- [ ] **Step 4: Run the test and watch it pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): API schemas + typed errors"
```

---

### Task 4: `periods.py` — pure fiscal-period math

**Files:**
- Create: `backend/app/modules/budget/periods.py`
- Test: `backend/tests/test_budget_periods.py`

**Interfaces:**
- Produces:
  - `period_count(granularity: str) -> int`
  - `period_bounds(fiscal_year: int, start_month: int, granularity: str, period_index: int) -> tuple[date, date]` — inclusive start, inclusive end
  - `period_for_date(fiscal_year: int, start_month: int, granularity: str, d: date) -> int | None`
  - `period_key_for_date(d: date) -> str` — canonical `"YYYY-MM"` used by materialization
- Raises `BudgetValidationError` on an unknown granularity or an out-of-range `period_index`.

No DB, no async, no imports beyond `calendar`, `datetime`, and the module's own errors — exactly like `app/modules/subscriptions/costs.py`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_budget_periods.py
"""Pure fiscal-period math (spec §4). Calendar-correct bounds -- no 30-day
approximations, and February/leap years are explicit cases."""

from datetime import date

import pytest

from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.periods import (
    period_bounds,
    period_count,
    period_for_date,
    period_key_for_date,
)


def test_period_count_by_granularity() -> None:
    assert period_count("month") == 12
    assert period_count("quarter") == 4


def test_unknown_granularity_raises() -> None:
    with pytest.raises(BudgetValidationError):
        period_count("weekly")


def test_calendar_year_month_bounds() -> None:
    assert period_bounds(2026, 1, "month", 1) == (date(2026, 1, 1), date(2026, 1, 31))
    assert period_bounds(2026, 1, "month", 12) == (date(2026, 12, 1), date(2026, 12, 31))


def test_february_is_28_days_and_leap_february_is_29() -> None:
    assert period_bounds(2026, 1, "month", 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds(2028, 1, "month", 2) == (date(2028, 2, 1), date(2028, 2, 29))


def test_non_calendar_fiscal_year_starting_in_july() -> None:
    """FY2026 starting in month 7 runs 2026-07-01 .. 2027-06-30."""
    assert period_bounds(2026, 7, "month", 1) == (date(2026, 7, 1), date(2026, 7, 31))
    assert period_bounds(2026, 7, "month", 12) == (date(2027, 6, 1), date(2027, 6, 30))


def test_quarter_bounds_span_three_months() -> None:
    assert period_bounds(2026, 1, "quarter", 1) == (date(2026, 1, 1), date(2026, 3, 31))
    assert period_bounds(2026, 1, "quarter", 4) == (date(2026, 10, 1), date(2026, 12, 31))


def test_non_calendar_quarter_crosses_the_year_boundary() -> None:
    assert period_bounds(2026, 7, "quarter", 3) == (date(2027, 1, 1), date(2027, 3, 31))


@pytest.mark.parametrize("bad", [0, 13])
def test_month_period_index_out_of_range_raises(bad: int) -> None:
    with pytest.raises(BudgetValidationError):
        period_bounds(2026, 1, "month", bad)


def test_quarter_period_index_13_is_out_of_range() -> None:
    with pytest.raises(BudgetValidationError):
        period_bounds(2026, 1, "quarter", 5)


def test_period_for_date_at_both_inclusive_edges() -> None:
    assert period_for_date(2026, 7, "month", date(2026, 7, 1)) == 1
    assert period_for_date(2026, 7, "month", date(2026, 7, 31)) == 1
    assert period_for_date(2026, 7, "month", date(2026, 8, 1)) == 2
    assert period_for_date(2026, 7, "month", date(2027, 6, 30)) == 12


def test_period_for_date_outside_the_fiscal_year_is_none() -> None:
    assert period_for_date(2026, 7, "month", date(2026, 6, 30)) is None
    assert period_for_date(2026, 7, "month", date(2027, 7, 1)) is None


def test_period_key_is_canonical_year_month() -> None:
    assert period_key_for_date(date(2026, 3, 9)) == "2026-03"
    assert period_key_for_date(date(2026, 11, 30)) == "2026-11"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_periods.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.budget.periods`.

- [ ] **Step 3: Implement**

```python
"""Pure fiscal-period helpers (docs/modules/budget/spec.md §4).

No DB, no async -- plain functions, fully unit-testable in isolation, in the
same spirit as app/modules/subscriptions/costs.py. Period boundaries are
NEVER stored: they are derived here from (fiscal_year, start_month,
granularity) so a single helper is the only place that knows the calendar.
"""

import calendar
from datetime import date

from app.modules.budget.errors import BudgetValidationError

_PERIODS = {"month": 12, "quarter": 4}
_MONTHS_PER = {"month": 1, "quarter": 3}


def period_count(granularity: str) -> int:
    try:
        return _PERIODS[granularity]
    except KeyError:
        raise BudgetValidationError(f"unknown granularity {granularity!r}") from None


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    total = (y * 12 + (m - 1)) + delta
    return total // 12, total % 12 + 1


def period_bounds(
    fiscal_year: int, start_month: int, granularity: str, period_index: int
) -> tuple[date, date]:
    count = period_count(granularity)
    if not 1 <= period_index <= count:
        raise BudgetValidationError(
            f"period_index {period_index} out of range 1..{count} for {granularity}"
        )
    span = _MONTHS_PER[granularity]
    sy, sm = _add_months(fiscal_year, start_month, (period_index - 1) * span)
    ey, em = _add_months(fiscal_year, start_month, period_index * span - 1)
    return date(sy, sm, 1), date(ey, em, calendar.monthrange(ey, em)[1])


def period_for_date(
    fiscal_year: int, start_month: int, granularity: str, d: date
) -> int | None:
    for idx in range(1, period_count(granularity) + 1):
        start, end = period_bounds(fiscal_year, start_month, granularity, idx)
        if start <= d <= end:
            return idx
    return None


def period_key_for_date(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_periods.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): pure fiscal-period helpers"
```

---

### Task 5: `fx.py` — rate resolution and conversion

**Files:**
- Create: `backend/app/modules/budget/fx.py`
- Test: `backend/tests/test_budget_fx.py`

**Interfaces:**
- Consumes: `FxRate`, `BudgetSettings` from `app.models`; `BudgetValidationError`.
- Produces:
  - `async get_settings(session, org_id) -> BudgetSettings` — lazily creates the row with `base_currency="USD"`, `fiscal_year_start_month=1`, `updated_by=None`
  - `async resolve_rate(session, org_id, from_currency: str, on_date: date) -> Decimal | None`
  - `convert(amount: Decimal, rate: Decimal) -> Decimal` — pure, `ROUND_HALF_UP`, quantized to 2 places
  - `async convert_for(session, org_id, amount, currency, on_date) -> tuple[Decimal | None, Decimal | None]` returning `(fx_rate, amount_base)`, both `None` when unresolvable

- [ ] **Step 1: Write the failing test**

The four rules in this test are the ones that make FX honest. Each is a real failure mode.

```python
# backend/tests/test_budget_fx.py
"""FX resolution and conversion (spec §3).

Four rules, each a real failure mode if broken:
  - pick the greatest effective_date <= d (never a later rate)
  - never extrapolate backwards from a later rate
  - never invert the opposite pair
  - base -> base is exactly 1 with no lookup
"""

from datetime import date
from decimal import Decimal

from app.core.tenancy import org_session
from app.models import FxRate
from app.modules.budget.fx import convert, convert_for, get_settings, resolve_rate


async def _seed_rate(session, org_id, user_id, frm, rate, on) -> None:
    session.add(
        FxRate(
            org_id=org_id, from_currency=frm, to_currency="USD",
            rate=Decimal(rate), effective_date=on, created_by=user_id,
        )
    )
    await session.flush()


def test_convert_rounds_half_up_once() -> None:
    assert convert(Decimal("10.005"), Decimal("1")) == Decimal("10.01")
    assert convert(Decimal("100"), Decimal("0.00456")) == Decimal("0.46")


async def test_settings_are_created_lazily_with_documented_defaults(api_env) -> None:
    org_id = await api_env.create_org("fx")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        settings = await get_settings(session, org_id)
    assert settings.base_currency == "USD"
    assert settings.fiscal_year_start_month == 1
    assert settings.updated_by is None  # never operator-set


async def test_resolve_picks_greatest_effective_date_not_later_than_target(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_rate(session, org_id, user_id, "COP", "4000", date(2026, 1, 1))
        await _seed_rate(session, org_id, user_id, "COP", "4200", date(2026, 6, 1))
        await _seed_rate(session, org_id, user_id, "COP", "4500", date(2026, 9, 1))
        rate = await resolve_rate(session, org_id, "COP", date(2026, 7, 15))
    assert rate == Decimal("4200.00000000")


async def test_no_backward_extrapolation_from_a_later_rate(api_env) -> None:
    """A rate effective AFTER the date must not be used -- returning 4200 here
    would silently value a January expense at a June rate."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_rate(session, org_id, user_id, "COP", "4200", date(2026, 6, 1))
        rate = await resolve_rate(session, org_id, "COP", date(2026, 1, 15))
    assert rate is None


async def test_pair_is_not_inverted(api_env) -> None:
    """Only USD->? is seeded; COP->USD must stay unresolvable rather than
    silently using 1/rate."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        session.add(
            FxRate(
                org_id=org_id, from_currency="USD", to_currency="COP",
                rate=Decimal("4200"), effective_date=date(2026, 1, 1),
                created_by=user_id,
            )
        )
        await session.flush()
        rate = await resolve_rate(session, org_id, "COP", date(2026, 6, 1))
    assert rate is None


async def test_base_to_base_is_one_without_a_lookup(api_env) -> None:
    org_id, _ = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate, base = await convert_for(
            session, org_id, Decimal("50.00"), "USD", date(2026, 1, 1)
        )
    assert rate == Decimal("1")
    assert base == Decimal("50.00")


async def test_unresolvable_currency_yields_none_none(api_env) -> None:
    org_id, _ = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate, base = await convert_for(
            session, org_id, Decimal("50.00"), "COP", date(2026, 1, 1)
        )
    assert rate is None
    assert base is None
```

Write the `_org_with_user(api_env)` helper once at module level (create org, create an `org-admin` user, return `(org_id, user_id)`); later tasks reuse the same shape.

- [ ] **Step 2: Add the load-bearing tenancy test**

```python
async def test_tenancy_fx_rate_lookup_org_filter_is_load_bearing(
    api_env, admin_engine
) -> None:
    """Org B's rate must never resolve for org A.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:111-117) -- so org B's row is fully visible on this
    connection and resolve_rate's own org_id predicate is the SOLE guard.
    Under org_session(engine=app_engine) RLS would mask it and this test
    would pass with the predicate deleted.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await _seed_rate(session, org_b, user_b, "COP", "4200", date(2026, 1, 1))
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rate = await resolve_rate(session, org_a, "COP", date(2026, 6, 1))
    assert rate is None
```

- [ ] **Step 3: Run and watch both fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_budget_fx.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.budget.fx`.

- [ ] **Step 4: Implement**

```python
_TWO_PLACES = Decimal("0.01")


def convert(amount: Decimal, rate: Decimal) -> Decimal:
    """Apply a rate and round ONCE, at the end."""
    return (amount * rate).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


async def resolve_rate(
    session: AsyncSession, org_id: UUID, from_currency: str, on_date: date
) -> Decimal | None:
    settings = await get_settings(session, org_id)
    if from_currency == settings.base_currency:
        return Decimal("1")
    stmt = (
        select(FxRate.rate)
        .where(
            FxRate.org_id == org_id,                      # invariant #5
            FxRate.from_currency == from_currency,
            FxRate.to_currency == settings.base_currency,
            FxRate.effective_date <= on_date,             # never a later rate
        )
        .order_by(FxRate.effective_date.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
```

`get_settings` selects `WHERE BudgetSettings.org_id == org_id`; on miss it inserts the documented defaults and flushes. `convert_for` calls `resolve_rate` and returns `(None, None)` when it is `None`, else `(rate, convert(amount, rate))`.

- [ ] **Step 5: Run and watch pass, then mutation-verify the tenancy test**

Run the suite: `.venv/bin/python -m pytest tests/test_budget_fx.py -q` → PASS.

Then delete `FxRate.org_id == org_id` from the `resolve_rate` predicate and re-run `test_tenancy_fx_rate_lookup_org_filter_is_load_bearing`. It **must** fail. Restore the line, confirm `git diff app/modules/budget/fx.py` is empty, re-run, confirm PASS. **Report the exact failure output.**

- [ ] **Step 6: Gates and commit**

```bash
git commit -m "feat(budget): FX rate resolution + conversion"
```

---

### Task 6: Reference data — settings, categories, cost centers

**Files:**
- Create: `backend/app/modules/budget/refdata.py`
- Test: `backend/tests/test_budget_refdata.py`

**Interfaces:**
- Produces:
  - `async seed_default_categories(session, org_id, actor_id) -> list[ExpenseCategory]`
  - `async list_categories(session, org_id, *, include_inactive=False) -> list[ExpenseCategory]`
  - `async create_category(session, org_id, actor_id, *, key, name) -> ExpenseCategory`
  - `async update_category(session, org_id, actor_id, category_id, fields: dict) -> ExpenseCategory`
  - `async resolve_category(session, org_id, category_id) -> ExpenseCategory` — raises `BudgetNotFoundError`
  - `async category_by_system_key(session, org_id, system_key: str) -> ExpenseCategory | None`
  - the same five for `cost_center` (`create_cost_center`, `list_cost_centers`, `update_cost_center`, `resolve_cost_center`)
- `DEFAULT_CATEGORIES: tuple[tuple[str, str], ...]` — the seven `(key, name)` pairs from spec §2.

- [ ] **Step 1: Write the failing tests**

Cover: seeding is idempotent (running twice does not duplicate); `system_key` is set on seeded rows and `None` on operator-created ones; renaming a category leaves `system_key` intact (the property materialization depends on); a duplicate `key` in one org is rejected; the same `key` in two orgs is fine; `resolve_category` raises `BudgetNotFoundError` for another org's id.

```python
async def test_renaming_a_category_preserves_its_system_key(api_env) -> None:
    """system_key is the stable identifier the auto-feeds resolve against
    (spec §6.1) -- renaming the display name must never break materialization."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await seed_default_categories(session, org_id, user_id)
        cat = await category_by_system_key(session, org_id, "saas_subscriptions")
        assert cat is not None
        await update_category(session, org_id, user_id, cat.id, {"name": "Cloud SaaS"})
        again = await category_by_system_key(session, org_id, "saas_subscriptions")
    assert again is not None
    assert again.name == "Cloud SaaS"
    assert again.system_key == "saas_subscriptions"
```

Plus a load-bearing cross-org test on `resolve_category` in the Task-5 style (raw `AsyncSession(admin_engine)`).

- [ ] **Step 2: Run and watch fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Every mutation calls `audit_service.record(session, org_id=..., actor_type="user", actor_id=actor_id, action="budget.category_created", target_type="expense_category", target_id=str(cat.id), metadata={...})` then flushes — the pattern in `app/modules/subscriptions/service.py`. Actions: `budget.category_created`, `budget.category_updated`, `budget.cost_center_created`, `budget.cost_center_updated`.

```python
DEFAULT_CATEGORIES = (
    ("hardware", "Hardware"),
    ("software_licenses", "Software licenses"),
    ("saas_subscriptions", "SaaS subscriptions"),
    ("cloud_hosting", "Cloud & hosting"),
    ("telecom", "Telecom"),
    ("services", "Services"),
    ("maintenance", "Maintenance"),
)
```

`seed_default_categories` selects existing `system_key`s for the org first and inserts only the missing ones, so it is idempotent by construction rather than by luck.

- [ ] **Step 4: Run, watch pass, mutation-verify the tenancy test, restore, report output.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): settings, categories, cost centers"
```

---

### Task 7: FX-rate service + base-currency change

**Files:**
- Create: `backend/app/modules/budget/rates.py`
- Test: `backend/tests/test_budget_rates.py`

**Interfaces:**
- Produces:
  - `async create_rate(session, org_id, actor_id, *, from_currency, rate, effective_date) -> FxRate`
  - `async list_rates(session, org_id, *, from_currency=None) -> list[FxRate]`
  - `async update_rate(session, org_id, actor_id, rate_id, fields: dict) -> FxRate`
  - `async set_base_currency(session, org_id, actor_id, *, base_currency, acknowledge_mixed_basis=False) -> BudgetSettings`
  - `async set_fiscal_year_start(session, org_id, actor_id, *, start_month: int) -> BudgetSettings`

- [ ] **Step 1: Write the failing tests**

The base-currency rule is the subtle one. Spec §3: *once any non-void expense carries an `amount_base`, changing the base currency requires an explicit `acknowledge_mixed_basis=true`.*

```python
async def test_base_currency_change_is_free_before_any_converted_expense(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        settings = await set_base_currency(session, org_id, user_id, base_currency="COP")
    assert settings.base_currency == "COP"


async def test_base_currency_change_is_blocked_once_an_expense_is_converted(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id)
        with pytest.raises(BudgetValidationError):
            await set_base_currency(session, org_id, user_id, base_currency="COP")


async def test_base_currency_change_proceeds_with_explicit_acknowledgement(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id)
        settings = await set_base_currency(
            session, org_id, user_id, base_currency="COP", acknowledge_mixed_basis=True
        )
    assert settings.base_currency == "COP"


async def test_base_currency_change_does_not_retro_convert_history(api_env) -> None:
    """Spec §3: stored rows keep the basis they were converted at."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        exp = await _seed_converted_expense(session, org_id, user_id)
        before_rate, before_base = exp.fx_rate, exp.amount_base
        await set_base_currency(
            session, org_id, user_id, base_currency="COP", acknowledge_mixed_basis=True
        )
        await session.refresh(exp)
    assert exp.fx_rate == before_rate
    assert exp.amount_base == before_base


async def test_a_voided_expense_does_not_block_the_change(api_env) -> None:
    """The rule says NON-void."""
```

Also: a duplicate `(from_currency, to_currency, effective_date)` is rejected; `to_currency` is always set from settings and never taken from the caller; every mutation writes an audit event; `set_base_currency` audits `budget.base_currency_changed` with the old and new codes in metadata.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.** `create_rate` sets `to_currency` from `get_settings(...)`, never from input. `set_base_currency` runs the guard as a query — `SELECT 1 FROM expense WHERE org_id = :o AND status <> 'void' AND amount_base IS NOT NULL LIMIT 1` — before mutating.

- [ ] **Step 4: Run and watch pass.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): FX rate service + base-currency change guard"
```

---

### Task 8: Budgets and budget lines

**Files:**
- Create: `backend/app/modules/budget/budgets.py`
- Test: `backend/tests/test_budget_budgets.py`

**Interfaces:**
- Produces:
  - `async create_budget(session, org_id, actor_id, *, fiscal_year, name, granularity) -> Budget`
  - `async list_budgets(session, org_id) -> list[Budget]`
  - `async get_budget(session, org_id, budget_id) -> Budget` — raises `BudgetNotFoundError`
  - `async update_budget(session, org_id, actor_id, budget_id, fields: dict) -> Budget`
  - `async close_budget(session, org_id, actor_id, budget_id) -> Budget`
  - `async put_lines(session, org_id, actor_id, budget_id, lines: list[BudgetLineInput]) -> list[BudgetLine]` — bulk upsert
  - `async delete_line(session, org_id, actor_id, budget_id, line_id) -> None`

- [ ] **Step 1: Write the failing tests**

```python
async def test_put_lines_upserts_rather_than_duplicating(api_env) -> None:
    """Called twice with the same (category, cost_center, period) it updates
    the amount -- it does not create a second line."""


async def test_duplicate_org_wide_line_is_rejected_by_the_partial_index(
    api_env,
) -> None:
    """The NULL-cost_center_id case. A single unique index over a nullable
    column enforces NOTHING here (Postgres treats NULLs as distinct), so this
    is the test that proves uq_budget_line_nocc exists and works."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        # two lines, same category + period, both with cost_center_id = None
        ...
    # second insert must raise IntegrityError / be upserted, never duplicate


async def test_period_index_is_validated_against_granularity(api_env) -> None:
    """period_index=5 is legal for a monthly budget and illegal for a
    quarterly one."""


async def test_a_closed_budget_rejects_line_mutations(api_env) -> None:
    """Spec §4: a reported year must not shift under a reader."""
    ...
    with pytest.raises(BudgetValidationError):
        await put_lines(session, org_id, user_id, budget.id, [line])


async def test_closing_a_budget_is_audited(api_env) -> None: ...
```

Plus load-bearing cross-org tests on `get_budget` and on `put_lines`' `category_id` / `cost_center_id` resolution.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.** `put_lines` resolves every `category_id` and `cost_center_id` org-scoped **before** writing anything, validates each `period_index` against `period_count(budget.granularity)`, then upserts. Audit `budget.lines_updated` once per call with the line count — not once per line, which would flood the chain.

- [ ] **Step 4: Run, watch pass, mutation-verify the two tenancy tests, restore, report output.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): budgets + budget lines"
```

---

### Task 9: Expense service

**Files:**
- Create: `backend/app/modules/budget/expenses.py`
- Test: `backend/tests/test_budget_expenses.py`

**Interfaces:**
- Produces:
  - `async create_expense(session, org_id, actor_id, data: ExpenseCreate) -> Expense`
  - `async list_expenses(session, org_id, *, status=None, category_id=None, cost_center_id=None, vendor_id=None, source=None, date_from=None, date_to=None) -> list[Expense]`
  - `async get_expense(session, org_id, expense_id) -> Expense`
  - `async update_expense(session, org_id, actor_id, expense_id, fields: dict) -> Expense`
  - `async confirm_expense(session, org_id, actor_id, expense_id, *, amount: Decimal | None = None) -> Expense`
  - `async void_expense(session, org_id, actor_id, expense_id) -> Expense`
  - `EXPENSE_EDITABLE_FIELDS: tuple[str, ...]`, `NON_NULLABLE_EXPENSE_FIELDS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

```python
async def test_create_captures_the_rate_and_base_amount(api_env) -> None:
    """Spec §3: conversion is CAPTURED at write time, not computed on read."""


async def test_a_later_rate_correction_does_not_re_value_a_stored_expense(
    api_env,
) -> None:
    """The whole point of storing fx_rate + amount_base on the row."""
    ...
    # seed rate 4000, create expense, then add a corrective rate 4500 on the
    # same effective_date via a second row / update, reload the expense
    assert exp.fx_rate == Decimal("4000.00000000")
    assert exp.amount_base == before


async def test_unconvertible_expense_is_created_with_null_base(api_env) -> None:
    """Never guess a rate (spec §0.1) -- the row exists, amount_base is NULL."""


async def test_confirm_stamps_actor_and_time_and_accepts_a_corrected_amount(
    api_env,
) -> None:
    """Spec §0.3: the confirmed amount may differ from the projected one."""


async def test_confirm_re_runs_conversion_against_expense_date(api_env) -> None: ...


async def test_void_stamps_and_is_audited(api_env) -> None: ...


async def test_patch_omitted_field_is_left_untouched(api_env) -> None:
    """exclude_unset -- the Phase-12 lesson."""


async def test_patch_explicit_null_clears_a_nullable_field(api_env) -> None:
    """{"cost_center_id": null} must actually clear it -- Phase 12 shipped a
    PATCH that returned 200 and changed nothing."""


@pytest.mark.parametrize("field", sorted(NON_NULLABLE_EXPENSE_FIELDS))
async def test_patch_explicit_null_on_a_non_nullable_field_is_rejected(
    api_env, field: str
) -> None:
    """Rejected BEFORE any setattr, so the ORM object is never left partially
    mutated when the error is raised."""


async def test_edit_audit_records_field_names_and_never_amounts(api_env) -> None:
    """Spec §8: amounts are the sensitive payload in this module."""
    ...
    assert set(event.event_metadata["changed_fields"]) == {"amount", "description"}
    assert "1234.56" not in json.dumps(event.event_metadata)
```

Plus load-bearing cross-org tests on `category_id`, `cost_center_id`, and `vendor_id` resolution in `create_expense`.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.** The explicit-null rejection runs as a **separate pass before any `setattr`** — that ordering is what guarantees no partial mutation, and it is why Phase 12's equivalent was restructured. Audit actions: `expense.created`, `expense.updated` (metadata `{"changed_fields": [...]}`, names only), `expense.confirmed`, `expense.voided`.

- [ ] **Step 4: Run, watch pass, mutation-verify the three tenancy tests, restore, report output.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): expense lifecycle — create, confirm, void"
```

---

### Task 10: Projection and idempotent materialization

**Files:**
- Create: `backend/app/modules/budget/feeds.py`
- Test: `backend/tests/test_budget_feeds.py`

**Interfaces:**
- Consumes: `Subscription`, `License` from `app.models`; `app.modules.subscriptions.costs.advance_renewal`; `periods.period_key_for_date`; `fx.convert_for`; `refdata.category_by_system_key`.
- Produces:
  - `ProjectedCost` — a frozen dataclass: `source, link_id, link_type, period_key, on_date, amount, currency, description, vendor_id, category_system_key`
  - `async project_costs(session, org_id, *, from_date, to_date) -> list[ProjectedCost]` — **pure projection, writes nothing**
  - `async materialize(session, org_id, actor_id, *, from_date, to_date) -> dict[str, int]` returning `{"created": n, "skipped_existing": n, "skipped_unconvertible": n}`
- `MAX_MATERIALIZE_MONTHS = 24`

- [ ] **Step 1: Write the failing tests**

This task's tests are the ones that keep financial records from duplicating.

```python
async def test_materialize_twice_creates_nothing_the_second_time(api_env) -> None:
    """The idempotency guarantee. Not an assumption -- a test."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_monthly_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))
        first = await materialize(
            session, org_id, user_id, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )
        second = await materialize(
            session, org_id, user_id, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )
    assert first["created"] > 0
    assert second["created"] == 0
    assert second["skipped_existing"] == first["created"]


async def test_a_voided_entry_is_not_resurrected(api_env) -> None:
    """Spec §2: voided rows deliberately stay under the unique constraint.
    An operator voided it on purpose; re-running must not undo that."""


async def test_unconvertible_projection_is_created_and_counted(api_env) -> None:
    assert result["skipped_unconvertible"] == 1
    # ...and the row exists with amount_base IS NULL


async def test_cancelled_and_trial_subscriptions_are_excluded(api_env) -> None:
    """Mirrors subscriptions/spec.md §7. The trial exclusion is flagged there
    as an open product question -- if it changes, it changes in both places."""


async def test_missing_system_key_category_fails_loudly(api_env) -> None:
    """Spec §6.1: no fallback guess. The error names the missing key."""
    with pytest.raises(BudgetValidationError) as exc:
        await materialize(...)
    assert "saas_subscriptions" in str(exc.value)


async def test_window_longer_than_24_months_is_rejected(api_env) -> None: ...


async def test_project_costs_writes_nothing(api_env) -> None:
    """The forecast must never persist -- that is what keeps it from drifting."""
    before = await _expense_count(session, org_id)
    await project_costs(session, org_id, from_date=..., to_date=...)
    assert await _expense_count(session, org_id) == before


async def test_licenses_project_renewal_and_maintenance_dates(api_env) -> None: ...
```

Plus a load-bearing cross-org test: org B's subscription must not appear in org A's projection.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.**

`project_costs` walks each active subscription forward from `renews_at` by `advance_renewal(...)` until past `to_date`, and each license's `renews_at` / `maintenance_expires_at` as one-offs. `materialize` calls `project_costs`, then for each projection checks for an existing row on `(source, link_id, period_key)` — including voided ones — and inserts only when absent. The DB unique index is the backstop; the pre-check is what lets the function report `skipped_existing` honestly.

Audit once per call: `budget.expenses_materialized` with the three counts, plus `expense.created` per inserted row.

- [ ] **Step 4: Run, watch pass, mutation-verify the tenancy test, restore, report output.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): renewal projection + idempotent materialization"
```

---

### Task 11: Reporting — variance, position, forecast, exports

**Files:**
- Create: `backend/app/modules/budget/reporting.py`
- Test: `backend/tests/test_budget_reporting.py`

**Interfaces:**
- Produces:
  - `async variance(session, org_id, budget_id) -> dict[str, Any]`
  - `async position(session, org_id, budget_id, *, as_of: date) -> dict[str, Any]`
  - `async forecast(session, org_id, *, months: int) -> dict[str, Any]`
  - `async export_rows(session, org_id, *, date_from=None, date_to=None) -> list[dict[str, Any]]`
  - `async odoo_export_rows(session, org_id, *, date_from=None, date_to=None) -> list[dict[str, Any]]`
- Every aggregate returns `unconvertible_count: int` and `unconvertible_by_currency: dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_variance_keeps_four_columns_separate(api_env) -> None:
    """planned / actual / committed / projected are never collapsed --
    collapsing committed and projected is how a forecast double-counts."""
    row = ...
    assert set(row) >= {"planned", "actual", "committed", "projected"}


async def test_actual_counts_only_confirmed_and_committed_only_expected(
    api_env,
) -> None: ...


async def test_projected_excludes_periods_that_already_have_a_materialized_row(
    api_env,
) -> None:
    """The double-count guard, stated as a test."""


async def test_voided_expenses_are_excluded_from_every_figure(api_env) -> None: ...


async def test_unconvertible_rows_are_excluded_from_totals_and_disclosed(
    api_env,
) -> None:
    """Spec §3: a total is never quietly understated."""
    assert report["actual"] == "100.00"
    assert report["unconvertible_count"] == 1
    assert report["unconvertible_by_currency"] == {"COP": "500000.00"}


async def test_variance_pct_with_zero_plan_is_none_not_infinity(api_env) -> None:
    assert row["variance_pct"] is None


async def test_position_uses_the_fiscal_calendar_not_wall_clock_months(
    api_env,
) -> None:
    """Elapsed periods come from period_for_date, so a July-start fiscal year
    reports correctly in January."""


async def test_export_rows_money_is_a_string(api_env) -> None: ...


async def test_odoo_export_emits_the_pinned_column_contract(api_env) -> None:
    assert list(rows[0]) == [
        "date", "reference", "vendor", "category", "cost_center",
        "description", "amount", "currency", "amount_base", "base_currency",
        "link_type", "link_id",
    ]


async def test_reports_write_no_audit_event(api_env) -> None:
    """No-audit-on-view (invariant #4). Only the export ROUTES audit, in
    Task 12 -- the reporting layer itself writes nothing."""
```

Plus load-bearing cross-org tests: another org's expenses never appear in any aggregate, and an unresolvable `vendor_id` label is suppressed **with its id** (the Phase-12 `group_id` lesson — suppressing a label while emitting the raw id leaks a cross-tenant identifier).

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.** Aggregate in SQL where practical, in Python where clarity wins. Money stays `Decimal` until the moment it is serialized to a string. Division guards return `None`, never `Infinity` and never a raise.

- [ ] **Step 4: Run, watch pass, mutation-verify the tenancy tests, restore, report output.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): variance, position, forecast, exports"
```

---

### Task 12: API routes, registration, RBAC registry, deploy notes

**Files:**
- Create: `backend/app/modules/budget/api.py`
- Modify: `backend/app/modules/budget/__init__.py`, `backend/app/modules/__init__.py` (add to `register_all`), `backend/app/core/rbac.py` (add the four permissions), `deploy/README.md` (notes)
- Test: `backend/tests/test_budget_api.py`; update `backend/tests/test_rbac.py` and `backend/tests/test_api_routes.py`

**Interfaces:**
- Consumes: every service function from Tasks 6-11.
- Produces: the routes in spec §9.

> **The Phase-7 lesson:** `app/core/rbac.py`'s `PERMISSIONS` registry and the perm-set tests are updated **in this same task**, not a later one. A migration that seeds a permission the app registry lacks is a live authorization bug.

- [ ] **Step 1: Write the failing tests**

```python
async def test_technician_is_denied_every_budget_route(api_env) -> None:
    """Spec §0.5 -- the module-defining RBAC assertion."""
    for method, path in _ALL_ROUTES:
        resp = await api_env.client.request(method, path, headers=tech_headers)
        assert resp.status_code == 403, f"{method} {path} leaked to technician"


async def test_expense_record_cannot_confirm(api_env) -> None: ...
async def test_expense_confirm_cannot_create(api_env) -> None: ...


async def test_cross_org_budget_id_is_404_not_500(api_env) -> None: ...
async def test_invalid_granularity_is_422_not_500(api_env) -> None: ...


async def test_export_is_audited_and_list_is_not(api_env) -> None:
    """Invariant #4. Viewing must never be indistinguishable from exporting."""
    before = await _audit_count(api_env, org_id, "budget.export")
    await api_env.client.get("/api/v1/budget/expenses", headers=h)
    assert await _audit_count(api_env, org_id, "budget.export") == before
    await api_env.client.get("/api/v1/budget/expenses/export", headers=h)
    assert await _audit_count(api_env, org_id, "budget.export") == before + 1


async def test_static_paths_resolve_before_the_id_route(api_env) -> None:
    """/budget/expenses/export must not be parsed as an expense id."""
    resp = await api_env.client.get("/api/v1/budget/expenses/export", headers=h)
    assert resp.status_code == 200


async def test_forecast_route_resolves(api_env) -> None: ...


async def test_materialize_is_idempotent_through_the_api(api_env) -> None: ...
```

Update `tests/test_rbac.py`'s expected permission set and `tests/test_api_routes.py`'s route inventory in the same commit.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.**

Copy `_http_error` from `app/modules/subscriptions/api.py:87-103` exactly — `isinstance(exc, BudgetNotFoundError)` checked **before** the parent class:

```python
def _http_error(exc: BudgetValidationError) -> HTTPException:
    if isinstance(exc, BudgetNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
```

Declare `/budget/expenses/export`, `/budget/expenses/export/odoo`, `/budget/expenses/materialize`, and `/budget/forecast` **before** any `{id}`-shaped sibling.

Add to `app/core/rbac.py`'s `PERMISSIONS`:

```python
"budget.read": "View budgets, expenses, dashboards, and exports",
"budget.manage": (
    "Create and edit budgets, lines, categories, cost centers, FX rates, "
    "and the org base currency"
),
"expense.record": "Create and edit expenses, and run materialization",
"expense.confirm": "Confirm an expected expense, or void an expense",
```

Deploy notes in `deploy/README.md`: object-storage prefix `{org_id}/budget/expenses/{expense_id}/`, the Odoo CSV column contract verbatim, the statement that `invoice_ref` is a reference string and **not** a credential, and that a base-currency change is not retroactive.

- [ ] **Step 4: Run, watch pass.**

- [ ] **Step 5: Gates and commit**

```bash
git commit -m "feat(budget): API routes + module registration"
```

---

### Task 13: Frontend

**Files:**
- Create: `frontend/src/modules/budget/` — `types.ts`, `api.ts`, `budgetState.ts`, `BudgetPlannerPage.tsx`, `ExpensesPage.tsx`, `ConfirmationQueuePage.tsx`, `BudgetDashboard.tsx`, `BudgetAdminPage.tsx`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/App.tsx`
- Test: `frontend/src/modules/budget/__tests__/budget.test.tsx`

**Interfaces:**
- Consumes: the routes from Task 12.
- Produces: nav entries and routes for all five surfaces.

> **The Phase-12 lesson, stated as a requirement:** every page this task creates must be **reachable from navigation**. Phase 12 shipped two report pages that were routed and rendered but had no link anywhere in the app — reachable only by typing the URL. A page with a route and no link is not done.

- [ ] **Step 1: Write the failing tests**

```tsx
it("links to every budget surface from the module nav", async () => { ... })

it("hides budget navigation without budget.read", async () => { ... })

it("submits a new expense with the entered values", async () => {
  // wiring test in the Phase-12 style: query by label/role as a user would,
  // and assert the exact call arguments
  expect(api.createExpense).toHaveBeenCalledWith(
    expect.objectContaining({ amount: "1234.56", currency: "USD" }),
  )
})

it("confirms an expected expense with a corrected amount", async () => { ... })

it("renders money as strings without float arithmetic", async () => { ... })

it("shows the unconvertible disclosure when the API reports one", async () => {
  // spec §3 -- an understated total must never look complete
})

it("hides confirm controls without expense.confirm", async () => { ... })
```

For each of the two mutation tests, verify it is load-bearing: break the wiring, confirm the test FAILS, restore the source exactly (`git diff` on the source must be **empty**), confirm it passes. Report both observations.

- [ ] **Step 2: Run and watch fail.**

- [ ] **Step 3: Implement.** Strict TypeScript, **no `any`** in source or tests. Money is `string` — never `Number()`. Badge-variant maps use only vocabulary the backend emits, with a `?? "muted"` fallback and a comment citing the backend source. Every mutation hook's `invalidateQueries` keys must match the keys registered in `budgetState.ts`. Permission gating hides controls; add no comment implying the frontend is a security boundary.

- [ ] **Step 4: Run the gates**

```bash
cd frontend && npm test && npm run typecheck && npm run build
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(budget): planner, expenses, confirmation queue, dashboard"
```

---

## Self-review

**Spec coverage.** §0 decisions → Tasks 1 (RBAC), 5/7 (FX), 6 (cost centers), 9 (no auto-confirm), 10 (no worker), 12 (Odoo CSV). §2 data model → Tasks 1-2. §3 FX → Tasks 5, 7, 9. §4 periods → Task 4. §5 expense lifecycle → Task 9. §6 auto-feeds → Task 10. §7 reporting → Task 11. §8 invariants → distributed, with tenancy tests in 5-11 and RBAC/audit in 12. §9 API → Task 12. §10 worker (none) → asserted in Task 12's registration test. §11 frontend → Task 13. §12 testing → every task. §13 deploy → Task 12.

**Two gaps found and closed while reviewing:** the invoice-attachment upload/download routes from §5 were missing a home — they belong in Task 9 (service) and Task 12 (routes), and both task descriptions now name them. `set_fiscal_year_start` was in no task's interface block and is now in Task 7's.

**Known thin spot, deliberate:** Tasks 6-11 give test **names and intent** with representative bodies rather than every assertion spelled out, because each is a straightforward application of the fully-worked patterns in Tasks 1-5. An implementer who follows Task 5's tenancy-test shape and Task 4's pure-function shape has the templates for all of them. If a task's implementer finds an ambiguity, the spec is authoritative — raise it rather than guessing.

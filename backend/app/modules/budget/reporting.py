"""Read-only budget reporting: variance, position, forecast, and the two CSV
exports (docs/modules/budget/spec.md §7).

This is the module's payoff: everything else exists so this file can answer
"where does spend stand against plan" and "what's coming". Every function
here is a pure read -- **no writes, no audit events** (spec §7's
"no-audit-on-view": lists, detail, variance, position, forecast, and the
confirmation queue write nothing; only the export ROUTES audit, and that
happens one layer up, in the future API task -- this module never imports
`audit_service`). Every query carries an explicit `org_id` predicate
(invariant #5); RLS is the backstop, never the only guard.

**The four-column rule (spec §7), never collapsed.** `variance()`'s rows
carry `planned` (from `budget_line`), `actual` (`status='confirmed'`),
`committed` (`status='expected'`, regardless of whether the row was
manually pre-recorded or auto-materialized by Task 10's `feeds.materialize`
-- the spec's parenthetical "i.e. materialized" describes the common case,
not an additional filter), and `projected` (this module's own read-time call
into `feeds.project_costs`, EXCLUDING any projection whose materialization
identity is already REPRESENTED by an existing `Expense` row -- auto-fed or
hand-entered, see `_materialized_keys` below. It is NOT "any (category,
period) that does not already have a committed row" -- that description was
this file's own stale mental model and is exactly what produced the L1/L2
Criticals below: a category/period is not an identity, so one row in it
could suppress every OTHER row sharing it, or lose the suppression entirely
the moment its `status` changed). Collapsing `committed` and `projected` is
exactly how a forecast double-counts a renewal that has already been
booked -- `variance()` and `forecast()` share ONE helper, `_materialized_keys`,
that answers "is this projected cost already represented by SOME expense
row" by querying `Expense.link_type, Expense.link_id, Expense.expense_date`
for every row whose `link_id` is one of the candidate link ids -- every
auto-fed row, AND every manual row an operator has pointed at the thing it
pays for (Task 9's org-scoped `link_type`/`link_id` on manual rows; spec §5
blesses hand pre-recording a known upcoming renewal as a first-class path,
so a hand-entered row representing it must suppress that renewal's
projection too, exactly as an auto-fed row does -- M1). Keyed on that row's
materialization identity, `(link_type, link_id, period_key)` -- where
`period_key` is read off the STORED `Expense.period_key` column when the row
has one, falling back to a DERIVATION from `expense_date` only for a manual
row (whose `period_key` column is always NULL; spec §5 gives a caller no
field to set it) (I1 fix: deriving unconditionally let an EDIT to an
auto-fed row's `expense_date` -- permitted by `update_expense`, which never
touches `period_key` -- desynchronize the guard key from the row's actual
materialization identity and let the same renewal double-count; see
`_materialized_keys`'s docstring for the money scenario. A manual row's
suppression is therefore inherently month-based: moving one across a month
boundary changes which projection it suppresses) -- and with NO status
filter at all (EXISTENCE is what matters for the guard, regardless of
whether the row is `expected` or `confirmed`, and regardless of FX
convertibility). This key is
**deliberately BROADER than `feeds.py`'s own `uq_expense_client_autofeed`**
(`(org_id, client_id, source, link_id, period_key) WHERE source <> 'manual'`) -- the two answer
different questions and must NOT be "fixed" back into agreement.
`feeds.materialize`'s pre-check asks "does an AUTO-MATERIALIZED row already
own this slot" (keyed on `source`, since only an auto-fed row's `source` is
the stable, meaningful value `"subscription"`/`"license"` -- a manual row's
`source` is always the literal `"manual"`, so a guard keyed on `source`, as
this file's own guard used to be, can NEVER match a manual row no matter
what it's linked to). This guard asks the broader question above, so it
keys on `link_type` instead, which BOTH an auto-fed row (copied from the
projection) and an operator-authored manual row (Task 9's resolution) carry
meaningfully. A prior version of this file had `variance()` build its own
divergent copy of this guard, keyed on `(category_id, period_index)` and
filtered to `status == 'expected'`; that let a CONFIRMED renewal's period
resurface as `projected` (a confirmed row isn't `expected` any more, so it
fell out of the guard) and let one materialized row in a category/period
suppress every OTHER projection sharing that category/period (the guard
couldn't tell renewals apart once it forgot their identity). A later
version keyed correctly on identity but on `(source, link_id, period_key)`,
which excluded manual rows outright and so double-counted a hand-recorded
renewal (M1). Two copies of this guard 160 lines apart, and a too-narrow
key on the one shared copy, is precisely what let each of these bugs
through in turn; there is now exactly one helper, and its key is wide
enough for both kinds of row that can represent a renewal.

**Voided rows are excluded from every figure, everywhere in this file** --
`status != 'void'` on every `Expense` query, including both exports (a CSV
export is still a "figure": an accounting feed built from voided rows would
double-book a cost the operator explicitly retracted). The ONE exception is
`_materialized_keys` itself: it carries no status filter at all, matching
`feeds.py`'s own materialization pre-check verbatim, because spec §2 requires
a voided auto-fed row to never resurrect. If the guard excluded void rows, a
cost the operator explicitly retracted would return as live forward-looking
`projected` spend forever -- `materialize` would (correctly) refuse to
recreate the `expected` row, so nothing would ever put it back in `committed`
to keep suppressing it.

**Unconvertible rows: excluded from base-currency sums, always disclosed
(spec §3).** Every aggregate that sums `amount_base` -- `variance`,
`position`, `forecast` -- also returns `unconvertible_count: int` and
`unconvertible_by_currency: dict[str, str]` (deliberately `str` values, not
`Decimal`, matching the pinned interface -- these three functions return
plain `dict[str, Any]`, not a Pydantic model, so nothing downstream
coerces a `Decimal` for us). The two CSV exports do NOT carry these fields:
they are flat per-row dumps, not sums, so an unconvertible expense simply
shows its own `amount_base` as empty in its own row -- there is nothing to
under-report at the row level.

**Division guards return `None`, never `Infinity`, never a raise** --
`variance_pct` (variance() ) and `pct_of_plan` (position()) on a zero
denominator. `avg_monthly_burn`/`projected_year_end` guard the same zero
divisor to `Decimal("0.00")` instead of `None`: a "burn rate" before any
period has elapsed is meaningfully zero, not undefined, mirroring
`subscriptions.reporting.idle_seats`'s zero-seat guard.

**`position()` uses the fiscal calendar, never wall-clock month count**
(spec §7). Elapsed periods come from `periods.period_for_date`, not
`as_of.month - start_month`, so a July-start fiscal year reports correctly
in January (period 7, not a negative number).

**Vendor labels: suppressed together with their id when unresolved (the
Phase-12 `group_id` lesson, `subscriptions/reporting.py`'s pattern).** A
`vendor_id` that fails to resolve against `Vendor.org_id == org_id` --
deleted row, or a cross-org id that should never have reached this org's
data -- is suppressed in BOTH the label and the id field of `export_rows`:
emitting the raw id while blanking the name would still leak another
tenant's identifier into this org's report. `odoo_export_rows` has no id
column to leak in the first place (the pinned contract, spec §7, has no
`vendor_id`) -- an unresolved vendor there is simply `""`.

**Category resolution for `project_costs`'s output (variance/forecast)
fails loudly, never guesses** -- mirrors `feeds.materialize`'s spec §6.1
rule verbatim ("If the org has no active category with that system_key,
[...] fails with a clear validation error naming the missing key"). That
rule is stated at the level of "category assignment resolves by
system_key", not narrowly scoped to materialization, so both `variance()`
and `forecast()` apply it too: a projection this module cannot categorize
raises `BudgetValidationError` rather than silently omitting it (which
would under-report the very figure spec §3 insists must never be quietly
understated) or guessing a category (which spec §6.1 forbids outright).

**Expenses are not partitioned by budget (an acknowledged v1 gap).**
`Expense` has no `budget_id` column -- only `budget_line` does. So
`variance()`/`position()` scope actual/committed/projected to the
requested budget's FISCAL YEAR DATE RANGE, not to "categories this budget
happens to have lines for" -- a category with real spend but no planned
line still shows up (with `planned=0`), which is the useful behavior for
catching unplanned spend. The gap: if an org ever creates two budgets for
the same `fiscal_year` (different `name`s, spec §2's uniqueness allows it),
both would report identical actual/committed/projected figures, differing
only in `planned`. Spec doesn't call for expense-to-budget partitioning in
v1, so this is a deliberate, documented limitation rather than a bug.

**One client at a time (MSP clients plan 2026-10-26 Task 8).** A budget is
one client's, and so are its figures: `variance()`/`position()` count the
budget's client's expenses and projections alone, in that client's fiscal
calendar and base currency. `forecast()` and both exports are one client's by
the caller's choice -- each client has its own base currency, and summing
across clients would mix currencies -- for a client this session can see
(else the 404 of one that does not exist). A client without the budget
module, or archived, has no forecast (empty rows), and its reads seed nothing:
`variance()` and `forecast()` seed default categories only for a client that
uses budget and is not archived (`clients.budget_in_use`), and `variance()`
projects nothing for any other. Its exports still answer: what it recorded
stays exportable.
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import BudgetLine, CostCenter, Expense, ExpenseCategory
from app.modules.budget.budgets import get_budget
from app.modules.budget.clients import budget_in_use, visible_client
from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.feeds import project_costs
from app.modules.budget.fx import convert_for, get_settings
from app.modules.budget.periods import (
    period_bounds,
    period_count,
    period_for_date,
    period_key_for_date,
)
from app.modules.budget.refdata import category_by_system_key, seed_default_categories
from app.modules.licenses.models import Vendor

_TWO_PLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """`numerator / denominator * 100`, quantized to 2dp. `None` on a zero
    denominator -- never `Infinity`, never a raise (spec §7)."""
    if denominator == 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(
        _TWO_PLACES, rounding=ROUND_HALF_UP
    )


def _add_unconvertible(totals: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    """Accumulate one unconvertible row's native-currency amount into the
    per-currency disclosure map (spec §3 -- native currency, since there is
    by definition no base-currency figure for this row)."""
    totals[currency] = totals.get(currency, _ZERO) + amount


def _finalize_unconvertible(totals: dict[str, Decimal]) -> dict[str, str]:
    """`str` values on the way out -- the pinned interface types this
    `dict[str, str]`, not `dict[str, Decimal]` (these functions return a
    plain `dict[str, Any]`, so nothing downstream would coerce it for us)."""
    return {
        currency: str(amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
        for currency, amount in totals.items()
    }


async def _resolve_vendor_names(
    session: AsyncSession, org_id: UUID, vendor_ids: set[UUID]
) -> dict[UUID, str]:
    """One org-scoped lookup for the vendor names of `vendor_ids` (skipped
    entirely when empty). An id that doesn't resolve -- deleted row, or a
    cross-org id -- is simply absent from the returned map; callers must
    suppress the id alongside the missing name (never fall back to the raw
    id), mirroring `subscriptions/reporting.py`'s `_resolve_vendor_names`."""
    if not vendor_ids:
        return {}
    rows = await session.execute(
        select(Vendor.id, Vendor.name).where(Vendor.org_id == org_id, Vendor.id.in_(vendor_ids))
    )
    return {vendor_id: name for vendor_id, name in rows.all()}


async def _resolve_category_names(
    session: AsyncSession, org_id: UUID, category_ids: set[UUID]
) -> dict[UUID, str]:
    """Same shape as `_resolve_vendor_names`, for `ExpenseCategory`."""
    if not category_ids:
        return {}
    rows = await session.execute(
        select(ExpenseCategory.id, ExpenseCategory.name).where(
            ExpenseCategory.org_id == org_id, ExpenseCategory.id.in_(category_ids)
        )
    )
    return {category_id: name for category_id, name in rows.all()}


async def _resolve_cost_center_names(
    session: AsyncSession, org_id: UUID, cost_center_ids: set[UUID]
) -> dict[UUID, str]:
    """Same shape as `_resolve_vendor_names`, for `CostCenter`."""
    if not cost_center_ids:
        return {}
    rows = await session.execute(
        select(CostCenter.id, CostCenter.name).where(
            CostCenter.org_id == org_id, CostCenter.id.in_(cost_center_ids)
        )
    )
    return {cc_id: name for cc_id, name in rows.all()}


async def _resolve_projection_categories(
    session: AsyncSession, org_id: UUID, client_id: UUID, system_keys: set[str]
) -> dict[str, UUID]:
    """Resolve every `system_key` a batch of `ProjectedCost`s needs, UP
    FRONT -- mirrors `feeds.materialize`'s spec §6.1 rule: a projection this
    module cannot categorize raises `BudgetValidationError` naming the
    missing key, rather than being silently dropped (which would
    under-report the very figure spec §3 insists must never be quietly
    understated) or guessing a category (spec §6.1 forbids that outright)."""
    resolved: dict[str, UUID] = {}
    for system_key in system_keys:
        category = await category_by_system_key(session, org_id, system_key, client_id=client_id)
        if category is None:
            raise BudgetValidationError(
                tr(
                    "no active expense category with system_key={system_key}; this report cannot "
                    "categorize this projected cost without one",
                    system_key=repr(system_key),
                )
            )
        resolved[system_key] = category.id
    return resolved


async def _materialized_keys(
    session: AsyncSession, org_id: UUID, client_id: UUID, link_ids: set[UUID]
) -> set[tuple[str, UUID, str]]:
    """The single "is this projected cost already REPRESENTED by an existing
    expense row" guard shared by `variance()` and `forecast()` -- previously
    each implemented its own copy and the two disagreed, which is exactly
    what produced two Critical findings (see module docstring), and even
    after unification the key was briefly too narrow, which produced a third
    (M1, below).

    Keyed on `(link_type, link_id, period_key)`, where `period_key` prefers
    the STORED `Expense.period_key` column and falls back to
    `periods.period_key_for_date(expense_date)` only when that column is
    NULL (I1 fix). Every non-manual (auto-fed) row has a stored `period_key`
    -- `feeds.materialize` always writes it from the same projected date as
    `expense_date` -- so it is read off the column rather than re-derived. A
    MANUAL row's `period_key` column is always NULL (spec §5 gives a caller
    no field to set it directly), so it still falls back to the derived
    value; suppression for a manual row is therefore inherently
    MONTH-based, and moving a manual row across a month boundary changes
    which projection it suppresses.

    Deriving from `expense_date` unconditionally (the pre-fix behavior) was
    wrong for an auto-fed row whose date is later EDITED: `update_expense`
    permits editing `expense_date` on a non-manual row (only `link_id`/
    `link_type` are guarded to `source == "manual"`), but never touches the
    stored `period_key` column. Money scenario: `materialize` writes
    `expected` for subscription S, `expense_date` 2026-03-20, `period_key`
    "2026-03", $12,000; the invoice actually lands April 2 and the operator
    PATCHes `expense_date` -- a routine correction. `variance()`'s
    `committed` column now counts it in period 4 (unaffected -- that loop
    always used `expense.expense_date` directly), but the OLD derived-only
    guard would key this row on ("subscription", S, "2026-04"), so
    ("subscription", S, "2026-03") would miss and the SAME renewal would
    reappear in `projected` for period 3 -- inflating the fiscal-year total
    by $12,000. Reading the stored column keeps the guard key at
    ("subscription", S, "2026-03") regardless of what `expense_date` is
    edited to, so the projection stays suppressed.

    Includes every non-manual (auto-fed) row AND every manual row that
    carries a non-NULL `link_id`, with no `source` filter needed to express
    that: `link_ids` is always a set of non-NULL projection link ids, so
    `Expense.link_id.in_(link_ids)` alone already excludes every manual row
    with no `link_id`. Spec §5 blesses a manual `expected` entry as a
    first-class way to "pre-record a known upcoming" renewal, and Task 9
    made `link_type`/`link_id` settable on a manual row (org-scoped
    resolution) precisely so it can point at the thing it pays for -- such a
    row represents that renewal exactly as much as an auto-fed one does, so
    it must suppress that renewal's projection too. Before this (M1), a
    hand-pre-recorded renewal was double-counted: it showed up in BOTH
    `committed` (the manual row itself) and `projected` (the guard, keyed on
    `source`, could never match it, since a manual row's `source` is always
    the literal `"manual"`, never `"subscription"`/`"license"`).

    Carries NO status filter (mirrors `feeds.py`'s own materialization
    pre-check verbatim): a void row still counts as "represented" here, or a
    voided auto-fed cost would resurrect as `projected` forever (spec §2).

    **Deliberately BROADER than `feeds.py`'s own `uq_expense_client_autofeed`**
    (`(org_id, client_id, source, link_id, period_key) WHERE source <> 'manual'`) --
    the two answer DIFFERENT questions and must not be "fixed" back into
    agreement. `feeds.materialize`'s pre-check asks "does an
    AUTO-MATERIALIZED row already own this slot" (it only ever needs to know
    that, to decide whether writing a new auto-fed row would violate the
    partial unique index -- a manual row is never a candidate for THAT
    question, so keying on `source` is correct there). This guard asks "is
    this projected cost already represented by SOME expense row, auto-fed or
    hand-entered" -- a strictly broader question -- so it keys on
    `link_type` instead, which both kinds of row can carry meaningfully. Do
    not narrow this back to `source` or to `feeds.py`'s key: that is exactly
    what produced M1.

    Only `client_id`'s expense rows count (plan 2026-10-26 Task 8): the
    projections asked about are that client's, and so is what represents
    them."""
    if not link_ids:
        return set()
    rows = await session.execute(
        select(
            Expense.link_type, Expense.link_id, Expense.expense_date, Expense.period_key
        ).where(
            Expense.org_id == org_id,
            Expense.client_id == client_id,
            Expense.link_id.in_(link_ids),
        )
    )
    return {
        (
            r.link_type,
            r.link_id,
            r.period_key if r.period_key is not None else period_key_for_date(r.expense_date),
        )
        for r in rows.all()
        if r.link_type is not None
    }


def _add_months_end(d: date, months: int) -> date:
    """The last calendar day of the month `months` months after `d`.
    Deliberately NOT `periods._add_months` (that helper is private and
    fiscal-year-framed); `forecast()`'s window is a plain calendar walk
    forward from wall-clock "today", unrelated to any budget's fiscal
    year."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, calendar.monthrange(year, month)[1])


# ---------------------------------------------------------------------------
# variance
# ---------------------------------------------------------------------------


async def variance(
    session: AsyncSession, org_id: UUID, budget_id: UUID, actor_id: UUID
) -> dict[str, Any]:
    """Per category x period x optional cost center: `planned`, `actual`,
    `committed`, `projected`, `variance = planned - actual`, `variance_pct`
    (spec §7). Rows are the UNION of every key any of the four columns is
    nonzero for -- a category with real spend but no planned line still
    appears (`planned=0`), which is what makes this useful for catching
    unplanned spend; see the module docstring's "not partitioned by budget"
    note for the corresponding limitation.

    `actor_id` is used only to lazily seed the client's default categories
    (C1 fix) if they don't exist yet -- there is no bootstrap hook anywhere
    in the platform that does this ahead of time, so without it this
    function 400s via `_resolve_projection_categories` for any client with a
    subscription or license renewing in the requested range.

    The budget's client is the report's (module docstring): its expenses,
    its projections, its fiscal calendar and base currency. For a client
    that does not use budget (it was turned off after the budget was made),
    or that is archived, nothing is seeded and nothing is projected
    (`budget_in_use`): that client has no forecast, and its report is the
    lines and expenses it already has.
    """
    budget = await get_budget(session, org_id, budget_id)  # 404 if cross-org/missing
    client_id = budget.client_id
    budgeted = await budget_in_use(session, org_id, client_id)
    if budgeted:
        await seed_default_categories(session, org_id, actor_id, client_id=client_id)
    settings = await get_settings(session, org_id, client_id)
    count = period_count(budget.granularity)
    year_start, _ = period_bounds(
        budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, 1
    )
    _, year_end = period_bounds(
        budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, count
    )

    Key = tuple[UUID, UUID | None, int]
    planned: dict[Key, Decimal] = {}
    actual: dict[Key, Decimal] = {}
    committed: dict[Key, Decimal] = {}
    projected: dict[Key, Decimal] = {}
    unconvertible_count = 0
    unconvertible_totals: dict[str, Decimal] = {}

    lines = (
        (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.org_id == org_id, BudgetLine.budget_id == budget_id
                )
            )
        )
        .scalars()
        .all()
    )
    for line in lines:
        key = (line.category_id, line.cost_center_id, line.period_index)
        planned[key] = planned.get(key, _ZERO) + line.planned_amount

    expenses = (
        (
            await session.execute(
                select(Expense).where(
                    Expense.org_id == org_id,
                    Expense.client_id == client_id,
                    Expense.expense_date >= year_start,
                    Expense.expense_date <= year_end,
                    Expense.status != "void",
                )
            )
        )
        .scalars()
        .all()
    )
    for expense in expenses:
        idx = period_for_date(
            budget.fiscal_year,
            settings.fiscal_year_start_month,
            budget.granularity,
            expense.expense_date,
        )
        if idx is None:  # defensive: bounded by the query above, never expected
            continue
        key = (expense.category_id, expense.cost_center_id, idx)
        if expense.status == "confirmed":
            if expense.amount_base is not None:
                actual[key] = actual.get(key, _ZERO) + expense.amount_base
            else:
                unconvertible_count += 1
                _add_unconvertible(unconvertible_totals, expense.currency, expense.amount)
        elif expense.status == "expected":
            if expense.amount_base is not None:
                committed[key] = committed.get(key, _ZERO) + expense.amount_base
            else:
                unconvertible_count += 1
                _add_unconvertible(unconvertible_totals, expense.currency, expense.amount)

    projections = (
        await project_costs(
            session, org_id, client_id=client_id, from_date=year_start, to_date=year_end
        )
        if budgeted
        else []
    )
    category_by_key = await _resolve_projection_categories(
        session, org_id, client_id, {p.category_system_key for p in projections}
    )
    materialized_keys = await _materialized_keys(
        session, org_id, client_id, {p.link_id for p in projections}
    )
    for proj in projections:
        idx = period_for_date(
            budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, proj.on_date
        )
        if idx is None:  # defensive: project_costs was bounded to [year_start, year_end]
            continue
        category_id = category_by_key[proj.category_system_key]
        if (proj.link_type, proj.link_id, proj.period_key) in materialized_keys:
            continue  # the double-count guard: already represented, never also projected
        _, amount_base = await convert_for(
            session, org_id, proj.amount, proj.currency, proj.on_date, client_id=client_id
        )
        key = (category_id, None, idx)
        if amount_base is not None:
            projected[key] = projected.get(key, _ZERO) + amount_base
        else:
            unconvertible_count += 1
            _add_unconvertible(unconvertible_totals, proj.currency, proj.amount)

    all_keys = set(planned) | set(actual) | set(committed) | set(projected)
    rows: list[dict[str, Any]] = []
    for category_id, cost_center_id, period_index in all_keys:
        key = (category_id, cost_center_id, period_index)
        planned_amt = planned.get(key, _ZERO)
        actual_amt = actual.get(key, _ZERO)
        committed_amt = committed.get(key, _ZERO)
        projected_amt = projected.get(key, _ZERO)
        variance_amt = planned_amt - actual_amt
        rows.append(
            {
                "category_id": category_id,
                "cost_center_id": cost_center_id,
                "period_index": period_index,
                "planned": planned_amt,
                "actual": actual_amt,
                "committed": committed_amt,
                "projected": projected_amt,
                "variance": variance_amt,
                "variance_pct": _pct(variance_amt, planned_amt),
            }
        )
    rows.sort(key=lambda r: (r["period_index"], str(r["category_id"]), str(r["cost_center_id"])))

    return {
        "rows": rows,
        "unconvertible_count": unconvertible_count,
        "unconvertible_by_currency": _finalize_unconvertible(unconvertible_totals),
    }


# ---------------------------------------------------------------------------
# position
# ---------------------------------------------------------------------------


async def position(
    session: AsyncSession, org_id: UUID, budget_id: UUID, *, as_of: date
) -> dict[str, Any]:
    """Year-to-date burn against plan (spec §7): `ytd_actual`, `pct_of_plan`
    (`None` on a zero-plan denominator), `avg_monthly_burn` over elapsed
    MONTHS -- `elapsed_periods` converted to months via `12 // period_count`,
    since `elapsed_periods` counts fiscal PERIODS (a period is a quarter on
    a `granularity='quarter'` budget, and `avg_monthly_burn` would otherwise
    overstate a quarterly budget's monthly rate 3x) -- and a straight-line
    `projected_year_end`. `elapsed_periods` itself still comes from the
    FISCAL calendar via `periods.period_for_date`, never wall-clock month
    count. The budget's client's confirmed expenses alone, in its fiscal
    calendar (module docstring).
    """
    budget = await get_budget(session, org_id, budget_id)  # 404 if cross-org/missing
    settings = await get_settings(session, org_id, budget.client_id)
    count = period_count(budget.granularity)
    year_start, _ = period_bounds(
        budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, 1
    )
    _, year_end = period_bounds(
        budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, count
    )

    if as_of < year_start:
        elapsed_periods = 0
    elif as_of > year_end:
        elapsed_periods = count
    else:
        idx = period_for_date(
            budget.fiscal_year, settings.fiscal_year_start_month, budget.granularity, as_of
        )
        assert idx is not None  # narrowed: as_of is within [year_start, year_end] here
        elapsed_periods = idx

    ytd_actual = _ZERO
    unconvertible_count = 0
    unconvertible_totals: dict[str, Decimal] = {}
    ytd_end = min(as_of, year_end)
    if as_of >= year_start:
        expenses = (
            (
                await session.execute(
                    select(Expense).where(
                        Expense.org_id == org_id,
                        Expense.client_id == budget.client_id,
                        Expense.status == "confirmed",
                        Expense.expense_date >= year_start,
                        Expense.expense_date <= ytd_end,
                    )
                )
            )
            .scalars()
            .all()
        )
        for expense in expenses:
            if expense.amount_base is not None:
                ytd_actual += expense.amount_base
            else:
                unconvertible_count += 1
                _add_unconvertible(unconvertible_totals, expense.currency, expense.amount)

    total_planned = (
        await session.execute(
            select(BudgetLine.planned_amount).where(
                BudgetLine.org_id == org_id, BudgetLine.budget_id == budget_id
            )
        )
    ).scalars().all()
    plan_total = sum(total_planned, _ZERO)

    # `avg_monthly_burn` is published as a MONTHLY figure regardless of the
    # budget's granularity, so it must divide by elapsed MONTHS, never
    # elapsed PERIODS -- on a quarterly budget a period is a quarter, and
    # dividing by periods there overstates the monthly rate 3x. `12 //
    # count` is the number of calendar months in one period of this
    # granularity (1 for monthly, 3 for quarterly).
    #
    # M2: `projected_year_end` is computed from the UNROUNDED quotient
    # (`avg_monthly_burn_raw`), quantizing ONLY ONCE, at the very end -- the
    # same "round once, at the end" rule `fx.convert` follows. Multiplying
    # the ALREADY-quantized `avg_monthly_burn` by 12 (the previous bug)
    # amplifies that one rounding step 12x; on a quarterly budget the
    # published `avg_monthly_burn` is already itself a 3x-amplified quotient
    # (`ytd_actual / elapsed_months`, `elapsed_months` a third of
    # `elapsed_periods`' wall-clock span), so quantizing THAT before
    # multiplying by 12 amplifies the single rounding step 3x on top --
    # $100/3 quarterly rounds to $33.33, and $33.33 * 12 = $399.96, three
    # cents short of the true $400.00 a reader would hand-compute.
    elapsed_months = elapsed_periods * (12 // count)
    if elapsed_months > 0:
        avg_monthly_burn_raw = ytd_actual / elapsed_months
        avg_monthly_burn = avg_monthly_burn_raw.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        projected_year_end = (avg_monthly_burn_raw * 12).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )
    else:
        avg_monthly_burn = _ZERO
        projected_year_end = _ZERO

    return {
        "ytd_actual": ytd_actual,
        "pct_of_plan": _pct(ytd_actual, plan_total),
        "avg_monthly_burn": avg_monthly_burn,
        "elapsed_periods": elapsed_periods,
        "projected_year_end": projected_year_end,
        "unconvertible_count": unconvertible_count,
        "unconvertible_by_currency": _finalize_unconvertible(unconvertible_totals),
    }


# ---------------------------------------------------------------------------
# forecast (one client's, not scoped to a single budget -- spec §6.2)
# ---------------------------------------------------------------------------


async def forecast(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, client_id: UUID, months: int
) -> dict[str, Any]:
    """Read-time projection over Subscriptions/Licenses for the next
    `months` calendar months from today, INCLUDING the current one (spec
    §6.2, controller decision: "N months" means N calendar months total, not
    N months plus whatever remains of the current one) -- reuses
    `feeds.project_costs`, never reimplements the projection. Each row
    carries `materialized: bool` so a caller can separate "already booked"
    from "still pure projection" without summing the two into one
    ambiguous figure (spec §6.2).

    `actor_id` is used only to lazily seed the client's default categories
    (C1 fix), same rationale as `variance()`.

    `client_id`'s subscriptions and licenses, in its base currency: a client
    this session can see (else the 404 of one that does not exist). A client
    without the budget module, or archived, has no forecast -- empty rows,
    and nothing seeded (`budget_in_use`; a read is never a 409)."""
    if months < 1:
        raise BudgetValidationError(tr("months must be >= 1"))
    await visible_client(session, org_id, client_id)

    out_rows: list[dict[str, Any]] = []
    unconvertible_count = 0
    unconvertible_totals: dict[str, Decimal] = {}
    if not await budget_in_use(session, org_id, client_id):
        return {"rows": out_rows, "unconvertible_count": 0, "unconvertible_by_currency": {}}

    await seed_default_categories(session, org_id, actor_id, client_id=client_id)

    today = date.today()
    to_date = _add_months_end(today, months - 1)
    projections = await project_costs(
        session, org_id, client_id=client_id, from_date=today, to_date=to_date
    )
    category_by_key = await _resolve_projection_categories(
        session, org_id, client_id, {p.category_system_key for p in projections}
    )

    materialized_keys = await _materialized_keys(
        session, org_id, client_id, {p.link_id for p in projections}
    )

    for proj in projections:
        _, amount_base = await convert_for(
            session, org_id, proj.amount, proj.currency, proj.on_date, client_id=client_id
        )
        if amount_base is None:
            unconvertible_count += 1
            _add_unconvertible(unconvertible_totals, proj.currency, proj.amount)
        out_rows.append(
            {
                "category_id": category_by_key[proj.category_system_key],
                "period_key": proj.period_key,
                "amount": proj.amount,
                "currency": proj.currency,
                "amount_base": amount_base,
                "materialized": (
                    (proj.link_type, proj.link_id, proj.period_key) in materialized_keys
                ),
            }
        )
    out_rows.sort(key=lambda r: (r["period_key"], str(r["category_id"])))

    return {
        "rows": out_rows,
        "unconvertible_count": unconvertible_count,
        "unconvertible_by_currency": _finalize_unconvertible(unconvertible_totals),
    }


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------


async def _export_client(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """Both exports are one client's (module docstring): a client this
    session can see (else the 404 of one that does not exist), whether or
    not it still uses budget, and archived or not. A client that turned the
    module off keeps the expenses it recorded -- its lists, `variance()` and
    `position()` still show them -- and the export is how that history
    leaves (Task 8 review, controller's decision). No module check, so no
    lock either: the export only reads, and the route's audit row names the
    client without a foreign key, so it never waits on an archive."""
    await visible_client(session, org_id, client_id)


async def _export_expenses(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    date_from: date | None,
    date_to: date | None,
    include_unconfirmed: bool = True,
) -> list[Expense]:
    """Shared query for both exports: `client_id`'s expenses, non-void,
    optionally date-bounded, ordered deterministically (spec: "any
    list-equality assertion... needs an explicit ORDER BY").

    `include_unconfirmed` (I2 fix) defaults to `True`, preserving
    `export_rows`' existing behavior (every non-void row, `expected` or
    `confirmed` -- it is self-describing: it already carries `status` as a
    column, per spec). `odoo_export_rows` passes `False` by default: its
    pinned column contract has NO `status` column at all, so an `expected`
    row (a projected/materialized cost nobody has confirmed) would reach the
    ERP indistinguishable from a real, asserted cost -- exactly the failure
    spec §0.3's confirm/void split exists to prevent, one layer up."""
    stmt = select(Expense).where(
        Expense.org_id == org_id, Expense.client_id == client_id, Expense.status != "void"
    )
    if not include_unconfirmed:
        stmt = stmt.where(Expense.status == "confirmed")
    if date_from is not None:
        stmt = stmt.where(Expense.expense_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.expense_date <= date_to)
    stmt = stmt.order_by(Expense.expense_date, Expense.id)
    return list((await session.execute(stmt)).scalars().all())


async def export_rows(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    """Flat, org-scoped expense rows for a general-purpose CSV export (spec
    §7). Money fields are stringified here -- Decimal is only the internal
    working type (module docstring); this is "the wire". Vendor/category/
    cost-center names are resolved for readability; an id that fails to
    resolve for THIS org is suppressed together with its name (never just
    the name -- see module docstring's Phase-12 lesson) rather than leaking
    the raw id. One client's rows (`_export_client`)."""
    await _export_client(session, org_id, client_id)
    expenses = await _export_expenses(
        session, org_id, client_id=client_id, date_from=date_from, date_to=date_to
    )

    vendor_ids = {e.vendor_id for e in expenses if e.vendor_id is not None}
    category_ids = {e.category_id for e in expenses}
    cost_center_ids = {e.cost_center_id for e in expenses if e.cost_center_id is not None}
    vendor_names = await _resolve_vendor_names(session, org_id, vendor_ids)
    category_names = await _resolve_category_names(session, org_id, category_ids)
    cost_center_names = await _resolve_cost_center_names(session, org_id, cost_center_ids)

    rows: list[dict[str, Any]] = []
    for e in expenses:
        vendor_resolved = e.vendor_id is not None and e.vendor_id in vendor_names
        category_resolved = e.category_id in category_names
        cc_resolved = e.cost_center_id is not None and e.cost_center_id in cost_center_names
        rows.append(
            {
                "id": e.id,
                "expense_date": e.expense_date,
                "amount": str(e.amount),
                "currency": e.currency,
                "fx_rate": str(e.fx_rate) if e.fx_rate is not None else None,
                "amount_base": str(e.amount_base) if e.amount_base is not None else None,
                "vendor_id": e.vendor_id if vendor_resolved else None,
                "vendor": vendor_names[e.vendor_id] if vendor_resolved and e.vendor_id else None,
                "category_id": e.category_id if category_resolved else None,
                "category": category_names[e.category_id] if category_resolved else None,
                "cost_center_id": e.cost_center_id if cc_resolved else None,
                "cost_center": (
                    cost_center_names[e.cost_center_id]
                    if cc_resolved and e.cost_center_id
                    else None
                ),
                "description": e.description,
                "source": e.source,
                "link_type": e.link_type,
                "link_id": e.link_id,
                "period_key": e.period_key,
                "invoice_ref": e.invoice_ref,
                "status": e.status,
                "entered_by": e.entered_by,
                "created_at": e.created_at,
                "confirmed_by": e.confirmed_by,
                "confirmed_at": e.confirmed_at,
            }
        )
    return rows


async def odoo_export_rows(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    date_from: date | None = None,
    date_to: date | None = None,
    include_unconfirmed: bool = False,
) -> list[dict[str, Any]]:
    """The Odoo CSV variant -- the PINNED column contract (spec §7,
    verbatim, exact order): `date, reference, vendor, category, cost_center,
    description, amount, currency, amount_base, base_currency, link_type,
    link_id`. No `vendor_id`/`category_id`/`cost_center_id`/`org_id` columns
    exist in this contract, so there is no raw id to leak in the first
    place; an unresolved vendor/category/cost-center is simply `""`.

    `include_unconfirmed=False` by default (I2 fix): this is the export the
    README tells you to point an ERP import job at, and the pinned contract
    has no `status` column, so an `expected` row -- a projected/materialized
    cost nobody has confirmed, potentially at a projected (not actual)
    amount -- would book into Odoo indistinguishable from a real cost. Money
    scenario: an org materializes Jan-Dec (12 `expected` rows per monthly
    subscription at PROJECTED amounts) and exports to Odoo at month end --
    without this default, Odoo books 12 months of SaaS as real cost for 11
    renewals that have not happened yet, at amounts nobody confirmed. Pass
    `include_unconfirmed=True` as an explicit opt-in for a caller that
    genuinely wants everything. One client's rows, in its base currency
    (`_export_client`)."""
    await _export_client(session, org_id, client_id)
    settings = await get_settings(session, org_id, client_id)
    expenses = await _export_expenses(
        session,
        org_id,
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        include_unconfirmed=include_unconfirmed,
    )

    vendor_ids = {e.vendor_id for e in expenses if e.vendor_id is not None}
    category_ids = {e.category_id for e in expenses}
    cost_center_ids = {e.cost_center_id for e in expenses if e.cost_center_id is not None}
    vendor_names = await _resolve_vendor_names(session, org_id, vendor_ids)
    category_names = await _resolve_category_names(session, org_id, category_ids)
    cost_center_names = await _resolve_cost_center_names(session, org_id, cost_center_ids)

    rows: list[dict[str, Any]] = []
    for e in expenses:
        rows.append(
            {
                "date": e.expense_date.isoformat(),
                "reference": e.invoice_ref or "",
                "vendor": (vendor_names.get(e.vendor_id, "") if e.vendor_id is not None else ""),
                "category": category_names.get(e.category_id, ""),
                "cost_center": (
                    cost_center_names.get(e.cost_center_id, "")
                    if e.cost_center_id is not None
                    else ""
                ),
                "description": e.description,
                "amount": str(e.amount),
                "currency": e.currency,
                "amount_base": str(e.amount_base) if e.amount_base is not None else "",
                "base_currency": settings.base_currency,
                "link_type": e.link_type or "",
                "link_id": str(e.link_id) if e.link_id is not None else "",
            }
        )
    return rows

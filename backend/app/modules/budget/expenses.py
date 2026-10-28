"""Expense lifecycle: create, list, update, confirm, void
(docs/modules/budget/spec.md §3, §5, §8).

Mirrors `budgets.py`/`refdata.py`/`rates.py`'s style: org-scoped load + audit
+ flush for every mutation (invariant #4), an explicit `org_id` predicate on
every query (invariant #5), and a typed `BudgetValidationError`/
`BudgetNotFoundError` pre-check in place of letting a DB constraint or an
unresolved cross-ref surface as a 500.

**Ordering (the load-bearing rule for this file).** `AsyncSession` defaults
to `autoflush=True` and nothing overrides it (`app.core.tenancy`); `fx.py`'s
`get_settings` runs a SELECT, which autoflushes every pending object on the
session. `create_expense` therefore resolves every cross-reference AND
computes the FX conversion (`fx.convert_for`) BEFORE constructing or adding
the `Expense` ORM object -- never `session.add(expense)` first. The same
ordering applies inside `update_expense`/`confirm_expense`: conversion is
computed before any `setattr` on the already-tracked `Expense`, so a
`convert_for` overflow (see below) never has to unwind a mutation that
already happened.

**Two failure modes, not one.** `fx.convert_for` returns `(None, None)` when
no rate resolves as of the relevant date -- spec §0.1 forbids ever guessing a
rate, so that is NOT an error: the row is created/updated with
`amount_base = NULL` and is reported (by the caller) as unconvertible. It
RAISES `BudgetValidationError` when the converted result would overflow
`Numeric(14,2)`. The two are never collapsed into one handling path here.

**Audit metadata never carries amounts (spec §8).** Amounts are the
sensitive payload in this module. `expense.updated`'s metadata is
`{"changed_fields": [...]}` -- field NAMES only. `expense.created`,
`expense.confirmed`, and `expense.voided` metadata is likewise built only
from non-monetary identifiers (category/cost-center/vendor ids, booleans),
never `amount`/`amount_base`/`fx_rate`.

**PATCH semantics (the Phase-12 lesson).** `update_expense` receives a plain
`dict` (the route layer passes `ExpenseUpdate.model_dump(exclude_unset=True)`)
-- a key's PRESENCE, not its value, decides whether a field is touched. An
explicit `None` on a NON-NULLABLE column
(`NON_NULLABLE_EXPENSE_FIELDS`) is rejected in a pass that runs BEFORE any
`setattr`, so the ORM object is never left partially mutated when that
rejection raises.

**An expense belongs to ONE client (MSP clients plan 2026-10-26 Task 8).** It
is created for the client the caller names
(`clients.client_for_new_budget_rows`), converted into THAT client's base
currency, and never changes client. Every other write loads it and calls
`require_budget` with its client before anything else (`_expense_for_write`).
What it points at is its client's: a category or cost center of another
client is a 422 (the database holds the same rule,
`fk_expense_*_client`), and so is a linked subscription, license, device or
change of another client (`_resolve_link`; `link_id` has no foreign key).
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire_words import say
from app.models import (
    ChangeRequest,
    Device,
    Expense,
    License,
    Subscription,
)
from app.modules.budget.clients import client_for_new_budget_rows, require_budget, same_client
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.fx import convert_for
from app.modules.budget.refdata import category_of_client, cost_center_of_client
from app.modules.budget.schema import ExpenseCreate
from app.modules.budget.words import (
    EXPENSE_SOURCE_WORDS,
    EXPENSE_STATUS_WORDS,
    LINK_TYPE_WORDS,
)
from app.modules.licenses.models import Vendor

# update_expense's editable-field set, decided by key PRESENCE in `fields`
# (the refdata.py/rates.py/budgets.py convention -- callers pass
# `ExpenseUpdate.model_dump(exclude_unset=True)`). `status` is deliberately
# absent: status only ever moves through `confirm_expense`/`void_expense`,
# each of which is its own audited action, never a side effect of a generic
# PATCH.
EXPENSE_EDITABLE_FIELDS: tuple[str, ...] = (
    "expense_date",
    "amount",
    "currency",
    "category_id",
    "cost_center_id",
    "vendor_id",
    "description",
    "invoice_ref",
    "link_type",
    "link_id",
)

# NOT NULL columns on `expense` that are also in `EXPENSE_EDITABLE_FIELDS`.
# `description` is `Text` with a Python-side default of `""`, not a nullable
# column (`Mapped[str]`, not `Mapped[str | None]`) -- an explicit `null` here
# would otherwise reach Postgres as a NOT NULL violation and surface as a
# 500. `cost_center_id`/`vendor_id`/`invoice_ref`/`link_type`/`link_id` are
# genuinely nullable and are deliberately absent: an explicit `null` there
# is a caller's request to CLEAR the field, and must actually clear it.
NON_NULLABLE_EXPENSE_FIELDS: frozenset[str] = frozenset(
    {"expense_date", "amount", "currency", "category_id", "description"}
)

# The subset of EXPENSE_EDITABLE_FIELDS that feeds FX conversion (spec §5:
# "on a confirmed row an edit is permitted but re-runs conversion"). Presence
# of ANY of these three in a PATCH re-runs `convert_for`, even for an
# `expected` row -- an edit to the recorded amount/currency/date must never
# leave a stale `fx_rate`/`amount_base` behind.
_CONVERSION_FIELDS = frozenset({"amount", "currency", "expense_date"})


def _reject_null_for_non_nullable(
    fields: dict[str, Any], non_nullable: frozenset[str]
) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `refdata.py`/`rates.py`/`budgets.py` pattern).
    Runs BEFORE any `setattr` so the ORM object is never left partially
    mutated when this raises."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise BudgetValidationError(tr("{field} cannot be null", field=field))


def reject_if_void(expense: Expense) -> None:
    """Void is terminal (spec §5: "reversible only by creating a new entry").
    Shared by `update_expense` and the attachment-upload route (P4 of the
    post-review controller fix, .git/sdd/task-12-report.md) so the condition
    is defined in exactly one place, never duplicated."""
    if expense.status == "void":
        raise BudgetValidationError(
            tr("expense {expense_id} is void and cannot be edited", expense_id=expense.id)
        )


async def _resolve_vendor(session: AsyncSession, org_id: UUID, vendor_id: UUID) -> Vendor:
    """Org-scoped load. A vendor id from another org is indistinguishable
    from a nonexistent one -- both raise `BudgetNotFoundError` (spec §8,
    invariant #5), matching `refdata.resolve_category`/`resolve_cost_center`."""
    vendor = (
        await session.execute(
            select(Vendor).where(Vendor.org_id == org_id, Vendor.id == vendor_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise BudgetNotFoundError(tr("vendor {vendor_id} not found", vendor_id=vendor_id))
    return vendor


# `link_id` has no ForeignKey by design (schema.LinkType names four
# unrelated tables), so this dispatch table is the only backstop for it
# (spec §8: "Every cross-reference ... including the advisory link_id -- is
# resolved WHERE org_id AND id"). Every model here carries a bare `org_id`
# column, matching `_resolve_vendor`/`refdata.resolve_category`, and a NOT
# NULL `client_id` (plan 2026-10-26 Tasks 6 and 7, and phase 1's devices).
_LINK_TYPE_MODELS: dict[str, type[Any]] = {
    "subscription": Subscription,
    "license": License,
    "device": Device,
    "change": ChangeRequest,
}


async def _resolve_link(
    session: AsyncSession, org_id: UUID, client_id: UUID, link_type: str, link_id: UUID
) -> UUID:
    """Org-scoped existence check for `link_id`, dispatched on `link_type`,
    held to the expense's client. A cross-org or nonexistent id is
    indistinguishable -- both raise `BudgetNotFoundError` (spec §8, invariant
    #5), and so is one of a client this session cannot see. One of another
    client of the org is a `BudgetClientError` (422): X's expense paying for
    Y's subscription would put Y's renewal in X's budget."""
    model = _LINK_TYPE_MODELS[link_type]
    resolved = (
        await session.execute(
            select(model.id, model.client_id).where(model.org_id == org_id, model.id == link_id)
        )
    ).one_or_none()
    if resolved is None:
        raise BudgetNotFoundError(
            tr(
                "{link_type} {link_id} not found",
                link_type=say(LINK_TYPE_WORDS, link_type),
                link_id=link_id,
            )
        )
    same_client(
        resolved.client_id,
        client_id,
        tr(
            "{link_type} {link_id} belongs to another client",
            link_type=say(LINK_TYPE_WORDS, link_type),
            link_id=link_id,
        ),
    )
    resolved_id: UUID = resolved.id
    return resolved_id


# ---------------------------------------------------------------------------
# create_expense
# ---------------------------------------------------------------------------


async def create_expense(
    session: AsyncSession, org_id: UUID, actor_id: UUID, data: ExpenseCreate
) -> Expense:
    """Manual entry (spec §5). The caller (via `data.status`) chooses
    `confirmed` (recording a real cost) or `expected` (pre-recording a known
    upcoming one); this always sets `status` explicitly -- the schema-level
    DB column default of `"expected"` is a lower-level safety floor for any
    insert that bypasses this service, never the mechanism used here. A
    `"confirmed"` result stamps `confirmed_by`/`confirmed_at` with this call's
    actor/time -- there is no path that confirms without a user (spec §0.3).

    The expense is `data.client_id`'s (`client_for_new_budget_rows`: a
    client the caller may file under, that uses budget -- checked first,
    before anything else). Resolves `category_id`/`cost_center_id`/
    `vendor_id`/`link_id` org-scoped, holds each to that client, and runs the
    FX conversion into that client's base currency BEFORE constructing the
    `Expense` -- never
    `session.add` first -- so `convert_for`'s autoflush-triggering SELECT
    (`fx.get_settings`) never flushes a half-built row, and its overflow
    guard (`BudgetValidationError`) never has to unwind one either.
    `convert_for` returning `(None, None)` is not an error: the row is still
    created, with `amount_base = NULL` (spec §0.1 -- never guess a rate)."""
    client_id = data.client_id
    await client_for_new_budget_rows(session, org_id, client_id)
    category = await category_of_client(session, org_id, client_id, data.category_id)

    cost_center_id: UUID | None = None
    if data.cost_center_id is not None:
        cost_center = await cost_center_of_client(session, org_id, client_id, data.cost_center_id)
        cost_center_id = cost_center.id

    vendor_id: UUID | None = None
    if data.vendor_id is not None:
        vendor = await _resolve_vendor(session, org_id, data.vendor_id)
        vendor_id = vendor.id

    # The advisory link_id (spec §8) -- resolved org-scoped, dispatched on
    # link_type. `link_id` without a `link_type` can't be dispatched to a
    # table at all, so it is rejected rather than silently stored unresolved.
    link_id: UUID | None = None
    if data.link_id is not None:
        if data.link_type is None:
            raise BudgetValidationError(tr("link_id requires link_type"))
        link_id = await _resolve_link(session, org_id, client_id, data.link_type, data.link_id)

    # Conversion computed BEFORE the ORM object exists -- ordering rule #1.
    fx_rate, amount_base = await convert_for(
        session, org_id, data.amount, data.currency, data.expense_date, client_id=client_id
    )

    # spec §0.3/§5: "there is no path that confirms without a user." A POST
    # that asks for `status="confirmed"` outright is such a path unless
    # stamped here -- `confirm_expense` is not the only door into the
    # `confirmed` state. (The schema defaults to "expected", so this branch
    # is only ever reached by an explicit request.)
    confirmed_by = actor_id if data.status == "confirmed" else None
    confirmed_at = datetime.now(UTC) if data.status == "confirmed" else None

    expense = Expense(
        org_id=org_id,
        client_id=client_id,
        expense_date=data.expense_date,
        amount=data.amount,
        currency=data.currency,
        fx_rate=fx_rate,
        amount_base=amount_base,
        vendor_id=vendor_id,
        category_id=category.id,
        cost_center_id=cost_center_id,
        description=data.description,
        source="manual",
        invoice_ref=data.invoice_ref,
        link_type=data.link_type,
        link_id=link_id,
        status=data.status,
        entered_by=actor_id,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
    )
    session.add(expense)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="expense.created",
        target_type="expense",
        target_id=str(expense.id),
        client_id=client_id,
        metadata={
            "category_id": str(category.id),
            "cost_center_id": str(cost_center_id) if cost_center_id else None,
            "vendor_id": str(vendor_id) if vendor_id else None,
            "source": "manual",
            "status": expense.status,
        },
    )
    await session.flush()
    return expense


# ---------------------------------------------------------------------------
# list_expenses / get_expense
# ---------------------------------------------------------------------------


async def list_expenses(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID | None = None,
    status: str | None = None,
    category_id: UUID | None = None,
    cost_center_id: UUID | None = None,
    vendor_id: UUID | None = None,
    source: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Expense]:
    """Filterable expense list (spec §7): every visible client's, or
    `client_id`'s alone. Explicit ORDER BY -- newest first -- so a caller
    asserting list equality depends on that, never on incidental
    insertion/scan order."""
    stmt = select(Expense).where(Expense.org_id == org_id)
    if client_id is not None:
        stmt = stmt.where(Expense.client_id == client_id)
    if status is not None:
        stmt = stmt.where(Expense.status == status)
    if category_id is not None:
        stmt = stmt.where(Expense.category_id == category_id)
    if cost_center_id is not None:
        stmt = stmt.where(Expense.cost_center_id == cost_center_id)
    if vendor_id is not None:
        stmt = stmt.where(Expense.vendor_id == vendor_id)
    if source is not None:
        stmt = stmt.where(Expense.source == source)
    if date_from is not None:
        stmt = stmt.where(Expense.expense_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.expense_date <= date_to)
    stmt = stmt.order_by(Expense.expense_date.desc(), Expense.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_expense(session: AsyncSession, org_id: UUID, expense_id: UUID) -> Expense:
    """Org-scoped load. An expense id from another org is indistinguishable
    from a nonexistent one -- both raise `BudgetNotFoundError` (spec §8,
    invariant #5)."""
    expense = (
        await session.execute(
            select(Expense).where(Expense.org_id == org_id, Expense.id == expense_id)
        )
    ).scalar_one_or_none()
    if expense is None:
        raise BudgetNotFoundError(tr("expense {expense_id} not found", expense_id=expense_id))
    return expense


async def expense_for_write(session: AsyncSession, org_id: UUID, expense_id: UUID) -> Expense:
    """The expense a write is about, of a client that uses budget: an expense
    this session cannot see is the 404 it always was, and one whose client
    has the module off is `ModuleNotEnabled` (404) -- before anything is
    written. Also the attachment upload's door (`api.py`)."""
    expense = await get_expense(session, org_id, expense_id)
    await require_budget(session, org_id, expense.client_id)
    return expense


# ---------------------------------------------------------------------------
# update_expense
# ---------------------------------------------------------------------------


async def update_expense(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    expense_id: UUID,
    fields: dict[str, Any],
) -> Expense:
    """Apply the SUPPLIED fields to an expense, org-scoped-loaded (spec §5).

    Passes, deliberately not interleaved -- the same discipline
    `budgets.put_lines` uses, extended with the conversion re-run:

    1. Reject an explicit `null` on any `NON_NULLABLE_EXPENSE_FIELDS` member
       up front -- before the row is even loaded.
    2. Load the row, then reject the edit outright if it is `void` (spec §5:
       void is terminal, reversible only by creating a new entry) or if it
       touches `link_id`/`link_type` on a non-`manual` row (`uq_expense_client_autofeed`
       keys an auto-fed row's identity on `link_id`; only the materializer
       that owns `source` may move it).
    3. Resolve every supplied cross-reference (`category_id`,
       `cost_center_id`, `vendor_id`, `link_id`) org-scoped, and -- if any of
       `amount`/`currency`/`expense_date` was supplied -- recompute the FX
       conversion against the (possibly new) amount/currency/date. `link_id`
       is resolved on the MERGED (`link_type`, `link_id`) pair whenever
       EITHER key is supplied -- not gated on `link_id`'s presence alone --
       so a bare `link_type` PATCH re-validates the row's existing link_id
       against the new type instead of silently re-tagging it (spec §7's
       Odoo export emits `link_type, link_id` verbatim). Nothing is
       `setattr`'d on the tracked `Expense` in this pass, so a cross-org
       reference or a `convert_for` overflow leaves the row untouched.
    4. Apply every resolved value in one pass, tracking which field names
       actually changed (for the audit event -- names only, never values).
    """
    _reject_null_for_non_nullable(fields, NON_NULLABLE_EXPENSE_FIELDS)
    expense = await expense_for_write(session, org_id, expense_id)
    client_id = expense.client_id

    reject_if_void(expense)

    if expense.source != "manual" and ("link_id" in fields or "link_type" in fields):
        raise BudgetValidationError(
            tr(
                "expense {expense_id} is auto-fed (source={source}); link_id/link_type are not "
                "editable",
                expense_id=expense_id,
                source=say(EXPENSE_SOURCE_WORDS, expense.source, quoted=True),
            )
        )

    # Pass 3a: resolve cross-references. `None` on a nullable ref clears it;
    # a supplied id is resolved org-scoped and held to the expense's client.
    resolved: dict[str, Any] = {}
    if "category_id" in fields:
        category = await category_of_client(session, org_id, client_id, fields["category_id"])
        resolved["category_id"] = category.id
    if "cost_center_id" in fields:
        if fields["cost_center_id"] is None:
            resolved["cost_center_id"] = None
        else:
            cost_center = await cost_center_of_client(
                session, org_id, client_id, fields["cost_center_id"]
            )
            resolved["cost_center_id"] = cost_center.id
    if "vendor_id" in fields:
        if fields["vendor_id"] is None:
            resolved["vendor_id"] = None
        else:
            vendor = await _resolve_vendor(session, org_id, fields["vendor_id"])
            resolved["vendor_id"] = vendor.id
    # J3: resolution is keyed on the MERGED (link_type, link_id) pair, not on
    # `link_id`'s presence alone. A bare `{"link_type": "license"}` PATCH on a
    # manual row must not pass through unresolved -- the row's existing
    # link_id was resolved against its OLD type, and re-typing it without
    # re-checking would hand spec §7's Odoo export a `link_type` that no
    # longer matches the table the stored `link_id` actually lives in. A bare
    # `{"link_type": null}` must not strand a non-null link_id with no
    # dispatch type either. So: whenever EITHER key is supplied, resolve the
    # merged pair; if the merged link_id is non-null it is re-resolved
    # against the merged link_type (existing type included), raising
    # `BudgetNotFoundError` if it does not exist there.
    if "link_id" in fields or "link_type" in fields:
        merged_link_id = fields.get("link_id", expense.link_id)
        merged_link_type = fields.get("link_type", expense.link_type)
        if merged_link_id is None:
            resolved["link_id"] = None
        else:
            if merged_link_type is None:
                raise BudgetValidationError(tr("link_id requires a link_type"))
            resolved["link_id"] = await _resolve_link(
                session, org_id, client_id, merged_link_type, merged_link_id
            )
        # Ensure the (re-)resolved link_id is actually applied in pass 4 even
        # when only `link_type` was supplied -- pass 4 only setattr's fields
        # present in `fields`, and a bare link_type PATCH must not leave the
        # newly-validated link_id unapplied. Copy first: `fields` may be the
        # caller's own dict.
        fields = {**fields, "link_id": merged_link_id}

    # Pass 3b: recompute conversion BEFORE any setattr -- ordering rule #1
    # applies here too: `convert_for` -> `fx.get_settings` autoflushes this
    # already-tracked `expense`, so if it were called after mutating
    # amount/currency/expense_date, a subsequent overflow raise would have
    # to unwind an already-flushed partial update.
    new_fx_rate = expense.fx_rate
    new_amount_base = expense.amount_base
    if _CONVERSION_FIELDS & fields.keys():
        conv_amount = fields.get("amount", expense.amount)
        conv_currency = fields.get("currency", expense.currency)
        conv_date = fields.get("expense_date", expense.expense_date)
        new_fx_rate, new_amount_base = await convert_for(
            session, org_id, conv_amount, conv_currency, conv_date, client_id=client_id
        )

    # Pass 4: apply everything.
    changed_fields = []
    for field in EXPENSE_EDITABLE_FIELDS:
        if field not in fields:
            continue
        new_value = resolved.get(field, fields[field])
        if getattr(expense, field) != new_value:
            changed_fields.append(field)
        setattr(expense, field, new_value)

    if _CONVERSION_FIELDS & fields.keys():
        expense.fx_rate = new_fx_rate
        expense.amount_base = new_amount_base

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="expense.updated",
        target_type="expense",
        target_id=str(expense.id),
        client_id=client_id,
        # Names only, never values -- spec §8: amounts are the sensitive
        # payload in this module.
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return expense


# ---------------------------------------------------------------------------
# confirm_expense / void_expense
# ---------------------------------------------------------------------------


async def confirm_expense(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    expense_id: UUID,
    *,
    amount: Decimal | None = None,
) -> Expense:
    """expected -> confirmed (spec §0.3/§5). Carries the actual amount,
    which MAY differ from the projected figure -- omitting `amount` confirms
    at the currently-recorded amount. Always re-runs FX conversion against
    `expense_date` (not "now"): a rate that did not cover this date at
    creation time may exist by confirmation time, and a corrected amount
    must be converted at whatever rate applies, never left over from
    creation. Stamps `confirmed_by`/`confirmed_at` -- there is no path that
    confirms without a user.

    Only an `expected` row may be confirmed. A voided expense is terminal
    (spec §5: "reversible only by creating a new entry"). An
    ALREADY-`confirmed` row is rejected too -- spec §3 is emphatic that a
    corrective rate must never re-value history, and re-running this on a
    confirmed row would silently re-convert at whatever rate applies today
    and re-stamp `confirmed_by`/`confirmed_at` over the original asserter,
    even with `amount` omitted."""
    expense = await expense_for_write(session, org_id, expense_id)
    if expense.status != "expected":
        raise BudgetValidationError(
            tr(
                "expense {expense_id} is {expense_status} and cannot be confirmed",
                expense_id=expense_id,
                expense_status=say(EXPENSE_STATUS_WORDS, expense.status),
            )
        )

    confirmed_amount = amount if amount is not None else expense.amount

    # Conversion computed BEFORE any setattr -- ordering rule #1.
    fx_rate, amount_base = await convert_for(
        session,
        org_id,
        confirmed_amount,
        expense.currency,
        expense.expense_date,
        client_id=expense.client_id,
    )

    expense.amount = confirmed_amount
    expense.fx_rate = fx_rate
    expense.amount_base = amount_base
    expense.status = "confirmed"
    expense.confirmed_by = actor_id
    expense.confirmed_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="expense.confirmed",
        target_type="expense",
        target_id=str(expense.id),
        client_id=expense.client_id,
        metadata={"amount_overridden": amount is not None},
    )
    await session.flush()
    return expense


async def void_expense(
    session: AsyncSession, org_id: UUID, actor_id: UUID, expense_id: UUID
) -> Expense:
    """-> void (spec §5). Stamps `voided_by`/`voided_at`. Reversible only by
    creating a new entry -- void is terminal, so voiding an already-void
    expense is rejected rather than silently re-stamping over the original
    voiding actor/time."""
    expense = await expense_for_write(session, org_id, expense_id)
    if expense.status == "void":
        raise BudgetValidationError(
            tr("expense {expense_id} is already void", expense_id=expense_id)
        )

    expense.status = "void"
    expense.voided_by = actor_id
    expense.voided_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="expense.voided",
        target_type="expense",
        target_id=str(expense.id),
        client_id=expense.client_id,
        metadata={},
    )
    await session.flush()
    return expense

"""Renewal projection and idempotent materialization
(docs/modules/budget/spec.md §6).

`project_costs` is a PURE read-time projection over the two upstream
registries (Subscriptions, Licenses) -- it never touches `session.add`/
`session.execute` for anything but SELECTs, and writes nothing (spec §6.2,
§0.6: "no rows, no drift, always current"). `materialize` is the only
function in this module that writes: it turns a window of projected costs
into `expected` `Expense` rows, idempotent under `uq_expense_autofeed`
(models.py), and is always explicit and audited (spec §6.3) -- there is no
worker, no sweep, no path that runs this without an operator action.

**Idempotency, honestly reported.** The DB unique index
`(org_id, source, link_id, period_key) WHERE source <> 'manual'` is the
backstop that makes a duplicate physically impossible; the pre-check this
module runs against `Expense` (querying by `(source, link_id, period_key)`
with NO `status` filter) is what lets `materialize` report `skipped_existing`
honestly rather than let a second call surface an `IntegrityError` as a 500.
That pre-check deliberately does NOT exclude `status == "void"` rows --
spec §2/§5 is explicit that a voided auto-fed row must never resurrect: an
operator voided it on purpose, and the unique index stays keyed on it for
exactly that reason.

**Category resolution fails loudly, and BEFORE any write.** Spec §6.1: a
projected cost's category is resolved by `system_key`
(`saas_subscriptions` for subscriptions, `software_licenses` for licenses),
never by the free-text `subscription.category`, and never with a fallback
guess -- and never a RETIRED (`active=False`) category either;
`refdata.category_by_system_key` treats a retired category the same as a
never-seeded one (K2 fix). `materialize` resolves every DISTINCT
`category_system_key` the current projection actually needs up front, before
creating a single `Expense` row -- so a missing/retired category aborts the
whole call with nothing partially written, rather than raising mid-loop
after some rows already landed (this module has no `try`/rollback of its
own; it relies on the caller's transaction, exactly like `expenses.py`, and
a mid-loop raise inside that same transaction would otherwise leave earlier
flushes to be cleaned up by whichever code happens to unwind the session --
resolving up front removes that dependency entirely).

**The no-partial-write guarantee is NOT total -- only the §6.1 category
check is up front.** The FX overflow raised by `convert_for` -> `convert`
(spec §8's `Numeric(14,2)` ceiling) happens INSIDE the per-projection loop,
after zero or more earlier iterations have already inserted their `Expense`
row and recorded its `expense.created` audit event in the SAME transaction.
If that raise propagates, those earlier rows are not rolled back by this
module -- there is no `try`/rollback here, by design (see above). Whatever
transaction boundary the caller uses is what decides whether they survive.
As of this writing that caller is the (future, Task-12) HTTP route, which
therefore owns deciding whether a mid-call FX overflow rolls back the whole
materialization or keeps the rows already written.

**The 24-month bound is materialize's alone.** Spec §6.3: bounded to keep
one call from generating an unbounded write. `project_costs` itself is NOT
bounded -- it is the read-time forecast's engine too (`GET /budget/forecast`,
a later task), which has its own, wider bound at the schema boundary
(`months` 1-60). This module only enforces `MAX_MATERIALIZE_MONTHS` on the
write path.

**Bounded by ROWS too, not just months (I4 fix).** `MAX_MATERIALIZE_MONTHS`
bounds the WINDOW, not the write: an org with hundreds of monthly
subscriptions can still generate thousands of `Expense` inserts -- and as
many `audit_service.record` calls, each taking
`pg_advisory_xact_lock(hashtext('audit:' || org))` -- in a single
transaction, serializing every other audited action in that org (job
dispatch, credential access, remote sessions) for the run's duration.
`MAX_MATERIALIZE_ROWS` is checked against the projection count immediately
after `project_costs`, before category resolution or any write, raising
`BudgetValidationError` naming the count and the limit so the operator can
narrow the window instead.

**Lazily seeds the org's default categories (C1 fix).** There is no
org-bootstrap hook anywhere in the platform that seeds `expense_category`
ahead of time, so `materialize` calls `refdata.seed_default_categories`
itself, before resolving any category below -- otherwise every real org's
first materialization would 400 on the very next block, with no route able
to create a system-keyed category to recover.

**Two failure modes from `fx.convert_for`, not one (mirrors `expenses.py`).**
`(None, None)` -- no rate resolves -- is NOT an error: the row is still
created, with `amount_base = NULL`, and counted in `skipped_unconvertible`.
An overflow RAISES `BudgetValidationError` and is allowed to propagate
unchanged, same as `create_expense`.

**Colliding projections are MERGED, not dropped (K1 fix).** `uq_expense_autofeed`
is keyed `(org_id, source, link_id, period_key)`, so a license whose
`renews_at` and `maintenance_expires_at` land in the SAME calendar month can
only ever have one row. `project_costs` merges any projections that collide
on that key into a single `ProjectedCost` -- amounts summed, descriptions
combined -- so the forecast (§6.2) and `materialize` (§6.3) agree on the
total. Merging is only valid within one currency: colliding projections that
disagree on currency raise `BudgetValidationError` naming the record rather
than sum across currency codes (never sum across codes -- the per-record
currency rule).

**The actual write is conflict-safe, not just pre-checked (K3 fix).** The
in-memory `existing_keys` pre-check is a fast path, not the sole guard --
two overlapping `materialize` calls can both miss it before either has
written. The `Expense` insert itself goes through
`pg_insert(...).on_conflict_do_nothing()` against `uq_expense_autofeed`
(same precedent as `fx.get_settings`, Task-5 finding F2); a row that loses
that race is counted as `skipped_existing`, never raised as a raw
`IntegrityError` (spec §8 forbids a raw DB error surfacing as a 500 on a
financial path).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire_words import say
from app.models import Expense, License, SoftwareProduct, Subscription
from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.fx import convert_for
from app.modules.budget.periods import period_key_for_date
from app.modules.budget.refdata import category_by_system_key, seed_default_categories
from app.modules.budget.words import LINK_TYPE_WORDS
from app.modules.subscriptions.costs import advance_renewal

# Mirrors the partial-unique-index predicate on `uq_expense_autofeed`
# (models.py) -- `ON CONFLICT` target inference for a partial index must
# repeat its WHERE clause exactly, or Postgres won't match the index.
_AUTOFEED_CONFLICT_WHERE = text("source <> 'manual'")

# spec §6.1: the STABLE identifiers the auto-feeds resolve against -- never
# the free-text `subscription.category`.
SUBSCRIPTION_CATEGORY_SYSTEM_KEY = "saas_subscriptions"
LICENSE_CATEGORY_SYSTEM_KEY = "software_licenses"

# spec §7 (subscriptions): "cancelled" and "trial" are excluded from
# projection. Mirrored here rather than imported, because subscriptions'
# STATUS values are that module's own domain literal, not budget's --
# duplicating the two strings is cheaper than a cross-module import of a
# Literal type for a two-item set, and the mirroring is called out at both
# ends (this docstring, and subscriptions/spec.md §7) so a change to one
# is a deliberate, visible decision in the other.
_EXCLUDED_SUBSCRIPTION_STATUSES = frozenset({"cancelled", "trial"})

# spec §6.3: "The window is bounded (max 24 months)."
MAX_MATERIALIZE_MONTHS = 24

# I4 fix: a row bound, independent of the month bound above. 500 monthly
# subscriptions x 24 months = 12,000 inserts (and as many audit events) in
# ONE transaction; 2000 is ample for any real materialization run and cheap
# to check against the projection count before a single row is written.
MAX_MATERIALIZE_ROWS = 2000


@dataclass(frozen=True)
class ProjectedCost:
    """One projected cost line -- a subscription renewal occurrence or a
    license one-off (renewal or maintenance expiry). Never persisted by
    itself; `materialize` is what turns this into an `Expense` row."""

    source: str
    link_id: UUID
    link_type: str
    period_key: str
    on_date: date
    amount: Decimal
    currency: str
    description: str
    vendor_id: UUID | None
    category_system_key: str


def _months_span(from_date: date, to_date: date) -> int:
    """Inclusive month count spanned by [from_date, to_date]. E.g.
    2026-01-01..2026-01-31 spans 1 month; 2026-01-01..2027-12-31 spans 24."""
    return (to_date.year - from_date.year) * 12 + (to_date.month - from_date.month) + 1


def _combine_descriptions(first: str, second: str) -> str:
    """Combine two descriptions that collided on the same
    `(source, link_id, period_key)` key so the merged row still says what it
    covers. The common case (K1) is two events on the SAME record -- e.g.
    "Acme Suite license renewal" + "Acme Suite license maintenance" -- so a
    shared leading-word prefix is kept once and only the differing tails are
    joined: "Acme Suite license renewal + maintenance". Descriptions with no
    shared prefix are just joined in full."""
    first_words = first.split()
    second_words = second.split()
    shared = 0
    for a, b in zip(first_words, second_words, strict=False):
        if a != b:
            break
        shared += 1
    if shared == 0:
        return f"{first} + {second}"
    prefix = " ".join(first_words[:shared])
    tails = " + ".join(
        tail for tail in (" ".join(first_words[shared:]), " ".join(second_words[shared:])) if tail
    )
    return f"{prefix} {tails}".strip()


def merge_colliding_projections(projections: list[ProjectedCost]) -> list[ProjectedCost]:
    """Aggregate projections that collide on `(source, link_id, period_key)`
    into ONE `ProjectedCost` per key (K1 fix). `uq_expense_autofeed` can only
    ever hold one `Expense` row per that key, so without this, a license
    whose `renews_at` and `maintenance_expires_at` land in the same calendar
    month would have one of its two costs silently dropped rather than
    merged -- see the K1 money scenario in the fix directive.

    Amounts are summed and descriptions combined (`_combine_descriptions`).
    Merging is only valid within a single currency: colliding projections
    that disagree on currency raise `BudgetValidationError` naming the
    record rather than sum across currency codes (the per-record-currency
    rule -- never sum across codes). `on_date` for a merged row is the
    EARLIEST of the colliding dates; `vendor_id` and `category_system_key`
    are carried from the first-seen projection (both events on one record
    always share them)."""
    merged: dict[tuple[str, UUID, str], ProjectedCost] = {}
    order: list[tuple[str, UUID, str]] = []
    for proj in projections:
        key = (proj.source, proj.link_id, proj.period_key)
        existing = merged.get(key)
        if existing is None:
            merged[key] = proj
            order.append(key)
            continue
        if existing.currency != proj.currency:
            raise BudgetValidationError(
                tr(
                    "cannot merge colliding projected costs for {link_type} {link_id} in period "
                    "{period_key}: currencies differ ({currency} vs {currency2}); refusing to sum "
                    "across currency codes",
                    link_type=say(LINK_TYPE_WORDS, existing.link_type),
                    link_id=existing.link_id,
                    period_key=proj.period_key,
                    currency=existing.currency,
                    currency2=proj.currency,
                )
            )
        merged[key] = ProjectedCost(
            source=existing.source,
            link_id=existing.link_id,
            link_type=existing.link_type,
            period_key=existing.period_key,
            on_date=min(existing.on_date, proj.on_date),
            amount=existing.amount + proj.amount,
            currency=existing.currency,
            description=_combine_descriptions(existing.description, proj.description),
            vendor_id=existing.vendor_id,
            category_system_key=existing.category_system_key,
        )
    return [merged[key] for key in order]


# ---------------------------------------------------------------------------
# project_costs -- pure, read-time, writes NOTHING
# ---------------------------------------------------------------------------


async def project_costs(
    session: AsyncSession, org_id: UUID, *, from_date: date, to_date: date
) -> list[ProjectedCost]:
    """Project subscription renewals and license renewal/maintenance dates
    forward, org-scoped (invariant #5), for every occurrence landing in
    `[from_date, to_date]` inclusive. Pure: only SELECTs run here, nothing is
    added to the session and nothing is flushed."""
    if from_date > to_date:
        raise BudgetValidationError(tr("from_date must not be after to_date"))

    projections: list[ProjectedCost] = []

    # --- Subscriptions: walk forward from renews_at by billing_cycle
    # (spec §6.1) until past to_date. cancelled/trial excluded (spec §7,
    # mirrored here).
    subs = (
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.org_id == org_id,
                    Subscription.renews_at.is_not(None),
                    Subscription.status.not_in(_EXCLUDED_SUBSCRIPTION_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for sub in subs:
        assert sub.renews_at is not None  # narrowed by the query filter above
        current = sub.renews_at
        while current <= to_date:
            if current >= from_date:
                projections.append(
                    ProjectedCost(
                        source="subscription",
                        link_id=sub.id,
                        link_type="subscription",
                        period_key=period_key_for_date(current),
                        on_date=current,
                        amount=sub.cost_per_cycle,
                        currency=sub.currency,
                        description=f"{sub.service_name} subscription renewal",
                        vendor_id=sub.vendor_id,
                        category_system_key=SUBSCRIPTION_CATEGORY_SYSTEM_KEY,
                    )
                )
            current = advance_renewal(current, sub.billing_cycle)

    # --- Licenses: renews_at and maintenance_expires_at, each a one-off
    # (spec §6.1). A license with no cost/currency recorded has nothing to
    # project -- it is skipped rather than materialized with a fabricated
    # amount.
    licenses = (
        (await session.execute(select(License).where(License.org_id == org_id))).scalars().all()
    )

    # Bulk-resolve product names up front (one query, not one per license) --
    # every license in an org otherwise produces an identical "License
    # renewal" description, which is useless once an org has more than one
    # (K7's "cheap fix").
    product_ids = {lic.product_id for lic in licenses}
    product_names: dict[UUID, str] = {}
    if product_ids:
        product_rows = (
            await session.execute(
                select(SoftwareProduct.id, SoftwareProduct.name).where(
                    SoftwareProduct.org_id == org_id,
                    SoftwareProduct.id.in_(product_ids),
                )
            )
        ).all()
        product_names = {row.id: row.name for row in product_rows}

    for lic in licenses:
        if lic.cost is None or lic.currency is None:
            continue
        product_name = product_names.get(lic.product_id, "License")
        license_dates = (
            (lic.renews_at, "renewal"),
            (lic.maintenance_expires_at, "maintenance"),
        )
        for lic_date, kind in license_dates:
            if lic_date is None:
                continue
            if not (from_date <= lic_date <= to_date):
                continue
            projections.append(
                ProjectedCost(
                    source="license",
                    link_id=lic.id,
                    link_type="license",
                    period_key=period_key_for_date(lic_date),
                    on_date=lic_date,
                    amount=lic.cost,
                    currency=lic.currency,
                    description=f"{product_name} license {kind}",
                    vendor_id=lic.vendor_id,
                    category_system_key=LICENSE_CATEGORY_SYSTEM_KEY,
                )
            )

    # K1 fix: merge same-license-same-month collisions (renewal +
    # maintenance) into one row instead of letting `materialize`'s guard
    # silently drop the second. Done here, not in `materialize`, so the
    # forecast and the write agree.
    return merge_colliding_projections(projections)


# ---------------------------------------------------------------------------
# materialize -- the only writer; explicit, idempotent, audited
# ---------------------------------------------------------------------------


async def materialize(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    from_date: date,
    to_date: date,
) -> dict[str, int]:
    """Write `expected` `Expense` rows for every projected cost in
    `[from_date, to_date]` that does not already have one (spec §6.3).
    Returns `{"created", "skipped_existing", "skipped_unconvertible"}`.

    Idempotent: a second call over the same window creates nothing --
    `skipped_existing` accounts for every row the first call created,
    INCLUDING any that were subsequently voided (spec §2: a voided
    materialized row must never resurrect).
    """
    if from_date > to_date:
        raise BudgetValidationError(tr("from_date must not be after to_date"))
    months = _months_span(from_date, to_date)
    if months > MAX_MATERIALIZE_MONTHS:
        raise BudgetValidationError(
            tr(
                "materialization window spans {months} months, exceeding the "
                "{max_months}-month maximum",
                months=months,
                max_months=MAX_MATERIALIZE_MONTHS,
            )
        )

    # C1 fix: lazily seed the org's default categories before resolving any
    # of them below -- there is no org-bootstrap hook anywhere in the
    # platform that does this ahead of time, so without it every real org's
    # first materialize would 400 on the category-resolution loop just
    # below, with no route able to create a system-keyed category to
    # recover (see refdata.seed_default_categories).
    await seed_default_categories(session, org_id, actor_id)

    projections = await project_costs(session, org_id, from_date=from_date, to_date=to_date)

    # I4 fix: bounded by ROWS, not just months. `MAX_MATERIALIZE_MONTHS`
    # bounds the WINDOW, not the write -- an org with hundreds of monthly
    # subscriptions can still generate thousands of inserts (and as many
    # audit events, each taking `audit_service.record`'s
    # `pg_advisory_xact_lock`) in a single call, serializing every other
    # audited action in the org for the run's duration. Checked BEFORE any
    # write, against the projection count the write loop below would
    # actually attempt.
    if len(projections) > MAX_MATERIALIZE_ROWS:
        raise BudgetValidationError(
            tr(
                "materialization would write {count} rows, exceeding the {max_rows}-row "
                "maximum; narrow the window",
                count=len(projections),
                max_rows=MAX_MATERIALIZE_ROWS,
            )
        )

    # Resolve every DISTINCT category this run needs UP FRONT, before a
    # single Expense is constructed or added -- spec §6.1's "fails loudly and
    # namably" must abort the whole call with NOTHING partially written, not
    # raise after some rows already landed in this transaction.
    needed_system_keys = {p.category_system_key for p in projections}
    category_by_key: dict[str, UUID] = {}
    for system_key in needed_system_keys:
        category = await category_by_system_key(session, org_id, system_key)
        if category is None:
            raise BudgetValidationError(
                tr(
                    "no active expense category with system_key={system_key}; materialization "
                    "cannot categorize this cost without one",
                    system_key=repr(system_key),
                )
            )
        category_by_key[system_key] = category.id

    # Pre-check existing rows for this org's relevant (source, link_id) pairs
    # -- INCLUDING voided ones (no status filter): `uq_expense_autofeed`
    # stays keyed on a voided row on purpose (spec §2), and this pre-check
    # must see the same thing the constraint sees, or `skipped_existing`
    # would under-report and this function would try (and fail) to recreate
    # a deliberately-suppressed cost.
    existing_keys: set[tuple[str, UUID, str]] = set()
    link_ids = {p.link_id for p in projections}
    if link_ids:
        rows = (
            await session.execute(
                select(Expense.source, Expense.link_id, Expense.period_key).where(
                    Expense.org_id == org_id,
                    Expense.source != "manual",
                    Expense.link_id.in_(link_ids),
                )
            )
        ).all()
        existing_keys = {(r.source, r.link_id, r.period_key) for r in rows}

    created = 0
    skipped_existing = 0
    skipped_unconvertible = 0

    for proj in projections:
        key = (proj.source, proj.link_id, proj.period_key)
        if key in existing_keys:
            skipped_existing += 1
            continue

        # Conversion computed BEFORE the insert is attempted -- the same
        # ordering rule `expenses.create_expense` follows: `convert_for` ->
        # `fx.get_settings` autoflushes the session, and must never do so
        # over a half-built row.
        fx_rate, amount_base = await convert_for(
            session, org_id, proj.amount, proj.currency, proj.on_date
        )
        category_id = category_by_key[proj.category_system_key]

        # Conflict-safe write (K3 fix). The pre-check above is a fast path,
        # not the sole guard -- two overlapping `materialize` calls can both
        # miss it before either has written its row. `ON CONFLICT DO NOTHING`
        # targets the SAME partial unique index as `uq_expense_autofeed`
        # (`index_where` repeats its predicate exactly, or Postgres won't
        # match a partial index for conflict inference); the loser here gets
        # an empty `RETURNING`, never a raw `IntegrityError` -- same
        # precedent as `fx.get_settings` (Task-5 finding F2).
        inserted_id = (
            await session.execute(
                pg_insert(Expense)
                .values(
                    org_id=org_id,
                    expense_date=proj.on_date,
                    amount=proj.amount,
                    currency=proj.currency,
                    fx_rate=fx_rate,
                    amount_base=amount_base,
                    vendor_id=proj.vendor_id,
                    category_id=category_id,
                    description=proj.description,
                    source=proj.source,
                    link_type=proj.link_type,
                    link_id=proj.link_id,
                    period_key=proj.period_key,
                    status="expected",
                    entered_by=actor_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["org_id", "source", "link_id", "period_key"],
                    index_where=_AUTOFEED_CONFLICT_WHERE,
                )
                .returning(Expense.id)
            )
        ).scalar_one_or_none()

        if inserted_id is None:
            # Lost the race: some other call (concurrent or otherwise)
            # already holds this key. From this call's point of view that
            # row now IS "already existing" -- count it exactly like the
            # pre-check skip above, not as a failure.
            skipped_existing += 1
            existing_keys.add(key)
            continue

        # Counted only now that a row actually landed. Incrementing this
        # before the insert would double-count a projection that then lost
        # the ON CONFLICT race: it would appear in BOTH skipped_existing and
        # skipped_unconvertible, so the audited summary would report more
        # dispositions than there were projections and an auditor could not
        # tell whether the rows landed without a base amount or not at all.
        if amount_base is None:
            skipped_unconvertible += 1

        # Mark this key as no-longer-available for the remainder of THIS
        # run -- K1's merge in `project_costs` already prevents a single
        # license from producing two projections under the same
        # (source, link_id, period_key) identity, but the guard costs
        # nothing to keep and protects any other source that might one day
        # collide within a single run.
        existing_keys.add(key)
        created += 1

        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="expense.created",
            target_type="expense",
            target_id=str(inserted_id),
            metadata={
                "category_id": str(category_id),
                "vendor_id": str(proj.vendor_id) if proj.vendor_id else None,
                "source": proj.source,
                "status": "expected",
            },
        )
        await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.expenses_materialized",
        metadata={
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "created": created,
            "skipped_existing": skipped_existing,
            "skipped_unconvertible": skipped_unconvertible,
        },
    )
    await session.flush()

    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_unconvertible": skipped_unconvertible,
    }

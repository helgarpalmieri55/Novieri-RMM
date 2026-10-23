"""sla: redefine the breach-sweep indexes so the widened §7 predicate can use
them

docs/modules/sla/spec.md §2.5, §7. One migration, two indexes, no data change.

---------------------------------------------------------------------------
WHAT WAS BROKEN
---------------------------------------------------------------------------

Migration 0033 created the two partial indexes that make §7's breach sweep an
index scan rather than a table walk::

    ix_ticket_sla_org_response_due   (org_id, response_due_at)
        WHERE response_breached_at IS NULL AND response_met_at IS NULL
    ix_ticket_sla_org_resolution_due (org_id, resolution_due_at)
        WHERE resolution_breached_at IS NULL AND resolution_met_at IS NULL

They were written against §7's literal predicate, which asked `met_at IS
NULL`. That predicate has since been widened, for a reason that is not
cosmetic: `met_at IS NULL` alone makes SLA attainment depend on whether the
Celery beat fired before or after a late reply. A response deadline falls at
10:00 and the beat fires at 10:04. If nobody replied, the sweep stamps a
breach. If the technician replied at 10:02 -- four minutes into a deadline
that had ALREADY passed -- `response_met_at` is set, the narrow predicate
excludes the row forever, and the identical failure to answer in time reads as
MET. So the sweep now asks::

    (met_at IS NULL OR met_at > due_at)

**A partial index on `met_at IS NULL` does not contain those rows.** Not
"contains them but ranks them badly" -- it does not contain them, so Postgres
cannot use it for a query that must consider them, and it correctly refuses
to. The widening therefore turned both of the sweep's queries into sequential
scans over the whole of `ticket_sla`, twice per org, every 300 seconds,
forever. The indexes still existed and still looked healthy; nothing failed;
the sweep just stopped using them.

---------------------------------------------------------------------------
WHAT THIS MIGRATION DOES, AND WHY THE PREDICATE IS THE ONE IT IS
---------------------------------------------------------------------------

Both indexes are dropped and recreated on::

    WHERE <target>_breached_at IS NULL AND paused_at IS NULL AND voided_at IS NULL

which is exactly the set of §7's filters that are plain `IS NULL` tests on
`ticket_sla`'s own columns:

* `<target>_breached_at IS NULL` -- already stamped is settled (§10.7).
* `paused_at IS NULL` -- the clock is stopped right now (§5).
* `voided_at IS NULL` -- a cancelled ticket's promise is void, neither met nor
  broken (migration 0038).

The three that are NOT in it, each for its own reason:

* **`met_at` is gone, deliberately.** It is no longer compared to NULL but to
  another column of the same row, and a partial index predicate cannot express
  a cross-column comparison at all (Postgres requires an immutable expression
  over the row, and `met_at > due_at` is one -- but making it the *partial
  condition* would index only the late-met rows, which is the complement of
  what the sweep mostly reads). It is filtered on the heap instead, on a row
  set the index has already narrowed by `org_id` and deadline. Late-met rows
  are a small fraction of any real fleet, so the cost is a few extra heap
  fetches; the benefit is that they are reachable at all.
* **`due_at <= now` is a bound, not a filter** -- it is what the index's
  second column is FOR, and freezing an instant into a partial condition would
  make the index correct for exactly one moment.
* **`ticket.status NOT IN FINAL_STATUSES` is on another table.** A partial
  index cannot join.

`paused_at` and `voided_at` are additions rather than survivals: 0033 predates
both filters (`paused_at` was not yet swept on, `voided_at` did not exist until
0038). Including them narrows the index further -- a paused or voided row is
never a sweep candidate, so it is dead weight in an index whose only reader is
the sweep.

WHY DROP-AND-CREATE RATHER THAN A NEW PAIR ALONGSIDE. The names are the ones
`app/modules/sla/models.py` declares, and two indexes on the same columns
differing only in a partial condition would both be maintained on every write
to `ticket_sla` while only one could ever be chosen. Keeping the names also
keeps `models.py`'s declaration a single edit rather than a second pair of
`Index(...)` entries someone later has to reconcile.

NOT `CONCURRENTLY`. Alembic runs migrations inside a transaction and `CREATE
INDEX CONCURRENTLY` cannot run in one. `ticket_sla` is one row per ticket and
the on-prem deployments this ships to are far below the size where a brief
write lock on it matters; if that ever changes, the answer is an out-of-band
concurrent build, not a half-transactional migration.

NO DATA IS TOUCHED. Every row keeps every value; only the two indexes change.
The downgrade restores 0033's exact predicates, so 0033-0038 stay
byte-identical and forward-only discipline holds.

`app/modules/sla/models.py::TicketSla` declares both indexes in lockstep --
`test_schema_drift.py` deliberately does not compare indexes (an index living
only in a migration is one `alembic revision --autogenerate` away from a
migration proposing to DROP it), so the declaration is what keeps autogenerate
honest and it must move with this file.

Revision ID: 0039
Revises: 0038
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The sweep's stable half, per target. Kept as a table rather than four
# hand-written SQL strings so the response and resolution definitions cannot
# drift apart -- a mismatched column-to-clock pairing in an index is invisible
# to everything except a query plan nobody reads.
_TARGETS = ("response", "resolution")

# 0033's predicates, restored verbatim by `downgrade`.
_OLD_WHERE = "{t}_breached_at IS NULL AND {t}_met_at IS NULL"
_NEW_WHERE = "{t}_breached_at IS NULL AND paused_at IS NULL AND voided_at IS NULL"


def _rebuild(where: str) -> None:
    for target in _TARGETS:
        op.execute(f"DROP INDEX IF EXISTS ix_ticket_sla_org_{target}_due")
        op.execute(
            f"CREATE INDEX ix_ticket_sla_org_{target}_due "
            f"ON ticket_sla (org_id, {target}_due_at) "
            f"WHERE {where.format(t=target)}"
        )


def upgrade() -> None:
    _rebuild(_NEW_WHERE)


def downgrade() -> None:
    _rebuild(_OLD_WHERE)

"""sla: void a promise on a terminal-not-resolved ticket, and let the portal's
own connection materialise one

docs/modules/sla/spec.md §3, §5, §6.0, §2.5; docs/modules/portal/spec.md §2.1,
§6. One migration because both halves land on the same table for the same
reason: this is the revision that first makes `ticket_sla` a row real tickets
actually get.

---------------------------------------------------------------------------
PART A -- `ticket_sla.voided_at`
---------------------------------------------------------------------------

WHAT WAS BROKEN. A ticket that reaches `cancelled` or `rejected`
(`lifecycle.FINAL_STATUSES`) is finished, but nothing stopped its clocks.
Both `response_due_at` and `resolution_due_at` went on being due, so **every
cancelled or rejected ticket breaches with certainty** once its deadline
passes -- roughly 0.1-0.2 phantom breach events per ticket filed at a 5-10%
cancellation rate, each one publishing `ticket.sla_breached` and escalating a
ticket nobody is working. Worse, §6's escalation refuses a ticket in
`TERMINAL_STATUSES` by raising, and a sweep that processes an org in one
transaction would have that raise roll back the cycle's REAL breaches and
retry the identical failure every 300 seconds, forever.

WHY A COLUMN AND NOT A SWEEP FILTER. A `WHERE ticket.status NOT IN
('cancelled','rejected')` join in §7's sweep would stop the phantom events and
nothing else: the invariant would live in the one consumer that knows about
it, so the ticket detail panel (§9.1) would still render a ticking red
countdown on a cancelled ticket, and every future consumer -- reporting,
attainment, an export -- would have to rediscover the same join or repeat the
same bug. `voided_at` is column-local, needs no join, and is readable by
everything that already reads the row.

WHY NOT MET-STAMP INSTEAD. Stamping `response_met_at`/`resolution_met_at` on
cancellation would claim the promise was *kept*, which makes "cancel it" a
one-click way to inflate SLA attainment. A voided promise is neither met nor
breached. It is void, and it needs its own column to say so.

WHAT VOIDING DOES **NOT** DO: it does not erase history. `tracking.note_cancelled`
writes only this column; a clock that had already stopped keeps its
`*_met_at`/`*_breached_at` exactly as it was. So cancelling a ticket that has
already breached does not launder the breach away -- the symmetric hazard to
the met-stamping one above.

NULLABLE with no default and no backfill. NULL means "not voided", which is
the correct and honest value for every row that exists today: those tickets'
promises were never voided, and inventing a timestamp for the ones that happen
to sit on a cancelled ticket would put a fabricated instant in a table whose
whole purpose is recording when things actually happened. Existing stale
promises on already-cancelled tickets stay stale until something touches them;
the sweep (a later task) excludes `voided_at IS NOT NULL` and is the thing that
would otherwise act on them.

`app/modules/sla/models.py::TicketSla` declares the column in lockstep --
`test_schema_drift.py` compares `Base.metadata` to the live schema and fails
if either side moves alone.

---------------------------------------------------------------------------
PART B -- portal_user's grants for creation-time SLA materialisation
---------------------------------------------------------------------------

WHY THIS IS FORCED. `tickets.create_ticket` now calls `sla.tracking.
start_tracking` (spec §1: "an SLA record follows from that automatically
because §3's resolution runs at ticket creation regardless of origin"), and
`portal.tickets.create_my_ticket` delegates to `create_ticket` on the
requester's OWN `portal_session` connection -- migration 0030 deliberately
removed the second, elevated connection because portal writes must be refused
by the database rather than by application code that decided to elevate.

The read path had an out and used it: `tracking.note_public_comment` orders its
requester comparison BEFORE any `ticket_sla` access, so a portal comment
returns having touched only `ticket`. **An INSERT has no such out.** Ordering
cannot make a write not happen. So either `portal_user` gets a grant, or
creation-time tracking runs somewhere that is not `portal_user` -- and the
second option means either branching `create_ticket` on `origin == "portal"`
(portal tickets silently get a different SLA lifecycle from every other
origin, which is precisely the class of defect this sub-project keeps
shipping) or materialising in a SECOND transaction after the portal's commits,
which is not atomic with the ticket and cannot be, because the ticket row does
not exist to a second connection until the first transaction commits.

WHAT IS GRANTED, and why each is the minimum `start_tracking` actually
touches:

* `sla_policy`: SELECT. `policies.resolve_policy` walks §3's four resolution
  levels. Read-only.
* `sla_calendar`, `sla_calendar_window`, `sla_calendar_holiday`: SELECT.
  `calendars.load_calendar_spec` reads all three to build the `CalendarSpec`
  the deadline arithmetic runs on. Read-only.

  These four carry NO portal policy, matching the existing carve-out migration
  0028 made for `ticket_category`/`ticket_number_counter` and 0030 made for
  `audit_events`/`domain_events`: they are org-level operational configuration
  that a delegated ticket-core write must consult, not requester-owned data,
  and the pre-existing PERMISSIVE `tenant_isolation` policy (migration 0033)
  already bounds every one of them to a single org. A requester can learn what
  their own organisation promises; they cannot learn anything about another
  tenant, and they cannot write any of it.

* `ticket_sla`: SELECT **and** INSERT, plus a NEW RESTRICTIVE
  `portal_isolation` policy scoping every row to the caller's own tickets.
  SELECT is not optional and not an oversight: `start_tracking` checks for an
  existing row before resolving anything (its idempotency contract), and
  `_materialise` re-reads after `INSERT ... ON CONFLICT DO NOTHING` to return
  the winner of a materialisation race. Granting INSERT alone fails on the
  SELECT that runs first -- the same empirical finding migration 0030 recorded
  for `audit_events`.

  **The policy is the load-bearing half.** `tenant_isolation` is PERMISSIVE
  with `roles={public}`, so a bare `GRANT SELECT ON ticket_sla TO portal_user`
  would let any requester read every SLA row in the org -- every colleague's
  deadlines, every breach, every escalation -- which is exactly the blanket
  access invariant #5 forbids and which was correctly rejected once already on
  the read path. `_PARENT_IS_MINE` is migration 0028's own predicate, reused
  verbatim rather than restated, and because the policy is RESTRICTIVE it is
  AND'd with the org check rather than OR'd around it.

  Postgres uses a policy's `USING` expression as its `WITH CHECK` when the
  policy applies to INSERT and defines no separate `WITH CHECK` (verified
  empirically for `ticket_status_history`/`ticket_approval` in migration
  0030's docstring, and asserted behaviourally in `test_sla_wiring.py`), so
  the one predicate refuses both reading and writing another requester's row.

* **No UPDATE and no DELETE, on any table, deliberately.** Every SLA write a
  portal request can reach is the creation-time INSERT. `pause`/`resume`,
  every met stamp, every breach stamp and `voided_at` itself are all UPDATEs
  driven by staff-side transitions on `app_user`, so the portal role cannot
  move a deadline, cannot mark its own promise met, and cannot void one --
  which is what keeps "file it from the portal" from being a way to edit what
  was promised. `tests/test_portal_migration.py::EXPECTED_PORTAL_GRANTS` pins
  the whole surface in both directions and fails on anything added here later.

0033-0037 are shipped and migrations are forward-only, so this is a new
revision rather than an edit to any of them.

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_REQ_GUC = "current_setting('app.current_requester', true)::uuid"

# Migration 0028's predicate, verbatim. Copied rather than imported because
# migrations are frozen historical artefacts that must not depend on each
# other's Python, but it is deliberately character-identical: a tenancy
# predicate that drifts between two files is a tenancy predicate that is wrong
# in one of them.
_PARENT_IS_MINE = (
    f"ticket_id IN (SELECT id FROM ticket "
    f"WHERE org_id = {_ORG_GUC} AND requester_id = {_REQ_GUC})"
)

# Org-level SLA configuration the creation-time materialisation reads. SELECT
# only, no portal policy -- see the module docstring.
_PORTAL_READABLE_CONFIG = (
    "sla_policy",
    "sla_calendar",
    "sla_calendar_window",
    "sla_calendar_holiday",
)


def upgrade() -> None:
    # Part A: the void marker.
    op.add_column(
        "ticket_sla",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Part B: the portal's creation-time materialisation path.
    for table in _PORTAL_READABLE_CONFIG:
        op.execute(f"GRANT SELECT ON {table} TO portal_user")

    op.execute("GRANT SELECT, INSERT ON ticket_sla TO portal_user")
    op.execute(
        f"CREATE POLICY portal_isolation ON ticket_sla AS RESTRICTIVE TO portal_user "
        f"USING ({_PARENT_IS_MINE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS portal_isolation ON ticket_sla")
    op.execute("REVOKE SELECT, INSERT ON ticket_sla FROM portal_user")
    for table in _PORTAL_READABLE_CONFIG:
        op.execute(f"REVOKE SELECT ON {table} FROM portal_user")
    op.drop_column("ticket_sla", "voided_at")

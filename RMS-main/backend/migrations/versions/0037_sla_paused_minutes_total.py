"""sla: accumulate completed pause credit on ticket_sla

docs/modules/sla/spec.md §0.3, §2.5, §5; Task 7 (pause and resume), closing the
gap the Task 5 review found and quantified.

WHAT WAS BROKEN. `ticket_sla` stored only `paused_at` -- an *open*-pause
marker. Once a pause ended, the only surviving record of it was the offset
already folded into `response_due_at`/`resolution_due_at`. §0.3's recompute
re-derives both deadlines from `ticket.created_at`, so it overwrote that offset
with a value that never knew a pause had happened. Every completed pause's
credit was silently dropped by the next re-triage.

Measured on a live database before this migration (24/7 calendar, ticket filed
Monday 09:00, 480-minute resolution target):

    start_tracking                         -> resolution_due_at 17:00
    pause + resume, 120 completed minutes  -> resolution_due_at 19:00
    recompute onto a policy with IDENTICAL
    targets (a pure queue move)            -> resolution_due_at 17:00

Two hours of credit destroyed by a recompute that changed nothing at all.

WHY IT MATTERS MORE THAN TWO HOURS. On a business-hours calendar the same
arithmetic re-derives a resolution deadline from Thursday 17:00 back to Tuesday
17:00 -- two working days already breached at the instant of a *correct*
routing action. The sweep then stamps `resolution_breached_at`, publishes
`ticket.sla_breached`, escalates the priority and notifies staff, all for a
promise that was never actually missed. The error is one-directional (a dropped
credit can only shorten a deadline, never lengthen it), so no real breach is
ever missed -- but reported SLA attainment is understated by the total paused
time of every ticket ever re-triaged after a pause, and the false breaches are
indistinguishable from real ones in the same reporting.

WHY A COLUMN AND NOT A DERIVATION. There is nothing to derive from: no pause
history table exists (§2.5 gives `ticket_sla` one row per ticket), and the
audit log deliberately does not record SLA materialisation (`tracking.py`'s
module docstring explains why). A running total is the smallest thing that
makes recompute idempotent with respect to pause. Per-pause history is a §13
concern; this column answers the only question the clock asks -- "how much
working time did we already give back?".

NOT NULL DEFAULT 0, with no NULL-means-"unknown" state: a NULL would have to be
coerced somewhere, and the only safe coercion is 0, so the ambiguity would buy
nothing and could silently swallow a write bug. Existing rows backfill to 0,
which is exactly right for them -- their completed-pause credit is already lost
and unrecoverable, and pretending otherwise would invent time.

`paused_at` stays. The two columns answer different questions and are both
needed: `paused_at` is "is the clock stopped right now" (the sweep's breach
pass skips rows where it is non-NULL); `paused_minutes_total` is "how much has
already been given back" (what recompute folds back in). Neither can be
computed from the other.

0033-0036 are shipped and migrations are forward-only, so this is a new
revision rather than an edit to 0033. `app/modules/sla/models.py::TicketSla`
declares the column in lockstep -- `test_schema_drift.py` compares
`Base.metadata` to the live schema and fails if either side moves alone.

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ticket_sla",
        sa.Column(
            "paused_minutes_total",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Working time is never negative (`working_minutes_between` floors at 0 so
    # that `end` before `start` is zero rather than a negative credit -- "a
    # paused ticket must never buy time back"). This says the same thing at the
    # database, where it also covers any future writer that is not
    # `tracking.resume`.
    op.create_check_constraint(
        "ck_ticket_sla_paused_minutes_non_negative",
        "ticket_sla",
        "paused_minutes_total >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ticket_sla_paused_minutes_non_negative", "ticket_sla", type_="check"
    )
    op.drop_column("ticket_sla", "paused_minutes_total")

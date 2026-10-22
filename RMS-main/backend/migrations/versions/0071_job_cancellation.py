"""jobs: a terminal `cancelled` state, and the record of who asked for it

Cycle 1 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §10 --
withdrawal and honest state. A job queued for a machine that has been off for
a week could not be withdrawn at all; `POST /api/v1/jobs/{job_id}/cancel` now
moves such a job straight to `cancelled` with `cancel_outcome = 'withdrawn'`,
which is the one outcome this platform can assert on its own authority
because the envelope was never written to a socket.

WHY THE VALUE IS APPENDED. `enumsortorder` is what `ORDER BY status` and every
range comparison on the column mean, so inserting `cancelled` next to the
other terminal states would silently re-sort rows written before it existed.
It goes last, like every value added to a live enum should.

WHY THIS MIGRATION WRITES NO ROWS. Postgres allows `ALTER TYPE ... ADD VALUE`
inside a transaction block, but the new value may not be USED in the same
transaction that added it. Nothing here inserts, casts or compares
`'cancelled'`, so one migration is enough -- and nothing that does may ever be
added to this file.

THREE COLUMNS, NOT ONE. `cancel_outcome` alone would say what happened but not
when or on whose say-so, and `cancel_requested_at` is read by
`app/worker.py::sweep_org` as the guard that stops a cancelled job being
re-signed and republished ten minutes later. `cancel_requested_by` is a
foreign key to `users` and is returned by no API route: the actor's name
belongs in the hash-chained audit log, which `JobOut` has never disclosed.

NO NEW INDEX. `ix_jobs_org_status` already serves the console's filters, and
the sweep's new `cancel_requested_at IS NULL` predicate rides an existing
`status = 'dispatched'` scan over a set bounded by ten minutes of dispatches.

THE DOWNGRADE HAS TO WORK AND POSTGRES CANNOT REMOVE AN ENUM VALUE, so it
recreates the type. That is the whole reason this `downgrade()` is longer than
its `upgrade()`, and it is not the `0001` carve-out: this is a new migration
whose downgrade must be correct the first time, because CI's `migrations` job
runs `upgrade head -> downgrade base -> upgrade head`.

Revision ID: 0071
Revises: 0070
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"

#: The seven values `0001_core_schema.py` created, in their original order.
#: The downgrade rebuilds the type from exactly this list.
_PRE_0071_STATUSES = (
    "queued",
    "dispatched",
    "running",
    "succeeded",
    "failed",
    "expired",
    "refused",
)


def upgrade() -> None:
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'cancelled'")
    op.add_column(
        _JOBS, sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_JOBS, sa.Column("cancel_requested_by", sa.UUID(), nullable=True))
    op.add_column(_JOBS, sa.Column("cancel_outcome", sa.String(length=24), nullable=True))
    op.create_foreign_key(
        "jobs_cancel_requested_by_fkey",
        _JOBS,
        "users",
        ["cancel_requested_by"],
        ["id"],
    )


def downgrade() -> None:
    """Undo the columns AND the enum value, in that order.

    Rollback in this platform is restore-from-backup (docs/superpowers/specs/
    2026-08-15-deploy-rollback-design.md D1), so this exists for CI's round
    trip rather than for an operator. It is still written to run on a database
    that holds real rows, because a downgrade that only works on an empty
    schema is a downgrade that has not been thought about.

    THE ROWS ARE MAPPED BEFORE THE TYPE IS REBUILT. `USING status::text::
    job_status` throws on any row whose text is not a value of the new type, so
    every `cancelled` job becomes `failed` first. That loses information, and
    the alternative loses more: `queued` would resurrect a job the operator
    withdrew and hand it to the endpoint on the next connect. `failed` is
    terminal, which is the property that has to survive. The `cancel_outcome`
    column that recorded which it was is dropped in the same breath, so nothing
    is left claiming to know.
    """
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("jobs_cancel_requested_by_fkey", _JOBS, type_="foreignkey")
    op.drop_column(_JOBS, "cancel_outcome")
    op.drop_column(_JOBS, "cancel_requested_by")
    op.drop_column(_JOBS, "cancel_requested_at")

    values = ", ".join(f"'{value}'" for value in _PRE_0071_STATUSES)
    op.execute("ALTER TYPE job_status RENAME TO job_status_pre_0071")
    op.execute(f"CREATE TYPE job_status AS ENUM ({values})")
    op.execute(
        "ALTER TABLE jobs ALTER COLUMN status TYPE job_status "
        "USING status::text::job_status"
    )
    op.execute("DROP TYPE job_status_pre_0071")

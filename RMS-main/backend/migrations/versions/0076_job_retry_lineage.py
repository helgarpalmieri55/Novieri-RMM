"""jobs: a retry is a NEW job that remembers which one it re-ran

Cycle 3 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §10 --
retry. `POST /api/v1/jobs/{job_id}/retry` creates a fresh `jobs` row through
`JobEngine.create`: new id, new nonce, a signature over different bytes, and
params re-validated against the type's model. The original row is never
written to.

WHY A NEW ROW AND NOT A RE-DISPATCH OF THE OLD ONE. `resign_for_retry` mutates
in place, which is right for the stuck-dispatch sweep (the job never reached a
terminal state) and wrong for an operator retrying a `failed` job, for three
reasons that are properties of the code rather than preferences:

  1. it would move a TERMINAL state back to `queued`, and "terminal states
     never transition" is what makes a job's outcome final;
  2. `settle_action_for_job` settles an `ai_action` on the FIRST terminal
     state and is idempotent, so a job that un-terminals leaves the AI
     confirmation history saying an action settled while its job runs again;
  3. the first attempt's `result` -- the failure body an operator is retrying
     FROM -- would be overwritten the moment they retried.

WHY THIS IS NOT A REPLAY. The new envelope is signed over a different `id` and
a different `nonce`, so the old signature verifies nothing here and the new
one covers this dispatch alone. Re-executing stored signed bytes would be a
replay primitive; the agent's own nonce store would refuse it anyway.

ONE NULLABLE COLUMN, NO DEFAULT. A job that is not a retry has no original,
and any non-null default would be a claim about a lineage that does not exist.

`ondelete` IS DELIBERATELY OMITTED, i.e. NO ACTION. A job row is never deleted
by this platform -- the jobs table is the record of what was commanded -- and
if that ever changes, a foreign key that refuses the delete is the right
failure: a retry whose original has silently vanished is a lineage that lies.

NO NEW INDEX. The column is read one row at a time by primary key (the console
resolves "retried from" for the job it is already showing), and the reverse
lookup ("what retried this?") has no caller. An index for a query nobody makes
is a write cost on the hottest table in the platform.

THE DOWNGRADE IS ONE DROP AND IT IS CORRECT THE FIRST TIME, because CI's
`migrations` job runs `upgrade head -> downgrade base -> upgrade head`. Unlike
0071 there is no enum to rebuild: dropping a nullable column with a foreign key
takes the constraint with it.

Revision ID: 0076
Revises: 0075
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS = "jobs"


def upgrade() -> None:
    op.add_column(_JOBS, sa.Column("retry_of_job_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "jobs_retry_of_job_id_fkey",
        _JOBS,
        _JOBS,
        ["retry_of_job_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("jobs_retry_of_job_id_fkey", _JOBS, type_="foreignkey")
    op.drop_column(_JOBS, "retry_of_job_id")

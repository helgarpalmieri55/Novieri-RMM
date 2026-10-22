"""scrub_backup_repo_keys: remove repository keys from finished backup jobs

`backup.run` and `backup.restore` carried the repository encryption key in
`jobs.params` with no terminal scrubber, so every finished backup job kept it
in the database (invariant 3). The job types now declare one; this removes
the keys already stored. Jobs still queued or running keep theirs, because
the agent needs the key to do the work, and the scrubber removes it when they
finish.

Data only, no schema change. `downgrade()` cannot restore removed keys and
does nothing.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE jobs
           SET params = params - 'repo_key'
         WHERE type IN ('backup.run', 'backup.restore')
           AND status::text IN ('succeeded', 'failed', 'expired', 'refused', 'cancelled')
           AND params ? 'repo_key'
        """
    )
    # `status::text`, not the enum literal: on a fresh database the whole chain
    # runs in one transaction, and Postgres refuses a value added to an enum
    # earlier in that same transaction (`cancelled`).


def downgrade() -> None:
    pass

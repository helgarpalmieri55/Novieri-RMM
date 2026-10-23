"""backup consistency and warnings on runs, points and restores

spec-server-backup.md §3.1, §5.3. A format-2 agent reports how consistent a
point is (snapshot, or a live read that is not crash-consistent) and a list of
warnings; a restore reports warnings too. They are stored so the console and
the coverage report can say "Not crash-consistent: files were read while in
use" instead of calling every point equal. All nullable: format-1 agents send
none of it.

Revision ID: 0092
Revises: 0091
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backup_runs", sa.Column("consistency", postgresql.JSONB, nullable=True))
    op.add_column("backup_runs", sa.Column("warnings", postgresql.JSONB, nullable=True))
    op.add_column("backup_points", sa.Column("consistency", postgresql.JSONB, nullable=True))
    op.add_column(
        "backup_points", sa.Column("consistency_level", sa.String(16), nullable=True)
    )
    op.add_column("backup_points", sa.Column("crash_consistent", sa.Boolean, nullable=True))
    op.add_column("backup_points", sa.Column("warnings", postgresql.JSONB, nullable=True))
    op.add_column("restore_jobs", sa.Column("warnings", postgresql.JSONB, nullable=True))
    op.create_index(
        "ix_backup_points_consistency", "backup_points", ["org_id", "consistency_level"]
    )


def downgrade() -> None:
    op.drop_index("ix_backup_points_consistency", table_name="backup_points")
    op.drop_column("restore_jobs", "warnings")
    op.drop_column("backup_points", "warnings")
    op.drop_column("backup_points", "crash_consistent")
    op.drop_column("backup_points", "consistency_level")
    op.drop_column("backup_points", "consistency")
    op.drop_column("backup_runs", "warnings")
    op.drop_column("backup_runs", "consistency")

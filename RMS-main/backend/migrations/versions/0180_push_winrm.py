"""push_host_key.readiness for the WinRM probe (WinRM push, design §2.4).

The WinRM probe records whether 5986/5985 answered and whether a KDC was
reachable, instead of an SSH host key. That is per-target readiness state, so
it lives on the existing `push_host_key` row (one per org+address) rather than
a new table. Additive and nullable: every existing row and every ssh probe
leaves it NULL.

Forward-only. `downgrade()` drops the column; it has never run in production
(rollback is restore-from-backup) and CI runs upgrade->downgrade->upgrade.

Revision ID: 0180
Revises: 0170
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0180"
down_revision: str | None = "0170"  # PR 1 install-link head (confirmed with `alembic heads`).
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_host_key",
        sa.Column("readiness", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("push_host_key", "readiness")

"""platform: the health samples table

docs/superpowers/specs/2026-08-15-platform-health-design.md §6.

THE FIRST TABLE WITHOUT A TENANT COLUMN, and it is handled explicitly rather
than quietly. RLS is ENABLED and FORCED with NO tenant policy -- which in
Postgres means deny-all -- plus exactly one policy granting `app_user`. So the
table is not an RLS-exempt hole; its policy is "the application, and nobody
else". `tests/test_tenant_column_sweep.py` asserts it stays the only one.

Revision ID: 0055
Revises: 0054
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_health_sample"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("probe", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_phs_probe_sampled", _TABLE, ["probe", "sampled_at"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY application_only ON {_TABLE} "
        "FOR ALL TO app_user USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO app_user")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS application_only ON {_TABLE}")
    op.drop_index("ix_phs_probe_sampled", table_name=_TABLE)
    op.drop_table(_TABLE)

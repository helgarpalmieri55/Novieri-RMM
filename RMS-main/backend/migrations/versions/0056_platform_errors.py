"""platform: error groups and events

docs/superpowers/specs/2026-08-15-error-monitoring-design.md §4.

THE SECOND AND THIRD TABLES WITHOUT A TENANT COLUMN, after
`platform_health_sample` (0055). RLS is ENABLED and FORCED on both with NO
tenant policy -- deny-all in Postgres -- plus exactly one policy granting
`app_user`. `tests/test_tenant_column_sweep.py` names both in
`TENANTLESS_BY_DESIGN`.

UNLIKE 0055: the grant here includes UPDATE. `platform_error_group` rows are
updated in place -- `last_seen`, `event_count`, `resolved_at` -- every time a
second occurrence of an already-seen fingerprint drains; a grant of
`SELECT, INSERT, DELETE` alone lets the first occurrence insert and the
second fail.

Revision ID: 0056
Revises: 0055
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP = "platform_error_group"
_EVENT = "platform_error_event"


def upgrade() -> None:
    op.create_table(
        _GROUP,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.String(16), nullable=False, unique=True),
        sa.Column("exc_type", sa.String(255), nullable=False),
        sa.Column("module", sa.String(255), nullable=False),
        sa.Column("lineno", sa.Integer(), nullable=False),
        sa.Column("qualname", sa.String(255), nullable=False),
        sa.Column("where_", sa.String(16), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_peg_last_seen", _GROUP, ["last_seen"])

    op.create_table(
        _EVENT,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey(f"{_GROUP}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_pee_group_occurred", _EVENT, ["group_id", "occurred_at"])

    for table in (_GROUP, _EVENT):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY application_only ON {table} "
            "FOR ALL TO app_user USING (true) WITH CHECK (true)"
        )
        # UPDATE matters here -- see the module docstring. `platform_health_
        # sample` (0055) never updates a row and correctly omits it; this pair
        # does and must not.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user")


def downgrade() -> None:
    for table in (_EVENT, _GROUP):
        op.execute(f"DROP POLICY IF EXISTS application_only ON {table}")
    op.drop_index("ix_pee_group_occurred", table_name=_EVENT)
    op.drop_table(_EVENT)
    op.drop_index("ix_peg_last_seen", table_name=_GROUP)
    op.drop_table(_GROUP)

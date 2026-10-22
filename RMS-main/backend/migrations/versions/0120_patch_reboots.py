"""patch_reboots: restarts a device owes after patching

docs/modules/patch/spec.md §8.1. One row per restart: `pending` until the
maintenance window opens, `dispatched` while a signed `patch.reboot` is on its
way, `scheduled` once the OS has a restart booked, then `completed` (a later
scan's boot time is after the row), `cancelled` or `failed`. A partial unique
index holds a device to one open restart. FORCE RLS like every patch table.

Revision ID: 0120
Revises: 0100
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0120"
down_revision: str | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def _uuid(name: str, *fk: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        *(sa.ForeignKey(f) for f in fk),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    op.create_table(
        "patch_reboots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("device_id", "devices.id"),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        _uuid("apply_job_id", "jobs.id", nullable=True),
        _uuid("job_id", "jobs.id", nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("scheduled_for"),
        sa.Column("user_present", sa.Boolean(), nullable=True),
        _ts("verify_requested_at"),
        _ts("closed_at"),
        sa.Column("reason", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.CheckConstraint(
            "state IN ('pending', 'dispatched', 'scheduled', 'completed', 'cancelled', 'failed')",
            name="ck_patch_reboots_state",
        ),
        sa.CheckConstraint("origin IN ('apply', 'scan')", name="ck_patch_reboots_origin"),
    )
    op.create_index(
        "uq_patch_reboots_open",
        "patch_reboots",
        ["org_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'dispatched', 'scheduled')"),
    )
    op.create_index("ix_patch_reboots_state", "patch_reboots", ["org_id", "state"])
    op.execute("ALTER TABLE patch_reboots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE patch_reboots FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON patch_reboots "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON patch_reboots")
    op.drop_index("ix_patch_reboots_state", table_name="patch_reboots")
    op.drop_index("uq_patch_reboots_open", table_name="patch_reboots")
    op.drop_table("patch_reboots")

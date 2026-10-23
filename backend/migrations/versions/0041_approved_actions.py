"""the shared approved-action registry and the autonomy gate

docs/modules/endpoint-health-alerting/spec.md §2.9, §2.10, §8.3, §8.4.

Two KERNEL tables, not health tables. Phase 17's AI-automation layer shares
this registry (`docs/06-roadmap.md`: "one registry, one dial, one audit"), so
it lives beside the other kernel models in `app/core/approved_actions.py`.
Phase 17 adds `personas` VALUES to existing rows; it does not add a second
approved-action table, and a review that sees one should reject it.

THE HARD GATE IS A DATABASE CHECK, NOT APPLICATION LOGIC
(`ck_approved_action_autonomous_is_bounded`). "Nothing destructive or
fleet-wide auto-fires" has to survive a future persona, a future service
refactor and a direct SQL fixup. As a Python validator it survives none of
those; as a CHECK, relaxing it takes a migration, which a reviewer sees.

The gate's third clause (`max_target_scope <> 'device'`) cannot fire in v1
because `ck_approved_action_max_target_scope` already forbids every other
value. It is written anyway and deliberately: when Phase 17 widens the scope
CHECK by a reviewed migration, the gate must already be in place, or widening
the scope silently re-opens autonomous fleet-wide execution.

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = ["approved_action", "approved_action_script_exemption"]


def upgrade() -> None:
    op.create_table(
        "approved_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # NOT NULL, no global rows (spec §2.9): a global approved action would
        # be a cross-tenant command path into every fleet.
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("params_template", postgresql.JSONB(), nullable=False),
        sa.Column(
            "script_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scripts.id"), nullable=True
        ),
        sa.Column("script_version", sa.Integer(), nullable=True),
        sa.Column("risk_class", sa.String(8), nullable=False),
        sa.Column("destructive", sa.Boolean(), nullable=False),
        sa.Column(
            "max_target_scope", sa.String(8), server_default="device", nullable=False
        ),
        sa.Column("autonomy", sa.String(16), nullable=False),
        sa.Column("personas", postgresql.JSONB(), nullable=False),
        sa.Column("cooldown_s", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("max_per_device_per_day", sa.Integer(), server_default="3", nullable=False),
        sa.Column("max_per_org_per_hour", sa.Integer(), server_default="20", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "key", name="uq_approved_action_org_key"),
        sa.CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')", name="ck_approved_action_risk_class"
        ),
        sa.CheckConstraint(
            "autonomy IN ('suggest', 'require_approval', 'autonomous')",
            name="ck_approved_action_autonomy",
        ),
        sa.CheckConstraint(
            "max_target_scope IN ('device')", name="ck_approved_action_max_target_scope"
        ),
        sa.CheckConstraint(
            "NOT (autonomy = 'autonomous' AND (destructive OR risk_class = 'high' "
            "OR max_target_scope <> 'device'))",
            name="ck_approved_action_autonomous_is_bounded",
        ),
        # `script_id`/`script_version` are REQUIRED for `script.run` and
        # FORBIDDEN otherwise. Without the pin, approving a script approves its
        # future contents -- `update_script` bumps `version` in place on the
        # same row and the old body is not retained anywhere.
        sa.CheckConstraint(
            "(job_type = 'script.run') = (script_id IS NOT NULL AND script_version IS NOT NULL)",
            name="ck_approved_action_script_pin",
        ),
        sa.CheckConstraint("cooldown_s >= 0", name="ck_approved_action_cooldown_nonneg"),
        sa.CheckConstraint(
            "max_per_device_per_day > 0 AND max_per_org_per_hour > 0",
            name="ck_approved_action_fuses_positive",
        ),
    )
    op.create_index(
        "ix_approved_action_org_active", "approved_action", ["org_id", "active"]
    )

    op.create_table(
        "approved_action_script_exemption",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        # ON DELETE CASCADE (spec §2.10): deleting a script must not leave an
        # exemption a later script could inherit by id reuse.
        sa.Column(
            "script_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("script_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "granted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        # Keyed to the PAIR, not to an approved action: two approved actions
        # naming the same pinned version share one exemption.
        sa.UniqueConstraint(
            "org_id",
            "script_id",
            "script_version",
            name="uq_approved_action_script_exemption",
        ),
        sa.CheckConstraint(
            "script_version > 0", name="ck_approved_action_script_exemption_version_positive"
        ),
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0025, 0032, 0033).
    # WITH CHECK is stated explicitly. It is NOT filling a gap -- for an
    # ALL-command policy PostgreSQL reuses the USING expression as WITH CHECK
    # whenever WITH CHECK is omitted, so a USING-only policy here would reject a
    # cross-org INSERT identically. The explicit clause matters the day a future
    # edit gives this policy a different read predicate than write predicate: an
    # omitted WITH CHECK would silently follow that new USING expression instead
    # of keeping the original tenant check. A cross-tenant exemption in
    # particular would be a cross-tenant path to `autonomous` execution of
    # arbitrary code.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("approved_action_script_exemption")
    op.drop_index("ix_approved_action_org_active", table_name="approved_action")
    op.drop_table("approved_action")

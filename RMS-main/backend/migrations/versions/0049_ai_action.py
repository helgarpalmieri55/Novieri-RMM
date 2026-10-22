"""ai-automation: the ai_action instance table and its preview clock

docs/modules/ai-automation.md §5.1, §5.2, §6.4, §6.5.

FOUR THINGS HERE ARE NOT ORDINARY DDL.

1. `session_id` IS `ON DELETE RESTRICT`, AND `ai_message` STILL CASCADES. That
   asymmetry is the point (§5.1). A transcript is deletable BECAUSE IT IS NOT
   AUDIT; an `ai_action` that reached `dispatched` names a signed job that ran
   on somebody's machine, and the retention sweep may only blank its
   `rationale`. RESTRICT is not conditional -- a session whose actions are all
   `rejected` cannot be hard-deleted either. That is the honest cost, and the
   existing `ai_admin` transcript sweep has to handle it rather than start
   raising in production.

2. `effective_autonomy IN ('suggest','require_approval')`. A stored `off` would
   be a row that should never have been minted (§4.2: `off` mints nothing), and
   `autonomous` is unreachable by an AI persona in v1 (§0.4).

3. THERE IS NO UNIQUE CONSTRAINT AND NO IDEMPOTENCY KEY (§0.5). Reusing
   `health_alert_action`'s `UNIQUE (alert_id, kind)` was considered and refused:
   an AI proposal has no alert, and two proposals of one action in one session
   are two decisions a human may answer differently. A second confirm is a 409
   by the state machine.

4. `ai_settings` GAINS TWO TUNABLES WITH CHECK CEILINGS, in the same style as
   the four `0045` added. §6.4 bounds the preview clock at 60 s - 24 h and §6.5
   caps proposals per turn at 1-10; neither section said WHERE the value lives.
   RECORDED SPEC GAP, closed here: they live on `ai_settings`, beside the four
   tunables that already carry ceilings there, because every write to that table
   is already `ai.admin` and already audited. Recorded back into the spec.

Every index below is declared in `app/models/ai.py::AiAction.__table_args__` as
well -- `tests/test_schema_drift.py::test_metadata_indexes_match_live_schema`
compares `Base.metadata` to the live schema in BOTH directions, so an unmodelled
index fails exactly as loudly as a missing one.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

_INDEXES = [
    # The expiry sweep's own predicate, in its own order.
    "CREATE INDEX ix_ai_action_org_state_expiry ON ai_action (org_id, state, expires_at)",
    # "what did this conversation propose"
    "CREATE INDEX ix_ai_action_org_session ON ai_action (org_id, session_id)",
    # "what has the AI done to this machine"
    "CREATE INDEX ix_ai_action_org_device ON ai_action (org_id, target_device_id)",
]


def upgrade() -> None:
    op.create_table(
        "ai_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        # Point 1 of the module docstring.
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_session.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("message_seq", sa.Integer(), nullable=False),
        sa.Column("persona", sa.String(16), nullable=False),
        sa.Column(
            "approved_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approved_action.id"),
            nullable=False,
        ),
        sa.Column("approved_action_key", sa.String(100), nullable=False),
        sa.Column("job_type", sa.String(100), nullable=False),
        # §0.12: ONE device. No group column, no target array, no selector.
        sa.Column(
            "target_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=False,
        ),
        sa.Column("rendered_params", postgresql.JSONB(), nullable=False),
        sa.Column(
            "script_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scripts.id"), nullable=True
        ),
        sa.Column("script_version", sa.Integer(), nullable=True),
        sa.Column("risk_class", sa.String(8), nullable=False),
        sa.Column("destructive", sa.Boolean(), nullable=False),
        sa.Column("effective_autonomy", sa.String(20), nullable=False),
        sa.Column("tainted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("preview_digest", sa.CHAR(64), server_default="", nullable=False),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "confirmed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.String(200), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "persona IN ('end_user', 'technician', 'admin')", name="ck_ai_action_persona"
        ),
        sa.CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')", name="ck_ai_action_risk_class"
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'previewed', 'confirmed', 'dispatched', 'settled', "
            "'rejected', 'expired', 'failed')",
            name="ck_ai_action_state",
        ),
        # Point 2.
        sa.CheckConstraint(
            "effective_autonomy IN ('suggest', 'require_approval')",
            name="ck_ai_action_effective_autonomy",
        ),
    )

    for statement in _INDEXES:
        op.execute(statement)

    # Standard tenant_isolation pattern (0002_rls.py, 0045_ai_admin.py), with
    # WITH CHECK stated explicitly.
    op.execute("ALTER TABLE ai_action ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_action FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON ai_action "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    # Point 4.
    op.add_column(
        "ai_settings",
        sa.Column("action_expiry_s", sa.Integer(), server_default="900", nullable=False),
    )
    op.add_column(
        "ai_settings",
        sa.Column("max_proposals_per_turn", sa.Integer(), server_default="3", nullable=False),
    )
    op.create_check_constraint(
        "ck_ai_settings_action_expiry",
        "ai_settings",
        "action_expiry_s BETWEEN 60 AND 86400",
    )
    op.create_check_constraint(
        "ck_ai_settings_max_proposals",
        "ai_settings",
        "max_proposals_per_turn BETWEEN 1 AND 10",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_settings_max_proposals", "ai_settings", type_="check")
    op.drop_constraint("ck_ai_settings_action_expiry", "ai_settings", type_="check")
    op.drop_column("ai_settings", "max_proposals_per_turn")
    op.drop_column("ai_settings", "action_expiry_s")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ai_action")
    # Dropping the table drops its indexes.
    op.drop_table("ai_action")

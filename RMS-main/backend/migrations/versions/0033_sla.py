"""sla: business calendars, resolution policies, ticket sla state

docs/modules/sla/spec.md §2, §8, §11.

Revision ID: 0033
Revises: 0032
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = [
    "sla_calendar",
    "sla_calendar_window",
    "sla_calendar_holiday",
    "sla_policy",
    "ticket_sla",
    "sla_org_settings",
]

# NOTE: the live `permissions` table has columns (id, code, module, action) --
# NO `resource` and NO `description` column (verified against
# 0032_email_intake.py and app/models/core.py::Permission). The
# human-readable description lives only in app/core/rbac.py's PERMISSIONS
# dict, matching every other module's migration.
#
# `sla.admin` is HIGH-PRIVILEGE (spec §8): it sets what the organisation has
# promised its customers and can silence breach escalation entirely. Admin
# tier only, never technician. Both codes go to super-admin/org-admin, as
# every other module's migration does, so the full-permission invariant
# `test_org_admin_has_all_permissions` continues to hold.
PERMISSIONS = [
    ("sla.read", "sla", "read"),
    ("sla.admin", "sla", "admin"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": [c for c, _, _ in PERMISSIONS],
    "org-admin": [c for c, _, _ in PERMISSIONS],
}


def upgrade() -> None:
    op.create_table(
        "sla_calendar",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_sla_calendar_org_name"),
    )

    op.create_table(
        "sla_calendar_window",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "calendar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_calendar.id"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.CheckConstraint(
            "start_time < end_time", name="ck_sla_calendar_window_start_before_end"
        ),
    )

    op.create_table(
        "sla_calendar_holiday",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "calendar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_calendar.id"),
            nullable=False,
        ),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "calendar_id", "holiday_date", name="uq_sla_calendar_holiday_cal_date"
        ),
    )

    op.create_table(
        "sla_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "queue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_queue.id"),
            nullable=True,
        ),
        sa.Column("priority", sa.String(16), nullable=True),
        sa.Column(
            "calendar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_calendar.id"),
            nullable=False,
        ),
        sa.Column("response_target_minutes", sa.Integer(), nullable=False),
        sa.Column("resolution_target_minutes", sa.Integer(), nullable=False),
        sa.Column("escalate_priority", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "escalate_to_queue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_queue.id"),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("response_target_minutes > 0", name="ck_sla_policy_response_positive"),
        sa.CheckConstraint(
            "resolution_target_minutes > 0", name="ck_sla_policy_resolution_positive"
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent') OR priority IS NULL",
            name="ck_sla_policy_priority_valid",
        ),
    )

    # Spec §2.4/§3: a NULL never collides with another NULL in a Postgres
    # unique constraint, so a plain UNIQUE(org_id, queue_id, priority) only
    # actually enforces uniqueness for the fully-specific case below (level
    # 1) -- an org could otherwise hold two conflicting `(NULL, NULL)`
    # fallback policies (or two conflicting queue-only / priority-only ones),
    # and resolution would then pick one arbitrarily. Four partial unique
    # indexes, one per §3 resolution level, close that gap explicitly.
    op.execute(
        "CREATE UNIQUE INDEX uq_sla_policy_queue_and_priority ON sla_policy "
        "(org_id, queue_id, priority) WHERE queue_id IS NOT NULL AND priority IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sla_policy_queue_wildcard_priority ON sla_policy "
        "(org_id, queue_id) WHERE queue_id IS NOT NULL AND priority IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sla_policy_wildcard_queue_priority ON sla_policy "
        "(org_id, priority) WHERE queue_id IS NULL AND priority IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sla_policy_wildcard_both ON sla_policy "
        "(org_id) WHERE queue_id IS NULL AND priority IS NULL"
    )

    op.create_table(
        "ticket_sla",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_policy.id"),
            nullable=False,
        ),
        sa.Column(
            "calendar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sla_calendar.id"),
            nullable=False,
        ),
        sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_met_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_met_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_source", sa.String(16), nullable=False),
        sa.Column("last_recomputed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "policy_source IN ('initial', 'retriage', 'escalation')",
            name="ck_ticket_sla_policy_source_valid",
        ),
    )

    # Spec §2.5/§7: what make the sweep an index scan rather than a table walk.
    op.execute(
        "CREATE INDEX ix_ticket_sla_org_response_due ON ticket_sla (org_id, response_due_at) "
        "WHERE response_breached_at IS NULL AND response_met_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_ticket_sla_org_resolution_due ON ticket_sla (org_id, resolution_due_at) "
        "WHERE resolution_breached_at IS NULL AND resolution_met_at IS NULL"
    )

    op.create_table(
        "sla_org_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("auto_close_after_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "auto_close_after_days > 0", name="ck_sla_org_settings_auto_close_positive"
        ),
    )

    # Standard tenant_isolation pattern (migrations/versions/0032_email_intake.py).
    # WITH CHECK is stated explicitly to match 0002_rls.py's/0025's/0032's
    # convention. It is NOT filling a gap: for an ALL-command policy,
    # PostgreSQL reuses the USING expression as WITH CHECK whenever WITH
    # CHECK is omitted, so a USING-only policy here would reject a cross-org
    # INSERT identically. The explicit clause matters only the day a future
    # edit gives this policy a different read predicate than write predicate
    # -- an omitted WITH CHECK would silently follow that new USING
    # expression instead of keeping the original tenant check.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'sla')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'sla'")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("sla_org_settings")
    op.drop_table("ticket_sla")
    op.drop_table("sla_policy")
    op.drop_table("sla_calendar_holiday")
    op.drop_table("sla_calendar_window")
    op.drop_table("sla_calendar")

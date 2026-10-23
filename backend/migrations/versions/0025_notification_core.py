"""notification core: outbox, channels, rules, queue members, deliveries, allowlist

docs/modules/notification-core/spec.md.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES = [
    "domain_events",
    "notification_channels",
    "notification_rules",
    "queue_members",
    "notification_deliveries",
    "webhook_allowlist_entries",
]

# Spec §8: notification.admin controls where control-plane data is sent and
# which internal hosts the server will connect to (the webhook allowlist) --
# admin tier only, never granted to technician.
PERMISSIONS = [
    ("notification.read", "notification", "read"),
    ("notification.admin", "notification", "admin"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ["notification.read", "notification.admin"],
    "org-admin": ["notification.read", "notification.admin"],
}


def upgrade() -> None:
    op.create_table(
        "domain_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # Spec §2.1: NULLABLE on purpose -- machine-origin events have no actor.
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_domain_events_drain", "domain_events", ["claimed_at", "occurred_at"])
    op.create_index("ix_domain_events_org_type", "domain_events", ["org_id", "event_type"])

    op.create_table(
        "notification_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('webhook', 'email')", name="ck_channel_kind"),
        sa.UniqueConstraint("org_id", "name", name="uq_channel_org_name"),
    )

    op.create_table(
        "notification_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_channels.id"), nullable=False),
        # NOTE: the ticket-queue table is named `ticket_queue` (singular) --
        # see backend/app/modules/service_desk/models.py -- not `ticket_queues`.
        sa.Column("queue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_queue.id"), nullable=True),
        sa.Column("recipient_kind", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "recipient_kind IN ('queue_members', 'assignee', 'requester', 'channel_default')",
            name="ck_rule_recipient_kind",
        ),
        # Spec §6.4 defence in depth: the service rejects this pairing at write
        # time; the constraint makes it unrepresentable even via direct SQL.
        sa.CheckConstraint(
            "NOT (event_type = 'ticket.internal_note_added' AND recipient_kind = 'requester')",
            name="ck_rule_no_internal_to_requester",
        ),
    )
    op.create_index("ix_rules_org_event", "notification_rules", ["org_id", "event_type"])

    op.create_table(
        "queue_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("queue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_queue.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("queue_id", "user_id", name="uq_queue_member"),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("domain_events.id"), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("notification_channels.id"), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'dead')", name="ck_delivery_status"
        ),
    )
    op.create_index(
        "ix_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"]
    )

    op.create_table(
        "webhook_allowlist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "host", name="uq_allowlist_org_host"),
    )

    # Standard tenant_isolation pattern (see migrations/versions/0002_rls.py):
    # the app session sets `app.current_org` per-transaction (app.core.tenancy
    # .org_session); `missing_ok=true` means a session with no tenant context
    # sees zero rows and cannot insert. WITH CHECK is stated explicitly (not
    # relied upon as the USING fallback) to match every other tenant-scoped
    # migration in this repo.
    ctx = "current_setting('app.current_org', true)::uuid"
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {ctx}) WITH CHECK (org_id = {ctx})"
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
        "(SELECT id FROM permissions WHERE module = 'notification')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'notification'")

    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("webhook_allowlist_entries")
    op.drop_table("notification_deliveries")
    op.drop_table("queue_members")
    op.drop_table("notification_rules")
    op.drop_table("notification_channels")
    op.drop_table("domain_events")

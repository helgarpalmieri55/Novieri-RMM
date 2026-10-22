"""email intake: mailbox config, inbound message log, reply token

docs/modules/email-intake/spec.md §2, §5.1, §9, §12.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = ["email_mailbox", "inbound_message"]

# NOTE: the live `permissions` table has columns (id, code, module, action) --
# NO `resource` and NO `description` column (verified against
# 0025_notification_core.py and app/models/core.py::Permission). The
# human-readable description lives only in app/core/rbac.py's PERMISSIONS
# dict, matching every other module's migration.
#
# `email_intake.admin` is HIGH-PRIVILEGE (spec §9): it sets which mailbox the
# platform authenticates to and which category inbound tickets land in. Admin
# tier only, never technician. Both codes go to super-admin/org-admin, as
# every other module's migration does, so the full-permission invariant
# `test_org_admin_has_all_permissions` continues to hold.
PERMISSIONS = [
    ("email_intake.read", "email_intake", "read"),
    ("email_intake.admin", "email_intake", "admin"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": [c for c, _, _ in PERMISSIONS],
    "org-admin": [c for c, _, _ in PERMISSIONS],
}


def upgrade() -> None:
    op.create_table(
        "email_mailbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("use_ssl", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("folder", sa.String(100), server_default="INBOX", nullable=False),
        sa.Column("username", sa.String(320), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("default_category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket_category.id"), nullable=False),
        sa.Column("reopen_window_days", sa.Integer(), server_default="14", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        # Not in spec §2.1's column list: required by §4's per-mailbox
        # `SELECT ... FOR UPDATE SKIP LOCKED` claim (Task 10 amends the spec).
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "address", name="uq_email_mailbox_org_address"),
    )

    op.create_table(
        "inbound_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("email_mailbox.id"), nullable=False),
        sa.Column("message_id", sa.String(998), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(512), server_default="", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(200), server_default="", nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket.id"), nullable=True),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "message_id", name="uq_inbound_message_org_message_id"),
        sa.CheckConstraint(
            "disposition IN ('created', 'commented', 'reopened', 'quarantined', "
            "'refused_loop', 'refused_rate', 'refused_parse')",
            name="ck_inbound_message_disposition_valid",
        ),
    )

    op.add_column("ticket", sa.Column("email_token", sa.String(43), nullable=True))
    op.create_unique_constraint("uq_ticket_email_token", "ticket", ["email_token"])

    # Spec §5.1: sender resolution runs on EVERY inbound message. Additive and
    # behaviour-neutral -- adds no column and no constraint to a kernel table.
    op.execute("CREATE INDEX ix_users_org_lower_email ON users (org_id, lower(email))")

    # Standard tenant_isolation pattern (migrations/versions/0025_notification_core.py).
    # WITH CHECK is stated explicitly to match 0002_rls.py/0025's convention.
    # It is NOT filling a gap: for an ALL-command policy, PostgreSQL reuses the
    # USING expression as WITH CHECK whenever WITH CHECK is omitted, so a
    # USING-only policy here would reject a cross-org INSERT identically. The
    # explicit clause matters only the day a future edit gives this policy a
    # different read predicate than write predicate -- an omitted WITH CHECK
    # would silently follow that new USING expression instead of keeping the
    # original tenant check.
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
        "(SELECT id FROM permissions WHERE module = 'email_intake')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'email_intake'")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP INDEX IF EXISTS ix_users_org_lower_email")
    op.drop_constraint("uq_ticket_email_token", "ticket", type_="unique")
    op.drop_column("ticket", "email_token")
    op.drop_table("inbound_message")
    op.drop_table("email_mailbox")

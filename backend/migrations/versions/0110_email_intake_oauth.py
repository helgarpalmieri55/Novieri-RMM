"""email intake: delegated Microsoft sign-in

Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.7.

`email_mailbox.credential_id` becomes nullable. `ck_email_mailbox_credential`
keeps what the NOT NULL protected: a mailbox with no credential can never be
active, and `poller._claim` only claims active mailboxes. The nullable window is
"an OAuth mailbox exists, nobody has consented yet".

Two tables: the org's Azure application (its secret is a vault credential) and
the single-use, ten-minute consent authorizations. Both FORCE RLS.

The downgrade restores NOT NULL, which only succeeds where no unconnected OAuth
mailbox exists; CI runs it against an empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0110"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def upgrade() -> None:
    op.add_column(
        "email_mailbox",
        sa.Column("auth_kind", sa.String(20), nullable=False, server_default="password"),
    )
    op.add_column(
        "email_mailbox",
        sa.Column("credential_state", sa.String(20), nullable=False, server_default="ok"),
    )
    op.create_check_constraint(
        "ck_email_mailbox_auth_kind",
        "email_mailbox",
        "auth_kind IN ('password', 'microsoft_oauth')",
    )
    op.create_check_constraint(
        "ck_email_mailbox_credential_state",
        "email_mailbox",
        "credential_state IN ('ok', 'needs_reconsent')",
    )
    op.alter_column("email_mailbox", "credential_id", nullable=True)
    op.create_check_constraint(
        "ck_email_mailbox_credential",
        "email_mailbox",
        "credential_id IS NOT NULL OR active IS FALSE",
    )

    op.create_table(
        "email_oauth_app",
        sa.Column(
            "org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), primary_key=True
        ),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=False),
        # The client secret is a vault credential, never a column.
        sa.Column(
            "credential_id", UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False
        ),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    _rls("email_oauth_app")

    op.create_table(
        "email_oauth_authorization",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column(
            "mailbox_id",
            UUID(as_uuid=True),
            sa.ForeignKey("email_mailbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        # Sealed with the vault, bound to this row's id.
        sa.Column("sealed_verifier", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "state", name="uq_email_oauth_auth_org_state"),
    )
    _rls("email_oauth_authorization")


def downgrade() -> None:
    # DROP TABLE takes the policies with it.
    op.drop_table("email_oauth_authorization")
    op.drop_table("email_oauth_app")
    op.drop_constraint("ck_email_mailbox_credential", "email_mailbox", type_="check")
    op.alter_column("email_mailbox", "credential_id", nullable=False)
    op.drop_constraint("ck_email_mailbox_credential_state", "email_mailbox", type_="check")
    op.drop_constraint("ck_email_mailbox_auth_kind", "email_mailbox", type_="check")
    op.drop_column("email_mailbox", "credential_state")
    op.drop_column("email_mailbox", "auth_kind")

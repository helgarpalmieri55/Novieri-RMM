"""push_credential_realm: the realm a Windows domain push credential names
(review I1, 2026-09-29).

The realm -- and the KDC that receives the account's pre-authentication --
used to come from each target's DNS name. It now comes from the credential:
the upper-cased domain of its user principal name, recorded at save time so
dispatch can refuse a target outside it before the credential is decrypted.
Not a secret. Tenant-scoped under RLS like every push table
(`0077_agent_push.py`).

Forward-only. `downgrade()` drops the table; it has never run in production
(rollback is restore-from-backup) and CI runs upgrade->downgrade->upgrade.

Revision ID: 0190
Revises: 0180
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0190"
down_revision: str | None = "0180"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLE = "push_credential_realm"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("realm", sa.String(253), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)

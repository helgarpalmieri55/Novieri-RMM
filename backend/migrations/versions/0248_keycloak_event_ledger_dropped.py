"""own authentication phase 4: Keycloak's event ledger goes

`keycloak_event_ingest` (migration 0064) was the dedupe ledger of the worker
sweep that copied four of Keycloak's credential events into the audit chain
(docs/modules/console-auth/spec.md §7.1). Keycloak is gone, and with it the
sweep and the module that wrote this table (own authentication design
2026-11-04, §1.3 and §6.1: "Se borra `keycloak_event_ingest`"; phase 1 plan,
decision D4, deferred it to this phase).

NOTHING A PERSON READS GOES WITH IT. The ledger was never evidence: each event
it let through was written to `audit_events`, which keeps it, and only the
sweep ever read the table. The ledger's own rows -- fingerprints and the time
each was seen -- describe a sweep that no longer runs.

FORWARD-ONLY (CLAUDE.md). `downgrade()` recreates the table exactly as 0064
made it -- no later migration altered it: 0231 leaves a G table's
`tenant_isolation` as it is -- so CI's `upgrade head -> downgrade base ->
upgrade head` still runs 0064's own downgrade, which drops it again. The rows
are not restored: rollback is restore-from-backup (deploy-rollback design
D1).

Revision ID: 0248
Revises: 0247
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0248"
down_revision: str | None = "0247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "keycloak_event_ingest"
_ORG_CTX = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_keycloak_event_age", table_name=_TABLE)
    op.drop_table(_TABLE)


def downgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("fingerprint", name="uq_keycloak_event_fingerprint"),
    )
    op.create_index("ix_keycloak_event_age", _TABLE, ["ingested_at"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_CTX}) WITH CHECK (org_id = {_ORG_CTX})"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO app_user")

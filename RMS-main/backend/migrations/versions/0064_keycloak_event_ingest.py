"""console-auth: dedupe ledger for forwarded Keycloak events

docs/modules/console-auth/spec.md §7.1.

WHY A TABLE RATHER THAN A CURSOR. Keycloak's `dateFrom`/`dateTo` filters on the
events endpoint are DAY-GRANULAR, so a sweep that runs hourly re-reads the same
day's events twenty-four times. A high-water mark on timestamp alone cannot fix
that -- two events can share a millisecond, and a sweep that skipped everything
at exactly the watermark would drop them.

So each forwarded event is recorded by fingerprint, and the audit event is
written ONLY when the insert takes. A duplicate does not corrupt the hash
chain; it pollutes it, which for evidence is nearly as bad -- an auditor
counting passkey registrations would count each one once per sweep.

NOT EVIDENCE ITSELF. This table exists to stop duplicates inside an overlapping
fetch window and nothing else reads it, so its rows are swept on the same
retention the audit log uses. The evidence is in `audit_events`.

Revision ID: 0064
Revises: 0063
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "keycloak_event_ingest"
_ORG_CTX = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        # SHA-256 hex over the fields KEYCLOAK sets, never anything this
        # process computes -- so the same event fingerprints identically on
        # every sweep and from every replica of the worker.
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        # GLOBAL, not per-org: one Keycloak event must be forwarded once even
        # though the realm is shared by every tenant. Scoping uniqueness by org
        # would let the same event land twice if a user's org attribute changed
        # between sweeps.
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


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_keycloak_event_age", table_name=_TABLE)
    op.drop_table(_TABLE)

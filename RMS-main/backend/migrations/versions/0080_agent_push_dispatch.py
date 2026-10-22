"""agent push-install, cycle B2: host-key pinning and batch grouping

`docs/modules/agent-distribution/spec-push-install.md` §4.4, §5.3, §7.3, under
`CLAUDE.md` invariant 2a (approved by the operator 2026-08-26).

**CYCLE B1 SHIPPED A FEATURE THAT DISPATCHED NOTHING. B2 DISPATCHES, AND THESE
TWO TABLES ARE WHAT MAKE IT DEFENSIBLE.** No permission codes are added: the two
from 0077 (`agent.push.run`, `agent.push.scope.manage`) still gate everything.

`push_host_key` IS THE CRUX. On first contact the platform cannot authenticate
the target -- there is no host key, no certificate, no prior relationship, which
is what "unmanaged" means (spec §1.1, §7.3, and the "does not claim" clause of
invariant 2a). This is trust-on-first-use, and TOFU with an administrative
credential is a real exposure. The mitigation the spec specifies -- and this
table stores -- is: a credential-free `agent.push_probe` fetches the fingerprint
first, the operator CONFIRMS it, and only the confirmed key is signed into a
push so the agent's SSH client verifies against it rather than accepting
blindly. `address` is native `inet`, because the gating lookup is an address
comparison and a string one is wrong at every representation boundary.

`push_batch_target` groups the N jobs of one push act FOR DISPLAY ONLY (spec
§5.3). It holds no aggregate state that could hide a partial failure, and it
exists here because the kernel `jobs` table cannot carry a `batch_id` -- it is
not this module's to change.

THE DOWNGRADE IS WRITTEN TO RUN, and CI runs it (`upgrade head -> downgrade base
-> upgrade head`): it drops the policies and the tables (which drops their
indexes). No permission rows are touched, because none are added.

Revision ID: 0080
Revises: 0079
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# Creation order, which is also FK order. The RLS loop and the downgrade walk
# this list (the latter in reverse).
_TABLES = [
    "push_host_key",
    "push_batch_target",
]

# Declared here exactly as `app/modules/agent_push/models.py` declares them --
# `test_schema_drift.py` makes Postgres deparse both sides and compares, so a
# predicate written differently in the two places is drift even when it means
# the same thing.
_INDEXES = [
    "CREATE UNIQUE INDEX uq_push_host_key_address"
    " ON push_host_key (org_id, address)",
    "CREATE INDEX ix_push_batch_target_batch"
    " ON push_batch_target (org_id, batch_id)",
    "CREATE UNIQUE INDEX ix_push_batch_target_job"
    " ON push_batch_target (job_id)",
]


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "push_host_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=True,
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False, server_default="ssh"),
        sa.Column("observed_fingerprint", sa.String(128), nullable=False),
        sa.Column("observed_key_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observed_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id"),
            nullable=True,
        ),
        # NULL until a human confirms. An install verifies against THIS, never
        # against the observation alone.
        sa.Column("confirmed_fingerprint", sa.String(128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "confirmed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    op.create_table(
        "push_batch_target",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=True,
        ),
        sa.Column(
            "site_server_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=True,
        ),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    for statement in _INDEXES:
        op.execute(statement)

    # Standard tenant_isolation pattern (0002_rls.py, 0077). A host key or a
    # batch record leaking across tenants would tie one customer's confirmed
    # fingerprint or one operator's push to another org, so WITH CHECK is stated
    # explicitly for the day a future edit gives read and write different
    # predicates.
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
    # Dropping a table drops its indexes; every index in `_INDEXES` belongs to
    # one of these two, so there is nothing to drop separately.
    for table in reversed(_TABLES):
        op.drop_table(table)

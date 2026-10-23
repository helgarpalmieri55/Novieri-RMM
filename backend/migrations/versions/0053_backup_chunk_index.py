"""backup_chunks: the server-side chunk index the dedup exchange answers from

`docs/modules/backup/spec.md` §5.2. ONE org-scoped table, and the whole schema
change needed to move dedup's first-writer-wins check off the endpoint.

Until now the agent answered "do I already have this chunk?" itself, with an
`ObjectStore.Has` against a bucket it has no credential for and never will
(§6.1). The check does not need endpoint storage access: the server already
reasons about chunk references (`gc.py` reads manifests to decide what nothing
references). What it lacked was a cheap way to answer the question for a
six-figure hash list, and that is this table.

THREE THINGS HERE ARE NOT ORDINARY DDL:

1. `UNIQUE (repository_id, chunk_id)` AND NOT `(org_id, repository_id,
   chunk_id)`. `backup_repositories.id` is already globally unique, so the
   three-column form would be strictly weaker while reading as stricter --
   0050_site_server.py's `UNIQUE (device_id)` reasoning exactly. `org_id` is
   still on the row: RLS needs it, and every application-level query carries it
   as its own predicate (invariant #5) rather than leaning on the repository id
   to imply it.

2. THE `chunk_id` CHECK IS A KEY-SAFETY CONTROL, NOT A TIDINESS ONE. A chunk id
   becomes an object key (`{storage_ref}/chunks/{id[:2]}/{id}`) and rides into a
   presigned write grant. `^[0-9a-f]{64}$` is what stops `../` or an absolute
   path from ever being stored as a chunk id in the first place; the wire model
   rejects the same shape one layer earlier, and neither is the only layer.

3. NO `state` COLUMN, AND THAT IS A TRUST DECISION WORTH NAMING. A row means
   "an agent reported this chunk stored in this repository". The server does not
   re-HEAD the object to confirm -- doing so would put back the per-chunk round
   trip this whole exchange exists to remove, merely moved from endpoint->server
   to server->bucket. The residual risk is that a compromised agent poisons the
   dedup index for ITS OWN org's repository, causing a later manifest to
   reference a chunk that was never written. That is bounded to one tenant and
   it is exactly what `verify_point` detects; it is not a cross-tenant hazard,
   because a row is only ever created under the (org_id, repository_id) the
   server resolved from the job IT signed, never from anything the agent sent.

NO PERMISSIONS ARE ADDED. This table is reached only through the agent gateway,
authenticated by the mTLS client certificate; no operator route reads or writes
it, so there is no permission code to grant.

Revision ID: 0053
Revises: 0052
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        "backup_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_repositories.id"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        # `DEFAULT now()` matches this module's own 0014 tables. It is not
        # cosmetic: the ORM declares these `server_default`, so SQLAlchemy omits
        # them from an INSERT and a table without the database default takes a
        # NOT NULL violation on every write.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("repository_id", "chunk_id", name="uq_backup_chunks_repo_chunk"),
        sa.CheckConstraint("chunk_id ~ '^[0-9a-f]{64}$'", name="ck_backup_chunks_chunk_id_hex"),
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0043_ipam.py,
    # 0050_site_server.py). WITH CHECK is stated explicitly for the same reason
    # 0050 states it: for an ALL-command policy PostgreSQL reuses USING as WITH
    # CHECK when WITH CHECK is omitted, so deleting the clause falsifies no
    # test. The mutations that DO falsify `tests/test_backup_chunk_migration.py`
    # are `USING (true)` and `WITH CHECK (true)`, and both were run.
    op.execute("ALTER TABLE backup_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE backup_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON backup_chunks "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON backup_chunks")
    op.drop_table("backup_chunks")

"""agent distribution: the downloadable agent artefact catalogue

docs/modules/agent-distribution/spec.md §4.2.

A TABLE WITH NO TENANT COLUMN, recorded in
`tests/test_tenant_column_sweep.py::GLOBAL_BY_NATURE` alongside `permissions`
and for the same reason: the rows describe things shared by every org, not one
org's facts. An artefact is byte-identical for every tenant because an
enrollment token is never inside one -- `agent/packaging/README.md` forbids
that outright ("one image build and the whole fleet shares one token") -- so a
per-org copy would isolate nothing and multiply storage. What is per-tenant is
the right to PUBLISH, and that lives in RBAC (`agent.release.manage`, 0060).

RLS IS STILL ENABLED AND FORCED, with one `application_only` policy, following
0055 and 0056. This is not tenancy -- there is nothing to scope by -- it is
deny-by-default at the database for every role except the application's, so a
mistaken direct connection reads nothing. The distinction matters: a reader who
sees RLS here must not conclude that a route's `require_permission` is
redundant. It is the only tenant-facing control on this table.

GRANT INCLUDES UPDATE, unlike 0055's sample table: a release row is created
with the metadata and then updated in place when the artefact bytes land, to
store `size_bytes` and the server-computed `sha256`. A grant of
SELECT/INSERT/DELETE alone lets publication succeed and the upload fail.

Revision ID: 0059
Revises: 0058
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_releases"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("os", sa.String(16), nullable=False),
        sa.Column("arch", sa.String(16), nullable=False),
        sa.Column("storage_ref", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        # 64 = a sha256 hex digest. Computed server-side over the received
        # bytes; a digest supplied by the uploader verifies nothing.
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "signed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("notes", sa.String(500), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "published_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        # Two artefacts for one (version, os, arch) is unresolvable: an install
        # command names exactly one object and nothing in the data chooses
        # between them, so the ambiguity would be settled by whichever ORDER BY
        # a given query happened to carry.
        sa.UniqueConstraint(
            "version", "os", "arch", name="uq_agent_releases_version_os_arch"
        ),
    )
    op.create_index("ix_agent_releases_published", _TABLE, ["published_at"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY application_only ON {_TABLE} "
        "FOR ALL TO app_user USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO app_user")


def downgrade() -> None:
    # Policy first, then index, then table -- and the enum-style leftovers this
    # migration creates are none, so `downgrade base` leaves nothing behind for
    # the next `upgrade head` to collide with. CI's `migrations` job runs that
    # round trip; 0001's downgrade defect was exactly this class.
    op.execute(f"DROP POLICY IF EXISTS application_only ON {_TABLE}")
    op.drop_index("ix_agent_releases_published", table_name=_TABLE)
    op.drop_table(_TABLE)

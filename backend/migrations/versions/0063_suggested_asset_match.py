"""asset inventory: suggested matches an operator must resolve

docs/modules/asset-inventory/spec.md §4, as amended 2026-08-18.

WHY THIS TABLE EXISTS AT ALL. When an agent enrols on a machine the inventory
already has a record for, the record must be ENRICHED, not duplicated -- and
the platform can only be SURE of that when a physical MAC matches. On weaker
evidence (a shared hostname, a shared address, or two records matching at once)
the spec refuses to link: auto-linking on a hostname silently merges two real
machines that happen to share a name across sites, and the merge is
destructive -- you cannot recover which inventory data belonged to which. A
suggestion queue is mildly annoying; a bad merge is data loss discovered months
later.

So this table is the annoyance, deliberately chosen over the data loss.

THE PARTIAL UNIQUE INDEX IS LOAD-BEARING. A device that re-enrols (a rebuild, a
certificate rotation that went the long way round) would otherwise stack a
second, third and fourth identical open suggestion for the same pair, and an
operator facing forty copies of one question stops reading the queue. Unique on
the pair only WHERE unresolved, so a pair rejected in March can legitimately be
raised again in June if the evidence recurs -- `ON CONFLICT` against it must
therefore infer with `index_elements` + `index_where`; naming the constraint
fails at runtime against a partial index.

Revision ID: 0063
Revises: 0062
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "suggested_asset_match"
_ORG_CTX = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        # The row that was just created by enrolment.
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
        # The row that already existed and might be the same machine.
        sa.Column(
            "candidate_device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False
        ),
        # WHY this was a suggestion rather than a link -- the first thing the
        # operator resolving it needs, and not derivable afterwards.
        sa.Column("basis", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(20), nullable=True),
        sa.Column(
            "resolved_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.CheckConstraint(
            "basis IN ('hostname', 'ip', 'multiple')", name="ck_sam_basis"
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('linked', 'rejected')",
            name="ck_sam_resolution",
        ),
        # A row cannot suggest itself. Cheap, and the shape a matching bug
        # produces first.
        sa.CheckConstraint("device_id <> candidate_device_id", name="ck_sam_distinct"),
    )
    op.create_index(
        "uq_sam_open_pair",
        _TABLE,
        ["device_id", "candidate_device_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_sam_open", _TABLE, ["org_id", "resolved_at"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_CTX}) WITH CHECK (org_id = {_ORG_CTX})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO app_user")


def downgrade() -> None:
    # Policy, then indexes, then table -- so `downgrade base` leaves nothing
    # for the next `upgrade head` to collide with. CI runs that round trip;
    # 0001's downgrade defect was exactly this class of leftover.
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_sam_open", table_name=_TABLE)
    op.drop_index("uq_sam_open_pair", table_name=_TABLE)
    op.drop_table(_TABLE)

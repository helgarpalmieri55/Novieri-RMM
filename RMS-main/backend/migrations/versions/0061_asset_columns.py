"""asset inventory: agentless assets in `devices`

docs/modules/asset-inventory/spec.md §2.

WHY NO `assets` TABLE. `agents.device_id` is a unique FK to `devices.id`, so
"managed" is already a join and a `devices` row with no agent is already legal
today. A separate table would duplicate hostname, serial, group, tags and
tenancy, then need a link table to reunite them -- and every fleet query in the
codebase would have to remember to consult both.

WHY THE DEFAULTS ARE WHAT THEY ARE. Every row that exists when this runs came
from an enrolling agent and is in service, so `source='agent'` and
`lifecycle='in_service'` describe the existing fleet correctly. A default of
anything else would silently reclassify every machine in the estate at
migration time.

THE `NOT NULL` DROPS ARE THE RISK, and not because of this migration. Postgres
applies them instantly and reversibly; what changes is that every read path may
now receive `None` where it has always had a string, and none of them was
written expecting it. `tests/test_asset_read_paths.py` is that audit, and it is
why this migration is not the whole task.

Revision ID: 0061
Revises: 0060
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("lifecycle", sa.String(20), nullable=False, server_default="in_service"),
    )
    op.add_column(
        "devices",
        sa.Column("asset_type", sa.String(20), nullable=False, server_default="computer"),
    )
    op.add_column(
        "devices",
        sa.Column("source", sa.String(12), nullable=False, server_default="agent"),
    )
    op.add_column("devices", sa.Column("description", sa.String(1000), nullable=True))
    op.add_column("devices", sa.Column("primary_ip", postgresql.INET(), nullable=True))
    op.add_column("devices", sa.Column("owner", sa.String(200), nullable=True))
    op.add_column("devices", sa.Column("location", sa.String(200), nullable=True))

    op.create_check_constraint(
        "ck_devices_lifecycle",
        "devices",
        "lifecycle IN ('in_service', 'stock', 'unmanageable', 'retired')",
    )
    op.create_check_constraint(
        "ck_devices_asset_type",
        "devices",
        "asset_type IN ('computer', 'server', 'network', 'printer', 'phone', "
        "'appliance', 'other')",
    )
    op.create_check_constraint(
        "ck_devices_source",
        "devices",
        "source IN ('agent', 'manual', 'discovery')",
    )

    op.alter_column("devices", "hostname", existing_type=sa.String(255), nullable=True)
    op.alter_column("devices", "os_family", existing_type=sa.String(20), nullable=True)


def downgrade() -> None:
    # RE-IMPOSING NOT NULL WILL FAIL ON ANY ROW THIS FEATURE CREATED, and that
    # is the correct behaviour rather than an oversight: a downgrade that
    # deleted agentless assets to make the constraint fit would destroy the
    # operator's inventory to satisfy a schema. Rollback in this platform is
    # restore-from-backup (docs/superpowers/specs/2026-08-15-deploy-rollback-
    # design.md D1); this exists so CI's `upgrade head -> downgrade base ->
    # upgrade head` round trip passes on a schema with no such rows.
    op.alter_column("devices", "os_family", existing_type=sa.String(20), nullable=False)
    op.alter_column("devices", "hostname", existing_type=sa.String(255), nullable=False)
    op.drop_constraint("ck_devices_source", "devices", type_="check")
    op.drop_constraint("ck_devices_asset_type", "devices", type_="check")
    op.drop_constraint("ck_devices_lifecycle", "devices", type_="check")
    for column in (
        "location",
        "owner",
        "primary_ip",
        "description",
        "source",
        "asset_type",
        "lifecycle",
    ):
        op.drop_column("devices", column)

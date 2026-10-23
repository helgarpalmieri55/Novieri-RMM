"""patch_reboots: a restart an operator asked for

A technician can ask for a restart from the device page. It is booked as a
`patch_reboots` row with origin `operator` and dispatched by the same sweep,
inside the device's maintenance window. The origin has to be its own value:
a scan that finds no restart pending cancels a `scan` row, and the sweep asks
the agent to restart only if required unless the row is an operator's.

Revision ID: 0150
Revises: 0086
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0150"
down_revision: str | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_patch_reboots_origin", "patch_reboots", type_="check")
    op.create_check_constraint(
        "ck_patch_reboots_origin", "patch_reboots", "origin IN ('apply', 'scan', 'operator')"
    )


def downgrade() -> None:
    # An operator's restart has no older equivalent; closed ones are history
    # the narrower constraint cannot hold, so they go with it.
    op.execute("DELETE FROM patch_reboots WHERE origin = 'operator'")
    op.drop_constraint("ck_patch_reboots_origin", "patch_reboots", type_="check")
    op.create_check_constraint(
        "ck_patch_reboots_origin", "patch_reboots", "origin IN ('apply', 'scan')"
    )

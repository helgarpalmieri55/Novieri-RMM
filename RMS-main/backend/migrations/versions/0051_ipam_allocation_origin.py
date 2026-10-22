"""ipam: an observation records HOW the endpoint said its address was allocated

`docs/modules/ipam/spec.md` §3.2, §4.1 point 3, §14.1.

ONE COLUMN ON ONE TABLE, and it closes the last of §4.1's four legacy-shape
consequences. §14.1 landed on the wire in 2026-08: the agent supplies
`origin in {static, dhcp, slaac, link_local, unknown}` on every address it
reports, and IPAM read the address out of each entry and dropped the rest. §3.2's
`allocation_method` was therefore `unknown` for everything inventory-derived --
not because the fact was unavailable, but because nothing on this side retained
it. This column is where it now lands.

THREE THINGS ABOUT IT ARE DELIBERATE.

1. IT IS CALLED `allocation_origin`, NOT `origin`. `ipam_address.origin` already
   exists and means something else entirely -- how the ROW got into the plan
   (`manual | imported | discovered`), which is a fact about an operator, not
   about an endpoint. The two are in scope together in `allocation.address_grid`
   and `reconcile.is_conflicting`, so one name for the two would be a bug waiting
   for a hurried afternoon. The name is the same on the parser's `NicObservation`
   and here: one fact, one name, end to end.

2. IT IS CHECK-CONSTRAINED TO §14.1's FIVE VALUES, and that is what
   `app/modules/ipam/nic.py` normalises against. That parser is PURE AND TOTAL --
   it runs inside the `inventory.collect` result path and a raising result
   handler becomes a `failed` job -- so it maps anything it does not recognise to
   `'unknown'` rather than passing it through. A pass-through would convert an
   unrecognised origin into an `IntegrityError` HERE, and a database error is
   precisely the failure no `except` in that file can contain (§0.17: that is
   what layer 1's SAVEPOINT exists for). The CHECK and the normalisation are two
   halves of one guarantee, not belt and braces.

3. EXISTING ROWS BECOME `'unknown'`, AND THAT IS THE TRUE ANSWER RATHER THAN A
   PLACEHOLDER. No row written before this migration carries the fact -- the
   parser discarded it -- and no backfill could invent it. Sweep observations
   (`source='sweep'`) will keep the default forever by design: ARP and ICMP see
   that an address answers, not how it was configured, and §2.7's `confidence`
   column already says why that is the honest limit of active evidence.

NOT `ipam_address`. The obvious alternative -- writing the reported method
straight onto the address row -- is refused by §0.6, which is the design decision
this module is built around: an ingestion path that could write a column an
operator asserted is how "the IPAM changed my reservation by itself" happens.
`allocation_method` on `ipam_address` stays operator-set and this column stays
fleet-reported; `allocation.derive_allocation_method` combines them AT READ TIME,
the same way `derive_status` already combines `state` with observations, and
`GridEntryOut.allocation_method_source` says which of the two answered.

NO NEW INDEX. Every read of this column is inside a query that has already
selected the observations for one address or one subnet page by an existing
index; a column that is only ever projected is not one to index.

NO RLS CHANGES: `ipam_observation` already has `ENABLE + FORCE` with
`tenant_isolation` from migration 0043, and adding a column does not touch a
policy.

Revision ID: 0051
Revises: 0050
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# §14.1's vocabulary, verbatim. `app/modules/ipam/nic.py::_ORIGINS` holds the
# same set and `tests/test_ipam_migration.py` asserts the CHECK admits exactly
# these, so the parser cannot start emitting a sixth value without this
# constraint refusing it loudly rather than the two drifting apart quietly.
_ORIGINS = ("static", "dhcp", "slaac", "link_local", "unknown")


def upgrade() -> None:
    op.add_column(
        "ipam_observation",
        sa.Column(
            "allocation_origin",
            sa.String(12),
            server_default="unknown",
            nullable=False,
        ),
    )
    values = ", ".join(f"'{value}'" for value in _ORIGINS)
    op.execute(
        "ALTER TABLE ipam_observation ADD CONSTRAINT ck_ipam_obs_allocation_origin "
        f"CHECK (allocation_origin IN ({values}))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ipam_observation DROP CONSTRAINT IF EXISTS ck_ipam_obs_allocation_origin"
    )
    op.drop_column("ipam_observation", "allocation_origin")

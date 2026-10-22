"""devices.provenance: who, if anyone, stood behind this row

`docs/modules/asset-inventory/spec.md` §3, as amended on the operator's ruling
of 2026-08-26.

WHY THIS COLUMN EXISTS. From that ruling a `net.sweep` records every host it
finds as an asset, with no operator selecting anything. The objection that
design had to answer is that a ping sweep answers for printers, phones, a
colleague's laptop on the wrong VLAN and the coffee machine -- and that rows
built from it are indistinguishable from rows a human entered. They are
indistinguishable only if nothing records the difference. This records it:
after this migration every `devices` row says how it came to exist, and
"which of these did a person vouch for?" is a WHERE clause.

WHY IT IS NOT THE EXISTING `source` COLUMN. `source` is `agent | manual |
discovery` and answers WHICH MECHANISM produced the row. The new question is
WHO STOOD BEHIND IT, and the two are independent exactly once: a discovered row
may have been promoted by a person or written by the sweep itself. Overloading
`source` with a fourth value would have changed the meaning of a column three
screens, one export and the enrolment matcher already read.

AND WHY THEY CANNOT DRIFT APART. Two columns describing overlapping facts is
how a database comes to disagree with itself silently -- the spec's own §2
warning, applied to the thing it warns about. The CHECK constraint below makes
the pairing unrepresentable rather than merely conventional: `manual` implies
`operator`, `agent` implies `agent`, and `discovery` is the only source with a
choice, between `promoted` and `sweep`.

THE BACKFILL IS NOT A GUESS. Every `discovery` row that exists when this runs
was created by `POST /devices/candidates/promote`, which was the only writer of
that value and required an operator's selection. So `discovery -> promoted` is
a statement of fact about the deployed data, not a default.

Revision ID: 0078
Revises: 0077

NUMBERED 0078 RATHER THAN 0077 because `0077_agent_push.py` was in the working
tree when this was written. Migrations are forward-only and the chain is a
shared resource: two files claiming one revision id makes `alembic upgrade`
refuse to run at all, which is a broken deployment rather than a merge
conflict.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `server_default='agent'` fills the existing fleet in one pass and keeps
    # the column NOT NULL from the first instant -- there is no window in which
    # a row can exist without an answer to this question.
    op.add_column(
        "devices",
        sa.Column("provenance", sa.String(12), nullable=False, server_default="agent"),
    )
    op.execute(
        """
        UPDATE devices
           SET provenance = CASE source
                                WHEN 'manual' THEN 'operator'
                                WHEN 'discovery' THEN 'promoted'
                                ELSE 'agent'
                            END
        """
    )
    op.create_check_constraint(
        "ck_devices_provenance",
        "devices",
        "provenance IN ('agent', 'operator', 'promoted', 'sweep')",
    )
    # THE PAIRING, not just the vocabulary. A row claiming `source='manual'`
    # and `provenance='sweep'` would say a human typed in something the
    # platform invented, and every audit of the inventory afterwards would be
    # reading a contradiction.
    op.create_check_constraint(
        "ck_devices_source_provenance",
        "devices",
        "(source = 'agent' AND provenance = 'agent')"
        " OR (source = 'manual' AND provenance = 'operator')"
        " OR (source = 'discovery' AND provenance IN ('promoted', 'sweep'))",
    )
    # The console filters the fleet list by this, and "show me what nobody
    # vouched for" is the query the whole column exists to serve.
    op.create_index("ix_devices_org_provenance", "devices", ["org_id", "provenance"])


def downgrade() -> None:
    # Symmetric and lossy in one direction only: dropping the column throws
    # away the distinction between a promoted row and a swept one, which is the
    # unavoidable consequence of removing the only place it is written.
    # Rollback in this platform is restore-from-backup
    # (docs/superpowers/specs/2026-08-15-deploy-rollback-design.md D1); this
    # exists so CI's `upgrade head -> downgrade base -> upgrade head` round trip
    # passes.
    op.drop_index("ix_devices_org_provenance", table_name="devices")
    op.drop_constraint("ck_devices_source_provenance", "devices", type_="check")
    op.drop_constraint("ck_devices_provenance", "devices", type_="check")
    op.drop_column("devices", "provenance")

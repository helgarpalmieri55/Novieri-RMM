"""device groups: explicit precedence, and a name an archive does not hold forever

Cycle 1 of `docs/superpowers/specs/2026-08-22-device-group-management-design.md`
(§10.1 and §10.2, both as the operator settled them on 2026-08-22).

WHY `precedence` LANDS NOW, IN THE CYCLE THAT CANNOT USE IT YET.
`app/modules/inventory/rules.py` walked rule-bearing groups `ORDER BY name` and
let the FIRST match claim the device, so the alphabet WAS the precedence list:
renaming `z-fallback` to `a-fallback` promoted it above every other rule group
and moved every device it matched onto a different patch policy, a different
backup policy and a different remote policy, with no rule edited and nothing on
screen saying so.

The column removes that coupling, and the argument for doing it in this cycle
is timing rather than design. NOTHING IN THE PLATFORM CREATES A DEVICE GROUP
TODAY -- the only `DeviceGroup(` constructors outside `backend/tests/` are none
at all, and no migration seeds a row -- so there are no rule-bearing groups to
re-evaluate and this migration is free exactly once. Every later moment costs a
re-evaluation of a live fleet, because changing what decides precedence moves
machines between groups. The free window is open precisely because the feature
does not work yet, and it closes the moment cycle 2 ships the rules editor.

`DEFAULT 0` IS WHAT MAKES IT FREE. Every existing row lands on the same
precedence, so `(precedence, name)` degenerates to `(0, name)` -- byte for byte
the order the evaluator already produced. The migration cannot move a device
even on a database that somehow holds hand-inserted rule groups.

WHY THE UNIQUE CONSTRAINT BECOMES A PARTIAL INDEX. `archived_at` is a soft
delete, chosen for `15ed5de`'s reason: the hash-chained audit log carries group
ids forever, `deployments.target_id` and `script_runs.target_id` name a group
by bare uuid with NO foreign key, and nine FKs onto `device_groups.id` are all
`NO ACTION` -- so a hard `DELETE` has two failure modes and no success mode. It
raises where an FK holds and dangles silently where none does.

But a soft delete under `UNIQUE (org_id, name)` means an archived group holds
its name against every future group in that org, forever. The alternative --
renaming on archive -- rewrites a name the audit log already references, which
is the thing archiving exists to avoid. So the constraint becomes a unique
index `WHERE archived_at IS NULL`: live names stay unique, archived names stop
occupying the namespace.

THE DOWNGRADE HAS TO RENAME ROWS, AND THAT IS NOT A DEFECT BEING WAVED THROUGH.
Dropping `archived_at` makes every archived group live again, and a total
`UNIQUE (org_id, name)` cannot be recreated over rows the partial index
legitimately allowed to collide. The loop below suffixes the later duplicates
until none remain, deterministically by id, so `downgrade base` succeeds on a
database holding real rows rather than only on an empty schema. Rollback in
this platform is restore-from-backup (`docs/superpowers/specs/
2026-08-15-deploy-rollback-design.md` D1); this exists for CI's
`upgrade head -> downgrade base -> upgrade head` round trip, and it is written
to run on real rows because a downgrade that only works on an empty schema is
a downgrade nobody has thought about.

Revision ID: 0072
Revises: 0071
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "device_groups"
#: The name Alembic gave `sa.UniqueConstraint('org_id', 'name')` in 0001.
_TOTAL_UNIQUE = "device_groups_org_id_name_key"
_LIVE_UNIQUE = "uq_device_groups_org_live_name"
_ORDER_INDEX = "ix_device_groups_org_precedence_name"

#: Make `(org_id, name)` unique again by suffixing every duplicate after the
#: first, oldest id first. Loops because one pass can itself create a
#: collision (` #2` may already be taken), and terminates because each pass
#: strictly reduces the number of duplicate rows.
_DEDUPE = """
DO $$
BEGIN
  LOOP
    UPDATE device_groups AS d
       SET name = left(d.name, 180) || ' #' || dup.n
      FROM (
        SELECT id, row_number() OVER (PARTITION BY org_id, name ORDER BY id) AS n
          FROM device_groups
      ) AS dup
     WHERE d.id = dup.id AND dup.n > 1;
    EXIT WHEN NOT FOUND;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "precedence",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        _TABLE, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint(_TOTAL_UNIQUE, _TABLE, type_="unique")
    op.create_index(
        _LIVE_UNIQUE,
        _TABLE,
        ["org_id", "name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    # The evaluator and the list route both order on this triple, and both run
    # per org. Cheap on a table this size, and it is the one ordering the
    # platform's targeting decisions are read from.
    op.create_index(_ORDER_INDEX, _TABLE, ["org_id", "precedence", "name"])


def downgrade() -> None:
    op.drop_index(_ORDER_INDEX, table_name=_TABLE)
    op.drop_index(_LIVE_UNIQUE, table_name=_TABLE)
    # Before the total constraint can come back, the names the partial index
    # allowed to collide have to stop colliding. See the module docstring.
    op.execute(_DEDUPE)
    op.create_unique_constraint(_TOTAL_UNIQUE, _TABLE, ["org_id", "name"])
    op.drop_column(_TABLE, "archived_at")
    op.drop_column(_TABLE, "precedence")

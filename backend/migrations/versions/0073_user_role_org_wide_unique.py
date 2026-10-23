"""user_roles: a uniqueness rule that also holds for org-wide grants

WHAT WAS BROKEN. `0001_core_schema.py` gave `user_roles` a
`UNIQUE (user_id, role_id, device_group_id)`, and `device_group_id` is
NULLABLE. In Postgres two NULLs are never equal, so a unique key containing a
NULL is indexed but can never collide -- the constraint has nothing to compare.
EVERY ORG-WIDE GRANT CARRIES `device_group_id IS NULL`, so the rule was inert
for precisely the grants that matter most and enforced only for the
group-scoped ones. The same `INSERT` could run any number of times through the
API, through the RBAC admin routes, or by hand, and the console then rendered
one line per row: the operator's deployment held `super-admin` three times and
showed three identical rows.

Reproduced on a database at 0072 before this migration was written: three
identical org-wide grants inserted, three accepted, `count(*)` = 3.

THE FIX IS A PARTIAL UNIQUE INDEX, the same shape 0072 used on
`device_groups`: `(user_id, role_id) WHERE device_group_id IS NULL`. The
predicate does the NULL-matching the constraint cannot -- inside it every row
has a NULL group, so the key is two non-null columns and ordinary equality
holds. The 0001 constraint STAYS: the group-scoped half of the rule was always
correct (no NULL, so no NULL semantics), and together the two allow exactly one
org-wide grant plus at most one grant per device group.

`org_id` IS NOT IN THE INDEX, checked rather than assumed. `users.org_id` is
NOT NULL and a user belongs to exactly one organization, so `user_id` already
determines the tenant, and both `UserRole(` constructors in `app/`
(`user_admin/service.py:589` and `:669`) write the target user's own org.
Adding `org_id` would ADMIT one duplicate per distinct `org_id` value written
against the same user -- a wider key is a weaker rule, not a tenancy-scoped
one. Unique indexes are enforced below RLS, so tenant isolation needs nothing
from this key.

THE DEDUPE HAS TO RUN FIRST, because `CREATE UNIQUE INDEX` fails on a database
that already holds duplicates and deployed databases do -- that is the bug
report. It keeps the LOWEST id per `(user_id, role_id)` among the org-wide
rows; ids are uuid7, so lowest is the earliest granted, and the surviving row
is the one the audit log's `user.role_assigned` entry refers to. There is no
`created_at` on this table, so id order is also the only ordering available.

THE NULL TRAP, AND HOW THIS SHAPE AVOIDS IT. Written as a self-join on the
triple, `a.device_group_id = b.device_group_id` matches NOTHING for org-wide
rows -- every comparison is NULL, never true -- and the dedupe silently deletes
zero rows, after which `CREATE UNIQUE INDEX` fails and the upgrade dies on a
live host. `IS NOT DISTINCT FROM` is the operator that treats two NULLs as
equal and would fix that formulation. The statement below instead moves the
NULL out of the comparison entirely: `WHERE device_group_id IS NULL` selects
the org-wide rows, and the window then partitions on two NOT NULL columns. No
NULL is ever compared, so there is nothing for NULL semantics to break. The
migration test seeds duplicates and asserts one row survives, because a dedupe
that quietly does nothing looks exactly like a dedupe that had nothing to do.

WHAT THE DOWNGRADE DOES NOT DO. It drops the index. IT DOES NOT RESTORE THE
DELETED ROWS -- they are gone, and nothing in this migration records what they
were. That is stated rather than papered over: the deleted rows are exact
duplicates of surviving ones, so nothing is lost that the survivor does not
already say, but a downgrade is not a rollback of the data change and must not
be read as one. Rollback in this platform is restore-from-backup
(`docs/superpowers/specs/2026-08-15-deploy-rollback-design.md` D1); the
downgrade exists so CI's `upgrade head -> downgrade base -> upgrade head`
round trip passes, and so it can run on a database holding real rows.

NO AUDIT ROW IS WRITTEN, SO THE COUNT IS PRINTED. The hash-chained log is
per-org and is written by `app/core/audit`, which a migration cannot reach
without duplicating the chain logic outside the code that owns it. But a
migration that deletes rows on a live host must not do it silently, so the
count goes to stdout.

`print`, and not the two channels that read better, because neither of them
reaches the operator on THIS repo -- both were tried against the reproduced
three-row database and both produced nothing. A Postgres `RAISE NOTICE` is
swallowed because asyncpg hands notices to a handler nobody installs.
`logging.getLogger('alembic.runtime.migration').info(...)` is swallowed
because `migrations/env.py` never calls `fileConfig`, so `alembic.ini`'s
`[handler_console]` is never installed and even Alembic's own
`Running upgrade 0072 -> 0073` line does not appear. Wiring that up would
change the output of every migration in the tree and is not this change's to
make.

Revision ID: 0073
Revises: 0072
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_roles"
_ORG_WIDE_UNIQUE = "uq_user_roles_user_role_org_wide"

#: Keep the earliest org-wide grant per (user_id, role_id); delete the rest.
#: See the module docstring for why the NULL lives in the WHERE clause and not
#: in the comparison.
_DEDUPE = """
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY user_id, role_id ORDER BY id) AS n
    FROM user_roles
   WHERE device_group_id IS NULL
)
DELETE FROM user_roles AS ur
 USING ranked
 WHERE ur.id = ranked.id AND ranked.n > 1
"""

def upgrade() -> None:
    removed = op.get_bind().execute(sa.text(_DEDUPE)).rowcount
    if removed:
        print(
            f"0073: removed {removed} duplicate org-wide role grant(s) from "
            "user_roles; the earliest grant of each (user, role) pair was kept"
        )
    op.create_index(
        _ORG_WIDE_UNIQUE,
        _TABLE,
        ["user_id", "role_id"],
        unique=True,
        postgresql_where=sa.text("device_group_id IS NULL"),
    )


def downgrade() -> None:
    # Drops the index only. The rows the upgrade deleted are NOT restored --
    # see the module docstring.
    op.drop_index(_ORG_WIDE_UNIQUE, table_name=_TABLE)

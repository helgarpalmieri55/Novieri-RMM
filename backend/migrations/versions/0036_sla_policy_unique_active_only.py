"""sla: scope the four sla_policy partial unique indexes to active policies

docs/modules/sla/spec.md §2.4, §3, §9; Task 4 review finding F2.

Migration 0033 created four partial unique indexes, one per §3 resolution
level, none of which mention `active`. `delete_policy` refuses to delete a
policy any `ticket_sla` row still references (§9: "deleting a policy leaves
existing `ticket_sla` rows intact", and `ticket_sla.policy_id` is a NOT NULL
RESTRICT FK) and directs the admin to `update_policy(..., {"active": False})`
instead. But because the indexes were not `active`-scoped, that retirement
path was a dead end: after deactivating, creating a REPLACEMENT policy at the
same level was still refused -- with a message reading "an active policy for
(...) already exists in this org" when no active policy existed at all.
Verified against a live database before this migration:

    create(queue=Support, priority='high')  -> ok
    update(active=False)                    -> ok  ("retired")
    create(queue=Support, priority='high')  -> SlaValidationError, blocked by
                                               the INACTIVE row

There was therefore NO way to change the promise for a `(queue, priority)`
cell once a ticket referenced the policy holding it.

§3 only ever considers `active` policies, so the invariant uniqueness must
actually protect is "at most one ACTIVE policy per (org, resolution level)".
Two retired policies at the same level are harmless history that resolution
never looks at. Adding `AND active` to each predicate makes the guard say
exactly that, makes the collision message true, and frees the retirement path.

The change is a strict NARROWING of what each index covers (fewer rows
indexed), so the rebuild can never fail on existing data. The DOWNGRADE can:
restoring the un-scoped predicates fails if an org has accumulated more than
one policy at a level, which is precisely the history this migration makes
legal.

0033-0035 are shipped and migrations are forward-only, so this is a new
revision rather than an edit to 0033. `app/modules/sla/models.py`'s `Index`
declarations and `app/modules/sla/policies.py`'s `_POLICY_INDEX_BY_LEVEL`
`index_where` clauses (the ON CONFLICT arbiter predicates, which must
reproduce the DDL predicate exactly for Postgres to infer the index) are
updated in lockstep.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, indexed columns, pre-0036 predicate, 0036 predicate)
_INDEXES: list[tuple[str, str, str, str]] = [
    (
        "uq_sla_policy_queue_and_priority",
        "(org_id, queue_id, priority)",
        "queue_id IS NOT NULL AND priority IS NOT NULL",
        "queue_id IS NOT NULL AND priority IS NOT NULL AND active",
    ),
    (
        "uq_sla_policy_queue_wildcard_priority",
        "(org_id, queue_id)",
        "queue_id IS NOT NULL AND priority IS NULL",
        "queue_id IS NOT NULL AND priority IS NULL AND active",
    ),
    (
        "uq_sla_policy_wildcard_queue_priority",
        "(org_id, priority)",
        "queue_id IS NULL AND priority IS NOT NULL",
        "queue_id IS NULL AND priority IS NOT NULL AND active",
    ),
    (
        "uq_sla_policy_wildcard_both",
        "(org_id)",
        "queue_id IS NULL AND priority IS NULL",
        "queue_id IS NULL AND priority IS NULL AND active",
    ),
]


def upgrade() -> None:
    for name, columns, _old_where, new_where in _INDEXES:
        op.execute(f"DROP INDEX {name}")
        op.execute(f"CREATE UNIQUE INDEX {name} ON sla_policy {columns} WHERE {new_where}")


def downgrade() -> None:
    for name, columns, old_where, _new_where in _INDEXES:
        op.execute(f"DROP INDEX {name}")
        op.execute(f"CREATE UNIQUE INDEX {name} ON sla_policy {columns} WHERE {old_where}")

"""device_group_evaluation: the sweep's watermark, and its visible last run

`docs/superpowers/specs/2026-08-22-device-group-management-design.md` §5.3 and
§9, closing the two things that document's cycle-2 notes name as follow-on work
and could not carry because cycle 2 spent its migration on `precedence`.

**ONE TABLE, ONE ROW PER ORG, OVERWRITTEN IN PLACE.** It is deliberately not a
history table: the history is the hash-chained audit log's
`device_group.membership_reevaluated` events, which are append-only and already
carry every run's counts. This row answers the two questions that log cannot be
asked from the Groups page.

**WHY A TABLE AND NOT A COLUMN ON `organizations`.** The state is a property of
one module's sweep, not of the tenant, and `organizations` is the one table
whose rows are read on the admin engine before any tenant context exists
(`enroll()` resolves a token's org there). Widening it with a module's
bookkeeping puts a module's schema in the kernel's most privileged read. An
org-scoped table takes the ordinary forced-RLS policy and is dropped cleanly by
`downgrade()`.

**WHAT THE COLUMNS ARE FOR.**

1. `last_evaluated_at` — "IS THE SWEEP RUNNING?" The page states the INTERVAL
   today ("membership is applied by a background re-evaluation"), because the
   only durable record was an audit event and reading it needs `audit.read`,
   which the Groups page's `devices.read` gate does not entitle. An interval
   cannot distinguish *swept four minutes ago* from *the worker has been down
   since Tuesday*. This is written on EVERY run, INCLUDING the short-circuit
   run for an org holding no rule-bearing group — "the worker is alive" is
   exactly the fact an org with no rules still needs, and it is the one the
   short circuit used to swallow.

2. `watermark_snapshot_id` / `next_watermark_snapshot_id` — "WHICH DEVICES
   STILL NEED LOOKING AT?" The sweep re-read every active device's latest
   `InventoryDocument` every 300 seconds; a document is a machine's whole
   collected estate, so a 10,000-device org moved a few hundred megabytes per
   sweep, forever. `SNAPSHOT_BATCH` bounds the resident set, not the transfer.

   **TWO COLUMNS, NOT ONE, AND THE SECOND ONE IS THE POINT.** A single
   "newest snapshot id at this run's start" has a commit-lag race: a snapshot
   whose uuid7 was assigned before the sweep read that maximum but whose
   transaction committed after it would sit below the boundary forever and
   never be evaluated — the device silently keeps a membership its inventory no
   longer justifies, and nothing on any screen says so. So the boundary in use
   LAGS ONE RUN: `next_watermark_snapshot_id` records this run's maximum and
   the next run promotes it. Every snapshot is offered to the evaluator across
   a full interval rather than at one instant, and re-evaluating a device twice
   is free — one already in the group its rule claims is not moved again.

   NO CLOCK IS INVOLVED, which matters because `inventory_snapshots.taken_at`
   is the AGENT's time and one skewed endpoint would move the boundary for a
   whole organisation. The boundary is a server-assigned uuid7, which orders by
   creation.

   THEY ARE PLAIN `uuid`, NOT A FOREIGN KEY TO `inventory_snapshots`. The value
   is a BOUNDARY, not a reference: `inventory/ingest.py` prunes old snapshots
   to `SNAPSHOT_KEEP`, so the row the watermark names is routinely deleted, and
   an FK would either block that prune or (with `ON DELETE SET NULL`) silently
   reset the org to a full pass. uuid7 sorts by creation, which is the property
   the comparison uses and the same one `_latest_documents` already relies on.

3. `rules_fingerprint` — a rules change must not wait for an ingest. A rule
   decides where a signed job lands, so when the org's rule-bearing groups
   change, the next pass is FULL whatever the watermark says.

4. The counts, including `snapshots_read`. That last one is the number the
   watermark exists to move, recorded rather than inferred: a regression that
   quietly restores the full read shows up as a count that stops falling.

**NO BACKFILL, AND NO ROW IS CREATED HERE.** An org's first sweep after this
migration finds no row, takes a full pass — which is exactly today's behaviour
— and writes one. There is no state to migrate because the state did not exist,
and seeding a row per org would be asserting a `last_evaluated_at` for a run
that never happened.

Revision ID: 0081
Revises: 0080

NUMBERED 0081 RATHER THAN 0080 because `0080_agent_push_dispatch.py` was in the
working tree when this was written. Migrations are forward-only and the chain
is a shared resource: two files claiming one revision id makes `alembic
upgrade` refuse to run at all, which is a broken deployment rather than a merge
conflict.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "device_group_evaluation"
_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        # PRIMARY KEY on `org_id`, so "one row per org" is a schema property
        # rather than a convention the service is trusted to keep. It is also
        # what makes the sweep's write a plain `ON CONFLICT (org_id) DO UPDATE`
        # instead of a read-then-write with a race in the middle.
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_full_pass_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "watermark_snapshot_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "next_watermark_snapshot_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "rules_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("groups_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("devices_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("devices_moved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "devices_without_inventory",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("snapshots_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "full_pass", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )

    # ONE INDEX ON A TABLE THIS MIGRATION DOES NOT OWN, and it is not
    # incidental. The sweep reads "the newest snapshot id this org holds" at
    # the start of every run, as the watermark above. `inventory_snapshots`
    # carries only `(device_id, id)`, so that read is a backward walk of the
    # primary key filtering on `org_id` -- and for a tenant that has ingested
    # nothing while its neighbours have, the walk is the whole table. An
    # incremental sweep whose bookkeeping costs more than the read it saves
    # would be a regression wearing an optimisation's name.
    #
    # DECLARED IN `app/models/inventory.py` TOO. An index that exists only in a
    # migration comes back from the next `alembic revision --autogenerate` as a
    # bare `op.drop_index`, one line of apparent noise in a long diff --
    # exactly how `ix_doc_articles_fts` was deleted for a month.
    # `tests/test_schema_drift.py` compares both sets on every run.
    op.create_index(
        "ix_inventory_snapshots_org_id", "inventory_snapshots", ["org_id", "id"]
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0043, 0050, 0077), with
    # WITH CHECK stated explicitly. It is NOT filling a gap -- for an
    # ALL-command policy PostgreSQL reuses the USING expression as WITH CHECK
    # when it is omitted -- but the explicit clause matters the day a future
    # edit gives this policy a different read predicate than write predicate.
    #
    # FORCED, INCLUDING FOR THE OWNER, because the sweep runs inside
    # `org_session` per org and this row's whole job is to say what happened to
    # ONE tenant's fleet. A leak here would put one customer's evaluation
    # counts on another customer's Groups page.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_snapshots_org_id", table_name="inventory_snapshots")
    # The policy goes with the table; `DROP TABLE` drops its policies. Named
    # here rather than dropped separately so the asymmetry is deliberate
    # rather than looking like an omission.
    op.drop_table(_TABLE)

"""notification: add an index the drain query can actually use

docs/modules/notification/spec.md §2.1, §10. Whole-branch review finding M11.

Migration 0025 created `ix_domain_events_drain` on `(claimed_at, occurred_at)`,
and spec §2.1 named it "for the drain query". The real drain query
(`app.worker.drain_outbox`) is:

    SELECT ... FROM domain_events
    WHERE org_id = :org_id AND processed_at IS NULL
    FOR UPDATE SKIP LOCKED

which filters on `(org_id, processed_at IS NULL)` -- neither `claimed_at` nor
`occurred_at` appears in the query at all, so `ix_domain_events_drain` cannot
be used to satisfy it (Postgres has no reason to touch an index whose leading
columns are absent from the predicate). The index the spec names for the
drain query has never been usable by the drain query.

This migration adds the index that actually matches: a PARTIAL index on
`org_id`, restricted to `WHERE processed_at IS NULL` -- exactly the drain's
own predicate, and exactly the rows that ever need to be found this way (once
`processed_at` is set, a row is never selected by this query again, spec
§10's "idempotent" note). A partial index also stays small relative to a full
index on every event ever published, most of which are processed within
moments of being drained.

`ix_domain_events_drain` (migration 0025, SHIPPED) is left in place rather
than dropped: migrations are forward-only (`CLAUDE.md` -- "never edit a
shipped migration"), and dropping an index in a later migration is a
different, and unrelated, decision from correcting what the spec claims about
it. Spec §2.1 is corrected in the same change to name THIS index for the
drain query and to stop claiming `ix_domain_events_drain` serves it.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_domain_events_org_unprocessed",
        "domain_events",
        ["org_id"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_domain_events_org_unprocessed", table_name="domain_events")

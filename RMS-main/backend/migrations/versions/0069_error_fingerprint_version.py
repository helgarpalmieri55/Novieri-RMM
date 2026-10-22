"""platform errors: record WHICH fingerprint rule named a group

Recommendation 9 of `docs/superpowers/audits/2026-08-21-ux-remediation-
closeout.md` (finding B79). `app/core/errors/capture.py` fingerprinted a
failure at the traceback's LAST frame, which is the raise site and is right
whenever this codebase raised -- and wrong whenever application code called a
library and the library raised, because the last frame is then `httpx` or
`asyncpg` or `sqlalchemy`. Groups came out named after third-party internals,
and every distinct failure funnelled through one library drifted toward that
library's handful of raise lines. Capture now prefers the deepest `app.*`
frame; this column records which of the two rules produced each row.

THERE IS NO BACKFILL IN THIS MIGRATION AND ONE CANNOT BE WRITTEN. Nobody
should come back later and add it. `platform_error_group` stores the DERIVED
site only -- `exc_type`, `module`, `lineno`, `qualname` -- and never the
traceback, deliberately and permanently (error-monitoring spec §0.1: a
captured record carries no exception text, and a traceback is exception text).
For a group already named `httpx._transports.default`, the application frame
that should have named it was discarded at capture time and exists nowhere in
this database. Re-deriving it would require the traceback this platform
refuses to keep. So existing rows stay at version 1 and age out as their sites
stop failing, and new captures record version 2.

WHY `server_default '1'` RATHER THAN A NULLABLE COLUMN. A row without a
version is not a row of unknown provenance: every row already in this table
was written by the version-1 rule, and that is a fact, not a gap. NULL would
say "we do not know", which would be false, and would put a three-valued
comparison in front of every reader for nothing.

WHY THE UNIQUE KEY DOES NOT MOVE. `fingerprint` stays UNIQUE on its own and
the drain keeps upserting `ON CONFLICT (fingerprint)`. A composite unique on
`(fingerprint_version, fingerprint)` was the alternative, and it would have
made every reader responsible for carrying the version -- failing open, by
merging two eras, on the day one of them forgot. Instead the version is
hashed INTO the digest (`fingerprint_of` prefixes `v2|`), so a v1 row and a v2
row for the same site cannot collide, and this column is for legibility: it is
the answer when an operator asks why two groups look like the same bug.

Revision ID: 0069
Revises: 0068
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP = "platform_error_group"


def upgrade() -> None:
    op.add_column(
        _GROUP,
        sa.Column(
            "fingerprint_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    # Rollback in this platform is restore-from-backup (docs/superpowers/specs/
    # 2026-08-15-deploy-rollback-design.md D1). This exists so CI's
    # `upgrade head -> downgrade base -> upgrade head` round trip passes.
    op.drop_column(_GROUP, "fingerprint_version")

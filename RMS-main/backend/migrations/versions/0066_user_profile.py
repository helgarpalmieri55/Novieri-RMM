"""the account profile: the three columns ITMS owns about a person

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7.3.

AFTER 0065, NEVER BESIDE IT. 0065 is this cycle's single migration by
deliberate policy -- four workstreams each planned their own `0065` and Alembic
accepts several files sharing a `down_revision` without complaint, failing only
later at `upgrade head` with "multiple heads", at which point every
database-backed test dies in fixture setup pointing at nobody's code. Piece E
was added mid-cycle, after 0065 had been written and run, so it gets its own
revision chained behind it rather than a second file at the same point.

WHY ALL THREE ARE NULLABLE, with no default. `phone`, `timezone` and `locale`
are facts about a person that this platform does not know until they type them.
A `server_default` would be this migration inventing an answer -- most
obviously for `timezone`, where the only defensible non-null value is the
server's own zone, which is the one thing it certainly is not for a remote
operator. NULL means "not stated", and the console renders it as such; an empty
string would be indistinguishable from a person who cleared the field.

WHY THEY LIVE ON `users` RATHER THAN A `user_profiles` TABLE. Three nullable
scalar columns on a table that is already read on every single admission
(`app/api/deps.py`) and already carries `display_name` -- the presentation
field they sit beside. A side table would add a join to the hottest read path
in the application to store three strings, and a second place for tenancy and
RLS to be got right.

NOT A CREDENTIAL, AND NOT A SECOND FACTOR. §7.4 falsifier 15: nothing in this
platform sends SMS, so `phone` is directory data. If a later change treats it
as an authentication factor, that is a new decision and this line is where it
gets contradicted -- a column an unprivileged user can write about themselves
must never become something that authenticates them.

Revision ID: 0066
Revises: 0065
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("timezone", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("locale", sa.String(16), nullable=True))


def downgrade() -> None:
    # Rollback in this platform is restore-from-backup (docs/superpowers/specs/
    # 2026-08-15-deploy-rollback-design.md D1). This exists so CI's
    # `upgrade head -> downgrade base -> upgrade head` round trip passes.
    for column in ("locale", "timezone", "phone"):
        op.drop_column("users", column)

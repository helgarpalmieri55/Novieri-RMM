"""dashboard_layouts: the operator's own arrangement of the command centre

The operator, 2026-09-01: "main dashboard should be customizable with tiles
also give me the ability to resize and order in whichever way i want."

**ONE ROW PER (ORG, USER), OVERWRITTEN IN PLACE.** A layout is a preference,
not an event: there is no history to keep and nothing downstream reads a
previous version, so the write is a plain `ON CONFLICT (org_id, user_id) DO
UPDATE` rather than an append with a "current" flag to keep straight.

**WHY A TABLE AND NOT `localStorage`.** The obvious cheap answer is the
browser, and it is wrong for this product specifically: an operator signs in
from the NOC workstation, a laptop and somebody else's machine while standing
at their desk, and a layout that exists on one of the three is worse than no
customization at all -- it looks broken rather than absent. It is also lost by
the private window, the cleared site data and the reimaged workstation, which
is a fleet-management platform's ordinary Tuesday.

**WHY NOT A COLUMN ON `users`.** `users` is read on the authentication path
for every request; a JSONB blob a console feature rewrites on every drag does
not belong in that row. A separate table also lets the layout be dropped
cleanly -- deleting the row is exactly "reset to default", with no NULL-vs-
empty ambiguity to interpret.

**`layout` IS JSONB AND DELIBERATELY NOT VALIDATED BY THE DATABASE.** The set
of widgets is a property of the console build, not of the schema: a release
that adds a tile must not need a migration, and a release that removes one
must not strand rows that name it. The server validates SHAPE (a list of
`{id, size}` with a bounded length and a bounded size) and the CLIENT resolves
ids against the widgets it actually has, ignoring what it does not recognise
and appending what the row does not mention. Both halves are required: without
the server's bound a row could be made arbitrarily large by anyone with a
session; without the client's tolerance an old row would blank the dashboard
after every release that touched the widget list.

**NO AUDIT EVENT.** Invariant 4 covers privileged action -- job dispatch,
credential access, config and policy change. Where a person put a tile on
their own screen is none of those, and writing it to the hash-chained log
would dilute a record whose value is that everything in it matters.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dashboard_layouts"
_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        # PART OF THE PRIMARY KEY, so "one layout per person" is a schema
        # property rather than a convention the service is trusted to keep --
        # and what makes the save an upsert with no read-then-write race.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "layout",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0043, 0050, 0077, 0081),
    # WITH CHECK stated explicitly.
    #
    # RLS SCOPES THE TENANT; IT DOES NOT SCOPE THE USER, and that distinction
    # is why the service layer's `user_id` predicate is not redundant. The
    # policy's job is that org A can never read org B's rows. Within one org,
    # "you get YOUR layout" is enforced by the route, which never accepts a
    # user id from the request -- it uses the authenticated principal's, so
    # there is no parameter to tamper with. Both are stated so a later reader
    # does not mistake one for the other and delete the wrong one.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    # The policy goes with the table; `DROP TABLE` drops its policies. Named
    # here rather than dropped separately so the asymmetry is deliberate
    # rather than looking like an omission.
    op.drop_table(_TABLE)

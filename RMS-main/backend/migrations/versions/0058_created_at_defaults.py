"""Replace three frozen `created_at` defaults with `now()`

THE DEFECT. `credentials`, `enrollment_tokens` and `org_vault_keys` each
declared their `created_at` column as

    sa.Column('created_at', sa.DateTime(timezone=True),
              server_default='now()', nullable=True)

with `now()` as a Python STRING rather than `sa.text('now()')`. SQLAlchemy
renders a plain string as a quoted literal, so the emitted DDL is

    DEFAULT 'now()'::timestamp with time zone

and Postgres evaluates that cast ONCE, when the DDL runs. The default is
therefore the wall-clock time the migration happened to execute, frozen
forever, and different on every deployment. Measured on a database migrated
to head just now:

    credentials        ->  '2026-08-16 02:04:30.663117+00'::timestamp with time zone
    enrollment_tokens  ->  '2026-08-16 02:04:30.663117+00'::timestamp with time zone
    org_vault_keys     ->  '2026-08-16 02:04:30.663117+00'::timestamp with time zone

Every other `created_at` in this schema uses `sa.text('now()')` and is
correct; these three are the only ones affected (`information_schema.columns`
where the default does not contain `now()` returns exactly these three).

WHAT IT ACTUALLY BREAKS. Any INSERT that omits `created_at` records the
migration's timestamp instead of the row's. All three columns are nullable
with no application-side default in the ORM, so this is reachable: a
credential or enrollment token created in 2027 would claim to have been
created the day the database was migrated. For `enrollment_tokens` that is
an audit-trail problem, and for `org_vault_keys` it misdates key material.

A FORWARD MIGRATION, NOT AN EDIT. Unlike 0001's missing `DROP TYPE` -- which
had to be corrected in place because a DROP has to happen at that
migration's own point in the chain -- a column default can be reset from
here, so the ordinary rule applies and 0001/0008 are left alone.

THE DOWNGRADE RESTORES THE DEFECT, deliberately. Setting the default back to
the string form reproduces the original behaviour exactly: Postgres freezes
it again, at downgrade time. A downgrade that left `now()` in place would
not be a downgrade.

Found while writing the migration round-trip CI job (spec §1.4): the job's
schema diff had to filter these three lines out, because a frozen literal
differs between any two databases. THAT FILTER IS DELETED IN THIS SAME
COMMIT -- a normalisation that outlives the defect it hides is how the
defect becomes permanent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("credentials", "enrollment_tokens", "org_vault_keys")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            server_default=sa.text("now()"),
        )


def downgrade() -> None:
    # The string form, reproducing the original frozen-literal behaviour.
    for table in _TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            server_default="now()",
        )

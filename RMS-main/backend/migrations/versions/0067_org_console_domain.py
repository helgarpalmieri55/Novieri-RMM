"""backfill the console email-domain allowlist into each org's settings

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.2.

DATA ONLY, NO SCHEMA CHANGE. `organizations.settings` is already a NOT NULL
JSONB document (0001). What moves here is where the console's email-domain
allowlist LIVES: it was `ITS_CONSOLE_EMAIL_DOMAIN`, an environment variable
that needed an SSH session and a restart to change and could refuse every
operator with no way back through the UI. It is now
`settings->>'console_email_domain'`, editable in Settings → Organization, with
the save path refusing a value that would refuse the person saving it.

WHY THIS MIGRATION EXISTS AT ALL. Without it, an upgrade CHANGES BEHAVIOUR.
A deployment that configured the allowlist would, from the moment the new code
resolved the setting first, have every org fall through to... something, and
the operator would have to notice and re-enter a value they already set. The
backfill makes the upgrade a no-op for them: the value they configured is
written into each org's document, which is exactly what the new resolution
order reads first.

WHY IT READS THE ENVIRONMENT, WHICH MIGRATIONS NORMALLY MUST NOT
================================================================
The value being backfilled is not in the database. It is in the process
environment of the api container, which is where `alembic upgrade` also runs
in this stack, so it is readable here and nowhere else. The alternative —
leaving operators to re-enter it — is the behaviour change this file exists to
prevent. It is read ONCE, it is never logged, and a blank or absent variable
means "no allowlist was configured", in which case there is nothing to
backfill and this migration writes nothing.

IT DOES NOT OVERWRITE A KEY THAT IS ALREADY THERE. Idempotent by predicate,
not by hope: re-running it after somebody has set a different value in the
console must not put the old environment value back.

STRIPPED, AND THAT IS LOAD-BEARING. `ITS_CONSOLE_EMAIL_DOMAIN="toeshee.com\\n"`
— what a here-doc, a copied `.env` line or a shell `read` produces — is
non-empty, matches no address at all, and would therefore be a refusal of
EVERY user at ALL THREE doors, backfilled permanently into every org's stored
settings. `app.core.identity.email_domain.domain_refusal` strips the same way
for the same reason; this stores the value it would have used.

Revision ID: 0067
Revises: 0066
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTINGS_KEY = "console_email_domain"
LEGACY_ENV_VAR = "ITS_CONSOLE_EMAIL_DOMAIN"


def backfill_statement(value: str) -> sa.TextClause:
    """The UPDATE, as a statement rather than a side effect.

    Separated so `tests/test_domain_policy.py` can execute it against a real
    Postgres and prove BOTH of its rules -- that it writes the value, and that
    a second run does not overwrite what an operator has since set in the
    console. A migration whose idempotence is asserted only in its own
    docstring is a migration whose idempotence is untested.
    """
    # EVERY BIND IS CAST EXPLICITLY, and that is not defensive noise: the
    # driver sends parameters as `varchar`, and `jsonb_set(jsonb, varchar,
    # jsonb, boolean)` is not a function Postgres has -- the statement fails at
    # runtime with "No function matches the given name and argument types",
    # which on a migration means a failed upgrade in the field. The path is a
    # `text[]`, not a string.
    return sa.text(
        "UPDATE organizations "
        "SET settings = jsonb_set("
        "    settings, CAST(:path AS text[]), to_jsonb(CAST(:value AS text)), true"
        ") "
        "WHERE settings -> CAST(:key AS text) IS NULL"
    ).bindparams(path="{" + SETTINGS_KEY + "}", value=value, key=SETTINGS_KEY)


def upgrade() -> None:
    configured = os.environ.get(LEGACY_ENV_VAR, "").strip()
    if not configured:
        # No allowlist was configured, so there is nothing to preserve. Writing
        # a key here would be this migration INVENTING a security setting.
        return
    op.execute(backfill_statement(configured))


def downgrade() -> None:
    # Rollback in this platform is restore-from-backup (docs/superpowers/specs/
    # 2026-08-15-deploy-rollback-design.md D1). This exists so CI's
    # `upgrade head -> downgrade base -> upgrade head` round trip passes, and it
    # removes the key rather than trying to distinguish a backfilled value from
    # one an operator later typed — on the only path this ever runs (a fresh CI
    # database) there is no such value to lose.
    op.execute(
        sa.text(
            "UPDATE organizations SET settings = settings - CAST(:key AS text)"
        ).bindparams(key=SETTINGS_KEY)
    )

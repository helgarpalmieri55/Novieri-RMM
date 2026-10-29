"""`domain_events` under the generic O check (plan 2026-10-26 Task 15).

Phase 1 (0231) let any pinned scope write a client-less event because
`events.publish` could not name the subject's client. Every caller names it
since phase 2 (Task 1 made it a required keyword; each family task filled
it), so a NULL client -- an event for every client -- is written only under
scope all, as on every other O table.

Revision ID: 0243
Revises: 0242
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0243"
down_revision: str | None = "0242"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE = "current_setting('app.client_scope', true)"
VISIBLE = "its_client_visible(client_id)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
READ = f"(client_id IS NULL AND {SCOPE_PINNED}) OR {VISIBLE}"
MODIFY = f"(client_id IS NULL AND {SCOPE} = 'all') OR {VISIBLE}"


def _policy(check: str) -> None:
    op.execute("DROP POLICY client_isolation ON domain_events")
    op.execute(
        "CREATE POLICY client_isolation ON domain_events AS RESTRICTIVE FOR ALL "
        f"USING ({READ}) WITH CHECK ({check})"
    )


def upgrade() -> None:
    _policy(MODIFY)


def downgrade() -> None:
    _policy(READ)

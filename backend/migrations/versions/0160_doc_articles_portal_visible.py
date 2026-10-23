"""documentation: help articles in the end-user portal

Adds `doc_articles.portal_visible` (default false, so every existing article
stays internal) and lets `portal_user` read the published, portal-visible
slice of the table.

The database enforces the slice, not the portal code: a RESTRICTIVE policy
bound to `portal_user` is AND'd with the permissive org `tenant_isolation`
policy, so a portal session sees only this org's articles that are both
flagged and not archived -- whatever id it asks for. The grant is
column-level, so `created_by`/`updated_by` and every other column stay
unreadable to the portal role. No grant on `doc_links` or `doc_versions`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0160"
down_revision: str | None = "0150"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Columns the portal needs: output (id, title, body) and the ones the two
# policies test (org_id, portal_visible, archived).
_PORTAL_COLUMNS = "id, org_id, title, body, portal_visible, archived"


def upgrade() -> None:
    op.add_column(
        "doc_articles",
        sa.Column("portal_visible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(f"GRANT SELECT ({_PORTAL_COLUMNS}) ON doc_articles TO portal_user")
    op.execute(
        "CREATE POLICY portal_isolation ON doc_articles AS RESTRICTIVE TO portal_user "
        "USING (portal_visible AND NOT archived)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS portal_isolation ON doc_articles")
    op.execute(f"REVOKE SELECT ({_PORTAL_COLUMNS}) ON doc_articles FROM portal_user")
    op.drop_column("doc_articles", "portal_visible")

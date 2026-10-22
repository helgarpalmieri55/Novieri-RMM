"""notification: add an explicit port to webhook_allowlist_entries

docs/modules/notification/spec.md §2.6, §4.1. Task 4b.

An allowlist entry previously named a HOST only. The task-4 review's port-
scope fix (commit ee61acc, closing Finding 4 -- "allowlisting a host grants
every TCP port on it") then constrained *every* outbound request, allowlisted
or not, to the scheme's default port, because there was no way in the schema
to express "this host, but also this specific other port" without a schema
change. That broke exactly the on-prem case spec §0.1 exists for: an internal
Mattermost on :8065, Gitea on :3000, Jenkins on :8080 are ordinary webhook
targets on this platform's LAN deployments, not an attacker's raw listener.

This migration adds `port` so an admin allowlists a HOST AND PORT together --
preserving §2.6's "auditable and specific" rationale (a bare host entry would
silently mean "every port", which is the exact blanket-permit problem §2.6
warns a CIDR entry would create) while making a real non-default-port
destination reachable again.

`port` is NOT NULL with no NULL-means-"any port"/"default port"/"unscoped"
ambiguity: every option other than NOT NULL reintroduces exactly the
ambiguity this migration exists to remove, and a NULL that quietly meant "any
port" would silently reopen Finding 4. Existing rows -- there are none in any
real deployment yet; this table has had no wired-in writer before Task 4b --
backfill to 443 via `server_default`, i.e. the only port the guard has
enforced as reachable since ee61acc, so no existing entry's actual
reachability changes as a side effect of this migration.

The uniqueness constraint moves from `(org_id, host)` to `(org_id, host,
port)`: the same host may now legitimately need two rows (Mattermost's
webhook on :8065 and a second internal tool on :3000 on the same box), which
was structurally impossible under the old constraint.

Migration 0025 is SHIPPED; never edit it. This is forward-only.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_allowlist_entries",
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
    )
    op.drop_constraint("uq_allowlist_org_host", "webhook_allowlist_entries", type_="unique")
    op.create_unique_constraint(
        "uq_allowlist_org_host_port", "webhook_allowlist_entries", ["org_id", "host", "port"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_allowlist_org_host_port", "webhook_allowlist_entries", type_="unique")
    op.create_unique_constraint(
        "uq_allowlist_org_host", "webhook_allowlist_entries", ["org_id", "host"]
    )
    op.drop_column("webhook_allowlist_entries", "port")

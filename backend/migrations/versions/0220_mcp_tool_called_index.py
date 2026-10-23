"""MCP: a partial index for the console's AI-connections read.

`GET /api/v1/mcp/connections` (`app/mcp/api.py`) aggregates this org's
`mcp.tool_called` audit events of the last 90 days. `audit_events` had only
`(org_id, id)` and `(org_id, target_type, target_id)`, neither of which serves a
`ts` window, so the read scanned the org's whole audit history. This index holds
only `mcp.tool_called` rows, keyed `(org_id, ts)`: the read becomes a range scan
of one org's tool calls in the window, and no other action's rows pay for it.

`audit_events` is append-only by trigger (0004) on UPDATE and DELETE only, and is
not partitioned, so a plain `CREATE INDEX` inside the migration transaction is
correct. It takes a SHARE lock that blocks audit appends while it builds; at
this table's size that is seconds, and `migrate` runs before `api` starts.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0220
Revises: 0210
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0220"
down_revision: str | None = "0210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_audit_mcp_tool_called ON audit_events (org_id, ts) "
        "WHERE action = 'mcp.tool_called'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_mcp_tool_called")

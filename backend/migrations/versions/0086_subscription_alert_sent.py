"""subscriptions: remember which notice-deadline and renewal alerts were sent

`docs/modules/subscriptions/spec.md` §0.1 computed notice and renewal "alerts"
only when somebody opened the page. A daily sweep
(`app/modules/subscriptions/alerts.py`) now publishes them through the
notification outbox, and this table makes that once per subscription per
deadline: the UNIQUE key is `(org_id, subscription_id, kind, deadline)`, so a
renewal that moves `renews_at` is a new deadline and is announced again, and
the same deadline never is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "subscription_alert_sent"
_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "org_id", "subscription_id", "kind", "deadline", name="uq_subscription_alert_sent"
        ),
        sa.CheckConstraint(
            "kind IN ('notice', 'renewal')", name="ck_subscription_alert_sent_kind"
        ),
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def downgrade() -> None:
    op.drop_table(_TABLE)

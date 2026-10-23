"""platform: remember, per org, what each org was told about each platform probe

`docs/08-observability.md` §6 said "no alerting": the platform sampled its own
probes every minute and told nobody. `app/core/health/notify.py` now publishes
`platform.probe_unhealthy` / `platform.probe_recovered` through the
notification outbox, and this table is its de-duplication state: the last
state announced, when, and whether the current episode was announced at all.

ORG-SCOPED, under the standard tenant policy. The probe verdict is platform
information (read from `platform_health_sample`), but a notification is
delivered through one org's rules and channels, so what was sent is that
org's fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_probe_notice"
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
        sa.Column("probe", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_unhealthy_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "episode_notified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.CheckConstraint(
            "state IN ('healthy', 'unhealthy')", name="ck_platform_probe_notice_state"
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

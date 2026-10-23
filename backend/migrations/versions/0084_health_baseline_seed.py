"""health: record, once per org, whether the default baseline profile was seeded

Platform review 2026-09-23: a fresh install monitors nothing until someone
writes a health profile. `app/modules/health/baseline.py` now seeds a
"Workstation baseline" profile and assigns it as the org default, for a new
org and for any existing org that has no profile at all.

WHY A TABLE. "Has no profile" cannot be the whole rule: an operator who
deletes every profile on purpose must not find the baseline back an hour
later. One row per org records that the decision was made -- `seeded`, or
`skipped` because the org already had profiles -- and the seed never runs for
that org again. The seed itself runs in Python, not here, so it goes through
the profile service's validation and writes the audit chain (invariant #4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: str | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "health_baseline_seed"
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
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column(
            "profile_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_profile.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outcome IN ('seeded', 'skipped')", name="ck_health_baseline_seed_outcome"
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

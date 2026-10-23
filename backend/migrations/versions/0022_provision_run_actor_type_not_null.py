"""Provisioning: enforce NOT NULL on provision_run.actor_type.

Schema-drift finding (backend/tests/test_schema_drift.py): migration 0015
created `provision_run.actor_type varchar(10)` without `NOT NULL`, but the
`ProvisionRun` model (app/modules/provisioning/models.py) declares
`actor_type: Mapped[str]` -- non-optional -- matching the same column on
every other actor_type column in the schema (`audit_events`, `backup_run`,
`restore_job`, all `NOT NULL`) and every call site that constructs a
`ProvisionRun` (app/modules/provisioning/dispatch.py and tests), which always
sets it. The migration was wrong; the model was right. Backfill any stray
NULL to "system" (the actor_type used for automated/background provisioning
outcomes elsewhere in this module) before tightening the column, so this is
safe to run against a database that already has rows.

Revision ID: 0022
Revises: 0021
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE provision_run SET actor_type = 'system' WHERE actor_type IS NULL")
    op.execute("ALTER TABLE provision_run ALTER COLUMN actor_type SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE provision_run ALTER COLUMN actor_type DROP NOT NULL")

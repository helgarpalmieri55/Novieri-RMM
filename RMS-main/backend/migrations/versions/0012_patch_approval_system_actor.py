"""Allow patch_approvals.approved_by to be NULL for system-initiated approvals.

Migration 0011 modeled `approved_by` as NOT NULL, matching the human-approval
flow (`approve_patch`, a later task). But PD-02 (docs/modules/patch/spec.md)
requires the ingest service to auto-approve security-severity patches into the
test ring with no human in the loop — there is no user to attribute the row
to. Rather than fabricate a synthetic "system" user row (which would leak into
user-facing lists/audits as if a person approved it), the honest model is:
`approved_by IS NULL` means the platform itself approved it, and the paired
`audit_events` row (`actor_type="system"`, `actor_id=NULL`) is the record of
who/what did it. This is additive-only forward migration; 0011 is not edited.

Revision ID: 0012
Revises: 0011
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patch_approvals ALTER COLUMN approved_by DROP NOT NULL")


def downgrade() -> None:
    op.execute("DELETE FROM patch_approvals WHERE approved_by IS NULL")
    op.execute("ALTER TABLE patch_approvals ALTER COLUMN approved_by SET NOT NULL")

"""Budget: add expense.invoice_filename (the sanitized attachment display name).

docs/modules/budget/spec.md §5, §8 -- P1(b) of the post-review controller fix
(.git/sdd/task-12-report.md): the object-storage key is now derived entirely
server-side (no client input, ever); the client-supplied filename is instead
sanitized and persisted here, as a DISPLAY name only, never as a path
component.

Revision ID: 0021
Revises: 0020
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE expense ADD COLUMN invoice_filename varchar(255)")


def downgrade() -> None:
    op.execute("ALTER TABLE expense DROP COLUMN IF EXISTS invoice_filename")

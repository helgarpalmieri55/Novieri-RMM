"""sla: allow end_time = 00:00 on sla_calendar_window (midnight end-of-day)

docs/modules/sla/spec.md §2.2, §4; Task 2 review finding F2, Task 3 review.

`ck_sla_calendar_window_start_before_end` (migration 0033) was
`start_time < end_time`. Postgres `TIME` has no representation for "24:00" --
its domain is `00:00:00 .. 23:59:59.999999` -- so `end_time = '00:00:00'` can
NEVER satisfy `start_time < end_time` for any `start_time`: midnight is the
type's own minimum value. The constraint therefore made it *physically
impossible* to store the one window shape Task 2's clock (`calendar.py`,
`CalendarSpec`'s docstring) explicitly treats as legal and necessary: "An
`end_time` of `00:00` means midnight at the end of that day. It is the only
way to express a window that runs to midnight, and therefore the only way to
express a genuinely 24-hour day" (a night shift's second half, or a 24/7
calendar). Confirmed against a live Postgres instance:

    SELECT '10:00'::time < '00:00'::time;   -- f, for EVERY start_time

0033 is already shipped and migrations are forward-only, so the fix is a new
migration rather than an edit to 0033. `ck_sla_calendar_window_weekday_valid`
(0034) is untouched.

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = "start_time < end_time"
_NEW_CHECK = "start_time < end_time OR end_time = '00:00:00'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sla_calendar_window DROP CONSTRAINT "
        "ck_sla_calendar_window_start_before_end"
    )
    op.execute(
        "ALTER TABLE sla_calendar_window ADD CONSTRAINT "
        f"ck_sla_calendar_window_start_before_end CHECK ({_NEW_CHECK})"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE sla_calendar_window DROP CONSTRAINT "
        "ck_sla_calendar_window_start_before_end"
    )
    op.execute(
        "ALTER TABLE sla_calendar_window ADD CONSTRAINT "
        f"ck_sla_calendar_window_start_before_end CHECK ({_OLD_CHECK})"
    )

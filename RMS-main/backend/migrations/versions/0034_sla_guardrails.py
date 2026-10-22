"""sla guardrails: staff-only breach routing, weekday domain

Two CHECK constraints, both closing a gap found reviewing migration 0033.
0033 is already shipped and migrations are forward-only, so these are added
here rather than by editing it.

1. `notification_rules.ck_rule_no_sla_breach_to_requester` -- SLA spec §6.
   `ticket.sla_breached` paired with `recipient_kind = 'requester'` was
   representable at every layer: the Pydantic validator, the service check and
   0025's `ck_rule_no_internal_to_requester` were each literal to
   `ticket.internal_note_added`. The breach mail is written staff-facing
   ("this ticket breached its SLA ... sign in to review it"), and
   `recipient_kind = 'requester'` resolves the recipient from the TICKET, not
   from the event payload -- so the SLA payload omitting `requester_id` was a
   convention, not a guarantee, and the mail would in fact have been delivered
   to the external customer. Refused at write time, matching notification spec
   §6.4's reasoning for internal notes: a render-time filter is one template
   edit away from failing open.

2. `sla_calendar_window.ck_sla_calendar_window_weekday_valid` -- SLA spec §2.2
   ("weekday (int 0-6, Monday=0)"). `weekday = 99` inserted successfully
   before this. Python's `date.weekday()` only ever returns 0-6, so an ISO
   Sunday-is-7 typo produced a window that silently never applied: the
   calendar quietly lost a working day, deadlines landed later than the org
   configured, and nothing errored anywhere.

Revision ID: 0034
Revises: 0033
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE notification_rules ADD CONSTRAINT ck_rule_no_sla_breach_to_requester "
        "CHECK (NOT (event_type = 'ticket.sla_breached' AND recipient_kind = 'requester'))"
    )
    op.execute(
        "ALTER TABLE sla_calendar_window ADD CONSTRAINT "
        "ck_sla_calendar_window_weekday_valid CHECK (weekday BETWEEN 0 AND 6)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE sla_calendar_window DROP CONSTRAINT "
        "IF EXISTS ck_sla_calendar_window_weekday_valid"
    )
    op.execute(
        "ALTER TABLE notification_rules DROP CONSTRAINT "
        "IF EXISTS ck_rule_no_sla_breach_to_requester"
    )

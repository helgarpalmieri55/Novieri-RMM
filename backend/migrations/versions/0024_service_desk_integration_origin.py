"""Service Desk: add 'integration' as a machine-origin ticket source, and pin
the `origin` domain with a CHECK constraint for the first time (whole-branch
security review, finding A3; docs/modules/service-desk/spec.md §2).

`change_mgmt`'s `push_change` adapter
(`app.modules.service_desk.adapters.ServiceDeskChangeTicketAdapter
.push_change`) auto-opens a ticket when a change transitions, attributed to
the change's own author -- but the ACT of creating that ticket is the system
reacting to someone else's later transition, not that author sitting at a
keyboard filing a helpdesk ticket. The origin it used before this fix,
`"technician"`, made the resulting `ticket_status_history` row and
`ticket.created` audit event read `actor_type="user"`, `actor_id=<the
author>` -- indistinguishable, in the immutable hash-chained log, from that
person filing the ticket by hand.

`"integration"` is the fifth origin value (alongside `"technician"`/
`"alert"`/`"portal"`/`"email"`). `app.modules.service_desk.tickets
.create_ticket` maps it to `actor_type="system"` for the CREATE action's
attribution the same way `"alert"` already does -- but, unlike `"alert"`, it
is NOT exempted from `ck_ticket_human_attribution`: a real `requester_id`/
`created_by` is still required and still carried, since the ticket genuinely
has a human behind it (the change's author); only the create ACT's
attribution differs.

`origin` had no CHECK constraint enumerating its legal values before this
migration -- `app.modules.service_desk.schema.TicketOrigin`'s `Literal` was
the only thing pinning the domain. This migration adds one
(`ck_ticket_origin_valid`), covering all five current values, so a raw SQL
insert cannot introduce a sixth the application layer does not know about
either.

Never edit 0023 (shipped); this is forward-only.

Revision ID: 0024
Revises: 0023
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_ORIGINS = ("technician", "alert", "portal", "email", "integration")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ticket ADD CONSTRAINT ck_ticket_origin_valid CHECK (origin IN ("
        + ", ".join(f"'{origin}'" for origin in _VALID_ORIGINS)
        + "))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ticket DROP CONSTRAINT ck_ticket_origin_valid")

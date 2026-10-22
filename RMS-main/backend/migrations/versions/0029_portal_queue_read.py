"""portal: grant portal_user SELECT on ticket_queue

Revision ID: 0029
Revises: 0028

WHY THIS EXISTS. `docs/modules/portal/spec.md` §2.1 originally gave
`portal_user` NO access to `ticket_queue`, on the reasoning that "a requester
never chooses a queue". That is true of the API surface and false of the
implementation: `ticket.queue_id` is NOT NULL, and
`service_desk.tickets.create_ticket` resolves it via
`refdata.resolve_queue(...)`, which issues `select(TicketQueue)`. Without a
grant that SELECT raises `permission denied for table ticket_queue`, so every
portal ticket creation would have failed with a 500 -- not even a clean error.

Found by probing the real schema while executing the portal plan, before the
service layer was written.

NO `portal_isolation` policy is added here. The existing PERMISSIVE
`tenant_isolation` policy already scopes `ticket_queue` to the caller's org,
which is the correct scope: a queue is org refdata, not requester-owned, and a
restrictive requester predicate on it would make queue resolution impossible.
A requester still never SEES a queue -- the portal's response models carry no
queue field (spec §5) -- this grant only lets the ticket-core service resolve
one internally.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON ticket_queue TO portal_user")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON ticket_queue FROM portal_user")

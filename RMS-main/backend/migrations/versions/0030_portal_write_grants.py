"""portal: grant portal_user the minimum additional access for ticket-core
write paths to run natively under portal_session

docs/modules/portal/spec.md §2.1, §6.

Revision ID: 0030
Revises: 0029

WHY THIS EXISTS. Migration 0028's grants let a requester create/read their
own `ticket`/`ticket_comment`/`ticket_attachment` rows, but
`service_desk.tickets.create_ticket`, `threads.add_comment`, and
`threads.add_attachment` -- the ticket-core functions the portal is REQUIRED
to delegate to rather than reimplement (spec §6.1) -- each unconditionally
touch tables `portal_user` had no grant on at all: `users` (`create_ticket`'s
`resolve_user`), `audit_events` (every one of the three calls
`audit_service.record`), and `domain_events` (`create_ticket` when a
category requires approval, and `add_comment`/`add_attachment`
unconditionally, via `publish()`); and `ticket_status_history`/
`ticket_approval` only had SELECT, never INSERT, though `create_ticket`
inserts both (the first unconditionally, the second when the category
requires approval).

Task 4 (`.git/sdd/portal-task-4-report.md`) worked around the gap by opening
a SECOND, `org_session`-scoped connection for the delegated write, after
verifying ownership on the portal connection first. That satisfied "not-mine
is 404" but meant portal WRITES were enforced by application code, not the
database -- exactly what spec §0.2 rejected ("this project has already
shipped a tenancy predicate that passed its test for the wrong reason").
This migration grants the minimum the ticket-core write paths ACTUALLY need,
determined empirically by running each write path directly on a
`portal_session` connection and widening one grant at a time until each
`permission denied` error cleared, rather than re-derived from a description
(`.git/sdd/portal-fix-db-enforced-writes-report.md` has the transcript).

GRANTS ADDED, and why each is the minimum:

* `users`: SELECT, plus a NEW restrictive policy `id = requester GUC`.
  `create_ticket`'s `resolve_user` always resolves exactly the requester
  (never `assigned_to` -- the portal never sets it) for a portal-origin
  ticket. A plain SELECT grant would let a requester read every colleague's
  name and email in the org (the existing `tenant_isolation` policy is
  org-scoped, not row-scoped) -- a new data leak invariant #5 forbids. The
  restrictive policy closes that: a requester can read ONLY their own row.
  Verified empirically: under the new policy a requester's `SELECT * FROM
  users` under `portal_session` returns exactly one row -- their own.

* `audit_events`: SELECT **and** INSERT -- not INSERT-only, which is what was
  expected going in. `audit_service.record` SELECTs the latest `hash` in the
  org's chain before computing the next link; granting INSERT alone still
  raised `permission denied for table audit_events`, because the SELECT
  executes first and fails before the INSERT is ever attempted. NO
  `portal_isolation` policy is added here, deliberately: the hash-chain
  SELECT must see the true latest event in the org regardless of which actor
  wrote it, or a portal-authored link would record the wrong `prev_hash` and
  `AuditService.verify()` would report the chain broken the next time a
  staff-authored event interleaves with a portal-authored one in the same
  org. This mirrors the existing precedent for `ticket_category`/
  `ticket_queue`/`ticket_number_counter` (spec §2.1): org-scoped,
  unrestricted-by-requester reads of operational/structural data a portal
  write must consult, relying on the pre-existing `tenant_isolation` policy
  (migration 0002) for the org boundary alone. UPDATE and DELETE remain
  revoked (invariant #4: append-only) -- verified empirically that
  `portal_user` can do neither after this migration.

* `domain_events`: INSERT only. `publish()` only ever appends
  (`session.add(DomainEvent(...))`); nothing in the portal's call paths reads
  it back, so no SELECT is granted. No new policy: `tenant_isolation`
  (migration 0025) already scopes it to the org, and a domain event is the
  notification outbox, not requester-owned data -- the same reasoning as
  `audit_events` above, and as the existing `ticket_category` carve-out.

* `ticket_status_history`, `ticket_approval`: INSERT only, added to the
  SELECT migration 0028 already granted. Both already carry a
  `portal_isolation` RESTRICTIVE policy (migration 0028) with a `USING`
  clause and no explicit `WITH CHECK`. Verified empirically, not assumed:
  Postgres uses the `USING` expression for both the read-side row filter AND
  the write-side check when a policy applies to INSERT/UPDATE and defines no
  separate `WITH CHECK` -- a direct cross-requester INSERT attempt against
  either table (a ticket belonging to a DIFFERENT requester in the same org)
  raises `new row violates row-level security policy "portal_isolation"`. No
  new policy is added for these two tables; the existing one, once INSERT is
  granted, already refuses a cross-requester write.

No new tables, no column changes -- grants and one new policy only, per spec
§2.4.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQ_GUC = "current_setting('app.current_requester', true)::uuid"


def upgrade() -> None:
    # users: SELECT, restricted to the caller's own row. Without the
    # restrictive policy this would let a requester read every colleague's
    # name and email in the org via the pre-existing org-scoped
    # tenant_isolation policy alone.
    op.execute("GRANT SELECT ON users TO portal_user")
    op.execute(
        f"CREATE POLICY portal_isolation ON users AS RESTRICTIVE TO portal_user "
        f"USING (id = {_REQ_GUC})"
    )

    # audit_events: SELECT (the hash-chain's own prev_hash lookup) + INSERT
    # (append the new link). Never UPDATE/DELETE (invariant #4). No portal
    # policy -- see module docstring on why restricting this SELECT by
    # requester would corrupt the hash chain.
    op.execute("GRANT SELECT, INSERT ON audit_events TO portal_user")

    # domain_events: INSERT only -- the notification outbox is write-only
    # from the portal's perspective.
    op.execute("GRANT INSERT ON domain_events TO portal_user")

    # ticket_status_history / ticket_approval: add INSERT to the existing
    # SELECT. Their existing portal_isolation policy's USING clause already
    # covers the write-side check (verified empirically -- see docstring).
    op.execute("GRANT INSERT ON ticket_status_history TO portal_user")
    op.execute("GRANT INSERT ON ticket_approval TO portal_user")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON ticket_approval FROM portal_user")
    op.execute("REVOKE INSERT ON ticket_status_history FROM portal_user")
    op.execute("REVOKE INSERT ON domain_events FROM portal_user")
    op.execute("REVOKE SELECT, INSERT ON audit_events FROM portal_user")
    op.execute("DROP POLICY IF EXISTS portal_isolation ON users")
    op.execute("REVOKE SELECT ON users FROM portal_user")

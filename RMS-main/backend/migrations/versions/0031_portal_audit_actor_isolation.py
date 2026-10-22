"""portal: restrict portal_user's audit_events INSERT to its own actor id

docs/modules/portal/spec.md, "Open question 1" in the whole-branch review
(.git/sdd/portal-final-review.md).

Revision ID: 0031
Revises: 0030

WHY THIS EXISTS. Migration 0030 granted `portal_user` SELECT+INSERT on
`audit_events` with NO portal policy at all -- deliberately, because the
hash-chain's own `prev_hash` lookup (`audit_service.record`) must see the
TRUE latest event in the org regardless of who wrote it (see 0030's own
docstring). That reasoning is correct for the SELECT half, but it says
nothing about the INSERT half: with no policy at all, a portal connection
can INSERT an `audit_events` row attributing ANY `actor_id` to itself --
i.e. a portal principal could write an audit event that appears to have
been performed by a different user entirely. Nothing in the application
code does this today (`audit_service.record` is always called with
`actor_id=requester_id` on every portal write path), but nothing at the
database layer prevented it either -- exactly the gap invariant #5's
"enforce it at the database layer, not only in application code" discipline
exists to close.

THE FIX. A RESTRICTIVE policy, scoped to `portal_user` only (never `TO
public`, so app_user/staff writes are completely unaffected), constraining
INSERT (via `WITH CHECK`) to rows whose `actor_id` matches the caller's own
pinned requester GUC. `USING` is deliberately omitted (defaults to `true`):
this policy must not touch the hash-chain's own SELECT of the latest event,
which -- per 0030's docstring -- has to see events from every actor in the
org, not just this one. This is cheap, additive, and does not touch the
audit chain's read side at all; it only narrows what a portal connection
may ATTRIBUTE a new event to.

This does NOT address the org-wide SELECT itself (portal_user can still
read every audit event in the org, by design -- the hash-chain lookup
needs it) or add a `SECURITY DEFINER` chain-head function. Both are
follow-ups tracked separately, deliberately out of scope here (see
CLAUDE.md's "DO NOT do these" list for this task and the review's Open
Question 1).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQ_GUC = "current_setting('app.current_requester', true)::uuid"


def upgrade() -> None:
    # RESTRICTIVE + TO portal_user: AND'd with every other applicable policy,
    # and inert for every other role. No USING clause (defaults to `true`) --
    # the hash-chain's prev_hash SELECT must keep seeing every actor's
    # events in the org; only the WRITE side is constrained.
    op.execute(
        f"CREATE POLICY portal_isolation ON audit_events AS RESTRICTIVE TO portal_user "
        f"WITH CHECK (actor_id = {_REQ_GUC})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS portal_isolation ON audit_events")

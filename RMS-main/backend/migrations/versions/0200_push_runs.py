"""push_run and push_run_target: a confirmed subnet push, paced 50 at a time
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.3;
CLAUDE.md 2a(e), 2a(f) as amended 2026-09-30).

Both tables are tenant-scoped under forced RLS, like every push table
(`0077_agent_push.py`, `0190_push_credential_realm.py`).

`uq_push_run_active` makes "one active run per organization" a database rule:
active means running, paused or `stopped_credential` (a run the lockout guard
stopped still waits for a person to resume or cancel it).

THE CONFIRMED LIST IS SEALED, AND SO IS WHAT A PERSON CONFIRMED (R14).

`push_run_target_fixed`, BEFORE INSERT OR UPDATE ON push_run_target, refuses:
  * an INSERT into a run whose `sealed_at` is set. `runs.create_run` sets it
    in the transaction that writes the targets, so no later code path, present
    or future, can add a machine to a confirmed run;
  * an INSERT whose run it cannot find in the target's own organization -- it
    FAILS CLOSED, so a run hidden from it is refused like a sealed one;
  * any UPDATE that changes which machine a target names: `run_id`, `org_id`,
    `address` or `position`, or `device_id` to anything but NULL. The
    confirmed machine is the unit of authority; `devices ON DELETE SET NULL`
    may take it away, nothing may substitute another. State, job, outcome and
    the guard's columns may move.

`push_run_fixed`, BEFORE UPDATE ON push_run, refuses:
  * clearing or moving `sealed_at` once it is set;
  * any change to `prefix`, `list_digest`, `target_count`, `created_by` or
    `org_id` -- what was confirmed, by whom, for whom;
  * any change to `credential_id` except to NULL. `credentials ON DELETE SET
    NULL` is the one change allowed (the pacer then refuses every remaining
    target as `credential_missing`); a run is never handed another credential.
  State, the timestamps and the lockout guard's window may move.

BOTH FUNCTIONS ARE SECURITY INVOKER, AND NEITHER NEEDS MORE. Only
`push_run_target_fixed`'s INSERT branch reads a table, and it runs with the
caller's privileges and the caller's RLS. Under FORCE ROW LEVEL SECURITY a
table's owner is subject to the policy too, and only a role with BYPASSRLS (or
a superuser) sees past it, so no definer could promise to "see the run
whatever the caller's RLS context is" without depending on which role owns the
function. Failing closed makes that question moot: a caller who cannot see the
run cannot add a target to it, and a caller who can sees its `sealed_at`.
The app role (NOBYPASSRLS) sees exactly the runs of `app.current_org`, which is
the only org whose targets its `WITH CHECK` lets it insert. Both functions pin
`search_path`.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0200
Revises: 0190
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0200"
down_revision: str | None = "0190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = ("push_run", "push_run_target")

_FIXED_FUNCTION = """
CREATE FUNCTION push_run_target_fixed() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_sealed timestamptz;
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT sealed_at INTO v_sealed FROM push_run
      WHERE id = NEW.run_id AND org_id = NEW.org_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'push_run % is not a run of this organization', NEW.run_id
        USING ERRCODE = '23514';
    END IF;
    IF v_sealed IS NOT NULL THEN
      RAISE EXCEPTION 'push_run % is sealed: a confirmed list is never extended', NEW.run_id
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.run_id IS DISTINCT FROM OLD.run_id
     OR NEW.org_id IS DISTINCT FROM OLD.org_id
     OR NEW.address IS DISTINCT FROM OLD.address
     OR NEW.position IS DISTINCT FROM OLD.position
     OR (NEW.device_id IS DISTINCT FROM OLD.device_id AND NEW.device_id IS NOT NULL) THEN
    RAISE EXCEPTION 'a push_run_target names one confirmed machine and never changes which'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$
"""

_RUN_FIXED_FUNCTION = """
CREATE FUNCTION push_run_fixed() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
BEGIN
  IF OLD.sealed_at IS NOT NULL AND NEW.sealed_at IS DISTINCT FROM OLD.sealed_at THEN
    RAISE EXCEPTION 'push_run % is sealed: the seal is never lifted or moved', OLD.id
      USING ERRCODE = '23514';
  END IF;
  IF NEW.org_id IS DISTINCT FROM OLD.org_id
     OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.prefix IS DISTINCT FROM OLD.prefix
     OR NEW.list_digest IS DISTINCT FROM OLD.list_digest
     OR NEW.target_count IS DISTINCT FROM OLD.target_count THEN
    RAISE EXCEPTION 'a push_run records what a person confirmed, and for whom, and never changes it'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.credential_id IS DISTINCT FROM OLD.credential_id AND NEW.credential_id IS NOT NULL THEN
    RAISE EXCEPTION 'a push_run never changes its credential; it can only lose a deleted one'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "push_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("list_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The lockout guard's window (spec §1.4a): when the run last started or
        # resumed. Only targets DISPATCHED at or after it are counted.
        sa.Column("auth_window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'paused', 'stopped_credential', 'cancelled', 'completed')",
            name="ck_push_run_state",
        ),
    )
    op.create_index(
        "uq_push_run_active",
        "push_run",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('running', 'paused', 'stopped_credential')"),
    )
    op.create_index("ix_push_run_org_created", "push_run", ["org_id", "created_at"])

    op.create_table(
        "push_run_target",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id"), nullable=False
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True
        ),
        sa.Column("outcome", sa.String(32), nullable=False, server_default=""),
        sa.Column("detail", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        # When the pacer dispatched it: the lockout guard's clock. A result
        # counts only in the window the target was DISPATCHED in (spec §1.4a).
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        # Which stored version of the secret the job carried: an opaque
        # SHA-256 over the vault envelope (`dispatch.credential_fingerprint`),
        # never the plaintext. A sign-in proves only that version.
        sa.Column("credential_fingerprint", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "state IN ('waiting', 'in_flight', 'installed', 'failed', 'refused', 'cancelled')",
            name="ck_push_run_target_state",
        ),
    )
    op.create_index(
        "uq_push_run_target_position", "push_run_target", ["run_id", "position"], unique=True
    )
    op.create_index(
        "uq_push_run_target_address", "push_run_target", ["run_id", "address"], unique=True
    )
    op.create_index("ix_push_run_target_state", "push_run_target", ["org_id", "run_id", "state"])
    op.create_index("ix_push_run_target_job", "push_run_target", ["job_id"])

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )

    op.execute(_FIXED_FUNCTION)
    op.execute(
        "CREATE TRIGGER push_run_target_fixed BEFORE INSERT OR UPDATE ON push_run_target"
        " FOR EACH ROW EXECUTE FUNCTION push_run_target_fixed()"
    )
    op.execute(_RUN_FIXED_FUNCTION)
    op.execute(
        "CREATE TRIGGER push_run_fixed BEFORE UPDATE ON push_run"
        " FOR EACH ROW EXECUTE FUNCTION push_run_fixed()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS push_run_fixed ON push_run")
    op.execute("DROP FUNCTION IF EXISTS push_run_fixed()")
    op.execute("DROP TRIGGER IF EXISTS push_run_target_fixed ON push_run_target")
    op.execute("DROP FUNCTION IF EXISTS push_run_target_fixed()")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("push_run_target")
    op.drop_table("push_run")

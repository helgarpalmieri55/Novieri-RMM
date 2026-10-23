"""approved actions: an upper bound on the rate fuses, and a floor for legacy rows

docs/modules/endpoint-health-alerting/spec.md §2.9, §8.3, §8.4.

THE GAP THIS CLOSES. Migration 0041 shipped `ck_approved_action_fuses_positive`
(`max_per_device_per_day > 0 AND max_per_org_per_hour > 0`) and
`ck_approved_action_cooldown_nonneg` (`cooldown_s >= 0`). Every one of those is
a FLOOR. §8.4's claim that "fleet-wide is structurally impossible" was then
resting on `_dispatch_remediation` taking a single device `UUID` -- which makes
ONE JOB device-scoped and says nothing whatever about HOW MANY. With
`max_per_org_per_hour = 1_000_000` accepted (it is, today, both by the CHECK and
by the service's `<= 0` test), and `execute_pending_actions` dispatching up to
500 rows every 30 seconds, a single org can be authorized for 60,000
machine-decided jobs an hour with the structural claim still technically true.
An operator integer with no upper bound is not a fuse; it is a text field next
to a fuse.

There is a second, duller cost. `actions.py::_fuse_breach` evaluates the per-org
fuse with `.limit(max_per_org_per_hour + 5)`, so an unbounded value is also an
unbounded row scan on `jobs` per remediation.

THE NUMBERS ARE ANCHORED, NOT CHOSEN.

  * `max_per_org_per_hour <= 500` is `actions.execute_pending_actions`' own
    `limit=500`: an org may not be authorized for more machine dispatches of one
    job type in an HOUR than the sweep can emit in a single CYCLE.
  * `max_per_device_per_day <= 100`: at the shipped 3600s cooldown a device can
    be reached at most 24 times a day, so this is four times the largest value
    the defaults can produce, and it bounds one endpoint at roughly four
    machine-decided jobs an hour.
  * `cooldown_s <= 2592000` (30 days): longer is indistinguishable from
    switching the action off and says so less clearly, and the value goes
    straight into a `timedelta` in `_fuse_breach`, which raises `OverflowError`
    on a large enough integer.

WHAT THE CEILING DOES NOT BUY, said here rather than left to be discovered: it
does not make reaching every device in a SMALL org impossible. It makes the
number bounded, bounded by something in this codebase rather than by taste, and
low enough that the per-org fuse query stays a small scan.

EXISTING ROWS ARE CLAMPED, NOT REFUSED. `ALTER TABLE ... ADD CONSTRAINT` on a
table holding a violating row fails and takes the whole migration with it, and
the violating row is precisely the dangerous one -- so the UPDATE runs first and
lowers it to the ceiling. Clamping DOWN is the safe direction: it can only make
a fuse blow earlier, never later. Deliberately not a delete and not a
deactivation: the row is an operator's configuration, and the fuse being tighter
than they asked is visible in the API response, where a row that vanished is not.

THE TWO CONSTRAINTS ARE RENAMED, because their old names say the wrong thing.
`ck_approved_action_fuses_positive` describes a floor-only check and would be
read as one by the next person who has to decide whether the ceiling is enforced.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_PER_ORG_PER_HOUR = 500
MAX_PER_DEVICE_PER_DAY = 100
MAX_COOLDOWN_S = 30 * 24 * 3600


def upgrade() -> None:
    # Clamp first: a row above the ceiling would abort the ADD CONSTRAINT, and
    # it is exactly the row this migration exists for.
    op.execute(
        f"UPDATE approved_action SET "
        f"max_per_org_per_hour = LEAST(max_per_org_per_hour, {MAX_PER_ORG_PER_HOUR}), "
        f"max_per_device_per_day = LEAST(max_per_device_per_day, {MAX_PER_DEVICE_PER_DAY}), "
        f"cooldown_s = LEAST(cooldown_s, {MAX_COOLDOWN_S}) "
        f"WHERE max_per_org_per_hour > {MAX_PER_ORG_PER_HOUR} "
        f"OR max_per_device_per_day > {MAX_PER_DEVICE_PER_DAY} "
        f"OR cooldown_s > {MAX_COOLDOWN_S}"
    )

    op.drop_constraint(
        "ck_approved_action_fuses_positive", "approved_action", type_="check"
    )
    op.drop_constraint(
        "ck_approved_action_cooldown_nonneg", "approved_action", type_="check"
    )
    op.create_check_constraint(
        "ck_approved_action_fuses_bounded",
        "approved_action",
        f"max_per_device_per_day > 0 AND max_per_device_per_day <= {MAX_PER_DEVICE_PER_DAY} "
        f"AND max_per_org_per_hour > 0 AND max_per_org_per_hour <= {MAX_PER_ORG_PER_HOUR}",
    )
    op.create_check_constraint(
        "ck_approved_action_cooldown_bounded",
        "approved_action",
        f"cooldown_s >= 0 AND cooldown_s <= {MAX_COOLDOWN_S}",
    )


def downgrade() -> None:
    # The clamped values are NOT restored: the original numbers are not retained
    # anywhere, and inventing them would be worse than leaving a tighter fuse.
    op.drop_constraint(
        "ck_approved_action_cooldown_bounded", "approved_action", type_="check"
    )
    op.drop_constraint(
        "ck_approved_action_fuses_bounded", "approved_action", type_="check"
    )
    op.create_check_constraint(
        "ck_approved_action_cooldown_nonneg", "approved_action", "cooldown_s >= 0"
    )
    op.create_check_constraint(
        "ck_approved_action_fuses_positive",
        "approved_action",
        "max_per_device_per_day > 0 AND max_per_org_per_hour > 0",
    )

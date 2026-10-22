"""health: the per-check action declaration §0.10 required and §2.2 never stored

docs/modules/endpoint-health-alerting/spec.md §0.10, §2.2, §6, §7.1, §7.3.

THE GAP THIS CLOSES. §0.10 says "a check may declare up to one of each action
kind: `remediate`, `open_ticket`, `notify`", §7.3 turns that declaration into
the difference between an alert that mails a team and one that is console-only,
and §6 makes the escalation sweep skip an alert whose check declares no
`notify`. §2.2's column list ended at `retired_at`, §2 enumerated nine module
tables and none of them related a check to an action, and migration 0042
implemented §2.2 faithfully. The declaration had nowhere to live, so
`app/modules/health/declared_actions.py` derived it: every check notified, no
check remediated. This migration gives it a home.

THREE COLUMNS ON `health_check`, NOT A TENTH TABLE. Both shapes were considered
against how the data is actually read; the reasons for columns are recorded
here because the shape is the decision:

  1. **"Up to one of each kind" becomes unrepresentable rather than rejected.**
     One slot per kind means a second `notify` on one check cannot be written,
     by any caller, through any path -- no `UNIQUE (check_id, kind)` to get
     right, no partial-index NULL trap (§0.2's, which has now bitten this
     schema twice), and nothing for a service layer to enforce.
  2. **Every read is on a check row the caller already holds.** The two call
     sites are `for kind in declared_action_kinds(check, settings)` and
     `if "notify" in declared_action_kinds(check, settings)`, and both are
     handed a loaded `HealthCheck`. A child table would add a query per alert
     open and one per escalation candidate -- §12's escalation sweep already
     loads the check row one alert at a time, so it would be a second N+1 on
     the same loop.
  3. **§0.12's table census stays true.** It says "the module owns nine tables
     (§2) ... and nothing in §2 may be added to it without amending this
     sentence first". A tenth table means arguing that a new table does not
     retain; three columns on a table already classified as configuration
     means the argument does not arise.

`remediate` IS DECLARED BY NAMING AN APPROVED ACTION, NOT BY A BOOLEAN. This is
the load-bearing asymmetry and it is deliberate. `health_alert_action.
approved_action_id` is what §7.1's dispatcher resolves, and
`actions.py::_execute_remediate` marks a row `skipped` when it is NULL -- so a
boolean `declares_remediate` would mint rows whose only possible outcome is
`skipped`, and would leave "which approved action answers this check?"
unanswered in the schema, exactly as it was before. Declaring remediation and
naming the action it dispatches are therefore ONE act: the column is NULL by
default, so remediation is off for every existing row and every new row unless
an operator names an action. Fail-closed by construction, for the one kind that
puts a signed job on an endpoint (invariant #1).

The FK is `ON DELETE RESTRICT`, matching `health_alert.check_id`: an approved
action a check still names cannot be deleted out from under it silently.
`approved_actions.delete_approved_action` refuses first and names the blocking
checks, so the `IntegrityError` is a backstop rather than the message.

WHAT DOES NOT CHANGE. `declares_notify` and `declares_open_ticket` default to
`true`, which reproduces the interim behaviour exactly for every row that
already exists: every check notified, and `open_ticket` remains conditioned in
the service on the org having configured both a queue and a category (without
both, `tickets.create_ticket` cannot run at all and the row could only ever
reach `dead`). The only behaviour this migration makes newly reachable is
§7.3's silent-by-design check -- and remediation, only where a human names an
action.

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a server default, so the 0042-era rows keep the behaviour
    # they had. A nullable column would make "not declared" and "never asked"
    # the same value, and §7.3's silence must be something an operator chose.
    op.add_column(
        "health_check",
        sa.Column(
            "declares_notify", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.add_column(
        "health_check",
        sa.Column(
            "declares_open_ticket", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    # NULL = no remediation declared, which is the default and the only state
    # reachable without an operator naming an action. There is no boolean
    # beside it: the declaration IS the action id (see the module docstring).
    op.add_column(
        "health_check",
        sa.Column(
            "remediate_approved_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approved_action.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("health_check", "remediate_approved_action_id")
    op.drop_column("health_check", "declares_open_ticket")
    op.drop_column("health_check", "declares_notify")

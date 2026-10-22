"""ai_settings: drop the per-org kill switch and the per-org provider selection

THE DECISION, VERBATIM, 2026-08-25 (the operator, who owns this product):
"dont allow anyone to choose an AI model, we will use the same for every tenant
and api key will be set in the .env per tenant, so this functionality must be
shipped enabled and working already."

WHAT THAT MEANS HERE. This platform deploys on-prem, one deployment per tenant,
so "the API key in the `.env` per tenant" and "the same model for every tenant"
are the same statement: the provider, its model and its key are SERVER
configuration, chosen once by whoever runs the control plane. Two columns on
`ai_settings` contradicted that and are dropped:

  * `enabled` -- a per-org boolean defaulting FALSE, which is why a fresh
    deployment answered 503 until an org-admin ticked a box. The assistant now
    ships on: a control plane with `ITS_AI_*` configured has it for every
    organization, and one without has it for none.
  * `provider_key` -- the value the console's provider dropdown wrote. There is
    nothing left to select: `ITS_AI_PROVIDERS` names exactly one provider (more
    than one is now a configuration error raised at startup), so a per-org copy
    of that name could only ever agree with it or be stale.

The remaining columns are untouched, and that is the line this migration draws:
retention, the two rate ceilings, the token budget, the action expiry and the
proposal cap are COST AND RETENTION dials and stay org-configurable under
`ai.admin`. REACH -- whether the assistant answers at all, and which host it
talks to -- is not an org setting any more.

WHAT REPLACES THE ACKNOWLEDGEMENT. `PUT /ai/settings` used to require an audited
`egress_acknowledged: true` on any write that left `enabled` on. That gate goes
with the column. The disclosure it guarded does not: the console still renders
what leaves this network and what never does, and the consent is now the
deliberate, server-side, reviewable act of writing `ITS_AI_*` into `.env`.
`ai.settings_changed` no longer records a boolean that could not be false; it
records `provider`, the key this deployment was configured with when the change
was made. See `app/core/ai/settings.py` and spec §0.14.

DATA LOSS, STATED PLAINLY. Both columns are dropped, so the values are gone. For
`enabled` that is the point of the change. For `provider_key` the value is
superseded by `ITS_AI_PROVIDERS`, which the operator must set for the assistant
to work at all -- an upgrade that leaves it unset lands on "no model provider is
configured on this server", which is the same 503 an unconfigured deployment
always answered.

THE DOWNGRADE RESTORES THE SCHEMA, NOT THE DATA. `enabled` comes back NOT NULL
DEFAULT false and `provider_key` comes back NULL, which is exactly the state
`0045` created and exactly what the pre-0074 code expects: every org disabled,
no provider selected, an administrator required to turn it back on. Restoring
`enabled = true` instead would be worse -- it would hand a rolled-back
deployment an egress-capable feature switched on for every org at once, which is
the outcome the old default existed to prevent. CI runs
`upgrade head -> downgrade base -> upgrade head`, so this round-trips.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ai_settings"


def upgrade() -> None:
    op.drop_column(_TABLE, "provider_key")
    op.drop_column(_TABLE, "enabled")


def downgrade() -> None:
    # `0045`'s definitions, restored exactly -- see the module docstring for why
    # `enabled` comes back FALSE rather than TRUE.
    op.add_column(
        _TABLE,
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(_TABLE, sa.Column("provider_key", sa.String(40), nullable=True))

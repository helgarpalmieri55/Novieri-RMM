"""deploy: the catalog records what a package MANAGER calls a package

`docs/modules/app-deploy/spec.md` §5.2.

ONE NULLABLE COLUMN ON ONE TABLE, and it closes a gap the agent found rather
than invented. `winget` and `choco` install by IDENTIFIER — `winget install
--id Mozilla.Firefox --exact` — and `AppDeployParams` carried no field for
one, so `agent/internal/capabilities/deploy/install.go::packageIdentifier`
read `detection_rule["name"]` and REFUSED when that was unusable. It refused
deliberately: it would not parse an identifier out of `install_args` and it
would not invent one, because the string it picks becomes an argument to a
process running as SYSTEM.

THREE THINGS ABOUT THIS COLUMN ARE DELIBERATE.

1. IT IS NOT `name`. `packages.name` is the display name an operator reads in
   a list ("Google Chrome"); the winget id is `Google.Chrome` and the choco id
   is `googlechrome`. They coincide often enough to look interchangeable and
   differ often enough that treating them as one would install the wrong
   package under a plausible-looking argument. `detection_rule["name"]` is a
   third thing again — it is matched against INSTALLED-SOFTWARE display names
   by `app/modules/deploy/detect.py`, so it answers "is it here?", not "what do
   I ask the manager for?".

2. IT IS NULLABLE, AND NULL IS THE TRUE ANSWER FOR EVERY EXISTING ROW. Nothing
   before this migration recorded the fact and no backfill could derive it:
   deriving one from `name` would write a guess into a column whose whole
   purpose is to stop the agent guessing. `artifacts.manager_package_identifier`
   falls back to `detection_rule["name"]` when it is null, which is EXACTLY
   what the fleet already does, so an unrevisited catalog behaves identically
   before and after.

3. IT IS SET FOR MANAGER-SOURCED TYPES AND IGNORED FOR THE REST. An
   artifact-backed package installs a file; the agent names it by installer
   type and action and needs no identifier, and `build_deploy_params` sends
   none. The column is not CHECK-constrained against `installer_type` because
   an operator may legitimately retype a package (msi -> winget) without
   losing the identifier they already recorded.

NO RLS CHANGES: `packages` already has `ENABLE + FORCE` with
`tenant_isolation` from migration 0013, and adding a column does not touch a
policy. No index: this column is only ever projected alongside the row it
belongs to, never filtered on.

Revision ID: 0052
Revises: 0051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("package_identifier", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("packages", "package_identifier")

"""Shared wire-protocol contracts used by more than one business module.

Lives in `app/core/` (alongside `schedule.py`, the shared cron matcher) so
modules can import without a module-to-module dependency. `app/core/` must
never import from `app/modules/*` — the dependency direction is one-way.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallOutcome(_Wire):
    """Shared go-forward install-result contract (docs/modules/app-deploy/
    spec.md AD-03). `app.deploy`/`script.run` (app/modules/deploy/schema.py)
    already emit this; `patch.apply` will migrate its module-specific
    `PatchApplyOutcome` (app/modules/patch/schema.py) onto this contract in a
    later, deferred cleanup.
    """

    identifier: str
    state: Literal["installed", "failed", "not_applicable", "succeeded"]
    exit_code: int | None = None
    log: str = ""
    log_ref: str | None = None
    detected_after: bool | None = None

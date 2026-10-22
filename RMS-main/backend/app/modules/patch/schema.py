"""Wire contracts for `patch.scan` / `patch.apply` (docs/modules/patch/spec.md).

These are the documents the agent sends back as job results
(`PatchScanDocument`, `PatchApplyResult`) and the params the server sends when
dispatching the corresponding signed jobs (`PatchScanParams`,
`PatchApplyParams`). All are strict wire contracts: `extra="forbid"` so a
malformed or unexpected field is rejected rather than silently dropped.
Exported as JSON Schema to docs/kernel/wire-protocol/ for `agent/`
repo to build its Go structs against.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatchEntry(_Wire):
    source: str
    identifier: str
    title: str
    severity: Literal["critical", "important", "moderate", "low", "unknown"]
    kb: str | None = None
    cve: list[str] | None = None
    superseded_by: str | None = None


class AppSourceStatus(_Wire):
    """Whether one app package manager (winget, Homebrew) was scanned."""

    source: str
    state: Literal["scanned", "unsupported", "failed"]
    detail: str | None = None


# Patch sources that are third-party application managers, shown as "Apps".
APP_SOURCES = frozenset({"winget", "homebrew"})


class PatchScanDocument(_Wire):
    os_family: Literal["windows", "linux", "macos"]
    patches: list[PatchEntry]
    installed_identifiers: list[str] = []
    scanned_at: str
    # result_version 2. None from an older agent, and from macOS, which cannot
    # tell outside an install.
    reboot_required: bool | None = None
    booted_at: str | None = None
    # None where this OS has no app package manager, and from an older agent.
    app_sources: list[AppSourceStatus] | None = None


class PatchApplyOutcome(_Wire):
    source: str
    identifier: str
    state: Literal["installed", "failed"]
    error: str | None = None


class PatchApplyResult(_Wire):
    results: list[PatchApplyOutcome]
    reboot_required: bool = False
    applied_at: str
    # result_version 2: when the OS will restart, if this job scheduled it.
    reboot_scheduled_for: str | None = None
    user_present: bool | None = None


# The agent adds the result_version 2 members only when the params announce
# this version, because an older control plane rejects unknown members.
RESULT_VERSION = 2


class PatchScanParams(_Wire):
    result_version: int = RESULT_VERSION


class PatchApplyParams(_Wire):
    patches: list[dict[str, str]]
    reboot: Literal["never", "if_required", "forced"]
    deferral_hours: int = 24
    # End of the maintenance window: an unattended restart must land before it.
    reboot_not_after: str | None = None
    result_version: int = RESULT_VERSION


class PatchRebootParams(_Wire):
    """`patch.reboot`: restart inside the maintenance window. A signed-in user
    on a workstation gets `deferral_hours` of notice; anything else restarts
    after a short grace, and only if that lands before `not_after`."""

    only_if_required: bool = True
    deferral_hours: int = 24
    not_after: str | None = None


class PatchRebootResult(_Wire):
    state: Literal["scheduled", "not_needed", "window_too_short"]
    scheduled_for: str | None = None
    user_present: bool = False
    workstation: bool = False

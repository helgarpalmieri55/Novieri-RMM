"""Wire contracts for `app.deploy` / `script.run` (docs/modules/app-deploy/
spec.md §5).

These are the params the server sends when dispatching the corresponding
signed jobs (`AppDeployParams`, `ScriptRunParams`) and the documents the agent
sends back as job results (`AppDeployResult`, `ScriptRunResult`). Both result
types are built on the shared `InstallOutcome` contract (AD-03,
`app.core.wire`) so `app.deploy` and `script.run` outcomes — and eventually
`patch.apply` outcomes — are expressible in one common shape. All are strict
wire contracts: `extra="forbid"` so a malformed or unexpected field is
rejected rather than silently dropped. Exported as JSON Schema to
docs/kernel/wire-protocol/ for `agent/` to build its Go structs
against.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.wire import InstallOutcome


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppDeployParams(_Wire):
    """Server → agent for `app.deploy`.

    `artifact_ref` IS A PRESIGNED `https://` URL, not the `s3://` reference on
    the `packages` row. The agent holds no object-store credential and must
    not (invariant #3), so the control plane resolves the reference at
    dispatch time into a URL good for one GET of one object for the life of
    the job. It rides inside the signature exactly like the hash does, and it
    is a BEARER CREDENTIAL for its lifetime: never logged, never audited,
    redacted out of every API read path
    (`app.modules.deploy.artifacts.redact_deploy_params`).

    `package_identifier` is the identifier a package MANAGER knows the package
    by — `Mozilla.Firefox`, `googlechrome` — and is meaningful only for
    `winget`/`choco`. It is ADDITIVE AND OPTIONAL, which is the whole
    compatibility argument (`docs/kernel/wire-protocol/README.md`):

    * **New control plane → old agent.** An agent built before this field
      exists ignores the unknown member (Go's `encoding/json` drops it) and
      goes on deriving the identifier from `detection_rule.name`, which is
      what `manager_package_identifier` falls back to when the catalog has no
      explicit value — so the two sides compute the same string and nothing
      changes in the field.
    * **New agent → old control plane.** The member is absent, which is
      `null`, which is the state the agent already handles: it falls back to
      `detection_rule.name` and refuses if that is unusable.

    A REQUIRED field would have been the outage: an old agent does not send
    it back, does not understand it, and a control plane that insisted on it
    would break every deployed agent at once.
    """

    action: Literal["install", "update", "uninstall"]
    installer_type: Literal["msi", "exe", "winget", "choco", "pkg", "dmg", "deb", "rpm", "script"]
    artifact_ref: str | None = None
    artifact_sha256: str | None = None
    package_identifier: str | None = None
    install_args: str | None = None
    detection_rule: dict[str, Any] = {}
    uninstall_rule: dict[str, Any] = {}
    deferral_hours: int = 24


class AppDeployResult(_Wire):
    outcomes: list[InstallOutcome]


class ScriptRunParams(_Wire):
    interpreter: Literal["powershell", "bash", "python"]
    body: str
    script_id: str
    script_version: int
    timeout_s: int = 3600


class ScriptRunResult(_Wire):
    outcome: InstallOutcome

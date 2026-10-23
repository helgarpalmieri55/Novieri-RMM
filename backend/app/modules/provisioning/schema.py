"""Wire contracts for `provision.enroll` (docs/modules/provisioning/spec.md),
plus the `provision.prepare` site-server contract envelope (spec §8), and the
API-layer models for the provisioning module's HTTP routes.

`ProvisionEnrollParams` is the params the server sends when dispatching the
signed `provision.enroll` job to the agent that just booted off an image;
`ProvisionEnrollResult` is the document the agent sends back as the job
result. `ProvisionPrepareEnvelope` is not a job params/result pair — it is the
contract handed to a site server so it can stage an image, answer file, and
wake-on-LAN payload ahead of a provisioning run. All three are strict wire
contracts: `extra="forbid"` so a malformed or unexpected field is rejected
rather than silently dropped. Exported as JSON Schema to
docs/kernel/wire-protocol/ for `agent/` (and site-server
tooling) to build against.

The remaining models in this file are the plain API-layer request/response
shapes used by the provisioning module's HTTP routes (Task 10). They are
NOT wire contracts — they don't forbid extra fields — and per security
invariant #3 they never carry secret values: no credential fields, no
sealed-vault refs, no job/enrollment token secrets. `ProfileOut.post_config`
is returned with secret credential refs already redacted by the API layer
before this model is constructed; this schema only describes the shape of
the (already-redacted) dict.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvisionEnrollParams(_Wire):
    provision_run_id: str
    os_family: Literal["windows", "linux", "macos"]
    hostname: str
    post_config: dict[str, Any]
    expected_agent_version: str | None = None


class ProvisionEnrollResult(_Wire):
    state: Literal["success", "failed"]
    hostname_set: bool
    domain_joined: bool | None = None
    agent_healthy: bool
    error: str | None = None


class ProvisionPrepareEnvelope(_Wire):
    provision_run_id: str
    os_family: Literal["windows", "linux", "macos"]
    image_ref: str | None
    image_sha256: str | None
    answer_ref: str | None
    target_ref: str | None
    wake_on_lan: dict[str, Any]


class OsImageOut(BaseModel):
    id: UUID
    os_family: str
    name: str
    kind: str
    artifact_ref: str | None
    artifact_sha256: str | None
    params: dict[str, Any]
    state: str
    created_at: datetime


class OsImageCreateRequest(BaseModel):
    os_family: str
    name: str
    kind: str
    params: dict[str, Any] = {}


class ProfileOut(BaseModel):
    id: UUID
    name: str
    image_id: UUID
    post_config: dict[str, Any]
    target_scope: dict[str, Any]
    enabled: bool
    created_at: datetime
    # The sealed `domain_join.password` / `local_admin.password` markers
    # (repository._CREDENTIAL_PATHS) are stripped from `post_config` before
    # this model is built (invariant #3 — never echo a `{"__sealed__": ...}`
    # ref); these booleans are the only signal the API gives that a
    # credential is configured.
    has_domain_join_credential: bool = False
    has_local_admin_credential: bool = False


class ProfileUpsertRequest(BaseModel):
    name: str
    image_id: UUID
    post_config: dict[str, Any] = {}
    target_scope: dict[str, Any] = {}
    enabled: bool = True


class RunOut(BaseModel):
    id: UUID
    profile_id: UUID
    device_id: UUID | None
    target_ref: str | None
    state: str
    answer_ref: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class ProvisionWakeRequest(BaseModel):
    """`wake_on_lan` on a run-start, as a MODEL rather than an untyped dict.

    It was `dict[str, Any] | None`, accepted by the API and then SILENTLY
    DROPPED: `start_provision_run` had no such parameter, `api.py` never passed
    it, and `dispatch.py` hard-coded `wake_on_lan={}` into the prepare envelope.
    An operator could ask for a wake and nothing happened, with no error and no
    log line (`docs/modules/site-server/spec.md` §13.5).

    Typed and `extra="forbid"`, so a caller who misspells `target_mac` gets a
    422 instead of a silently ignored request -- which is the same class of
    defect one layer up.
    """

    model_config = ConfigDict(extra="forbid")

    target_mac: str
    #: Which declared segment the target is on. Optional only when the serving
    #: site server declares exactly one.
    segment: str | None = None


class RunStartRequest(BaseModel):
    target_ref: str | None = None
    wake_on_lan: ProvisionWakeRequest | None = None


class RunStartResponse(BaseModel):
    run_id: UUID
    state: str


class FleetProvisioningReportOut(BaseModel):
    run_counts_by_state: dict[str, int]
    success_rate: float
    machines_provisioned: int
    per_profile: dict[str, Any]


class DeviceProvisioningOut(BaseModel):
    device_id: UUID
    profile_id: UUID | None
    image_id: UUID | None
    enrolled_at: datetime | None
    latest_run_state: str | None

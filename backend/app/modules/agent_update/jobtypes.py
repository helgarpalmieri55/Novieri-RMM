"""The `agent.update` job type (spec-self-update.md §3.1, §8.1).

An ordinary signed job: canonical bytes, the vault-held key, the agent's
verifier before any capability runs. Params are built only by this module's
rollout service from an `agent_releases` row; the kernel route, approved
actions and the AI layer all refuse the type by name (§8.5).

Additive on the wire: an agent that predates self-update refuses the type
`unknown_type` before anything runs, which the reconciler reads as "needs a
manual update".
"""

from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.registry import JobType, register

__all__ = [
    "JOB_TYPE",
    "MAX_ARTIFACT_BYTES",
    "AgentUpdateParams",
    "AgentUpdateResult",
    "redact_url",
    "register_job_types",
]

JOB_TYPE = "agent.update"
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

_SEMVER = (
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?$"
)


class AgentUpdateParams(BaseModel):
    """server -> agent: replace this agent with one release artefact.

    Per device: the artefact for that device's `(os, arch)` only. The agent
    checks every field before it fetches anything (spec §4.1).
    """

    model_config = ConfigDict(extra="forbid")

    target_version: str = Field(
        max_length=51, pattern=_SEMVER, description="Strict semver; no +build suffix."
    )
    os: Literal["linux", "windows", "darwin"] = Field(description="Must equal runtime.GOOS.")
    arch: Literal["amd64", "arm64"] = Field(description="Must equal runtime.GOARCH.")
    artifact_url: str = Field(
        pattern=r"^https://", description="Presigned GET for agent_releases.storage_ref."
    )
    artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="agent_releases.sha256, computed by the server.",
    )
    artifact_size: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)
    rollback: bool = Field(default=False, description="Must be true to install a lower version.")
    health_window_s: int = Field(default=600, ge=120, le=3600)
    rollout_id: str | None = Field(default=None, description="For logs only.")
    release_signature: str | None = Field(
        default=None,
        description="Base64 Ed25519 signature by the offline release key over version, "
        "platform and digest. Required by agents built with a pinned release key.",
    )


class AgentUpdateResult(BaseModel):
    """agent -> server: the outcome, reported by whichever process finished.

    `updated`/`already_current` arrive as `succeeded`; `rolled_back`/`aborted`
    as `failed` (spec §3.1). `JobResultMsg.state` is not widened.
    """

    model_config = ConfigDict(extra="ignore")

    outcome: Literal["updated", "already_current", "rolled_back", "aborted"]
    from_version: str
    to_version: str
    sha256: str = Field(description="Digest of the binary now running.")
    signature: Literal["verified", "not_required"] | None = None
    starts: int = Field(default=0, description="Starts of the new binary during trial.")
    reason: str | None = Field(default=None, description="Stable token (spec §4.4).")


def redact_url(url: str) -> str:
    """Drop the presigned query string: a read grant with no reason to spread."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def params_redactor(params: dict[str, Any]) -> dict[str, Any]:
    safe = dict(params)
    url = safe.get("artifact_url")
    if isinstance(url, str):
        safe["artifact_url"] = redact_url(url)
    return safe


def register_job_types() -> None:
    """Register `agent.update`. Not cancellable: stopping a swap halfway is
    worse than letting it finish or roll back. No retry relinker, so the
    kernel retry route refuses it and names the rollout routes."""
    register(
        JobType(
            JOB_TYPE,
            AgentUpdateParams,
            params_redactor=params_redactor,
            cancellable=False,
        )
    )

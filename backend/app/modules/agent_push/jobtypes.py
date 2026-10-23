"""The two job types cycle B2 owns: `agent.push_probe` and `agent.push_install`
(spec §4, §4.4, §6.4; `CLAUDE.md` invariant 2a, invariant #1, invariant #3).

**THESE ARE ORDINARY SIGNED JOBS. INVARIANT #1 GETS NO EXEMPTION.** A push is
"go install the agent on 10.0.4.17 using these credentials", which is a job:
`JobEngine.create` -> canonical bytes -> the vault-held signing key ->
`signed_envelope` -> the mTLS WebSocket -> the agent's four-check runner before
any capability runs. What is signed is the whole instruction, and the
instruction CONTAINS NO CODE (spec §2.3, §4.2, clause (b)).

**A JOB TYPE, NOT A WIRE `kind` (spec §11, the wire-protocol README).** An agent
built before this cycle has no `pushinstall` capability, so its verifier refuses
the type `unknown_type` LOUDLY and on the record, before anything runs -- the
same compatibility decision `job.cancel` made, and for the same reason: an
unknown wire *kind* is dropped in silence, which is the worst property a command
whose whole difficulty is knowing what the endpoint did could have.

**EVERY PARAMS MODEL IS `extra="forbid"` AT EVERY DEPTH WITH NO FREE-FORM
MEMBER** (spec §4.2, clause (b)). There is no `install_args`, no `script`, no
`command`, no `release_url` of anyone's choosing, no `dict[str, Any]`. The
`SECRET_PARAM_KEY_MARKERS` failure mode `approved_actions.py` names -- a free
string field is where a credential gets smuggled in -- has no field to live in
here. `tests/test_agent_push_dispatch.py` fires `install_args`, `script`,
`release_url` and `command` at both models and asserts each is rejected.

**THE PROBE CARRIES NO CREDENTIAL, AND ITS MODEL HAS NO FIELD THAT COULD HOLD
ONE** (spec §4.4, falsifier 22). It learns the host-key fingerprint before any
secret is presented, so the operator can confirm it first. The install model
carries the confirmed `host_key_fingerprint` and it is NEVER null (spec §4.1):
a push to a host with no pinned, confirmed key is refused before dispatch.

**THE REDACTOR IS MANDATORY ON THE INSTALL (spec §4.3).** `GET /jobs/{id}` is
gated on `jobs.read`, far weaker than `agent.push.run`; the redactor removes
`credential`, `enroll_token` and `release.url` from every API read path. The
probe declares none -- it signs nothing worth redacting.

**THE SCRUBBER IS THE OTHER HALF, AND IT IS THIS MODULE'S ANSWER TO §6.4.**
Signing requires canonical bytes, and this platform's canonical bytes are the
stored `jobs.params`, so at dispatch the credential plaintext IS written to the
application database. The durable fix is vault indirection for job params, which
is a kernel change and out of scope. Until it exists, `terminal_params_scrubber`
overwrites `credential` and `enroll_token` the instant the job reaches ANY
terminal state, so the plaintext's life in the database is bounded by the
300-second TTL rather than the retention period. This is a RESIDUAL, not a
solution (spec §1.3 item 3, §6.4).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.registry import JobType, register

__all__ = [
    "AgentPushInstallParams",
    "AgentPushInstallResult",
    "AgentPushProbeParams",
    "AgentPushProbeResult",
    "PushCredential",
    "PushRelease",
    "PROBE_JOB_TYPE",
    "INSTALL_JOB_TYPE",
    "install_params_redactor",
    "install_terminal_scrubber",
    "register_job_types",
]

PROBE_JOB_TYPE = "agent.push_probe"
INSTALL_JOB_TYPE = "agent.push_install"

#: Spec §4.3. The exposure window for a decrypted credential sitting in a signed
#: envelope and a `jobs.params` row is worth more than `net.sweep`'s revoked
#: authorization, so the window is shorter than the kernel's 3600s and shorter
#: than `net.sweep`'s 600s. A push that has not started inside five minutes
#: should be re-authorized by a human, not resurrected by a timer.
INSTALL_TTL_S = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentPushProbeParams(_Strict):
    """`agent.push_probe` -- the credential-free first contact (spec §4.4).

    Opens the transport, completes the key exchange, records the host-key
    fingerprint and the banner, and closes. **It never authenticates, so it
    presents no credential, and this model has no field that could carry one**
    (falsifier 22). Exactly one target, SSH, or WinRM for Windows (Kerberos only) (§13).
    """

    target_address: str = Field(description="Exactly one IP literal. Never a CIDR.")
    transport: Literal["ssh", "winrm"] = Field(
        default="ssh",
        description="`ssh` for Linux/macOS; `winrm` for Windows (Kerberos only). "
        "ADDITIVE: older control planes never send `winrm` and older agents never "
        "advertise the capability, so a mixed fleet is unaffected.",
    )
    dns_name: str | None = Field(
        default=None,
        description="The target's DNS name. Required for `winrm` (Kerberos needs "
        "`HTTP/<fqdn>`, not an IP); unused for `ssh`. Optional on the wire so the "
        "field is additive.",
    )


class AgentPushProbeResult(_Strict):
    """agent -> server: what the key exchange revealed, and nothing else.

    Carries no secret, because the probe presented none. `host_key_fingerprint`
    is pinned on the asset row for the operator to confirm before a push.
    """

    target_address: str
    transport: Literal["ssh", "winrm"]
    host_key_fingerprint: str = Field(
        default="", description="SHA256:... of the SSH host key; empty for winrm."
    )
    host_key_type: str = Field(default="", description="e.g. ssh-ed25519; empty for winrm.")
    banner: str = Field(default="", description="The server's identification string (ssh).")
    winrm_https: bool = Field(default=False, description="5986 answered (winrm).")
    winrm_http: bool = Field(default=False, description="5985 answered (winrm).")
    kdc_reachable: bool = Field(
        default=False, description="A KDC for the realm answered from the site server (winrm)."
    )


class PushRelease(_Strict):
    """The agent artefact to place: identified by version and SHA-256, server-read.

    **`url` IS PRESIGNED BY THE SERVER FROM THE `agent_releases` ROW IT SELECTED**
    (spec §4.2). An operator-supplied URL is an operator-supplied payload with
    extra steps, so there is no field for one. The agent verifies `sha256`
    against the bytes it downloads BEFORE transferring a byte to the target
    (spec §11, agent falsifier 6).
    """

    version: str
    sha256: str = Field(min_length=64, max_length=64, description="Hex digest.")
    url: str = Field(description="Presigned, short-lived, server-minted. A bearer grant.")


class PushCredential(_Strict):
    """The decrypted administrative credential, presented to the target's
    authentication exchange and never persisted on it (spec §6.1).

    **NO FREE-FORM MEMBER.** `username` and `secret` and nothing else; a push
    cannot authenticate without them, and nothing else in the params is sealed
    material (invariant #3, spec §2.4). Redacted on every API read and scrubbed
    from `jobs.params` on any terminal state.
    """

    username: str
    secret: str


class AgentPushInstallParams(_Strict):
    """`agent.push_install` -- the whole instruction, and it contains no code.

    Spec §4.1. Everything the endpoint acts on is here and signed; the endpoint
    never decides what it may touch (spec §11): `target_address` is the one
    address it may reach, and it is refused if the params name a different one
    (agent falsifier 4).
    """

    target_address: str = Field(description="Exactly one IP literal.")
    expected_mac: str | None = Field(
        default=None, description="From the promoted asset, if known. Never widened."
    )
    transport: Literal["ssh", "winrm"] = Field(
        description="`ssh` (Linux/macOS) or `winrm` (Windows, Kerberos only)."
    )
    os: Literal["linux", "darwin", "windows"] = Field(
        description="Operator-declared; the agent verifies it on the target (§7.2)."
    )
    dns_name: str | None = Field(
        default=None,
        description="Required for `winrm` (Kerberos `HTTP/<fqdn>`); unused for `ssh`.",
    )
    arch: Literal["amd64", "arm64"] = Field(description="Operator-declared; verified.")
    release: PushRelease
    enroll_token: str = Field(
        description="Single-use, <=30 min. Delivered, never minting an identity (§7.1)."
    )
    credential: PushCredential
    host_key_fingerprint: str = Field(
        min_length=1,
        description="NEVER null: the confirmed key the SSH client verifies against "
        "(§4.4, §7.3). A push to a host with no pinned, confirmed key is refused "
        "before this job exists.",
    )
    enroll_url: str | None = Field(
        default=None,
        description="The console's bare https origin (ITS_PUBLIC_CONSOLE_URL), "
        "written into the installed service's ITS_AGENT_ENROLL_URL; the agent's "
        "enrollment client appends /enroll/v1. Set on every install the server "
        "dispatches (`winrm` and `ssh`). Server configuration, never "
        "operator-authored, and never derived from the presigned release URL. "
        "Optional on the wire so the field is additive.",
    )


class AgentPushInstallResult(_Strict):
    """agent -> server: what the push did, carrying no secret (invariant #3).

    `enrolled` is the endpoint's own statement that it began the ordinary
    enrolment with the delivered token; the DEVICE IDENTITY is issued only by
    the enrolment path to a keypair the target generated itself (§7.1), never
    minted here.
    """

    target_address: str
    outcome: Literal["installed", "os_mismatch", "unreachable", "auth_failed", "failed"]
    os_detected: str = Field(default="")
    arch_detected: str = Field(default="")
    detail: str = Field(default="")


def install_params_redactor(params: dict[str, Any]) -> dict[str, Any]:
    """Strip every bearer field before `jobs.params` reach a `jobs.read` holder
    (spec §4.3). Pure and total: it never mutates its input.

    `credential` and `enroll_token` are secrets; `release.url` is a presigned
    grant. All three are removed rather than masked, because a masked field on a
    control surface is a claim that the value exists in a form worth showing.
    """
    safe = dict(params)
    safe.pop("credential", None)
    safe.pop("enroll_token", None)
    release = safe.get("release")
    if isinstance(release, dict):
        redacted = dict(release)
        redacted.pop("url", None)
        safe["release"] = redacted
    return safe


def install_terminal_scrubber(params: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the credential material in `jobs.params` on any terminal state
    (spec §6.4). Returns a NEW dict; the engine reassigns it, never mutating the
    JSONB under SQLAlchemy.

    Falsifier (spec §6.4): `SELECT params FROM jobs WHERE type = 'agent.push_install'
    AND status IN (terminal states)` returns any credential material.
    """
    scrubbed = dict(params)
    scrubbed.pop("credential", None)
    scrubbed.pop("enroll_token", None)
    return scrubbed


def register_job_types() -> None:
    """Register both push job types into the kernel registry.

    **REGISTERING THESE IS WHAT EMPTIES `RESERVED_JOB_TYPES`** (spec §14). Until
    this cycle both names were RESERVED -- decided-but-not-registered -- and the
    partition tests asserted `decided - registered == RESERVED_JOB_TYPES`. The
    day these register, those tests go red until the reserved set is emptied in
    the same commit, which is the intended handover.

    Both kernel gates already refuse these two by name (`direct_dispatch.py`,
    `approved_actions.py`), so registration does not open the generic-dispatch
    or the approvable-action door: the module's own route at
    `POST /api/v1/agent-push/requests` is the only path that dispatches them.
    """
    register(
        JobType(
            PROBE_JOB_TYPE,
            AgentPushProbeParams,
            result_handler=_handle_probe_result,
            # A probe in flight must be interruptible; its capability honours ctx.
            cancellable=True,
        )
    )
    register(
        JobType(
            INSTALL_JOB_TYPE,
            AgentPushInstallParams,
            result_handler=_handle_install_result,
            # Mandatory: `GET /jobs/{id}` is gated far weaker than dispatch.
            params_redactor=install_params_redactor,
            # This module's §6.4 answer: no credential survives the terminal state.
            terminal_params_scrubber=install_terminal_scrubber,
            # NO params_refresher: pushes are not retried automatically (§5.3),
            # so there is no time-bounded material to re-mint.
            cancellable=True,
        )
    )


async def _handle_probe_result(session: Any, job: Any, result: dict[str, Any]) -> None:
    """Trampoline into the dispatch service's probe-result handler.

    Imported at CALL time, not module-import time: the dispatch service imports
    `JobEngine` and the vault, and this module is imported by the kernel
    registry through the worker. A top-level import would make the registry
    depend on the whole dispatch graph.
    """
    from app.modules.agent_push.dispatch import record_probe_result

    await record_probe_result(session, job, result)


async def _handle_install_result(session: Any, job: Any, result: dict[str, Any]) -> None:
    """Trampoline into the dispatch service's install-result handler."""
    from app.modules.agent_push.dispatch import record_install_result

    await record_install_result(session, job, result)

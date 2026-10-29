"""Request and response models for the push-install control surface.

**EVERY MODEL IS `extra="forbid"`, AT EVERY DEPTH, WITH NO FREE-FORM MEMBER.**
Spec §4.2 names the failure mode this closes, quoting the kernel's own comment:
a free string field is where a credential gets smuggled in
("`app.deploy`'s `install_args` could carry `/PASSWORD=...`"). There is no
`install_args`, no `post_config`, no `options`, no `dict[str, Any]` anywhere
below, and adding one would be a decision rather than a convenience.

**NO RESPONSE MODEL CARRIES A SECRET, A `vault_ref`, A CIPHERTEXT OR AN
ENROLLMENT TOKEN** (invariant #3). The credential appears as its id and its
name, and `CredentialScopeOut` names the credential the same way. That is not
a redaction applied on the way out -- there is no field here that could hold
one.

**`address_count` IS COMPUTED SERVER-SIDE AND RETURNED**, rather than left to
the console to derive. `ScanAuthorizationsPage.tsx` states a scope's reach in
addresses because nobody converts mask lengths in their head; the same rule
applies one privilege level up, and computing it in one place means the
console and the audit log cannot disagree about how big a grant is.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AuthorizationCreate",
    "AuthorizationOut",
    "CredentialScopeBind",
    "CredentialScopeOut",
    "PushRequestCreate",
    "PushDispatchOut",
    "PushDispatchTargetOut",
    "HostKeyConfirm",
    "HostKeyOut",
    "TargetDecisionOut",
    "RunCountsOut",
    "RunCreate",
    "RunListRowOut",
    "RunOut",
    "RunPrefixOut",
    "RunPreviewOut",
    "RunTargetOut",
    "RunTargetPageOut",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizationCreate(_Strict):
    """Authorize credentialed pushes inside one prefix of one client's
    network."""

    client_id: UUID = Field(
        description="The client whose network the prefix is. A push is authorized "
        "there for that client's machines only."
    )
    prefix: str = Field(
        min_length=1,
        max_length=64,
        description="CIDR prefix, or a bare address meaning a single host.",
    )
    note: str = Field(
        default="",
        max_length=500,
        description="Why this prefix may receive credentialed pushes.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "When the grant lapses. Timezone-aware. Null means it stands until "
            "somebody revokes it."
        ),
    )


class AuthorizationOut(_Strict):
    """One `push_authorization` row, with its reach stated in addresses."""

    id: UUID = Field(description="The authorization's id.")
    client_id: UUID = Field(description="The client whose network the prefix is.")
    prefix: str = Field(description="The authorized prefix, canonicalised.")
    address_count: int = Field(
        description="How many addresses this grant reaches. Not a mask length."
    )
    note: str = Field(description="Why this prefix was authorized.")
    authorized_by: UUID = Field(
        description="The user who authorized it. Never null: there is no "
        "system-authorized push scope."
    )
    authorized_at: datetime = Field(description="When it was authorized.")
    expires_at: datetime | None = Field(description="When it lapses, if ever.")
    revoked_at: datetime | None = Field(
        description="When it was revoked. The row survives revocation."
    )
    revoked_by: UUID | None = Field(description="Who revoked it.")


class CredentialScopeBind(_Strict):
    """Bind one stored credential to one prefix."""

    credential_id: UUID = Field(
        description="A credential of type `push_install`. Any other type is refused."
    )
    prefix: str = Field(
        min_length=1,
        max_length=64,
        description="Where this credential may be presented.",
    )
    note: str = Field(default="", max_length=500, description="Why.")


class CredentialScopeOut(_Strict):
    """One `push_credential_scope` row.

    Carries the credential's NAME and never anything that could reconstruct
    its secret.
    """

    id: UUID = Field(description="The scope's id.")
    client_id: UUID = Field(description="The credential's client, which the scope is in.")
    credential_id: UUID = Field(description="The credential this scope binds.")
    credential_name: str = Field(description="The credential's label.")
    prefix: str = Field(description="Where the credential may be presented.")
    address_count: int = Field(description="How many addresses that prefix reaches.")
    note: str = Field(description="Why this binding exists.")
    bound_by: UUID = Field(description="Who bound it. Never null.")
    bound_at: datetime = Field(description="When it was bound.")
    unbound_at: datetime | None = Field(
        description="When it was unbound. The row survives unbinding."
    )
    unbound_by: UUID | None = Field(description="Who unbound it.")


class PushRequestCreate(_Strict):
    """Ask for a push install against explicitly named addresses.

    **THERE IS NO `group_id`, NO `subnet_id`, NO `filter` AND NO `saved_search`
    FIELD**, and there never may be: invariant 2a clause (f) permits only
    addresses a human selected from a list they were shown. A filter is
    evaluated at dispatch time against a set the operator did not see.

    **THERE IS NO COMMAND, SCRIPT, INTERPRETER, ARGUMENT OR ARTEFACT URL
    FIELD** -- clause (b). The only thing that would ever execute on a target
    is this platform's own published agent artefact, chosen server-side.
    """

    mode: Literal["probe", "install"] = Field(
        description=(
            "`probe` fetches the host-key fingerprint carrying NO credential "
            "(§4.4); `install` presents the credential and places the agent. "
            "An install is refused for any target with no confirmed host key."
        )
    )
    credential_id: UUID = Field(
        description="The `push_install` credential. On a probe it is checked for "
        "scope but never presented; only its id travels, never a secret."
    )
    targets: list[str] = Field(
        description=(
            "Between 1 and 25 host IP literals. Never a CIDR, a hostname, a "
            "range, a group or a saved search."
        )
    )


class PushDispatchTargetOut(_Strict):
    """One dispatched (address, job) pair. Carries no secret."""

    address: str = Field(description="The named target address.")
    job_id: UUID = Field(description="The signed job created for it.")
    site_server_device_id: UUID | None = Field(
        description="The site server that will perform it -- the job's target device."
    )


class PushDispatchOut(_Strict):
    """The result of an authorized push request: the batch and its jobs.

    NEVER a secret, a token, or a presigned URL: those live only in the signed
    params, are redacted on every `GET /jobs/{id}`, and are scrubbed from the
    row on any terminal state.
    """

    batch_id: UUID = Field(description="Groups these jobs for display (§5.3).")
    kind: Literal["probe", "install"] = Field(description="What was dispatched.")
    targets: list[PushDispatchTargetOut] = Field(
        description="One entry per address, each an independent signed job."
    )


class HostKeyConfirm(_Strict):
    """Confirm the fingerprint a probe observed for one address (§4.4, §7.3)."""

    address: str = Field(description="The target address, one IP literal.")
    fingerprint: str = Field(
        min_length=1,
        description="The observed fingerprint, confirmed out-of-band. Must equal "
        "what the last probe saw.",
    )


class HostKeyOut(_Strict):
    """A target's host key: what a probe observed and whether a human confirmed
    it. Public key material only -- never a secret."""

    address: str = Field(description="The target address.")
    client_id: UUID | None = Field(
        default=None,
        description="The client whose machine holds the address; null for a key "
        "recorded before its machine was known.",
    )
    transport: str = Field(description="ssh or winrm.")
    observed_fingerprint: str = Field(description="What the last probe saw.")
    observed_key_type: str = Field(description="e.g. ssh-ed25519.")
    observed_at: datetime = Field(description="When it was last observed.")
    confirmed_fingerprint: str | None = Field(
        description="What a human confirmed, if any. An install verifies against this."
    )
    confirmed_at: datetime | None = Field(description="When it was confirmed.")
    confirmed_by: UUID | None = Field(description="Who confirmed it.")
    installable: bool = Field(
        description="True only when a confirmed key still equals the latest "
        "observation. A changed key makes this false and refuses an install."
    )
    winrm_https: bool = False
    winrm_http: bool = False
    kdc_reachable: bool = False


class TargetDecisionOut(_Strict):
    """What the refusal ladder found for one address."""

    address: str = Field(description="The named target address.")
    authorized: bool = Field(
        description="True when every bound in invariant 2a holds for this address. "
        "It does not mean anything was dispatched -- nothing is."
    )
    reasons: list[str] = Field(
        description="Every failing bound, named. Empty when none failed."
    )


# --------------------------------------------------------------------------- #
# Push runs (design 2026-09-30 §1.3, §1.7). Still no secret, token, vault
# reference or presigned URL in any field.
# --------------------------------------------------------------------------- #


class RunPrefixOut(_Strict):
    """A subnet a run may be built from: an ACTIVE push authorization."""

    prefix: str = Field(description="The authorized prefix, exactly as authorized.")
    client_id: UUID = Field(description="The client whose network it is.")
    address_count: int = Field(description="How many addresses it reaches.")
    expires_at: datetime | None = Field(description="When the authorization lapses, if ever.")


class RunListRowOut(_Strict):
    """One listed machine. `ready` false means shown and never installed."""

    device_id: UUID = Field(description="The vouched-for device record.")
    address: str = Field(description="Its address.")
    hostname: str = Field(description="Its name.")
    os_family: str = Field(description="Its declared operating system.")
    ready: bool = Field(description="True when every bound holds.")
    reasons: list[str] = Field(description="Every failing bound, named. Empty when ready.")


class RunPreviewOut(_Strict):
    """The list a person is asked to confirm, and the digest that binds it."""

    prefix: str = Field(description="The subnet that built the list.")
    client_id: UUID = Field(description="The run's client: the credential's.")
    credential_id: UUID = Field(description="The credential the run would present.")
    credential_name: str = Field(description="Its label.")
    list_digest: str = Field(
        description="SHA-256 binding the client, the credential and the ready "
        "(device_id, address) pairs."
    )
    ready_count: int = Field(description="Machines that would be installed.")
    blocked_count: int = Field(description="Machines listed and not ready.")
    unvouched_count: int = Field(
        description="Rows in the subnet nobody vouched for (e.g. found by a sweep): not listed."
    )
    awaiting_review_count: int = Field(
        description="Vouched-for rows in the subnet still waiting for review: not listed."
    )
    ambiguous_count: int = Field(
        description="Addresses that name no single machine: not listed. Always 0 inside "
        "one client."
    )
    run_max: int = Field(description="The most ready machines one run may hold.")
    concurrency: int = Field(description="The most installs in flight at once.")
    rows: list[RunListRowOut] = Field(description="Every listed machine, in address order.")
    reason_help: dict[str, str] = Field(description="Each reason's remedy, in plain words.")


class RunCreate(_Strict):
    """Confirm a list. There is no target field: the list is re-expanded
    server-side and must hash to `list_digest`, or nothing is started."""

    prefix: str = Field(min_length=1, max_length=64, description="The authorized subnet.")
    credential_id: UUID = Field(description="The push credential to present.")
    list_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="The `list_digest` of the preview confirmed."
    )


class RunCountsOut(_Strict):
    waiting: int
    in_flight: int
    installed: int
    failed: int
    refused: int
    cancelled: int


class RunOut(_Strict):
    """One push run."""

    id: UUID
    client_id: UUID = Field(description="The run's client: its credential's.")
    prefix: str
    credential_id: UUID | None = Field(description="Null once the credential is gone.")
    credential_name: str
    created_by: UUID = Field(description="The person who confirmed the list.")
    created_at: datetime
    state: str = Field(
        description="running, paused, stopped_credential, cancelled or completed."
    )
    target_count: int = Field(description="Machines confirmed. Fixed at creation.")
    concurrency: int = Field(description="The most installs in flight at once.")
    list_digest: str
    finished_at: datetime | None
    counts: RunCountsOut
    stopped_at: datetime | None = Field(
        description="When the lockout guard stopped the run (spec §1.4a); null otherwise."
    )
    auth_failures: int = Field(
        description="Sign-in failures since the run last started or resumed."
    )
    auth_failure_limit: int = Field(description="How many stop the run.")
    signed_in: bool = Field(
        description="A sign-in has got through since the run last started or resumed. "
        "False means one machine at a time."
    )


class RunTargetOut(_Strict):
    position: int
    device_id: UUID | None
    address: str
    state: str = Field(
        description="waiting, in_flight, installed, failed, refused or cancelled."
    )
    job_id: UUID | None
    outcome: str = Field(description="What the site server reported, once dispatched.")
    detail: str
    reasons: list[str] = Field(description="Named refusals, when refused.")
    updated_at: datetime


class RunTargetPageOut(_Strict):
    total: int
    items: list[RunTargetOut]
    reason_help: dict[str, str]

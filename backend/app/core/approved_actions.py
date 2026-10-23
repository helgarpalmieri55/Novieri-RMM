"""The shared approved-action registry, and the one derivation of "destructive".

KERNEL-OWNED, NOT HEALTH-OWNED (docs/modules/endpoint-health-alerting/spec.md
§2.9, §8.5). This lives in `app/core/` for the reason `app/core/events.py`'s
docstring gives for the outbox: Phase 17's AI-automation layer publishes to the
same facility, and `docs/06-roadmap.md` asks for "one registry, one dial, one
audit". A registry owned by the health module is a registry the AI layer will
fork -- and two forks of `is_destructive` means two answers to "may this run
without a human", which is the question the whole autonomy dial exists to
answer once.

`personas` and `risk_class` ship from day one, with `["health"]` as the only
persona in v1, precisely so Phase 17 adds values to EXISTING ROWS rather than a
second table nobody reconciles.

WHAT THIS MODULE IS NOT. It has no service layer, no API, and no audit calls --
those belong to the module that registers into it (health's Task 13). It is the
closed maps -- which job types are destructive, which may be named by an approved
action AT ALL, which params values make one destructive, which params keys name a
credential, and which may receive a substitution -- the pure functions over them,
and two org-scoped reads. `is_destructive` in particular does NOT query: it takes
the exemption row (or `None`), so it cannot develop a different answer depending
on which session it was called with.

WHERE THE DECLARATIVE CLASSES LIVE. `ApprovedAction` and
`ApprovedActionScriptExemption` are defined in `app/models/approved_actions.py`
and re-exported here, so `from app.core.approved_actions import ApprovedAction`
works as spec §2.9's callers expect. That split is forced, not preferred: a
declarative model under `app/core/` cannot be registered in
`app/models/__init__.py` without an import cycle -- see that module's docstring
for the verified failure. The concept still has exactly one home; this one.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import N_, tr, use_locale
from app.models.approved_actions import (
    AUTONOMY_LEVELS,
    MAX_COOLDOWN_S,
    MAX_PER_DEVICE_PER_DAY,
    MAX_PER_ORG_PER_HOUR,
    MAX_TARGET_SCOPES,
    RISK_CLASSES,
    ApprovedAction,
    ApprovedActionScriptExemption,
)
from app.models.core import Job

# ---------------------------------------------------------------------------
# The closed maps (spec §8.2, §8.4)
# ---------------------------------------------------------------------------

# Job types that are destructive whatever their params say: each either
# overwrites data, re-images a machine, changes installed software, puts packets
# on somebody else's wire, or disarms the platform's own view of the endpoint --
# none of them without a human decision. Checked against the LIVE job-type
# registry by `tests/test_approved_actions_destructive.py`, so renaming a job
# type breaks that test rather than silently disarming this gate.
#
# `health.policy.apply` and `net.sweep` are here as the SECOND line, not the
# first: both are refused outright by `APPROVABLE_JOB_TYPES` below, so no
# approved action can name them at all. They are classified anyway because
# `is_destructive` is the kernel's single derivation and is consulted by callers
# that are not this registry's write path -- Phase 17's AI layer, a re-derivation
# over rows written before the allowlist existed. A row that already names one
# of them must at minimum be ineligible for `autonomous`.
_DESTRUCTIVE_JOB_TYPES: frozenset[str] = frozenset(
    {
        "backup.restore",
        "provision.enroll",
        "app.deploy",
        "patch.apply",
        # Delivers the device's ENTIRE monitoring policy. `checks: []` is a legal
        # policy meaning "run nothing" (`health/jobtypes.py`), so this job type
        # is the platform's own kill switch for its view of an endpoint, and
        # `policy_version` is reconciliation state -- a value chosen by an
        # operator makes the reconcile sweep believe the device converged.
        "health.policy.apply",
        # Emits packets at addresses that are NOT the target device. Destructive
        # in the sense that matters here: what it costs is not bounded by the
        # machine the job is signed for. `site.wake` is here for the identical
        # reason -- a magic packet is aimed at a MAC on some other machine.
        "site.wake",
        "net.sweep",
    }
)

# ---------------------------------------------------------------------------
# The approvable-job-type allowlist (spec §8.2, §8.4)
# ---------------------------------------------------------------------------

# WHICH JOB TYPES AN APPROVED ACTION MAY NAME AT ALL. Deny by default: a job
# type absent from this set cannot be stored as an approved action, so a module
# registering a new one gets NO remediation path until somebody adds it here in
# a reviewed change. That direction is the whole point -- the alternative, a
# denylist, is a list of the ones we happened to think of, and every job type
# registered after it was written silently becomes dispatchable by a machine.
#
# THE TWO CONDITIONS, BOTH REQUIRED. A job type is approvable iff:
#
#   1. **It is bounded by the device the job targets.** `max_target_scope` is
#      `'device'` and that is the ONLY scope dial this registry has; it bounds
#      which device the envelope is signed for, not what the payload reaches.
#      A job type whose params direct action at anything else has a scope this
#      registry cannot express, and "fleet-wide is structurally impossible"
#      stops being true of it.
#   2. **A frozen operator template can fill its params TRUTHFULLY.** No field
#      may need to correlate with server-side state minted for that one dispatch
#      -- a run id, a snapshot manifest, a repository key, a reconciliation
#      version. A template can only ever carry a forged or stale value for such
#      a field, and a forged one is worse than a missing one because the owning
#      module's own record says the work was authorized.
#
# `script.run` satisfies (2) trivially: the dispatcher builds `ScriptRunParams`
# from the PINNED `scripts` row and never from the template (§8.2), which is
# also why the exemption machinery exists for it.
#
# EVERY REGISTERED JOB TYPE IS EITHER HERE OR IN `NON_APPROVABLE_REASONS`, and
# `tests/test_approved_actions_destructive.py` asserts that partition against
# the live registry -- so registering a job type and forgetting to decide breaks
# a test instead of defaulting to either answer by accident.
APPROVABLE_JOB_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "inventory.collect",
        "logs.ship",
        "patch.scan",
        "health.check.now",
        "script.run",
        "patch.apply",
        "app.deploy",
    }
)

# Why each registered job type is refused, so the 400 says something an operator
# can act on rather than "not allowed".
NON_APPROVABLE_REASONS: dict[str, str] = {
    "agent.cert.renew": N_(
        "agent.cert.renew is one leg of automatic certificate rotation. It is dispatched by the "
        "kernel's own state machine, which holds a device to one rotation at a time and pairs "
        "the CSR it produces with the signing leg (docs/modules/agent/cert-renewal.md). An "
        "approved action firing it would produce a CSR nothing is waiting for, and remediation "
        "has no business minting credentials: rotation already retries on every connect, so "
        "there is nothing here for an approved action to fix"
    ),
    "agent.cert.install": N_(
        "agent.cert.install REPLACES AN ENDPOINT'S mTLS IDENTITY, and its params carry a "
        "certificate the kernel minted from that device's own CSR. A frozen params_template can "
        "only hold a certificate that is already superseded -- and a machine-fired action that "
        "pushes an arbitrary certificate onto an endpoint is the exact attack automatic renewal "
        "closes. Rotation is not remediation"
    ),
    "patch.reboot": N_(
        "patch.reboot RESTARTS AN ENDPOINT. The patch module dispatches it only for a device "
        "that still owes a restart after patching and only inside that device's maintenance "
        "window; an approved action firing it would restart a machine outside its window on "
        "an alert's timing"
    ),
    "job.cancel": N_(
        "job.cancel STOPS FLEET WORK, and a machine may not. It is a denial verb: its whole "
        "effect is to make an endpoint abandon something a human asked for, so an approved "
        "action naming it is a remediation whose remedy is 'do less'. A frozen "
        "params_template could not fill it truthfully either -- its one parameter is the id "
        "of a specific in-flight job, which is state minted per dispatch and cannot exist in "
        "a template. It is also the wrong shape for the autonomy dial: fleet-wide cancellation "
        "is a denial of service on patching and backup, and `max_target_scope='device'` bounds "
        "which device the envelope is signed for, not how much work stops"
    ),
    "site.wake": N_(
        "site.wake puts a magic packet on a LAN segment, and WHERE it may do so is decided by "
        "the site-server registry: the control plane picks the operator-declared segment "
        "containing the target and computes the broadcast ADDRESS itself, so the endpoint never "
        "chooses. An approved action reaches JobEngine.create with the operator's own frozen "
        "params_template, which would carry a broadcast address nobody re-derived from a current "
        "declaration -- a segment demoted or edited afterwards would not change it. Its target is "
        "also a MAC on some other machine, which `max_target_scope='device'` cannot bound: what "
        "this job costs is not limited to the device the job is signed for. Dispatch a wake "
        "through POST /api/v1/site-servers/{site_server_id}/wake"
    ),
    "net.sweep": N_(
        "net.sweep puts packets on somebody else's wire, and where it may do so is decided by "
        "ipam's scan authorization (an `ipam_scan_authorization` row, the `ipam.scope.manage` "
        "permission, and the target set intersected with the subnet) plus an `ipam_sweep_run` "
        "row the result is checked against. An approved action reaches JobEngine.create with "
        "the operator's own template and none of that; its `targets` also name hosts other "
        "than the device the job is signed for, which `max_target_scope='device'` cannot "
        "bound. Dispatch a sweep through ipam's own endpoint"
    ),
    "health.policy.apply": N_(
        "health.policy.apply delivers the device's whole monitoring policy and is owned by "
        "health's own policy push and reconciliation, not by remediation: `checks: []` is a "
        "legal policy meaning 'run nothing', so an approved action naming it is a monitoring "
        "kill switch, and its `policy_version` is reconciliation state that an operator-chosen "
        "value forges into 'this device has converged'"
    ),
    "backup.run": N_(
        "backup.run's params carry `repo_key`, the repository encryption key, plus a "
        "`repository_id`/`storage_ref` owned by the backup module. A frozen params_template "
        "would have to hold that key in plaintext JSONB, which invariant #3 forbids "
        "(credentials live encrypted in the vault and are decrypted only at point of use). "
        "Schedule backups through the backup module"
    ),
    "backup.restore": N_(
        "backup.restore's params carry `repo_key`, the repository encryption key, and a "
        "`manifest_ref` naming one snapshot -- state the backup module mints per restore. A "
        "frozen template can only hold a plaintext key and a stale snapshot (invariant #3)"
    ),
    "provision.enroll": N_(
        "provision.enroll's `provision_run_id` must resolve to a provisioning run the "
        "provisioning module created; a frozen template can only ever name a stale one, and "
        "the run row is what authorized re-imaging that machine"
    ),
    "agent.push_install": N_(
        "agent.push_install is NEVER reachable by any automated, scheduled or AI-driven actor. "
        "That is not a judgement about its params template -- it is `CLAUDE.md` invariant 2a "
        "clause (e), enforced where it is checked rather than asserted in prose. The act is "
        "causing a machine nobody has enrolled to execute this platform's agent installer, "
        "using an administrative credential, against a host that cannot be authenticated on "
        "first contact; spec §1.1 accounts for what that is worth to an attacker who has "
        "captured the actor doing it. It also fails both of this allowlist's own conditions: "
        "what it costs is not bounded by the device the job is signed for (the job targets the "
        "site server; the effect lands on a THIRD machine), and a frozen params_template "
        "cannot fill a single-use enrollment token, a per-push presigned artefact URL or a "
        "credential decrypted at dispatch. Dispatch a push through the module's own endpoint, "
        "with a human on it"
    ),
    "agent.push_probe": N_(
        "agent.push_probe is refused for the same reason as agent.push_install and under the "
        "same clause (e): no automated, scheduled or AI-driven actor may cause this platform "
        "to reach an unenrolled machine. It carries no credential, which bounds what a "
        "compromised actor GETS -- not whether one may fire it. Its effect also lands on a "
        "machine other than the device the job is signed for, which `max_target_scope='device'` "
        "cannot bound"
    ),
    "agent.update": N_(
        "agent.update is NEVER reachable by any automated, scheduled or AI-driven actor "
        "(spec-self-update.md §8.5). It replaces the agent -- the process that verifies every "
        "other job -- and its availability risk is managed by rings a person advances and a "
        "failure threshold that pauses the rollout; a frozen params_template would carry a "
        "presigned URL that has expired and a release nobody chose for this fleet. Roll out "
        "agent updates through POST /api/v1/agent/update-rollouts, with a human on it"
    ),
    "agent.uninstall": N_(
        "agent.uninstall is NEVER reachable by any automated, scheduled or AI-driven actor "
        "(install-link spec §3). It removes the agent -- the platform's only presence on "
        "that computer -- and it exists to carry out ONE person's decision that a machine "
        "does not belong here. A frozen template could only ever uninstall a device nobody "
        "rejected. Reject the machine through POST /api/v1/devices/{device_id}/review/reject"
    ),
    "remote.session.open": N_(
        "remote.session.open's params carry a `relay_token` -- a per-session credential the "
        "remote module mints from the vault at dispatch -- plus a `relay_url` and a "
        "`session_id` that must resolve to a `remote_session` row carrying the operator's "
        "consent decision. A frozen params_template can hold none of those truthfully, and a "
        "remote-control session opened by a machine with no human at either end is not a "
        "remediation. This is the same job type `SUBSTITUTABLE_PARAM_KEYS` already omits, for "
        "the same reason, now said once where it is enforced"
    ),
    "remote.screen.open": N_(
        "remote.screen.open shows and controls a person's screen. Its params carry a one-shot "
        "relay token valid for 60 s and a session_id minted by a technician's RBAC-checked "
        "start, so a frozen template cannot hold them, and screen share is never reachable by "
        "an automated or AI-driven actor (spec-screen-share.md §3.10)"
    ),
}


def non_approvable_reason(job_type: str) -> str | None:
    """Why `job_type` may not be named by an approved action, or `None`.

    DENY BY DEFAULT. A job type that is neither in `APPROVABLE_JOB_TYPES` nor in
    `NON_APPROVABLE_REASONS` is refused with a generic reason -- a newly
    registered job type is not dispatchable by a machine until somebody decides
    it is.
    """
    if job_type in APPROVABLE_JOB_TYPES:
        return None
    stated = NON_APPROVABLE_REASONS.get(job_type)
    if stated is not None:
        return stated
    return tr(
        "{job_type} is not in the approved-action allowlist. A job type is approvable only "
        "when what it does is bounded by the device the job targets AND a frozen "
        "params_template can fill its params truthfully -- no run id, snapshot, repository "
        "key or reconciliation version minted per dispatch. Adding one is a reviewed change "
        "to APPROVABLE_JOB_TYPES",
        job_type=job_type,
    )

# The job type whose destructiveness cannot be decided from its name: arbitrary
# code. `script.run` is DESTRUCTIVE BY DEFAULT (§8.4 clause 2) -- the server
# cannot know what a PowerShell body does, and pattern-matching bodies for
# dangerous verbs is security theatre that fails on the first base64 blob.
_ARBITRARY_CODE_JOB_TYPE = "script.run"

# Key -> the VALUES of that key that make an action destructive. A map of key
# NAMES would be wrong (§8.4 amendment): it would mark every `patch.apply`
# action destructive including `reboot: "never"`, and an over-broad gate is one
# that gets argued away. `PatchApplyParams.reboot` is
# `Literal["never","if_required","forced"]` and NOT a boolean -- verified
# against `app/modules/patch/schema.py`.
DESTRUCTIVE_PARAM_KEYS: dict[str, dict[str, frozenset[str]]] = {
    "patch.apply": {"reboot": frozenset({"if_required", "forced"})},
}

# Which params keys may receive a substitution, per job type. NOT a template
# language: exactly two substitutions exist (`{device_id}`, `{selector}`) and
# this map bounds where each may appear. A job type absent from this map permits
# none; this is NOT a job-type allowlist (that is
# `app.jobs.registry.registered_names()`, per §2.9). Adding an entry is a
# reviewed code change.
#
# EVERY ENTRY IS EMPTY IN V1, AND THAT IS A FINDING ABOUT THE CODE RATHER THAN
# AN OMISSION (§8.2). No registered job type has a params key a device id or a
# selector may be written into. `ScriptRunParams` in particular is
# (interpreter, body, script_id, script_version, timeout_s) -- there is NO
# arguments field, so a script on this platform takes no parameters and the body
# is the entire input. Per-service remediation is therefore one approved action
# per service, deliberately: adding a free `args: str` to `ScriptRunParams`
# would be an unbounded operator-controlled string in a params model that
# executes as SYSTEM/root, which is spec §0.1's free-form expression arriving
# from the server side instead of the edge.
#
# `hello` and `remote.session.open` are absent deliberately: the first is a
# kernel smoke type, the second needs a relay URL and a relay token this
# facility cannot mint. Absent and empty behave identically; the difference is
# that an empty entry is a reviewed statement and an absent one is silence.
SUBSTITUTABLE_PARAM_KEYS: dict[str, frozenset[str]] = {
    "script.run": frozenset(),
    "app.deploy": frozenset(),
    "patch.apply": frozenset(),
    "patch.scan": frozenset(),
    "inventory.collect": frozenset(),
    "logs.ship": frozenset(),
    "backup.run": frozenset(),
    "backup.restore": frozenset(),
    "provision.enroll": frozenset(),
}

DEVICE_ID_TOKEN = "{device_id}"
SELECTOR_TOKEN = "{selector}"

# ---------------------------------------------------------------------------
# Secrets may not be frozen into a params template (invariant #3)
# ---------------------------------------------------------------------------

# Substrings that make a params key a CREDENTIAL-CARRYING key. `params_template`
# is plaintext JSONB on a config row that `health.read` can list, so anything
# stored here is a stored credential in the application database, returned in an
# API response -- both halves of invariant #3, twice over.
#
# `BackupRunParams.repo_key` is the case that proved it: a REQUIRED `str`, so
# every dispatchable `backup.run` approved action had to carry the repository
# encryption key in the clear. That job type is now refused outright by
# `APPROVABLE_JOB_TYPES` for exactly this reason; this check is the general rule
# behind that one decision, so the next params model with a credential field
# cannot arrive by accident.
#
# A NAME-BASED GATE, AND ITS LIMIT IS STATED RATHER THAN IMPLIED. It sees
# `repo_key`, `api_token`, `admin_password`; it CANNOT see a credential smuggled
# into a legitimately-named free string (`app.deploy`'s `install_args` could
# carry `/PASSWORD=...`). It is a floor, not a proof. The real fix is vault
# indirection -- a template holding a reference that is resolved at point of use
# -- which this platform does not have for job params yet; recorded here rather
# than in a comment nobody reads, because a gate that is mistaken for a proof is
# worse than no gate.
SECRET_PARAM_KEY_MARKERS: frozenset[str] = frozenset(
    {"key", "secret", "password", "passwd", "passphrase", "token", "credential"}
)


def secret_bearing_keys(template: Mapping[str, Any]) -> list[str]:
    """Dotted paths of every params key whose NAME says it carries a secret.

    Recurses through nested mappings and lists: `provision.enroll`'s
    `post_config` and `backup.run`'s `sources` are free-form `dict[str, Any]`,
    so a top-level-only scan would miss the obvious hiding place.
    """
    found: list[str] = []
    _walk_for_secret_keys(template, prefix="", found=found)
    return sorted(found)


def _walk_for_secret_keys(node: Any, *, prefix: str, found: list[str]) -> None:
    if isinstance(node, Mapping):
        for raw_key, value in node.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if _names_a_secret(key):
                found.append(path)
            _walk_for_secret_keys(value, prefix=path, found=found)
        return
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node:
            _walk_for_secret_keys(item, prefix=prefix, found=found)


def _names_a_secret(key: str) -> bool:
    """The one test both the write gate and the read redaction apply.

    Shared deliberately: two copies of this predicate is how a key refused on
    the way in gets served on the way out.
    """
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_PARAM_KEY_MARKERS)


#: What a redacted value becomes on the read path. A VISIBLE PLACEHOLDER rather
#: than a dropped key: an operator looking at a legacy row has to be able to see
#: that the key is there and that its value was withheld. Removing the key would
#: read as "this action has no `repo_key`", and the next admin would helpfully
#: put one back.
REDACTED_PLACEHOLDER = "[redacted: stored credential]"


def redact_secret_values(
    template: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """A copy of `template` with every credential-named VALUE replaced, plus the
    dotted paths that were replaced.

    THE WRITE GATE DOES NOT REACH ROWS THAT ALREADY EXIST. `secret_bearing_keys`
    refuses a `params_template` naming a credential at write time, which closes
    the door for every row written after it shipped -- and does nothing at all
    about the rows written before. `params_template` is plaintext JSONB served
    verbatim to every holder of `health.read`, which is a broad, non-admin
    grant, so a legacy row is invariant #3 violated on both halves at once: a
    credential stored in the application database and a credential returned in
    an API response. This function is the read-side half, and it is not
    conditional on when the row was written -- a value that names a secret is
    withheld whatever wrote it.

    SERVED REDACTED, NOT REFUSED. Refusing to render the row would hide exactly
    the row an admin has to find and rewrite, and would take the whole registry
    listing down with it (one legacy row, `GET /approved-actions` unusable).
    The returned path list is what lets the console say WHICH key to fix.

    A SECRET-NAMED KEY IS REDACTED WHOLE, subtree and all: if `credentials` is a
    nested object, the object does not survive because its inner keys happened
    to be innocently named. That makes the returned paths a prefix-cover of
    `secret_bearing_keys`' paths rather than an exact match, which is the safe
    direction and is pinned by a test.
    """
    redacted: list[str] = []
    out = _redact_node(dict(template), prefix="", redacted=redacted)
    return out, sorted(redacted)


def _redact_node(node: Any, *, prefix: str, redacted: list[str]) -> Any:
    if isinstance(node, Mapping):
        result: dict[str, Any] = {}
        for raw_key, value in node.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if _names_a_secret(key):
                redacted.append(path)
                result[key] = REDACTED_PLACEHOLDER
            else:
                result[key] = _redact_node(value, prefix=path, redacted=redacted)
        return result
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_redact_node(item, prefix=prefix, redacted=redacted) for item in node]
    return node


class ApprovedActionTemplateError(ValueError):
    """A `params_template` that cannot be rendered.

    Raised at WRITE time (the API returns 400), never at dispatch time. §8.2's
    whole posture is that an approved action naming something impossible is a
    configuration bug that must surface when someone writes it, not at 03:00
    with an alert already open.
    """


# ---------------------------------------------------------------------------
# is_destructive (spec §8.4) -- PURE
# ---------------------------------------------------------------------------


@runtime_checkable
class ScriptExemptionPin(Protocol):
    """The two fields `is_destructive` reads off an exemption.

    A Protocol rather than the ORM class so the derivation stays pure and
    testable without a database, while `ApprovedActionScriptExemption` rows
    satisfy it directly.
    """

    @property
    def script_id(self) -> UUID: ...

    @property
    def script_version(self) -> int: ...


def is_destructive(
    job_type: str,
    params_template: Mapping[str, Any],
    script_exemption: ScriptExemptionPin | None,
    *,
    script_id: UUID | None = None,
    script_version: int | None = None,
) -> bool:
    """The ONE derivation of "destructive" (spec §8.4). Pure: it queries
    nothing, so this module and Phase 17 cannot disagree.

    True iff any of:

    1. `job_type` is in `_DESTRUCTIVE_JOB_TYPES`;
    2. `job_type` is `script.run` and no exemption exists for its EXACT
       `(script_id, script_version)` pair;
    3. `params_template` sets a key to a value listed in
       `DESTRUCTIVE_PARAM_KEYS`.

    RECORDED SPEC GAP, AND HOW IT IS CLOSED. §8.4 states the signature as
    `is_destructive(job_type, params_template, script_exemption)`, but clause 2
    compares the exemption against "its exact `(script_id, script_version)`
    pair" -- and that pair is on the `approved_action` ROW (§2.9), not in
    `params_template` (§8.2: the dispatcher builds `ScriptRunParams` from the
    pinned `scripts` row, so the template does not duplicate the pin, and
    duplicating it would create two truths that can disagree). Without the pin
    this function cannot tell an exemption for version 2 from one for version 3
    and would have to trust whatever row the caller handed it. The pin is
    therefore two KEYWORD-ONLY parameters, leaving the three positional ones
    exactly as §8.4 names them.

    FAILS CLOSED. A `script.run` action with no pin at all (`script_id=None`)
    can match no exemption and is destructive. That is the right direction: the
    database CHECK makes such a row unrepresentable, and if one ever existed the
    safe answer is "needs a human".
    """
    if job_type in _DESTRUCTIVE_JOB_TYPES:
        return True

    if job_type == _ARBITRARY_CODE_JOB_TYPE and not _exemption_matches(
        script_exemption, script_id, script_version
    ):
        return True

    return _params_make_destructive(job_type, params_template)


def _exemption_matches(
    exemption: ScriptExemptionPin | None,
    script_id: UUID | None,
    script_version: int | None,
) -> bool:
    """Whether `exemption` was granted for exactly this pinned script version.

    Keyed to the PAIR, never to `script_id` alone (§2.10):
    `deploy/scripts.py::update_script` bumps `version` IN PLACE on the same row
    and the previous body is not retained anywhere, so a script's `id` is not a
    version identifier. An exemption keyed on the id alone would silently follow
    every future edit -- the "approve whatever that script says tomorrow"
    failure the pinning exists to prevent.
    """
    if exemption is None or script_id is None or script_version is None:
        return False
    return exemption.script_id == script_id and exemption.script_version == script_version


def _params_make_destructive(job_type: str, params_template: Mapping[str, Any]) -> bool:
    """Clause 3 of `is_destructive`, separated so it can be pinned on its own.

    It has to be: `patch.apply` is the only job type in `DESTRUCTIVE_PARAM_KEYS`
    and it is ALSO in `_DESTRUCTIVE_JOB_TYPES`, so a test that went through
    `is_destructive` would be satisfied by clause 1 and would pass with this
    whole clause deleted.
    """
    destructive_values = DESTRUCTIVE_PARAM_KEYS.get(job_type)
    if not destructive_values:
        return False
    return any(
        key in params_template and str(params_template[key]) in values
        for key, values in destructive_values.items()
    )


# ---------------------------------------------------------------------------
# render_params_template (spec §8.2)
# ---------------------------------------------------------------------------


def render_params_template(
    job_type: str,
    template: Mapping[str, Any],
    *,
    device_id: UUID,
    selector: str,
) -> dict[str, Any]:
    """Substitute `{device_id}` and `{selector}` into a frozen params template.

    A STRUCTURAL WALK, NEVER `str.format`. Three separate reasons, any one
    sufficient (§8.2):

    1. `format` is an object-graph read. `"{0.__class__.__init__.__globals__}"`
       in a customer-authored template walks whatever object is passed in, and a
       template written by an org admin is untrusted input to a process holding
       the job signing key.
    2. `format` fails at the wrong time -- `KeyError` at dispatch, 03:00, alert
       already open -- where this walk raises at write time and the API returns
       400.
    3. A legitimate selector contains braces. A Windows volume path is
       `\\\\?\\Volume{GUID}\\` and `disk.free_pct` takes a volume as its
       selector. Under `format` that is a formatter exception; here it is data,
       because a SUBSTITUTED VALUE IS NEVER RE-SCANNED for tokens.

    Any other `{` or `}` in any string in the template raises, and a token in a
    key the job type's `SUBSTITUTABLE_PARAM_KEYS` entry does not permit raises
    too. In v1 every entry is empty, so every token raises -- see that map's
    comment for why that is a finding rather than a gap.
    """
    allowed = SUBSTITUTABLE_PARAM_KEYS.get(job_type, frozenset())
    return {
        str(k): _render_node(
            v, key=str(k), allowed=allowed, device_id=device_id, selector=selector
        )
        for k, v in template.items()
    }


def _render_node(
    node: Any,
    *,
    key: str | None,
    allowed: frozenset[str],
    device_id: UUID,
    selector: str,
) -> Any:
    """Recurse. `key` is the params key this node sits under -- a string inside
    a list is governed by the key of the list, not by its index."""
    if isinstance(node, Mapping):
        return {
            str(k): _render_node(
                v, key=str(k), allowed=allowed, device_id=device_id, selector=selector
            )
            for k, v in node.items()
        }
    if isinstance(node, str):
        return _render_string(
            node, key=key, allowed=allowed, device_id=device_id, selector=selector
        )
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [
            _render_node(v, key=key, allowed=allowed, device_id=device_id, selector=selector)
            for v in node
        ]
    return node


def _render_string(
    value: str,
    *,
    key: str | None,
    allowed: frozenset[str],
    device_id: UUID,
    selector: str,
) -> str:
    if value in (DEVICE_ID_TOKEN, SELECTOR_TOKEN):
        if key is None or key not in allowed:
            raise ApprovedActionTemplateError(
                tr(
                    "{value} is not permitted in params key {key} for this job type",
                    value=value,
                    key=repr(key),
                )
            )
        # The substituted value is returned as-is and never re-scanned: a
        # selector like \\?\Volume{GUID}\ is data, not a template.
        return str(device_id) if value == DEVICE_ID_TOKEN else selector
    if "{" in value or "}" in value:
        raise ApprovedActionTemplateError(
            tr(
                "params key {key} contains an unrecognised brace token: {value}",
                key=repr(key),
                value=repr(value),
            )
        )
    return value


# ---------------------------------------------------------------------------
# Reads. Every one carries an EXPLICIT org_id predicate in addition to RLS
# (invariant #5) -- RLS is the backstop, not the only filter, and a query that
# leans on RLS alone returns the wrong row the moment it runs on the admin
# engine (a worker sweep, a migration-time fixup, a test).
# ---------------------------------------------------------------------------


async def get_approved_action(
    session: AsyncSession, *, org_id: UUID, key: str
) -> ApprovedAction | None:
    return (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.key == key
            )
        )
    ).scalar_one_or_none()


async def get_script_exemption(
    session: AsyncSession, *, org_id: UUID, script_id: UUID, script_version: int
) -> ApprovedActionScriptExemption | None:
    """The exemption row `is_destructive` takes, looked up for the EXACT pair.

    This is the only place the pair is turned into a row; `is_destructive`
    itself stays pure and never queries.
    """
    return (
        await session.execute(
            select(ApprovedActionScriptExemption).where(
                ApprovedActionScriptExemption.org_id == org_id,
                ApprovedActionScriptExemption.script_id == script_id,
                ApprovedActionScriptExemption.script_version == script_version,
            )
        )
    ).scalar_one_or_none()


async def fuse_breach_worded(
    session: AsyncSession,
    org_id: UUID,
    action: ApprovedAction,
    *,
    device_id: UUID,
    now: datetime,
) -> str | None:
    """The three rate fuses, or `None` when all pass, worded in the current
    language. `fuse_breach` is the same answer in English, for stored text.

    NOT PURE, UNLIKE `is_destructive` DIRECTLY ABOVE. This one queries `jobs`,
    and the difference is deliberate: destructiveness is a property of a
    catalogue row and must give one answer everywhere, while "is this fused
    right now" is a property of what has recently run in one org. The signature
    therefore takes a session, and every read carries an explicit `org_id`
    predicate in addition to RLS -- the fuses are evaluated on the ADMIN engine
    by the health sweep, where RLS is not the thing doing the filtering.

    THIS BODY MOVED HERE FROM `app/modules/health/actions.py::_fuse_breach`
    (docs/modules/endpoint-health-alerting/spec.md §7.1 clause 2), unchanged,
    because `docs/modules/ai-automation.md` §6.1 and §6.5 need the SAME
    derivation and `app/core/ai/` may import no `app.modules.*` package. Two
    implementations of "is this fused" is the defect this module's own docstring
    exists to prevent, one question below "may this run without a human". Health
    still calls it, through a one-line delegation.

    COUNTED AGAINST `jobs`, NOT AGAINST A COUNTER TABLE (spec §2.9): `jobs` is
    already the durable, audited record of every dispatch, and a second counter
    would be a second truth that can disagree with what actually ran.

    THE CONSEQUENCE, STATED RATHER THAN PAPERED OVER (§7.1's amendment): the
    predicate is `(org_id, type, target_id, created_at)`, so TWO APPROVED
    ACTIONS SHARING A `job_type` SHARE ONE FUSE -- two different `script.run`
    actions consume the same budget -- and a technician running that job type
    manually consumes it too. Verified against the code rather than assumed:
    `jobs.requested_by` is NULL for a machine dispatch and set for a human one,
    so narrowing on it WOULD separate the two. It is deliberately not narrowed.
    The fuse exists to bound what reaches an ENDPOINT, and 20 script runs in an
    hour is 20 script runs whoever asked for them; a fuse that ignored manual
    runs would let an operator and the sweep each spend the full budget at once.
    That is why the refusal names the job ids counted against it -- an operator
    who cannot see WHAT consumed the budget has no way to tell which case they
    are in. ALL THREE BRANCHES NAME THEM, including the per-device one, which
    for a while returned a bare count while the test that should have caught it
    asserted `str(prior_id) not in last_error or True`.
    """
    if action.cooldown_s:
        recent = (
            await session.execute(
                select(Job.id, Job.created_at)
                .where(
                    Job.org_id == org_id,
                    Job.type == action.job_type,
                    Job.target_id == device_id,
                    Job.created_at > now - timedelta(seconds=action.cooldown_s),
                )
                .order_by(Job.created_at.desc())
                .limit(5)
            )
        ).all()
        if recent:
            return tr(
                "cooldown: {job_type} last ran on this device inside the action's "
                "{cooldown_s}s cooldown; job ids {job_ids}",
                job_type=action.job_type,
                cooldown_s=action.cooldown_s,
                job_ids=[str(job_id) for job_id, _ in recent],
            )

    # IDS, NOT `count()` (§7.1's amendment). This fuse counts `jobs` by TYPE, so
    # what consumed the budget may be another approved action sharing the type
    # or a technician's manual run; a bare number leaves an operator with no way
    # to tell which. Bounded by `limit` for the same reason the per-org branch
    # is: the message is capped at 500 characters on the row, so an unbounded
    # id list would be truncated mid-uuid rather than being short by design.
    per_device_rows = (
        await session.execute(
            select(Job.id)
            .where(
                Job.org_id == org_id,
                Job.type == action.job_type,
                Job.target_id == device_id,
                Job.created_at > now - timedelta(days=1),
            )
            .order_by(Job.created_at.desc())
            .limit(action.max_per_device_per_day + 5)
        )
    ).all()
    if len(per_device_rows) >= action.max_per_device_per_day:
        return tr(
            "max_per_device_per_day: {count} {job_type} job(s) already ran on this device in "
            "the last 24h (limit {limit}); most recent job ids {job_ids}",
            count=len(per_device_rows),
            job_type=action.job_type,
            limit=action.max_per_device_per_day,
            job_ids=[str(r[0]) for r in per_device_rows[:5]],
        )

    per_org_rows = (
        await session.execute(
            select(Job.id)
            .where(
                Job.org_id == org_id,
                Job.type == action.job_type,
                Job.created_at > now - timedelta(hours=1),
            )
            .limit(action.max_per_org_per_hour + 5)
        )
    ).all()
    if len(per_org_rows) >= action.max_per_org_per_hour:
        return tr(
            "max_per_org_per_hour: {count} {job_type} job(s) already ran across this org in "
            "the last hour (limit {limit}); first job ids {job_ids}",
            count=len(per_org_rows),
            job_type=action.job_type,
            limit=action.max_per_org_per_hour,
            job_ids=[str(r[0]) for r in per_org_rows[:5]],
        )
    return None


async def fuse_breach(
    session: AsyncSession,
    org_id: UUID,
    action: ApprovedAction,
    *,
    device_id: UUID,
    now: datetime,
) -> str | None:
    """`fuse_breach_worded` in English.

    For a caller that STORES the sentence (health's `last_error`) or hands it to
    the model (the AI act path): a stored value never takes the request's
    language. A caller that only answers a person uses `fuse_breach_worded`."""
    with use_locale("en"):
        return await fuse_breach_worded(session, org_id, action, device_id=device_id, now=now)


__all__ = [
    "APPROVABLE_JOB_TYPES",
    "AUTONOMY_LEVELS",
    "DESTRUCTIVE_PARAM_KEYS",
    "DEVICE_ID_TOKEN",
    "MAX_COOLDOWN_S",
    "MAX_PER_DEVICE_PER_DAY",
    "MAX_PER_ORG_PER_HOUR",
    "MAX_TARGET_SCOPES",
    "NON_APPROVABLE_REASONS",
    "REDACTED_PLACEHOLDER",
    "RISK_CLASSES",
    "SECRET_PARAM_KEY_MARKERS",
    "SELECTOR_TOKEN",
    "SUBSTITUTABLE_PARAM_KEYS",
    "_DESTRUCTIVE_JOB_TYPES",
    "ApprovedAction",
    "ApprovedActionScriptExemption",
    "ApprovedActionTemplateError",
    "ScriptExemptionPin",
    "fuse_breach",
    "fuse_breach_worded",
    "get_approved_action",
    "get_script_exemption",
    "is_destructive",
    "non_approvable_reason",
    "redact_secret_values",
    "render_params_template",
    "secret_bearing_keys",
]

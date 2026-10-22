"""Request/response models for the health API
(`docs/modules/endpoint-health-alerting/spec.md` §10, §13).

EVERY REQUEST MODEL FORBIDS UNKNOWN KEYS. This is not tidiness. Three services
in this module deliberately REFUSE an unknown field rather than ignore it --
`profiles.update_profile` ("unknown or server-owned profile field(s)"),
`settings._validate` ("unknown health settings field(s)") and
`approved_actions._validate_and_derive` ("`destructive` is DERIVED ... and is
never accepted from a request body") -- and a Pydantic model whose `extra` is
Pydantic's default `ignore` would STRIP the typo before the service could see
it, returning 200 for a body that changed nothing and auditing a no-op edit.
That defect was closed in the service layer and then reinstated for every HTTP
caller on the SLA module; `_StrictIn` is what carries the service's decision
across the wire, as a 422 naming the offending key.

RESPONSE MODELS ARE DELIBERATELY NOT STRICT: they are built in-process by
`api.py`'s own builders and never parsed from input.

NOTHING HERE RE-DERIVES A SERVER DECISION. `threshold_display` comes from
`registry.render_threshold`, `for_seconds` from `profiles.effective_for_seconds`,
`destructive` from `core.approved_actions.is_destructive`, and the resolution
level from `resolution.ResolvedProfile.level`. A browser is given the answer,
never the operands to compute a rival one -- the same posture
`docs/modules/sla/spec.md` §9.1 takes about breach state.

`policy_version` IS ON EVERY PROFILE RESPONSE AND ON NO REQUEST MODEL (§2.1):
it is server-owned, and a client-supplied value would let a caller claim
convergence for a fleet.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.approved_actions import AUTONOMY_LEVELS, MAX_TARGET_SCOPES, RISK_CLASSES
from app.modules.health.registry import (
    CADENCES,
    COMPARATORS,
    MAX_CHECKS_PER_PROFILE,
    OS_FAMILIES,
    SEVERITIES,
    VALUE_KINDS,
)

__all__ = [
    "AlertActionOut",
    "AlertDetailOut",
    "AlertOut",
    "AlertRefuseIn",
    "AlertSummaryOut",
    "ApprovedActionCreate",
    "ApprovedActionOut",
    "ApprovedActionUpdate",
    "AssignmentCreate",
    "AssignmentOut",
    "CheckIn",
    "CheckOut",
    "CheckTypeOut",
    "ChecksReplace",
    "DeviceCheckStateOut",
    "DeviceHealthOut",
    "ExemptScriptIn",
    "JobDispatchedOut",
    "MuteIn",
    "ProfileChecksOut",
    "ProfileClone",
    "ProfileCreate",
    "ProfileOut",
    "ProfileUpdate",
    "RemediateIn",
    "ScriptExemptionOut",
    "SettingsOut",
    "SettingsUpdate",
]

_MAX_NAME = 200
_MAX_SELECTOR = 200
_MAX_KEY = 100
_MAX_REASON = 500

Severity = Literal["info", "warning", "critical"]
AlertState = Literal["active", "acknowledged", "muted", "cleared"]
# The three states that are OPEN (§0.8): owned is still open, silenced is still
# open, only `cleared` is over. Kept apart from `AlertState` so that
# `AlertSummaryOut.by_state` -- a summary of what is OPEN -- cannot be typed
# with a key for the one state it never counts. The assertion below is what
# stops the two drifting.
OpenAlertState = Literal["active", "acknowledged", "muted"]
ClearedReason = Literal[
    "recovery",
    "summary_ok",
    "unavailable_timeout",
    "policy_removed",
    "device_inactive",
    "manual",
]
ActionKind = Literal["remediate", "open_ticket", "notify"]
ActionState = Literal[
    "pending",
    "awaiting_approval",
    "dispatched",
    "done",
    "skipped",
    "blocked_rate_limit",
    "blocked_flapping",
    "expired",
    "dead",
]
OsFamily = Literal["windows", "linux", "macos"]
Comparator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "is"]
RiskClass = Literal["low", "medium", "high"]
Autonomy = Literal["suggest", "require_approval", "autonomous"]
MaxTargetScope = Literal["device"]
ValueKind = Literal["numeric", "boolean", "enum"]
Cadence = Literal["tick", "posture"]
EvaluatedBy = Literal["agent", "server"]
ResolutionLevel = Literal["device", "group", "org"]

# `device_health_check_state.state` is `ok|breaching|unavailable`. `unknown` is
# NOT a stored state: it is what `api.py` reports for a check the device has
# never answered, and it is deliberately a FOURTH value rather than a fold into
# `ok` (§0.7's rule that absent is not healthy) or into `unavailable` (which
# means "asked and could not answer", a different fact with a clear timeout
# attached to it).
DeviceCheckState = Literal["ok", "breaching", "unavailable", "unknown"]

# These five assertions are the wire contract's tie to the registry. Each
# `Literal` above restates a closed set that `registry.py` (and, for the
# approved-action three, `app/models/approved_actions.py`) already owns; without
# these, adding a check type or an autonomy level leaves the API silently
# refusing the new member with a 422 nobody can explain. Module-level so the
# failure is an ImportError at start-up rather than a request-time surprise.
assert set(SEVERITIES) == set(Severity.__args__)  # type: ignore[attr-defined]
assert set(OpenAlertState.__args__) | {"cleared"} == set(  # type: ignore[attr-defined]
    AlertState.__args__  # type: ignore[attr-defined]
)
assert set(OS_FAMILIES) == set(OsFamily.__args__)  # type: ignore[attr-defined]
assert set(COMPARATORS) == set(Comparator.__args__)  # type: ignore[attr-defined]
assert set(VALUE_KINDS) == set(ValueKind.__args__)  # type: ignore[attr-defined]
assert set(CADENCES) == set(Cadence.__args__)  # type: ignore[attr-defined]
assert set(RISK_CLASSES) == set(RiskClass.__args__)  # type: ignore[attr-defined]
assert set(AUTONOMY_LEVELS) == set(Autonomy.__args__)  # type: ignore[attr-defined]
assert set(MAX_TARGET_SCOPES) == set(MaxTargetScope.__args__)  # type: ignore[attr-defined]


class _StrictIn(BaseModel):
    """See the module docstring. Unknown keys are a 422, never a silent drop."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Profiles (§2.1, §10)
# --------------------------------------------------------------------------- #


class ProfileCreate(_StrictIn):
    """The three intervals are bounded HERE only as far as Pydantic can: the
    cross-field rule (`posture_interval_s >= tick_interval_s`) belongs to
    `profiles._validate_intervals`, which owns the message, and to
    `ck_health_profile_posture_not_faster_than_tick`, which owns the fact."""

    name: str = Field(min_length=1, max_length=_MAX_NAME)
    description: str = ""
    tick_interval_s: int = Field(default=300, ge=60, le=3600)
    posture_interval_s: int = Field(default=3600, ge=900, le=86400)
    summary_interval_s: int = Field(default=1800, ge=300, le=86400)
    active: bool = True


class ProfileUpdate(_StrictIn):
    """Every field optional; `supplied()` is `exclude_unset`.

    `profiles.update_profile` refuses an EMPTY body with "no fields supplied"
    and refuses a key outside `PROFILE_EDITABLE_FIELDS` by name. Both survive
    this model: `exclude_unset` preserves "not mentioned", and `extra="forbid"`
    turns a typo into a 422 rather than into an empty `fields` dict that would
    read as the first refusal and mislead the caller.
    """

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME)
    description: str | None = None
    tick_interval_s: int | None = Field(default=None, ge=60, le=3600)
    posture_interval_s: int | None = Field(default=None, ge=900, le=86400)
    summary_interval_s: int | None = Field(default=None, ge=300, le=86400)
    active: bool | None = None

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ProfileClone(_StrictIn):
    name: str = Field(min_length=1, max_length=_MAX_NAME)


class ProfileOut(BaseModel):
    id: UUID
    name: str
    description: str
    tick_interval_s: int
    posture_interval_s: int
    summary_interval_s: int
    active: bool
    # Server-owned (§2.1). On the response because §13's device view has to be
    # able to show policy DRIFT, which is this number against the device's ack.
    policy_version: int
    # LIVE checks only -- a retired check is history and is not pushed to any
    # endpoint (§2.4's amendment). Without this the profile list cannot show
    # "this profile alarms on nothing", which is a state an operator must see.
    check_count: int
    assignment_count: int
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Checks (§2.2, §0.3, §4.1)
# --------------------------------------------------------------------------- #


class CheckIn(_StrictIn):
    """One check in a `PUT /health/profiles/{id}/checks` body.

    `for_ticks` AND `for_seconds` ARE BOTH OPTIONAL AND `supplied()` IS
    `exclude_unset`, because `profiles._validated_check` distinguishes
    "supplied" from "defaulted" for exactly these two keys: supplying both is
    refused ("two answers to one question"), and supplying `for_seconds` alone
    converts against THIS profile's tick interval (§0.3). A model that
    defaulted `for_ticks=3` would make `for_seconds` unreachable -- every body
    would look like it supplied both.

    `check_type` is `str`, not a `Literal` over the registry: the registry is
    the authority (`profiles._validated_check` raises a message naming the
    closed set and the fact that adding one is a code change in BOTH repos),
    and a 422 reading "unexpected value" would replace that explanation with a
    schema complaint.
    """

    check_type: str = Field(min_length=1, max_length=40)
    selector: str = Field(default="", max_length=_MAX_SELECTOR)
    comparator: Comparator
    threshold_num: float | None = None
    threshold_bool: bool | None = None
    for_ticks: int | None = Field(default=None, ge=1, le=60)
    for_seconds: int | None = Field(default=None, gt=0)
    severity: Severity = "warning"
    os_family: OsFamily | None = None
    enabled: bool = True
    # §0.10's per-check action declaration. THE TWO BOOLEANS DEFAULT TRUE AND
    # THE REMEDIATION SLOT DEFAULTS NULL, which is the asymmetry
    # `profiles._validated_check` owns: `declares_notify: false` is §7.3's
    # silent-by-design check and an explicit operator act, while remediation is
    # off until a human names the approved action that answers this check --
    # declaring it and naming the action are ONE act, because a remediate row
    # without an action id is a row the worker can only skip.
    #
    # BY ID, NOT BY KEY, and that is load-bearing at this layer specifically:
    # checks are replaced as a WHOLE SET, so a UI that GETs the set and PUTs it
    # back must round-trip the same identifier the read side returned, or an
    # unrelated edit silently disarms a remediation.
    declares_notify: bool = True
    declares_open_ticket: bool = True
    remediate_approved_action_id: UUID | None = None

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ChecksReplace(_StrictIn):
    """The whole set, replaced together (§10).

    An EMPTY list is legal and means §5.2's disarm -- "run nothing", as an
    instruction rather than as silence. `max_length` restates §0.11's cap so an
    oversized body is refused before it reaches the service; the service raises
    the message and the `ck`-backed trigger owns the fact.
    """

    checks: list[CheckIn] = Field(default_factory=list, max_length=MAX_CHECKS_PER_PROFILE)


class CheckOut(BaseModel):
    id: UUID
    profile_id: UUID
    check_type: str
    selector: str
    comparator: Comparator
    threshold_num: float | None
    threshold_bool: bool | None
    # What the agent is actually sent (§5.2): "95", "true", or the enum MEMBER
    # NAME. Rendered server-side by `registry.render_threshold` so the console
    # and the endpoint read the same alphabet.
    threshold_display: str
    for_ticks: int
    # §0.3: `for_ticks` in wall-clock terms ON THIS PROFILE. Both numbers are
    # sent because the UI must render "3 ticks (~15 min at this profile's
    # 5-minute tick)" -- a duration shown only in ticks is unreadable, and one
    # shown only in minutes hides that changing the tick interval changes it.
    for_seconds: int
    tick_interval_s: int
    severity: Severity
    os_family: OsFamily | None
    enabled: bool
    # §0.10's declaration, returned in exactly the shape `CheckIn` accepts so
    # the GET/PUT round trip is lossless.
    declares_notify: bool
    declares_open_ticket: bool
    remediate_approved_action_id: UUID | None
    # Registry facts (§4.1), inline so the editor can show BEFORE saving that a
    # BitLocker check will be `unavailable` on a Linux estate.
    value_kind: ValueKind
    cadence: Cadence
    evaluated_by: EvaluatedBy
    available_os_families: list[OsFamily]
    selector_meaning: str
    created_at: datetime
    updated_at: datetime


class ProfileChecksOut(BaseModel):
    """The check set plus the two numbers that give it meaning."""

    profile_id: UUID
    policy_version: int
    tick_interval_s: int
    checks: list[CheckOut]


class CheckTypeOut(BaseModel):
    """One row of §4.1's closed registry."""

    name: str
    value_kind: ValueKind
    cadence: Cadence
    evaluated_by: EvaluatedBy
    os_families: list[OsFamily]
    legal_comparators: list[Comparator]
    selector_meaning: str
    enum_values: list[str]


# --------------------------------------------------------------------------- #
# Assignments (§2.3, §0.2)
# --------------------------------------------------------------------------- #


class AssignmentCreate(_StrictIn):
    """A device OR a group OR neither (the org default) -- never both.

    Typed as two independent optionals rather than as a discriminated union
    because `profiles.create_assignment` owns the refusal and its message names
    all three levels; a Pydantic union would produce a 422 that says only that
    the body matched no branch.
    """

    profile_id: UUID
    device_id: UUID | None = None
    device_group_id: UUID | None = None


class AssignmentOut(BaseModel):
    id: UUID
    profile_id: UUID
    profile_name: str
    device_id: UUID | None
    device_group_id: UUID | None
    # `device`/`group`/`org` -- §0.2's resolution level, named server-side so
    # the console never has to infer it from which id is NULL.
    level: ResolutionLevel
    created_by: UUID | None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Alerts (§2.4, §2.5, §13)
# --------------------------------------------------------------------------- #


class AlertOut(BaseModel):
    """The console row (§13).

    `state` AND `cleared_reason` ARE BOTH CARRIED, ALWAYS. An acknowledged
    alert and a cleared alert are different states -- one is owned and still
    open, the other is over -- and a muted alert is STILL OPEN, which is why
    `muted_until` rides alongside rather than replacing the state. Collapsing
    any of the three into "handled" is the failure mode §0.8 calls out.
    """

    id: UUID
    device_id: UUID
    device_hostname: str
    device_group_id: UUID | None
    check_id: UUID
    profile_id: UUID
    check_type: str
    selector: str
    severity: Severity
    state: AlertState
    observed_value: str
    threshold_display: str
    opened_at: datetime
    opened_policy_version: int
    last_seen_at: datetime
    occurrence_count: int
    flapping: bool
    flap_count: int
    escalated_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: UUID | None
    muted_until: datetime | None
    muted_by: UUID | None
    cleared_at: datetime | None
    cleared_reason: ClearedReason | None
    cleared_by: UUID | None
    ticket_id: UUID | None
    previous_alert_id: UUID | None


class AlertSummaryOut(BaseModel):
    """`GET /health/alerts/summary` -- the TOTAL, which the alert list is not.

    WHY THIS EXISTS AS A ROUTE AT ALL. The console's health glyph had no count
    endpoint to read, so it fetched one page of `GET /health/alerts` (200 rows)
    and filtered it in the browser through the module's own definition of open.
    That is a floor, and on a fleet with more than a page of open alerts a
    silent one -- the badge said "at least 40 needing attention" because "40"
    would have been a claim it could not make.

    `open` COUNTS ACKNOWLEDGED AND MUTED ALERTS (§0.8). An acknowledged alert
    is owned and still open; a muted one is silenced and still open. Only
    `cleared` is over.

    EVERY BUCKET IS PRESENT AND ZERO. "No critical alerts are open" and "this
    server does not report criticals" must not be the same document.

    `worst_severity` IS NULL ONLY WHEN NOTHING IS OPEN. It is never "unknown":
    a caller who could not take the reading gets an error status, not a body.
    """

    open: int
    by_state: dict[OpenAlertState, int]
    by_severity: dict[Severity, int]
    worst_severity: Severity | None


class AlertActionOut(BaseModel):
    """One `health_alert_action` row and everything needed to read it (§13).

    `last_error` IS ON THE RESPONSE VERBATIM. For a `blocked_rate_limit` row the
    service writes which fuse tripped, its window, and the JOB IDS counted
    against it (`actions._fuse_breach`) -- and two approved actions sharing a
    `job_type` share one fuse, so "what consumed the budget" is not answerable
    from anything else on this row. Truncating it to a state badge is what makes
    a blocked remediation unexplainable.
    """

    id: UUID
    alert_id: UUID
    kind: ActionKind
    state: ActionState
    approved_action_id: UUID | None
    approved_action_key: str | None
    approved_action_name: str | None
    # §13: "a `require_approval` action renders the pinned script's name and
    # version -- nobody approves a script they cannot identify".
    script_id: UUID | None
    script_name: str | None
    script_version: int | None
    autonomy: Autonomy | None
    risk_class: RiskClass | None
    destructive: bool | None
    job_id: UUID | None
    ticket_id: UUID | None
    attempts: int
    next_attempt_at: datetime | None
    approved_by: UUID | None
    approved_at: datetime | None
    expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AlertDetailOut(AlertOut):
    profile_name: str
    ticket_number: str | None
    # §13: the predecessor's TICKET NUMBER, not just its id -- a chronic
    # flapper is only legible if the previous incident is reachable by the
    # number a human already has.
    previous_alert_ticket_number: str | None
    previous_alert_opened_at: datetime | None
    # Rendered in the BROWSER (§0.12). The server never fetches it, so it is not
    # an SSRF surface; `{hostname}` is substituted here because the template is
    # an org setting and the hostname is on this row.
    monitoring_deep_link: str | None
    actions: list[AlertActionOut]


class MuteIn(_StrictIn):
    """A mute is TIME-BOXED (§0.8): unbounded mutes are how a fleet accumulates
    permanently silent alarms everyone believes are still armed. The 30-day
    ceiling and the "must be in the future" rule belong to `alerts.mute` (which
    owns the message) and to `ck_health_alert_mute_bounded` (which owns the
    fact); neither is restated here, because both are relative to the alert's
    own `opened_at` and this model has never seen the alert."""

    muted_until: datetime


class RemediateIn(_StrictIn):
    """`POST /health/alerts/{id}/remediate` names an approved action by KEY.

    By key rather than by id because the key is what an operator recognises and
    what §8.2's registry is indexed on for humans; `dispatch_manual_remediation`
    resolves it org-scoped and 404s a key from another org.
    """

    action_key: str = Field(min_length=1, max_length=_MAX_KEY)


class AlertRefuseIn(_StrictIn):
    reason: str = Field(default="", max_length=_MAX_REASON)


class JobDispatchedOut(BaseModel):
    """What a route that put a signed job on an endpoint returns.

    The job id and nothing else: the envelope is signed bytes for the agent, and
    handing a browser the signature would invite a client to believe it could
    verify (or worse, replay) one.
    """

    job_id: UUID
    device_id: UUID


# --------------------------------------------------------------------------- #
# Settings (§2.8)
# --------------------------------------------------------------------------- #


class SettingsUpdate(_StrictIn):
    """Every field optional; `supplied()` is `exclude_unset`.

    The bounds restate `settings.SETTING_BOUNDS`, which restates migration
    0042's CHECK constraints. Three copies is two too many in general; here the
    outer two exist so a typo is a 422/400 naming the bound instead of a 500
    from an `IntegrityError` that names none, and `test_health_api.py` pins this
    model's bounds against `SETTING_BOUNDS` so they cannot drift apart.
    """

    flap_window_s: int | None = Field(default=None, ge=300, le=86400)
    flap_threshold: int | None = Field(default=None, ge=2, le=50)
    flap_cooldown_s: int | None = Field(default=None, ge=300, le=86400)
    unavailable_clear_after_s: int | None = Field(default=None, ge=3600, le=2592000)
    alert_retention_days: int | None = Field(default=None, ge=90, le=3650)
    escalate_after_s: int | None = Field(default=None, ge=300, le=604800)
    offline_after_s: int | None = Field(default=None, ge=300, le=86400)
    approval_ttl_s: int | None = Field(default=None, ge=300, le=604800)
    default_alert_queue_id: UUID | None = None
    default_ticket_category_id: UUID | None = None
    monitoring_deep_link_template: str | None = Field(default=None, max_length=500)
    auto_resolve_alert_tickets: bool | None = None

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class SettingsOut(BaseModel):
    flap_window_s: int
    flap_threshold: int
    flap_cooldown_s: int
    unavailable_clear_after_s: int
    alert_retention_days: int
    escalate_after_s: int
    offline_after_s: int
    approval_ttl_s: int
    default_alert_queue_id: UUID | None
    default_ticket_category_id: UUID | None
    monitoring_deep_link_template: str | None
    auto_resolve_alert_tickets: bool
    # `False` when this org has never written a row and is running on
    # `SETTING_DEFAULTS`. Without it the screen cannot tell "we chose 3600" from
    # "nobody has ever looked at this", and the two lead to different
    # conversations. Mirrors `sla.schema.SettingsOut.configured`.
    configured: bool
    # The consequence half: `declared_actions.declared_action_kinds` declares
    # `open_ticket` only when BOTH of the two ids above are set, so an org
    # without them gets alerts that never become tickets -- silently, unless
    # this is on the settings screen.
    alert_tickets_enabled: bool
    updated_at: datetime | None
    updated_by: UUID | None


# --------------------------------------------------------------------------- #
# Device health (§13, §3)
# --------------------------------------------------------------------------- #


class DeviceCheckStateOut(BaseModel):
    """One check's current state on one device.

    `unknown` IS NOT `ok`. A check the device has never answered has no
    `device_health_check_state` row, and reporting that as healthy is the single
    most dangerous default a monitoring module could pick (§0.7, §3).
    """

    check_id: UUID
    check_type: str
    selector: str
    comparator: Comparator
    threshold_display: str
    severity: Severity
    for_ticks: int
    for_seconds: int
    evaluated_by: EvaluatedBy
    state: DeviceCheckState
    last_value: str
    policy_version: int | None
    reported_at: datetime | None


class DeviceHealthOut(BaseModel):
    """`GET /devices/{device_id}/health` -- gated on `devices.read` (§10).

    `has_profile=False` IS A FIRST-CLASS ANSWER, never an empty check list that
    reads as "everything is fine": §3 leaves a device with no effective profile
    monitoring NOTHING, and the console must say so.

    `agent_online` IS REPORTED SEPARATELY FROM THE CHECK STATES because an
    offline device does not clear its alerts (§0.6, §12) -- a quiet device is
    not a healthy device, and the two facts must be readable side by side.
    """

    device_id: UUID
    hostname: str
    os_family: str | None
    has_profile: bool
    profile: ProfileOut | None
    # §0.2: "a resolution nobody can see is a resolution nobody trusts".
    assignment: AssignmentOut | None
    resolution_level: ResolutionLevel | None
    # The device's own acknowledgement (§2.11). `None` = no ack row at all,
    # which reads as version 0 and is why the reconcile sweep always re-pushes.
    acknowledged_policy_version: int | None
    policy_drift: bool
    checks: list[DeviceCheckStateOut]
    open_alerts: list[AlertOut]
    agent_last_seen_at: datetime | None
    agent_online: bool
    offline_after_s: int


# --------------------------------------------------------------------------- #
# Approved actions (§2.9, §2.10, §8)
# --------------------------------------------------------------------------- #


class ScriptExemptionOut(BaseModel):
    """§2.10, and §13's "first-class view".

    An exemption OUTLIVES the approved action that motivated it, so a later
    action pinning the same `(script_id, script_version)` inherits a grant made
    for a different purpose. That cost is accepted in §2.10 and mitigated by
    VISIBILITY -- which is this model. An invisible standing exemption is one
    nobody revokes.
    """

    id: UUID
    script_id: UUID
    script_name: str | None
    script_version: int
    reason: str
    granted_by: UUID
    granted_at: datetime


class ApprovedActionCreate(_StrictIn):
    """`destructive` IS ABSENT FROM THIS MODEL ON PURPOSE (§8.4).

    It is derived by `core.approved_actions.is_destructive` from the job type,
    the params template and the exemption, and stored. Sending it is refused --
    a 422 from `extra="forbid"` here, and the same refusal with a fuller message
    from `approved_actions._validate_and_derive` for any non-HTTP caller. It is
    NOT ignored: a caller who believes they set `destructive: false` and got a
    200 believes they disarmed the autonomy gate.
    """

    key: str = Field(min_length=1, max_length=_MAX_KEY)
    name: str = Field(min_length=1, max_length=_MAX_NAME)
    description: str = ""
    job_type: str = Field(min_length=1, max_length=100)
    params_template: dict[str, Any] = Field(default_factory=dict)
    script_id: UUID | None = None
    script_version: int | None = Field(default=None, gt=0)
    risk_class: RiskClass
    max_target_scope: MaxTargetScope = "device"
    autonomy: Autonomy
    personas: list[str] = Field(default_factory=lambda: ["health"])
    cooldown_s: int = Field(default=3600, ge=0)
    max_per_device_per_day: int = Field(default=3, gt=0)
    max_per_org_per_hour: int = Field(default=20, gt=0)
    active: bool = True

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ApprovedActionUpdate(_StrictIn):
    """Every field optional; `update_approved_action` merges over the stored row
    and RE-DERIVES `destructive` from the result."""

    key: str | None = Field(default=None, min_length=1, max_length=_MAX_KEY)
    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME)
    description: str | None = None
    job_type: str | None = Field(default=None, min_length=1, max_length=100)
    params_template: dict[str, Any] | None = None
    script_id: UUID | None = None
    script_version: int | None = Field(default=None, gt=0)
    risk_class: RiskClass | None = None
    max_target_scope: MaxTargetScope | None = None
    autonomy: Autonomy | None = None
    personas: list[str] | None = None
    cooldown_s: int | None = Field(default=None, ge=0)
    max_per_device_per_day: int | None = Field(default=None, gt=0)
    max_per_org_per_hour: int | None = Field(default=None, gt=0)
    active: bool | None = None

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ApprovedActionOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: str
    job_type: str
    # SERVED REDACTED. A `params_template` naming a credential is refused at
    # write time, but rows written before that gate shipped still carry one, and
    # `health.read` is a broad non-admin grant -- invariant #3 forbids returning
    # a stored credential in an API response whatever wrote it. Every
    # credential-named value here is replaced by `REDACTED_PLACEHOLDER`.
    params_template: dict[str, Any]
    # The dotted paths that were withheld, so the registry screen can tell an
    # admin WHICH key to rewrite. Empty for every row written since the write
    # gate shipped, which is the point: a non-empty list is a row to fix.
    params_template_redacted_keys: list[str] = Field(default_factory=list)
    script_id: UUID | None
    script_name: str | None
    script_version: int | None
    # The LIVE `scripts.version`. When it differs from the pinned one the action
    # will refuse to dispatch (`actions._refuse_reason`), so the registry screen
    # has to be able to show that before someone relies on it at 03:00.
    script_live_version: int | None
    risk_class: RiskClass
    # DERIVED AND STORED (§8.4). Read-only over the wire.
    destructive: bool
    max_target_scope: MaxTargetScope
    autonomy: Autonomy
    personas: list[str]
    cooldown_s: int
    max_per_device_per_day: int
    max_per_org_per_hour: int
    active: bool
    # §2.10 / §13. `None` when this action's pinned pair carries no exemption.
    script_exemption: ScriptExemptionOut | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class ExemptScriptIn(_StrictIn):
    """The `(script_id, script_version)` pair is NOT in this body (§10).

    It comes from the action's own pin, so an exemption can never be granted for
    a version the action does not name. Only the reason is caller-supplied, and
    it is audited.
    """

    reason: str = Field(default="", max_length=_MAX_REASON)

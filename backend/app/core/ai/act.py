"""SECURITY GATE: propose -> preview -> confirm -> dispatch.

docs/modules/ai-automation.md §0.6, §0.7, §0.11, §6, §7.1, §9.2.

**THE PROPERTY THIS MODULE ESTABLISHES: THERE IS NO PATH FROM A REQUEST BODY, OR
FROM MODEL OUTPUT, TO AN EFFECT.**

The only act-related tool any persona is offered is

    actions.propose(action_key: str, device_id: UUID, rationale: str)

and that signature IS the security design (§0.7). The model contributes a
SELECTION and prose. Job type, params, script pin, risk class, destructiveness
and the rate fuses all come from the `approved_action` row an org admin authored
through an audited, validated write path. Model output cannot construct an
effect; it can only select among effects a human already approved.

**Rejected: an act tool per job type with typed params** (`patch.apply(kbs=[...],
reboot=...)`). That is model output reaching `JobEngine.create`'s params, which
is the free-form-expression-from-the-server-side failure
`SUBSTITUTABLE_PARAM_KEYS`' own comment already refuses for OPERATOR templates --
and it would make this layer, rather than the org admin, decide what an action
does. `ProposeArgs` is `extra="forbid"`, so a model that emits `job_type` or
`params` is told that field does not exist, BY NAME, and the turn continues.

**A PREVIEW PERFORMS NO PART OF THE ACT** (§0.6). Nothing is signed, no agent is
contacted, `JobEngine.create` is not called, and where a read-only probe exists
for a job type (`patch.scan` before `patch.apply`) the preview may cite the most
recent EXISTING result and must not dispatch a fresh one -- dispatching a scan to
build a preview is performing an act to describe an act.

**THE PREVIEW IS PERSISTED, AND THAT IS THE CORRECTION OF IPAM'S IMPORT DIGEST.**
`app/modules/ipam/importer.py` records the finding in its own docstring: a digest
recomputed over one request's inputs "is a consistency check ... not proof that a
preview ever happened". IPAM judged that acceptable because its caller was
authorized to write every row anyway. The proposing party HERE is a language
model downstream of attacker-controlled text and the act is a signed job running
as SYSTEM/root, so the trade does not transfer: the proof that a preview happened
is the stored `ai_action` row, and the digest's job is only to catch that row
having changed underneath the human. Both halves are needed -- the row without
the digest confirms a stale act, the digest without the row confirms an act
nobody previewed.

**THE DEVICE VISIBILITY CHECK IS A LOOPBACK, NOT A QUERY** (§6.1 step 5), so
`require_permission` and RLS decide rather than this module. It goes through the
persona's own executor, which carries the caller's own bearer token back through
the whole ASGI stack -- the same control the read tools use, for the same reason.
See `_DEVICE_PROBE`'s comment for why its `response_fields` is deliberately
empty.
"""

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.autonomy import resolve_action_autonomy
from app.core.ai.errors import AiError, AiNotFoundError, AiValidationError
from app.core.ai.executor_staff import ToolError
from app.core.ai.personas import LoopbackExecutor
from app.core.ai.types import ReadTool, ToolResult, ToolSpec, TurnContext
from app.core.approved_actions import (
    ApprovedAction,
    fuse_breach,
    fuse_breach_worded,
    get_approved_action,
    get_script_exemption,
    is_destructive,
    non_approvable_reason,
    redact_secret_values,
    render_params_template,
)
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.jobs.canonical import canonical_bytes, rfc3339
from app.jobs.dispatch_permissions import dispatch_permission_for
from app.models import Device, Job, Script
from app.models.ai import AI_ACTION_REJECTION_REASONS, AiAction

__all__ = [
    "DEFAULT_ACTION_EXPIRY_S",
    "MAX_PROPOSALS_PER_TURN",
    "ActConflict",
    "ActError",
    "ActExpired",
    "ActForbidden",
    "ActRefused",
    "ActTool",
    "ConfirmOutcome",
    "PreviewOut",
    "ProposeArgs",
    "ProposeBinding",
    "build_preview",
    "compute_preview_digest",
    "confirm_action",
    "digest_fields",
    "list_actions",
    "load_preview",
    "propose",
    "reject_action",
]

#: §6.4's default preview clock and §6.5's default per-turn cap. Both are
#: org-configurable within CHECK-bounded ranges on `ai_settings`; these are the
#: values an org with no row gets.
DEFAULT_ACTION_EXPIRY_S = 900
MAX_PROPOSALS_PER_TURN = 3


class ActError(AiError):
    """Base for the act path's own failures."""


class ActRefused(ActError):
    """The proposal was refused. MODEL-FACING, and the turn continues."""


class ActConflict(ActError):
    """The previewed act and the current act are different acts -- 409.

    NOT a 400: the request is well formed, the world moved. A catalogue edit, a
    lowered dial, a bumped script version, a breached fuse and a second confirm
    all land here, and the message names which.
    """


class ActExpired(ActError):
    """`expires_at` passed -- 410, and the row becomes `expired`."""


class ActForbidden(ActError):
    """The principal does not hold what §9.2 requires -- 403, AND NO JOB.

    Raised for all three parts of the confirm check, including the second one:
    a job type whose owning module's route requires a permission this principal
    does not hold is not confirmable here, and neither is a job type nobody has
    classified.
    """


class ProposeArgs(BaseModel):
    """The ONLY act-related arguments any model may emit (§0.7).

    `extra="forbid"` is load-bearing rather than tidy: a model that emits
    `job_type` or a `params` blob -- because retrieved text told it to -- is told
    THAT FIELD DOES NOT EXIST, by name, and the turn continues. There is no field
    here through which model output could reach `JobEngine.create`.
    """

    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1, max_length=100)
    device_id: UUID
    rationale: str = Field(default="", max_length=2000)


class _DeviceProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: UUID


#: The loopback probe that answers "may this principal see this device".
#:
#: **`response_fields` IS EMPTY ON PURPOSE, AND EMPTY MEANS "BLOCK EVERY KEY".**
#: This is a probe for AUTHORIZATION, not a read: what it needs is the STATUS the
#: real route returns to the real caller's real token, and the device's body must
#: not enter model context through a tool the model did not call. So the scrubber
#: blocks the payload, `_device_is_visible` looks only at whether the outcome was
#: a 403 or a 404, and the hostname the preview displays is read separately, on
#: the server, AFTER the loopback has already said this principal may see the row.
#:
#: It goes through the persona's own executor rather than through an HTTP client
#: of its own: `tests/test_ai_boundaries.py` asserts this package has exactly two
#: client construction sites, and a third one here would be a third egress path
#: nobody counted.
_DEVICE_PROBE = ReadTool(
    name="devices.get",
    method="GET",
    path="/api/v1/devices/{device_id}",
    permission="devices.read",
    args=_DeviceProbeArgs,
    hint="internal visibility probe; never offered to a model",
)

_DEVICE_PROBE_REGISTRY: Mapping[str, ReadTool] = {_DEVICE_PROBE.name: _DEVICE_PROBE}


class _JobEngineLike(Protocol):
    async def create(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        type: str,
        target_device_id: UUID,
        params: dict[str, Any],
        requested_by: UUID | None = ...,
        ttl_s: int | None = ...,
    ) -> Job: ...

    async def signed_envelope(self, job: Job) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProposeBinding:
    """The turn-scoped facts a proposal needs and `TurnContext` does not carry.

    Built per turn by the persona's router, never stored: `persona` and
    `ai_session_id` decide what may be proposed at all, and a binding that
    outlived one turn would be a binding whose `message_seq` no longer describes
    where the proposal came from.
    """

    persona: str
    ai_session_id: UUID
    message_seq: int
    actor_id: UUID
    #: §7.3. True when `detect()` fired on anything this turn retrieved, or when
    #: any read touched a record whose provenance is unauthenticated ingress
    #: (`ticket.origin='email'`, a comment created by `email_intake`).
    #:
    #: **TAINT CAPS `effective_autonomy` AT `require_approval`, WHICH IS ALREADY
    #: THE V1 GLOBAL CLAMP -- SO ITS REAL WORK TODAY IS VISIBILITY** (§7.3). It
    #: is displayed prominently on the confirmation screen and carried into
    #: `ai.action_proposed`'s metadata. That is stated rather than overclaimed,
    #: and stated HERE, because a control whose effect is currently a no-op gets
    #: deleted by somebody tidying up unless the reason it exists is written
    #: down.
    tainted: bool = False
    #: §6.1 step 6: the technician copilot may only propose against the device
    #: its ticket links.
    #:
    #: **TWO FIELDS, NOT ONE, AND THE SECOND EXISTS BECAUSE ONE FAILED OPEN.**
    #: `ticket_device_id is not None` cannot distinguish "this persona has no
    #: ticket context" (the admin persona, correctly unbounded) from "this
    #: conversation IS about a ticket and that ticket names no device" -- and
    #: §3.2 says the second must propose NOTHING ("if the ticket has no linked
    #: device, the copilot cannot propose -- it can only say so"). With one
    #: field, a copilot turn on a device-less ticket skipped the check entirely
    #: and could prepare an action against any device its technician could see,
    #: which makes "the ticket" decorative in exactly the case §3.2 names. Found
    #: by the copilot's own test; the fix is `ticket_bound`, which says the
    #: BINDING applies, so a missing device is a refusal rather than an absence.
    ticket_bound: bool = False
    ticket_device_id: UUID | None = None
    #: The ticket this turn is bound to, carried for the AUDIT and for nothing
    #: else -- no check below reads it (§19.20).
    #:
    #: **THE BINDING IS RE-DERIVED EVERY TURN AND IS THEREFORE NEVER STALE, BUT
    #: NOTHING DURABLE SAID WHICH TICKET IT WAS.** `ai_session` carries no
    #: `ticket_id` (§5.3 forbids adding one) and `ai_action` records
    #: `session_id` and `message_seq`, so "which ticket authorized this device"
    #: -- the entire justification for the one device a ticket-bound persona may
    #: name -- was recoverable from nothing. Invariant #4 wants the privileged
    #: action recorded; a proposal whose authorizing constraint is unrecorded is
    #: recorded halfway. It rides on the EVENT rather than on a column: an audit
    #: event is per-turn, which is exactly the scope of the binding it describes.
    #:
    #: `None` for an unbounded persona, and the key is then ABSENT from the
    #: metadata rather than null -- `"ticket_id": null` on an admin proposal
    #: reads as "this was about a ticket and we lost it".
    ticket_id: UUID | None = None
    max_proposals_per_turn: int = MAX_PROPOSALS_PER_TURN
    action_expiry_s: int = DEFAULT_ACTION_EXPIRY_S


@dataclass(frozen=True)
class PreviewOut:
    """§6.2, rendered ENTIRELY from the stored catalogue row and the resolved
    target -- never from the model's prose.

    `rationale` is carried in ITS OWN FIELD, separately from every
    catalogue-derived fact, because §6.3 makes the LAYOUT a security property: a
    UI that lets the model's argument describe the action re-introduces the whole
    injection problem at the confirmation step, and a laundered instruction
    rendered as an authoritative action description is a confirmation the human
    did not really give. The separation is specified here rather than left to the
    frontend.
    """

    ai_action_id: UUID
    approved_action_id: UUID
    approved_action_key: str
    name: str
    description: str
    job_type: str
    rendered_params: dict[str, Any]
    redacted_param_paths: list[str]
    script_name: str | None
    script_version: int | None
    target_device_id: UUID
    target_hostname: str
    risk_class: str
    destructive: bool
    effective_autonomy: str
    tainted: bool
    fuse_verdict: str | None
    expires_at: datetime
    preview_digest: str
    state: str
    #: The assistant's argument. Nothing else in this dataclass comes from it.
    rationale: str = ""


@dataclass(frozen=True)
class ConfirmOutcome:
    """What a successful confirm produced, and what the CALLER must publish.

    **THE ENVELOPE IS RETURNED, NOT PUBLISHED HERE.**
    `app/modules/deploy/scripts.py::run_script` states the contract and health's
    routes follow it: a job published to Redis before the caller's tenant
    transaction commits could be handed to an agent with no durable server record
    if that commit later failed. The envelope is built inside the transaction;
    publishing happens after the block exits.
    """

    ai_action_id: UUID
    job_id: UUID
    envelope: dict[str, Any]


# --------------------------------------------------------------------------- #
# The digest (§6.4)
# --------------------------------------------------------------------------- #


def _aware(value: datetime) -> datetime:
    """Postgres hands these back tz-aware; a naive one would make `rfc3339`
    raise, which is the right direction but a poor error. Normalized once, here,
    rather than at four call sites."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def digest_fields(row: AiAction, catalogue_row: ApprovedAction) -> dict[str, Any]:
    """The exact field map §6.4 hashes, with every value already canonical.

    **CONSTRAINT: `canonical_bytes` RAISES `TypeError` ON A `UUID` AND ON A
    `datetime`.** It is `app/jobs/canonical.py::canonical_bytes`, whose
    `default=` handler rejects anything `json` cannot serialize BY DESIGN, so a
    formatting ambiguity can never silently change signed bytes. Every uuid here
    is therefore `str(...)`, and `approved_action_updated_at` is an `isoformat()`
    string -- see the correction below for why it is NOT `rfc3339(...)`.
    This is not a style note -- it is the failure mode this function exists to
    avoid, and a test hands the raw types straight to `canonical_bytes` and
    requires the raise, so the guard is proved to be `canonical_bytes` rather
    than a `str()` somewhere incidental.

    `approved_action_updated_at` IS THE FIELD THAT MAKES §6.4 TRUE. Without it,
    editing the catalogue row between preview and confirm changes what would run
    and does NOT change the digest, so "editing the catalogue invalidates the
    confirmation" would be prose rather than a property.

    **AND IT IS FORMATTED WITH FULL PRECISION, NOT BY `rfc3339`, WHICH IS A
    CORRECTION.** `app/jobs/canonical.py::rfc3339` renders `%H:%M:%SZ` --
    truncated to WHOLE SECONDS -- and `ApprovedAction.updated_at` carries
    `onupdate`, so an edit landing in the same wall-clock second as the row's
    previous update produced an IDENTICAL digest and the confirm proceeded
    against the edited row. §6.4's claim was therefore true only at one-second
    granularity. The window is small and nobody can steer it, but a digest whose
    job is to detect an edit must detect every edit, so this field is an
    `isoformat()` string (microsecond precision, still a `str`, so
    `canonical_bytes` is satisfied for the reason constraint 9 states).

    Found by `tests/test_ai_act_confirm.py::test_a_fuse_that_passed_at_preview_
    and_is_breached_at_confirm_refuses` flaking in a full-suite run: under load
    the preview and the catalogue edit straddled a second boundary, the digest
    changed, and a test about the FUSE got the CATALOGUE-EDITED conflict instead.
    The flake was the symptom; the truncation was the defect.

    THE COST, STATED: a `preview_digest` stored by a previous release does not
    match a recomputation by this one, so previews in flight across the deploy
    are 409s. That is fail-closed -- a refusal, never a dispatch -- and they
    expire within `action_expiry_s` anyway (bounded at 24h by a CHECK).
    """
    return {
        "ai_action_id": str(row.id),
        "approved_action_id": str(row.approved_action_id),
        "approved_action_updated_at": _aware(catalogue_row.updated_at)
        .astimezone(UTC)
        .isoformat(),
        "job_type": row.job_type,
        "rendered_params": dict(row.rendered_params or {}),
        "script_id": str(row.script_id) if row.script_id is not None else None,
        "script_version": row.script_version,
        "target_device_id": str(row.target_device_id),
        "risk_class": row.risk_class,
        "destructive": bool(row.destructive),
        "effective_autonomy": row.effective_autonomy,
    }


def compute_preview_digest(row: AiAction, catalogue_row: ApprovedAction) -> str:
    """sha256 over `digest_fields`, computed BY THE SERVER FROM THE STORED ROW.

    Never from a confirm request's body: the confirm carries an id and the digest
    the human was shown, and contributes no job type, no params and no target.
    """
    return hashlib.sha256(canonical_bytes(digest_fields(row, catalogue_row))).hexdigest()


# --------------------------------------------------------------------------- #
# Shared resolutions
# --------------------------------------------------------------------------- #


async def _device_is_visible(
    *, ctx: TurnContext, executor: LoopbackExecutor, device_id: UUID
) -> bool:
    """§6.1 step 5, THROUGH THE LOOPBACK.

    A query here would be this module deciding what the principal may see. The
    loopback re-enters the whole ASGI stack carrying the caller's own token, so
    `require_permission("devices.read")` and RLS decide -- and a device in
    another org comes back 404 rather than as a row.

    **`blocked` IS THE SUCCESS SIGNAL, AND IT IS THE ONLY ONE.** `blocked` means
    the route answered 200 and the SCRUBBER refused to hand the body onward,
    which is exactly what `_DEVICE_PROBE`'s empty `response_fields` asks for.
    `tests/test_ai_act_propose.py::test_the_probes_success_signal_really_is_
    blocked` pins that against the real route, because everything below rests
    on it.

    **AN ALLOWLIST, AND IT SHIPPED AS A DENYLIST -- WHICH FAILED OPEN.** The
    original was `kind not in {"forbidden", "not_found"}`: two of `ToolErrorKind`'s
    eight members, so it asked "did we get an explicit refusal" rather than "did
    we get an answer". `upstream_error`, `timeout`, `session_expired` (a 401),
    `unknown_tool` and `invalid_arguments` all returned VISIBLE, as did a
    `ToolResult`, which has no `kind` at all -- so ANY failure to obtain an
    answer authorized the proposal. Invariant #6 says authorization is
    server-side; a server that could not answer has authorized nothing.

    Written as an allowlist so the DEFAULT for a `ToolErrorKind` added later is
    refused. `test_only_a_blocked_probe_grants_visibility` parametrises over
    every member of the type rather than over the ones named here, so the new
    kind is covered the day it exists and is SEEN to default closed.
    """
    outcome = await executor(
        tool_name=_DEVICE_PROBE.name,
        raw_args={"device_id": str(device_id)},
        app=ctx.app,
        bearer=ctx.bearer,
        deadline=ctx.deadline,
        registry=_DEVICE_PROBE_REGISTRY,
    )
    return getattr(outcome, "kind", None) == "blocked"


async def _script_pin(
    session: AsyncSession, *, org_id: UUID, action: ApprovedAction
) -> tuple[UUID | None, int | None, str | None]:
    """The pinned `(script_id, script_version, name)` for a `script.run` row.

    The NAME and the VERSION, never the body (§6.2). Health's approval path
    already states the rule -- "nobody approves a script they cannot identify" --
    and the body is arbitrary code, which is why `script.run` is destructive by
    default.
    """
    if action.job_type != "script.run" or action.script_id is None:
        return None, None, None
    name = (
        await session.execute(
            select(Script.name).where(Script.org_id == org_id, Script.id == action.script_id)
        )
    ).scalar_one_or_none()
    return action.script_id, action.script_version, name


async def _rederive_destructive(
    session: AsyncSession, *, org_id: UUID, action: ApprovedAction
) -> bool:
    """`is_destructive` FROM THE PIN, never from the row's stored boolean.

    `approved_action.destructive` is a STORED column, and an exemption granted
    for version 2 does not follow a script edited to version 3 -- so a row can be
    stored non-destructive while the version it now pins has no exemption at all.
    Re-derived at preview AND again at confirm, because that gap is the whole
    reason `is_destructive` takes the pin as keyword arguments.
    """
    exemption = None
    if action.job_type == "script.run" and action.script_id is not None:
        exemption = await get_script_exemption(
            session,
            org_id=org_id,
            script_id=action.script_id,
            script_version=action.script_version or 0,
        )
    return is_destructive(
        action.job_type,
        action.params_template or {},
        exemption,
        script_id=action.script_id,
        script_version=action.script_version,
    )


async def _proposals_this_turn(
    session: AsyncSession, *, org_id: UUID, ai_session_id: UUID, message_seq: int
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(AiAction)
                .where(
                    AiAction.org_id == org_id,
                    AiAction.session_id == ai_session_id,
                    AiAction.message_seq == message_seq,
                )
            )
        ).scalar_one()
    )


# --------------------------------------------------------------------------- #
# Propose (§6.1) -- seven steps, in order, none skippable
# --------------------------------------------------------------------------- #


async def propose(
    session: AsyncSession,
    *,
    org_id: UUID,
    binding: ProposeBinding,
    ctx: TurnContext,
    executor: LoopbackExecutor,
    action_key: str,
    device_id: UUID,
    rationale: str = "",
    now: datetime | None = None,
) -> PreviewOut:
    """§6.1's seven steps, IN ORDER, none skippable. Raises `ActRefused` for
    anything the model is told about and the turn continues from.

    THE ORDER IS THE SECURITY PROPERTY, and each step is here for a stated
    reason:

    1. resolve the catalogue key -- an unknown key is a tool error, exactly as an
       unknown READ tool is today;
    2. refuse if the row does not carry this persona, or is inactive;
    3. refuse if `non_approvable_reason(job_type)` is not `None` -- RE-CHECKED
       here because the row may predate the allowlist, which the kernel's own
       comment anticipates in as many words ("a re-derivation over rows written
       before the allowlist existed");
    4. resolve the dial; `off` and `suggest` MINT NO ROW, and the model is told
       WHICH, truthfully -- an empty result would read as "nothing to do", which
       is the worse failure because it reads as an answer;
    5. verify the device is visible THROUGH THE LOOPBACK, and that the person
       holds `ai.act.propose` on ITS client;
    6. for a TICKET-BOUND persona, verify the ticket names a device AND that it
       is this one -- a ticket with no device proposes nothing, which is not the
       same as a persona with no ticket (§3.2);
    7. write the row, compute the digest FROM IT, and return the preview.
    """
    now = now or datetime.now(UTC)

    # 1.
    action = await get_approved_action(session, org_id=org_id, key=action_key)
    if action is None:
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"there is no approved action with key {action_key!r} in this organization"
        )

    # 2.
    if binding.persona not in (action.personas or []):
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"the approved action {action_key!r} is not available to this assistant"
        )
    if not action.active:
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"the approved action {action_key!r} is inactive"
        )

    # 3.
    refusal = non_approvable_reason(action.job_type)
    if refusal is not None:
        raise ActRefused(refusal)

    # 4.
    effective = await resolve_action_autonomy(
        session, org_id=org_id, persona=binding.persona, action=action
    )
    if effective == "off":
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"{action_key!r} is not permitted for this assistant at this organization's "
            f"current autonomy setting"
        )
    if effective == "suggest":
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"{action_key!r} may only be SUGGESTED at this organization's current autonomy "
            f"setting: say what you would run and why, but no action has been prepared and "
            f"there is nothing for anyone to confirm"
        )

    # The per-turn cap (§6.5), checked before anything is written.
    if (
        await _proposals_this_turn(
            session,
            org_id=org_id,
            ai_session_id=binding.ai_session_id,
            message_seq=binding.message_seq,
        )
        >= binding.max_proposals_per_turn
    ):
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            f"this turn has already prepared {binding.max_proposals_per_turn} actions, which "
            f"is the per-turn limit. Summarize what else you would suggest instead"
        )

    # 5.
    if not await _device_is_visible(ctx=ctx, executor=executor, device_id=device_id):
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            "no such device, or you do not have permission to see it"
        )
    # 5b. `ai.act.propose` ON THE MACHINE'S CLIENT (MSP clients plan
    # 2026-10-26 Task 14, T4-C2 item 11). The turn offers the act tool on the
    # code held on ANY client -- ergonomics -- and the model chooses the
    # machine; the proposal is that machine's client's row, so the person must
    # hold the code there. A machine this session cannot read is refused like
    # an invisible one.
    device_client = (
        await session.execute(
            select(Device.client_id).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device_client is None or not await has_permission(
        session, binding.actor_id, "ai.act.propose", client_id=device_client
    ):
        raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
            "you may not prepare actions on this device: proposing one needs "
            "ai.act.propose on the client it belongs to"
        )

    # 6.
    if binding.ticket_bound:
        if binding.ticket_device_id is None:
            raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
                "this ticket does not name a device, so there is no machine an action could "
                "target. Say so, and ask which device this is about"
            )
        if binding.ticket_device_id != device_id:
            raise ActRefused(  # i18n-exempt: model-facing, tool result read by the model
                "this conversation is about one ticket, and an action may only target the "
                "device that ticket names"
            )

    # 7.
    rendered = render_params_template(
        action.job_type, action.params_template or {}, device_id=device_id, selector=""
    )
    script_id, script_version, _ = await _script_pin(session, org_id=org_id, action=action)
    destructive = await _rederive_destructive(session, org_id=org_id, action=action)

    row = AiAction(
        org_id=org_id,
        session_id=binding.ai_session_id,
        message_seq=binding.message_seq,
        persona=binding.persona,
        approved_action_id=action.id,
        approved_action_key=action.key,
        job_type=action.job_type,
        target_device_id=device_id,
        rendered_params=rendered,
        script_id=script_id,
        script_version=script_version,
        risk_class=action.risk_class,
        destructive=destructive,
        effective_autonomy=effective,
        tainted=binding.tainted,
        rationale=rationale,
        state="proposed",
        expires_at=now + timedelta(seconds=binding.action_expiry_s),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()

    row.preview_digest = compute_preview_digest(row, action)
    row.state = "previewed"
    await session.flush()

    preview = await build_preview(session, org_id=org_id, row=row, action=action, now=now)

    # §8.2. BOUNDED IDENTIFIERS ONLY -- `rationale` is deliberately absent, and
    # §8.3 spends a paragraph on why: the audit chain is permanent and
    # un-redactable, and this text is downstream of whatever the model read.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=binding.actor_id,
        action="ai.action_proposed",
        target_type="ai_action",
        target_id=str(row.id),
        metadata={
            "ai_action_id": str(row.id),
            "session_id": str(row.session_id),
            "persona": row.persona,
            "approved_action_id": str(row.approved_action_id),
            "approved_action_key": row.approved_action_key,
            "job_type": row.job_type,
            "target_device_id": str(row.target_device_id),
            "risk_class": row.risk_class,
            "destructive": row.destructive,
            "effective_autonomy": row.effective_autonomy,
            "tainted": row.tainted,
            "preview_digest": row.preview_digest,
            # Present only for a ticket-bound persona; see `ProposeBinding.
            # ticket_id` for why absent beats null here.
            **({"ticket_id": str(binding.ticket_id)} if binding.ticket_id else {}),
        },
    )
    return preview


async def build_preview(
    session: AsyncSession,
    *,
    org_id: UUID,
    row: AiAction,
    action: ApprovedAction,
    now: datetime | None = None,
) -> PreviewOut:
    """§6.2. Every field comes from the stored catalogue row or the resolved
    target; `rationale` rides in its own field and describes nothing.

    `redact_secret_values` IS APPLIED ON THIS READ PATH. The registry's write
    gate refuses a `params_template` naming a credential and does nothing at all
    about rows written before it shipped -- so a legacy row's `repo_key` would
    otherwise be rendered, in full, on a confirmation screen. The registry's own
    read-side redaction is REUSED rather than reimplemented, because two copies
    of that predicate is how a key refused on the way in gets served on the way
    out.

    THE FUSE VERDICT HERE IS INFORMATIONAL. `confirm_action` reads the same
    fuses again and THAT read is the gate (§6.5 step 3).
    """
    now = now or datetime.now(UTC)
    _, _, script_name = await _script_pin(session, org_id=org_id, action=action)
    hostname = (
        await session.execute(
            select(Device.hostname).where(
                Device.org_id == org_id, Device.id == row.target_device_id
            )
        )
    ).scalar_one_or_none() or ""
    redacted, redacted_paths = redact_secret_values(row.rendered_params or {})
    verdict = await fuse_breach(session, org_id, action, device_id=row.target_device_id, now=now)
    return PreviewOut(
        ai_action_id=row.id,
        approved_action_id=row.approved_action_id,
        approved_action_key=row.approved_action_key,
        name=action.name,
        description=action.description,
        job_type=row.job_type,
        rendered_params=redacted,
        redacted_param_paths=redacted_paths,
        script_name=script_name,
        script_version=row.script_version,
        target_device_id=row.target_device_id,
        target_hostname=hostname,
        risk_class=row.risk_class,
        destructive=row.destructive,
        effective_autonomy=row.effective_autonomy,
        tainted=row.tainted,
        fuse_verdict=verdict,
        expires_at=row.expires_at,
        preview_digest=row.preview_digest,
        state=row.state,
        rationale=row.rationale,
    )


def _preview_payload(preview: PreviewOut) -> dict[str, Any]:
    """What the MODEL is shown about a preview it just created.

    Deliberately smaller than what the HUMAN is shown: the model gets what it
    needs to talk about the proposal ("I have prepared X on Y; a human must
    confirm it") and nothing it did not already know. Feeding the rendered params
    back would put the catalogue's own values into model context for no purpose,
    and the rationale is text the model itself wrote.
    """
    return {
        "ai_action_id": str(preview.ai_action_id),
        "approved_action_key": preview.approved_action_key,
        "name": preview.name,
        "job_type": preview.job_type,
        "target_device_id": str(preview.target_device_id),
        "target_hostname": preview.target_hostname,
        "risk_class": preview.risk_class,
        "destructive": preview.destructive,
        "state": preview.state,
        "expires_at": rfc3339(_aware(preview.expires_at)),
        "fuse_verdict": preview.fuse_verdict,
        "awaiting": "a human with the permission this job type's own dispatch route requires",
    }


@dataclass(frozen=True)
class ActTool:
    """The ONE act tool, implementing the same `Tool` protocol as `ReadTool`.

    It returns `ToolResult(..., mutating=True)` -- the first time that field is
    reachable as `True`. `app/core/ai/types.py` reserved it from day one so that
    act mode would add no branch to the audit writer or the console, and this is
    the path that finally sets it.

    **IT IS BUILT PER TURN, NOT REGISTERED IN A PERSONA'S FROZEN TOOL SET.** A
    proposal needs the session id, the turn's `message_seq`, the acting user and
    this turn's taint -- facts about ONE TURN. A tool object carrying them that
    lived in `PersonaProfile.tools` would carry one turn's binding into the next,
    and `message_seq` would stop describing where the proposal came from. It also
    keeps `PersonaProfile.tools` a mapping of `ReadTool` only, which
    `tests/test_ai_boundaries.py` asserts.
    """

    org_id: UUID
    binding: ProposeBinding
    executor: LoopbackExecutor
    #: An async context manager factory yielding the tenant session the row is
    #: written in. A CALLABLE rather than an engine so the caller decides the
    #: tenancy -- the staff path passes `org_session`, and nothing here can
    #: widen it.
    session_factory: Any
    #: **TAINT IS A FACT ABOUT THE TURN SO FAR, NOT ABOUT THE TURN'S START**
    #: (§7.3). `ProposeBinding.tainted` is frozen when the binding is built,
    #: which is BEFORE any tool has run -- so a proposal made in the same turn
    #: as, and after, a read that tripped the injection heuristic would carry
    #: `tainted=False` and the confirmation screen would omit the one badge that
    #: says "this suggestion is downstream of hostile text". The callable is read
    #: at INVOKE time and can only ever raise the flag, never lower it.
    taint: Any = None
    name: str = "actions.propose"

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "Prepare one action from your organization's approved-action catalogue for a "
                "human to confirm. You name a catalogue KEY and one device; the job type, its "
                "parameters, the script version and the risk all come from the catalogue entry "
                "your administrator authored. Nothing runs until a human with the right "
                "permissions confirms the preview this returns."
            ),
            input_schema=ProposeArgs.model_json_schema(),
        )

    async def invoke(self, ctx: TurnContext, args: Mapping[str, Any]) -> ToolResult | ToolError:
        """Validate, then propose. **THIS RETURNS; IT DOES NOT RAISE.**

        Every failure a proposal can have -- a fabricated field name, an unknown
        catalogue key, a device the caller cannot see, a dial set to `off` -- is
        information the model can act on, so it comes back as a `ToolError` and
        the turn continues, exactly as a failed read tool does. An exception
        escaping here would end the turn BEFORE it was persisted, and a turn that
        read real data and was never persisted was never audited either.
        """
        try:
            parsed = ProposeArgs.model_validate(dict(args))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
            # NAMES THE FIELD. A model that emitted `job_type` or `params` --
            # because retrieved text told it to -- is told that field does not
            # exist, which is the truthful answer and the one §0.7 wants a
            # reader of the transcript to see.
            return ToolError(  # i18n-exempt: model-facing, tool result read by the model
                tool=self.name,
                kind="invalid_arguments",
                message=f"Argument {where!r} is not acceptable: {first.get('msg', 'invalid')}.",
            )

        binding = self.binding
        if self.taint is not None and not binding.tainted and self.taint():
            binding = replace(binding, tainted=True)

        try:
            async with self.session_factory() as session:
                preview = await propose(
                    session,
                    org_id=self.org_id,
                    binding=binding,
                    ctx=ctx,
                    executor=self.executor,
                    action_key=parsed.action_key,
                    device_id=parsed.device_id,
                    rationale=parsed.rationale,
                )
                payload = _preview_payload(preview)
        except ActRefused as exc:
            return ToolError(tool=self.name, kind="forbidden", message=str(exc))
        except AiValidationError as exc:
            return ToolError(tool=self.name, kind="invalid_arguments", message=str(exc))

        # `mutating=True` FOR THE FIRST TIME IN THIS PLATFORM'S HISTORY. The
        # audit writer and the console already branch on it, so nothing
        # downstream changes shape.
        return ToolResult(
            payload=payload, status=201, result_count=1, truncated=False, mutating=True
        )


# --------------------------------------------------------------------------- #
# Confirm -> dispatch (§6.5, §9.2), and reject (§8.2)
# --------------------------------------------------------------------------- #


async def _load_for_update(session: AsyncSession, *, org_id: UUID, ai_action_id: UUID) -> AiAction:
    """The row, LOCKED, with an EXPLICIT `org_id` predicate in addition to RLS.

    A cross-org id is `AiNotFoundError` -- a 404, never a 403: a 403 would
    confirm the id exists somewhere, which is a cross-tenant existence oracle.
    The lock is what makes a second concurrent confirm lose rather than produce a
    second signed job against one row; `health/actions.py` records the same
    finding, having reproduced two jobs from one `dispatched` row.
    """
    row = (
        await session.execute(
            select(AiAction)
            .where(AiAction.org_id == org_id, AiAction.id == ai_action_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise AiNotFoundError(tr("no ai action {ai_action_id}", ai_action_id=ai_action_id))
    return row


async def confirm_action(
    session: AsyncSession,
    *,
    org_id: UUID,
    ai_action_id: UUID,
    digest: str,
    actor_id: UUID,
    permissions: Collection[str],
    job_engine: _JobEngineLike,
    ctx: TurnContext,
    executor: LoopbackExecutor,
    now: datetime | None = None,
) -> ConfirmOutcome:
    """§6.5, in ONE transaction. **The confirm carries an id and a digest and
    NOTHING ELSE** -- no job type, no params, no target. Everything dispatched is
    read back off the stored row.

    §9.2'S CHECK IS TWO-PART AND THE SECOND PART IS THE LOAD-BEARING ONE:

        require ai.act.confirm
        AND     require dispatch_permission_for(job_type)     # None => refuse
        AND     the confirming principal can read the target device (loopback)

    Without the second, a technician holding only `ai.act.confirm` could fire
    `patch.apply` here while being refused it on `POST /api/v1/patch/apply`. That
    is privilege escalation and it is invariant #6 violated.
    `dispatch_permission_for` returning `None` is a REFUSAL, not a pass: a job
    type nobody has classified is not confirmable through this layer.

    THE DIAL AND THE RATE FUSES ARE BOTH RE-EVALUATED HERE. The preview's fuse
    read was informational (§6.1); this one is the gate. The dial is org state an
    admin may change at any moment, and a confirmation is a decision about *now*.

    `permissions` ARE THE ONES HELD ON THE PROPOSAL'S CLIENT -- its machine's
    (MSP clients plan 2026-10-26 Task 14): the job lands there, and a grant on
    another client is no grant here. The route reads them so
    (`ai_admin.api._codes_on_action_client`).
    """
    now = now or datetime.now(UTC)
    held = set(permissions)

    if "ai.act.confirm" not in held:
        raise ActForbidden(tr("confirming an AI action requires ai.act.confirm"))

    row = await _load_for_update(session, org_id=org_id, ai_action_id=ai_action_id)

    if row.state != "previewed":
        raise ActConflict(
            tr(
                "this action is {state}, not awaiting confirmation. A second confirm is a "
                "conflict by the state machine, not an idempotent repeat",
                state=row.state,
            )
        )
    if now > _aware(row.expires_at):
        # **410, AND THE ROW IS LEFT ALONE HERE.** §6.4: "`now > expires_at` ->
        # 410, and THE SWEEP marks it `expired`." That is not a nicety -- this
        # function raises, its caller's tenant transaction rolls back, and a
        # `state = "expired"` written on this path would be rolled back with it.
        # A write that is always undone is worse than no write: it reads as a
        # state transition somebody can rely on, and the row would still be
        # `previewed` afterwards. The expiry sweep is what actually moves it, and
        # every later confirm re-checks the clock, so a row sitting `previewed`
        # past its expiry is not confirmable in the meantime.
        raise ActExpired(
            tr("the preview expired before it was confirmed; ask again and confirm the new preview")
        )

    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id,
                ApprovedAction.id == row.approved_action_id,
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise ActConflict(
            tr("the approved action this preview was built from no longer exists in the catalogue")
        )

    # **TWO COMPARISONS, BOTH AGAINST THE STORED DIGEST** (§6.4, §19.18).
    #
    # This shipped as `recomputed != digest` -- the server's recomputation
    # against the value the CALLER sent -- and `row.preview_digest` was never
    # read on this path at all. That made §6.4's invariant client-side: anybody
    # able to compute the current digest could confirm an act whose catalogue
    # row had been edited after the human saw it, and the server would agree
    # with itself. A test in this repo demonstrated the bypass working, using it
    # to get past a gate it was not testing.
    #
    # §6.4 names both halves and they are different failures:
    #
    #   * `recomputed != row.preview_digest` -- THE WORLD MOVED since the server
    #     issued this preview. Server-authoritative: the caller contributes
    #     nothing to it, so no client can wave it through.
    #   * `digest != row.preview_digest` -- the confirmation does not carry the
    #     digest of the preview that was issued. "The digest without the row
    #     confirms an act nobody previewed."
    #
    # A legacy row whose `preview_digest` is `''` (the column's server default)
    # fails the first and is not confirmable. That is fail-closed and already
    # stated as the cost of §19.15's format change.
    #
    # THE ORDER IS A CHOICE ABOUT THE MESSAGE, NOT ABOUT SAFETY. With both
    # present the two are interchangeable for the verdict -- the second forces
    # `digest == row.preview_digest`, so the first then says the same thing
    # whichever value it reads. What the order decides is which refusal an
    # operator is told about when both are true, and "the catalogue was edited"
    # is the fact they can act on; "your client sent the wrong digest" is
    # bookkeeping. A test pins the order for that reason.
    recomputed = compute_preview_digest(row, action)
    if recomputed != row.preview_digest:
        raise ActConflict(
            tr(
                "this preview no longer describes what would run: the approved-action "
                "catalogue entry has been edited since it was shown. Ask again and confirm the "
                "new preview",
            )
        )
    if digest != row.preview_digest:
        raise ActConflict(
            tr(
                "this confirmation does not carry the digest of the preview that was issued "
                "for this action, so it is not a confirmation of anything a human was shown. "
                "Open the preview again and confirm that",
            )
        )

    # Re-derived FROM THE PIN, never trusted from the stored boolean (§6.5 step
    # 1). An exemption granted for version 2 does not follow a script edited to
    # version 3, and the row could have been stored non-destructive before that.
    if await _rederive_destructive(session, org_id=org_id, action=action) != row.destructive:
        raise ActConflict(
            tr(
                "this action's destructiveness has changed since the preview (the pinned "
                "script version or its exemption moved). Ask again and confirm the new preview",
            )
        )

    current = await resolve_action_autonomy(
        session, org_id=org_id, persona=row.persona, action=action
    )
    if current != row.effective_autonomy:
        raise ActConflict(
            tr(
                "this organization's autonomy setting for this assistant has changed since the "
                "preview (now {current}, was {effective_autonomy})",
                current=repr(current),
                effective_autonomy=repr(row.effective_autonomy),
            )
        )

    required = dispatch_permission_for(row.job_type)
    if required is None:
        raise ActForbidden(
            tr(
                "{job_type} has no human dispatch route in this platform, so there is no "
                "permission that would authorize running it; it is not confirmable here",
                job_type=row.job_type,
            )
        )
    if required not in held:
        raise ActForbidden(
            tr(
                "confirming a {job_type} action additionally requires {required} -- the same "
                "permission that job type's own module route requires",
                job_type=row.job_type,
                required=required,
            )
        )

    if not await _device_is_visible(ctx=ctx, executor=executor, device_id=row.target_device_id):
        raise ActForbidden(tr("no such device, or you do not have permission to see it"))

    # THE GATE. The preview's read of the same fuses was informational.
    # Worded in the request's language: this text reaches only the 409.
    breach = await fuse_breach_worded(
        session, org_id, action, device_id=row.target_device_id, now=now
    )
    if breach is not None:
        raise ActConflict(tr("this action is currently rate-limited: {breach}", breach=breach))

    job = await job_engine.create(
        session,
        org_id=org_id,
        type=row.job_type,
        target_device_id=row.target_device_id,
        params=dict(row.rendered_params or {}),
        # THE HUMAN, because a human authorized it. There is no
        # `actor_type='ai'` anywhere in this platform (§8.2): the FACT that an AI
        # proposed it is `ai.action_proposed` existing at all.
        requested_by=actor_id,
    )

    row.state = "dispatched"
    row.job_id = job.id
    row.confirmed_by = actor_id
    row.confirmed_at = now
    row.updated_at = now
    await session.flush()

    seconds_since_preview = int((now - _aware(row.created_at)).total_seconds())
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ai.action_confirmed",
        target_type="ai_action",
        target_id=str(row.id),
        metadata={
            "ai_action_id": str(row.id),
            "confirmed_by": str(actor_id),
            "preview_digest": row.preview_digest,
            "seconds_since_preview": seconds_since_preview,
        },
    )
    # BESIDE the kernel's own job record, never instead of it -- the same
    # relationship `health.remediation_dispatched` has. The kernel record says a
    # job was sent; this one says why an AI proposed it and who agreed.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ai.action_dispatched",
        target_type="ai_action",
        target_id=str(row.id),
        metadata={
            "ai_action_id": str(row.id),
            "job_id": str(job.id),
            "approved_action_key": row.approved_action_key,
            "job_type": row.job_type,
            "target_device_id": str(row.target_device_id),
            "confirmed_by": str(actor_id),
            "tainted": row.tainted,
        },
    )

    envelope = await job_engine.signed_envelope(job)
    return ConfirmOutcome(ai_action_id=row.id, job_id=job.id, envelope=envelope)


async def list_actions(
    session: AsyncSession,
    *,
    org_id: UUID,
    ai_session_id: UUID | None = None,
    limit: int = 50,
) -> list[AiAction]:
    """This org's proposals, most recent first.

    ORG-WIDE, NOT PER USER, and gated on `ai.use` -- unlike `ai_session`, which
    is scoped to the administrator who started it. A proposal is not a private
    conversation: it is a pending request for somebody ELSE to confirm, and §9.2
    splits `ai.act.propose` from `ai.act.confirm` precisely so the confirming
    human is a different person from the proposing one. A list only its author
    could see would make the second grant unusable.

    THE `org_id` PREDICATE IS EXPLICIT AND IN ADDITION TO RLS (§9.1).
    """
    statement = select(AiAction).where(AiAction.org_id == org_id)
    if ai_session_id is not None:
        statement = statement.where(AiAction.session_id == ai_session_id)
    return list(
        (await session.execute(statement.order_by(AiAction.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )


async def load_preview(
    session: AsyncSession, *, org_id: UUID, ai_action_id: UUID, now: datetime | None = None
) -> PreviewOut:
    """One stored proposal, re-rendered as §6.2's preview.

    **RE-RENDERED, NOT REPLAYED.** The catalogue row is read again and the fuse
    verdict is evaluated again, so a screen opened ten minutes after the proposal
    shows what would run NOW -- which is what the human is being asked about. The
    `preview_digest` returned is still the STORED one: it is the digest the
    confirm compares against, and recomputing it here would make an edited
    catalogue row silently re-validate its own confirmation.

    A cross-org id is `AiNotFoundError` -- a 404, never a 403 (see
    `_load_for_update`).
    """
    row = (
        await session.execute(
            select(AiAction).where(AiAction.org_id == org_id, AiAction.id == ai_action_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AiNotFoundError(tr("no ai action {ai_action_id}", ai_action_id=ai_action_id))
    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.id == row.approved_action_id
            )
        )
    ).scalar_one_or_none()
    if action is None:  # pragma: no cover - the FK is NO ACTION, see spec §18.6
        raise AiNotFoundError(tr(
            "no approved action {approved_action_id}",
            approved_action_id=row.approved_action_id,
        ))
    return await build_preview(session, org_id=org_id, row=row, action=action, now=now)


async def reject_action(
    session: AsyncSession,
    *,
    org_id: UUID,
    ai_action_id: UUID,
    reason: str,
    actor_id: UUID,
    permissions: Collection[str],
    now: datetime | None = None,
) -> AiAction:
    """A human declines. `reason` is a BOUNDED ENUM, not free text (§8.2).

    Free text here would be a text box rendered beside the model's rationale at
    the exact moment a human is reading it, which is where that prose gets copied
    onto a durable row and then into whatever reads the row. §8.3 spends a
    paragraph refusing model-adjacent free text; this is the same refusal one
    field over.

    `permissions` are the ones held on the proposal's client, as in
    `confirm_action`.
    """
    now = now or datetime.now(UTC)
    if "ai.act.propose" not in set(permissions):
        raise ActForbidden(tr("declining an AI action requires ai.act.propose"))
    if reason not in AI_ACTION_REJECTION_REASONS:
        raise AiValidationError(
            tr(
                "{reason} is not a rejection reason; expected one of "
                "{ai_action_rejection_reasons}",
                reason=repr(reason),
                ai_action_rejection_reasons=AI_ACTION_REJECTION_REASONS,
            )
        )

    row = await _load_for_update(session, org_id=org_id, ai_action_id=ai_action_id)
    if row.state != "previewed":
        raise ActConflict(tr("this action is {state}, not awaiting a decision", state=row.state))

    row.state = "rejected"
    row.rejected_reason = reason
    row.updated_at = now
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ai.action_rejected",
        target_type="ai_action",
        target_id=str(row.id),
        metadata={
            "ai_action_id": str(row.id),
            "rejected_by": str(actor_id),
            "rejected_reason": reason,
        },
    )
    return row

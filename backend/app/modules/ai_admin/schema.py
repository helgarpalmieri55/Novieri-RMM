"""Request and response models for the AI admin agent's API (spec §6.1).

**NO MODEL HERE CARRIES A URL, A HOST, OR A KEY.** §0.10: the provider, its
model and its key are server configuration; nobody types a base URL and nobody
sees key material. That is not enforced by validation, it is enforced by the
absence of a field -- `test_ai_admin_providers.py` asserts over every Pydantic
model in this package that none declares `base_url`, `url`, `endpoint`,
`api_key`, `api_key_path` or `host`. Removing the SSRF primitive beats guarding
it, and a field that does not exist cannot be guarded wrongly.

**AND SINCE §0.14, NO MODEL HERE CARRIES A PROVIDER SELECTION EITHER.** The
operator's decision of 2026-08-25 makes the deployment's `ITS_AI_*` environment
the one provider for every organization on it, so `SettingsUpdate` has no
`provider_key` to write and no `enabled` to flip. `ProviderOut` survives as a
READ: it exposes `key`, `kind` and `model` and nothing else, so an `ai.admin`
holder can see where this deployment sends -- which is disclosure, not choice,
and is why a base URL still has no place in it. A base URL in that response is a
base URL in a browser's network tab, in a bug report, and in a screenshot.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AiStatus(BaseModel):
    """What `GET /ai/status` tells a caller who holds only `ai.use`.

    Two fields, deliberately. §10.6's disabled panel needs to say WHY the chat
    is unavailable; it does not need -- and an `ai.use` holder is not entitled
    to -- the provider key, the model id, or anything about the server's
    configuration.
    """

    enabled: bool
    reason: str = ""


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    persona: str
    mode: str
    model_id: str
    created_at: datetime
    last_turn_at: datetime
    message_count: int
    total_input_tokens: int
    total_output_tokens: int


class SessionCreate(BaseModel):
    """What a client may choose about a new conversation: its MODE, and nothing
    else.

    **`mode` IS FIXED AT CREATION AND THERE IS NO PROMOTION PATH** (§0.8). It is
    an input here and appears in no other request body on this surface, because
    promoting a session would import a whole read-mode transcript -- assembled
    under read mode's replay rules, where a laundered instruction was flagged and
    replayed verbatim -- into a context that can now propose an act.

    **`persona` IS NOT A FIELD, AND THAT IS A RECORDED DEVIATION FROM §12's ROUTE
    TABLE**, which writes "`POST /ai/sessions` (gains `mode`, `persona`)". This
    router IS the admin persona's surface; it passes `ADMIN_PROFILE.name`. A
    client-supplied persona here would let an administrator open a session
    LABELLED `technician` that is served by the admin persona's tool set and
    system prompt -- a persona name that lies, on the column the autonomy dial,
    the audit events and every `ai_action` row are keyed by. The other two
    personas' surfaces are their own routers (§12), which is where their persona
    names come from.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["read", "act"] = "read"


class MessageOut(BaseModel):
    """One transcript row. `tool_calls` carries `{tool, arg_hash, result_count,
    truncated, status}` and NO RESULT PAYLOAD (§3.2)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    flags: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    created_at: datetime


class TranscriptOut(BaseModel):
    session: SessionOut
    messages: list[MessageOut]


class TurnRequest(BaseModel):
    """`turn_id` IS OPTIONAL AND CLIENT-SUPPLIED, and that is deliberate.

    The SSE side-channel is named `ai:turn:{session_id}:{turn_id}`, so a client
    that wants to watch a turn arrive must be able to subscribe BEFORE the turn
    starts -- and a server-allocated id is only knowable after the `POST`
    returns, by which time the answer is already in hand and the stream has
    nothing left to carry. So the client may choose the id, open the stream, and
    then submit.

    **THE ID YOU CHOOSE NAMES YOUR STREAM; IT IS NOT THE ID THE TURN IS STORED
    UNDER.** `TurnOut.turn_id` carries that one, and it is derived from this
    value inside the organization's own id space (`api._stored_turn_id`) because
    `ai_message.id` is unique across the entire deployment while every query
    that can see it is tenant-scoped. A raw client id as the primary key meant a
    turn id already used by ANOTHER TENANT read as free and then violated the
    key at persist time, after the model had been paid -- and made the three
    outcomes an existence oracle over other tenants' message ids.

    **THE ID IS NOT A CAPABILITY, BECAUSE IT IS ONLY HALF OF A NAME.** The SSE
    channel is `ai:turn:{session_id}:{turn_id}` and the stream route verifies
    that the CONVERSATION belongs to the caller and to the token's org before it
    subscribes, so an id chosen -- or guessed, or copied -- names a channel
    under the caller's own conversations and nothing else. This was written down
    here before it was true: the route once subscribed on the turn id alone, and
    owning any conversation was enough to read any turn in the deployment.
    An id already used by a stored message is refused with a 400.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=8000)
    turn_id: UUID | None = None


class FlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    message_seq: int
    source_tool: str
    source_type: str
    source_id: str
    matched_rule: str
    excerpt: str
    created_at: datetime


class TurnOut(BaseModel):
    """The complete answer. **The `POST` is authoritative** -- SSE is an
    optional side-channel, and a client that never opens the stream loses
    nothing."""

    # THE ID OF THE STORED TURN -- `ai_message.id` of the question row, which is
    # what the console matches its live flagged records against. When the client
    # supplied a `TurnRequest.turn_id` this is DERIVED from it and differs from
    # it; see `TurnRequest`.
    turn_id: UUID
    answer: str
    stop_reason: str
    partial: bool
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    duration_ms: int
    model_id: str
    # §8.4's third obligation: the answer names the records whose text was
    # addressed to an automated reader. Counts and record ids only -- the prose
    # is in `ai_injection_flag`, for admins with `ai.admin`.
    flagged: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SettingsOut(BaseModel):
    """The org's dials. **Whether the assistant runs is not one of them**
    (§0.14) -- that is `GET /ai/status`, which answers from the deployment's
    provider configuration."""

    model_config = ConfigDict(from_attributes=True)

    transcript_retention_days: int
    max_turns_per_user_per_minute: int
    max_turns_per_org_per_hour: int
    daily_token_budget: int | None
    # docs/modules/ai-automation.md §6.4, §6.5.
    action_expiry_s: int
    max_proposals_per_turn: int


class SettingsUpdate(BaseModel):
    """**NOTHING ON THIS REQUEST CAN TURN THE ASSISTANT ON OR AIM IT** (§0.14).

    It used to carry three such fields: `enabled`, `provider_key`, and a
    required `egress_acknowledged` that the service refused any enabling write
    without. All three are gone, because the operator's decision of 2026-08-25
    moved reach into `.env`: the provider and its key are chosen once by whoever
    runs the control plane, and the console neither picks nor displays them.

    The acknowledgement moved rather than vanished -- setting `ITS_AI_*` IS the
    consent, and it is a deliberate, server-side, reviewable act. The console
    still shows the disclosure of what leaves; `extra="forbid"` is what makes a
    client that keeps sending `egress_acknowledged` fail loudly instead of
    having its tickbox quietly ignored.
    """

    model_config = ConfigDict(extra="forbid")

    transcript_retention_days: int | None = None
    max_turns_per_user_per_minute: int | None = None
    max_turns_per_org_per_hour: int | None = None
    daily_token_budget: int | None = None
    action_expiry_s: int | None = None
    max_proposals_per_turn: int | None = None


class ProviderOut(BaseModel):
    """The provider this deployment is configured with, as an `ai.admin` holder
    may see it. **Key, kind and model name only** -- never the base URL, never
    key material.

    A read, not a choice (§0.14): the list it appears in holds at most one
    entry, and the console renders it as "this is where your data goes" beside
    the egress disclosure. Nothing writes it back."""

    key: str
    kind: str
    model: str


class CeilingOut(BaseModel):
    """One stored `ai_autonomy_ceiling` row, as the console renders it.

    THE VOCABULARY IS NOT RESTATED HERE. `persona`, `risk_class` and `level` are
    plain strings on this model and are validated by the KERNEL
    (`app/core/ai/autonomy.py::set_ceiling`) against
    `app/models/ai.py`'s own constants, which are in turn the constants the
    database CHECKs are written from. Three copies of a closed vocabulary is
    three chances for one of them to be widened alone -- and a persona package
    listing the other personas' names is exactly what
    `test_ai_admin_boundaries.py` refuses, for the same reason.
    """

    persona: str
    risk_class: str
    level: str
    updated_by: UUID | None = None
    updated_at: datetime | None = None


class CeilingUpdate(BaseModel):
    """`PUT /ai/autonomy`: one `(persona, risk_class)` at a time.

    Not a bulk replace. A whole-table PUT means a console that has not been
    refreshed can silently reset a ceiling an administrator raised in another
    tab, and the audit event for that would say "changed" without saying which
    of nine values somebody actually meant to touch.
    """

    model_config = ConfigDict(extra="forbid")

    persona: str
    risk_class: str
    level: str


# --------------------------------------------------------------------------- #
# Act mode (docs/modules/ai-automation.md §6.2, §6.3, §12).
# --------------------------------------------------------------------------- #


class ActionOut(BaseModel):
    """One proposal, in the shape a LIST renders.

    **`rationale` IS ABSENT FROM THE LIST ON PURPOSE.** §6.3 makes the layout a
    security property, and the separation it demands -- catalogue-derived facts in
    one region, the model's argument in another, clearly labelled -- is only
    possible on a screen that has two regions. A list row has one, and a
    laundered instruction rendered as a line in a queue of pending actions is an
    authoritative-looking action description with nothing beside it to say
    otherwise. The rationale is served by the DETAIL route, where the confirm
    screen can put it where it belongs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    persona: str
    approved_action_key: str
    job_type: str
    target_device_id: UUID
    risk_class: str
    destructive: bool
    effective_autonomy: str
    tainted: bool
    state: str
    expires_at: datetime
    confirmed_by: UUID | None = None
    rejected_reason: str | None = None
    job_id: UUID | None = None
    created_at: datetime


class ActionPreviewOut(BaseModel):
    """§6.2's preview, and §6.3's two regions AS TWO GROUPS OF FIELDS.

    Everything above `rationale` is derived from the stored catalogue row and the
    resolved target. `rationale` is the model's prose and describes nothing: the
    console renders it in a visually distinct, clearly-labelled region, as TEXT
    and never as markup, and never uses it to compose the first region. A UI that
    lets the second region describe the first re-introduces the whole injection
    problem at the confirmation step -- a laundered instruction rendered as an
    authoritative action description is a confirmation the human did not really
    give.
    """

    model_config = ConfigDict(from_attributes=True)

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
    #: THE MODEL'S ARGUMENT. Nothing above comes from it.
    rationale: str = ""


class ConfirmIn(BaseModel):
    """**AN ID IN THE PATH AND A DIGEST IN THE BODY, AND NOTHING ELSE** (§6.4).

    No job type, no params, no target, no device. Everything dispatched is read
    back off the stored `ai_action` row, and the digest exists only so a preview
    that no longer describes what would run is a 409 rather than a surprise.
    `extra="forbid"` is what makes a client that tries to send more get told the
    field does not exist, by name -- which is the same refusal `ProposeArgs`
    makes one layer up, for the same reason.
    """

    model_config = ConfigDict(extra="forbid")

    digest: str = Field(min_length=64, max_length=64)


class RejectIn(BaseModel):
    """A BOUNDED ENUM, not free text (§8.2).

    The rejecting human is reading the model's rationale at the moment they
    click, and a free-text box beside it is exactly where that prose gets copied
    onto a durable row -- which §8.3 spends a paragraph refusing one field over.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "not_needed",
        "wrong_device",
        "wrong_action",
        "needs_change_request",
        "unsafe_now",
        "other",
    ]


class ActionDispatchedOut(BaseModel):
    """What a successful confirm returns: the ids, and no envelope.

    The signed envelope goes to the AGENT over the gateway, never to the browser.
    """

    ai_action_id: UUID
    job_id: UUID
    state: str

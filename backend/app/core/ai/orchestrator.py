"""The bounded turn loop (spec §5, §10.1, §10.4, §10.5).

**THIS FILE HOLDS NO DATABASE SESSION.** §0.1's one hard implementation
constraint, and it is structural rather than advisory: `run_turn` takes an
already-loaded message list and an already-computed allowed-tool list, and
returns a `TurnOutcome` the route persists afterwards. It opens no session,
imports no engine, and imports neither `org_session` nor `AsyncSession` --
`tests/test_ai_admin_orchestrator.py::test_the_orchestrator_holds_no_database_
session` asserts exactly that over this file's imports.

The reason is a connection-pool deadlock, not tidiness. A held outer
`org_session` plus an inner loopback request that opens its own session means
one turn occupies two pool slots for as long as a model takes to answer -- which
is seconds, controlled by a third party, and unbounded when that third party is
having a bad day. A pool of N therefore serves N/2 concurrent turns and then the
whole API stops answering, including for administrators who never opened the
chat panel. The behavioural half of this proof lives in the API tests, which run
a turn against a blocking provider with the pool size set to 1 and assert an
ordinary request still completes.

WHAT THE LOOP GUARANTEES, AND WHAT IT DOES NOT. It guarantees termination: every
one of §10.1's bounds ends the turn, and the wall clock ends it even when the
provider is behaving perfectly but slowly. It does NOT guarantee the model
behaves -- that is what the capability bound is for, and this file is downstream
of it. A model that emits `credentials.list` gets a tool error from the
executor's dict lookup; a model that emits sixty tool calls gets eight.

**A PARTIAL ANSWER SAYS IT IS PARTIAL AND NAMES THE BUDGET.** The notice is
SERVER-AUTHORED and appended here rather than requested from the model, for two
reasons: the deadline and provider-error paths have no model left to ask, and a
model instruction is a probabilistic mitigation for a property that must hold
every time. A copilot that silently truncates and then asserts completeness is
worse than one that fails.

**STREAMING AND THE DEADLINE ARE INDEPENDENT** (§5). The client sees tokens as
they arrive through `on_text`, but the deadline bounds the WHOLE exchange. A
per-phase read timeout would reset on every token and therefore never bound a
provider that trickles bytes forever -- the same property `app/core/egress.py`
writes down as its property 5.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.core.ai.describe import tool_specs
from app.core.ai.detect import MAX_FINDINGS, detect
from app.core.ai.envelope import mint_nonce, wrap
from app.core.ai.errors import NonceLeakError
from app.core.ai.executor_staff import ToolError
from app.core.ai.personas import PersonaProfile, ReplayPolicy
from app.core.ai.providers import (
    Block,
    Message,
    ModelProvider,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageBlock,
)
from app.core.ai.types import Tool, TurnContext

logger = logging.getLogger("its.ai.orchestrator")

StopReason = Literal[
    "answer",
    "tool_budget",
    "byte_budget",
    "token_budget",
    "deadline",
    "malformed",
    "nonce_leak",
    "provider_error",
    "provider_rate_limited",
    "session_expired",
    "internal_error",
]

# The two `ToolError` kinds that mean THE MODEL GOT THE CALL WRONG, as opposed
# to the call being right and the answer being "no" (`forbidden`, `not_found`)
# or the platform failing (`upstream_error`, `timeout`, `blocked`). Only these
# advance the consecutive-malformed counter: ending a turn because an
# administrator lacks a permission would be a denial of service dressed up as a
# safety feature.
_MALFORMED_KINDS = frozenset({"unknown_tool", "invalid_arguments"})


@dataclass(frozen=True)
class TurnBudgets:
    """§10.1's table. Every field ends the turn when it is reached, and every
    one of them is named in the answer when it does."""

    max_tool_calls: int = 8
    max_result_bytes: int = 256 * 1024
    # Provider-dependent and configured (§10.1); `None` means "the provider's
    # own limit governs". It is never an implicit "unlimited" the org pays for
    # -- the per-org daily token budget is enforced separately, at the API layer.
    max_output_tokens: int | None = None
    wall_clock_s: float = 60.0
    max_consecutive_malformed: int = 3


DEFAULTS = TurnBudgets()


@dataclass(frozen=True)
class ToolCallRecord:
    """One attempted tool call, in the shape the transcript and the audit event
    both derive from.

    `args` is the model's VALIDATED-OR-REJECTED argument mapping and is never
    persisted as-is: `store.persist_turn` reduces it to `arg_hash`. It travels
    here so the hash is computed once, at persistence, over the same
    canonicalization the audit chain uses.
    """

    tool: str
    args: Mapping[str, Any]
    status: str
    result_count: int = 0
    truncated: bool = False
    http_status: int | None = None


@dataclass(frozen=True)
class TurnFinding:
    """One injection-shaped field, with the tool that retrieved it. The excerpt
    goes to `ai_injection_flag` and NEVER to the audit log (§3.3)."""

    tool: str
    source_type: str
    source_id: str
    matched_rule: str
    excerpt: str


@dataclass
class TurnOutcome:
    """Everything the route needs to answer the caller and persist the turn, and
    nothing that outlives it. No session, no engine, no bearer token."""

    answer: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    findings: list[TurnFinding] = field(default_factory=list)
    stop_reason: StopReason = "answer"
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    model_id: str = ""

    @property
    def partial(self) -> bool:
        return self.stop_reason != "answer"


def tools_for_permissions(
    codes: Sequence[str] | set[str], *, registry: Mapping[str, Any]
) -> tuple[str, ...]:
    """§0.12's prefilter: the tools whose gate the caller already holds.

    **THIS IS ERGONOMICS, NOT SECURITY.** It shortens the tool list and saves
    tokens; it does not decide anything. The control is the loopback call, which
    re-enters the whole ASGI stack so `require_permission` runs verbatim -- and
    the orchestrator's tests re-run the executor's authorization case against a
    tool that was never offered, to prove a tool invoked directly still 403s.
    A reader who mistakes this function for the control will eventually delete
    the loopback for being slow.
    """
    held = set(codes)
    return tuple(name for name, tool in registry.items() if tool.permission in held)


# --------------------------------------------------------------------------- #
# Replayed history (docs/modules/ai-automation.md §0.8, §7.4; §13 test 14).
# --------------------------------------------------------------------------- #

#: The pseudo-tool replayed findings are attributed to. It is in no registry and
#: cannot be called; it names the SOURCE of the text, which for these findings is
#: this server's own replay of the transcript.
HISTORY_TOOL = "ai.history"


class TranscriptRow(Protocol):
    """The three fields replay needs off a stored transcript row.

    **A PROTOCOL RATHER THAN `AiMessage`, DELIBERATELY.** This file's own header
    makes "the orchestrator holds no database session" structural, and
    `tests/test_ai_admin_orchestrator.py::test_the_orchestrator_holds_no_database_
    session` asserts it over these imports. A declarative model is not a session,
    but importing one is the first step of the walk that ends with a query in the
    turn loop, and nothing here needs the class.
    """

    @property
    def id(self) -> Any: ...
    @property
    def role(self) -> str: ...
    @property
    def content(self) -> str: ...


def replay_policy_for(profile: PersonaProfile, *, mode: str) -> ReplayPolicy:
    """The policy for ONE session: the profile's answer, but only if the session
    can act.

    **THIS PAIR IS A RECORDED CORRECTION TO THE PLAN, WHICH SAID "a session whose
    PROFILE has `replay_policy='neutralize_flagged'`".** One profile serves both
    of a persona's session modes -- the admin persona's read sessions and its act
    sessions share `ADMIN_PROFILE` -- so a policy read off the profile alone
    neutralizes read mode too, and §13 test 14 requires the opposite: the SAME
    flagged answer is replaced by the stub in an act-capable session and replayed
    VERBATIM in a read one. §0.8 states it as a property of the session ("for a
    session whose `mode` is act-capable"), and that is what is implemented; the
    profile field is the persona's OPT-IN, and the mode is the condition.

    Read mode replays verbatim and that is still correct there, for the reason
    §0.8 gives and this build did not weaken: a read-mode turn cannot act, so a
    fully successful injection replayed back into its context reaches a frozen
    GET allowlist and nothing else. Editing what an administrator was told about
    their own estate costs more than it buys while that bound holds.
    """
    return profile.replay_policy if mode == "act" else "verbatim"


def _stub(matched_rule: str, source_id: str) -> str:
    """§0.8's server-authored replacement, verbatim from the spec.

    IT NAMES THE RULE AND THE SOURCE. A silent deletion would leave the model
    answering a conversation with a hole in it and no way to say why; this tells
    it a turn was withheld, why, and that the administrator may ask again.
    """
    return (
        "[A previous answer in this conversation quoted content that matched the\n"
        f" injection heuristic (rule: {matched_rule}, source: {HISTORY_TOOL}/{source_id})."
        " Its text has\n been withheld from this turn."
        " Ask again if you need it restated.]"
    )


def replay_history(
    rows: Iterable[TranscriptRow], *, policy: ReplayPolicy
) -> tuple[list[Message], list[TurnFinding]]:
    """The stored transcript as the provider sees it, plus what was flagged in it.

    **ONE FUNCTION, ONE `detect()` PASS, TWO OUTPUTS -- AND THAT IS THE POINT.**
    §7.4: "the stub is IN ADDITION TO the flag, not instead of it: the flag is the
    record that it happened, the stub is the neutralization." Computed separately
    they can disagree, and the way they disagree is the bad way round: a
    neutralization that fires while the flag does not is a mitigation that looks
    fine and leaves no record. Here the same finding produces both.

    **SECOND-ORDER INJECTION IS WHAT THIS EXISTS FOR.** §8.2's envelope is minted
    PER TURN and wraps only the free-text fields of THIS turn's tool results. A
    hostile line in a ticket is wrapped and flagged when retrieved -- and then, if
    the model quotes it in its answer (a correct thing for a model asked what a
    ticket says), it is stored as ordinary answer text and replayed into the
    ASSISTANT role, undelimited. Attacker-controlled prose has laundered itself
    from untrusted retrieved content into apparent model output.

    Read mode's answer to that was "flag it and leave it", and its argument was
    that a fully successful injection reaches a frozen GET allowlist. **ACT MODE'S
    PROPOSAL TOOL IS NOT THAT, SO THE PREMISE IS GONE** -- and this is the
    discharge of `docs/modules/ai-admin/spec.md` §17.11's "this reasoning expires
    with read mode."

    **USER ROWS ARE NEVER TOUCHED, IN EITHER MODE.** They are the caller's own
    words typed into their own console. Flagging them reports the caller to
    themselves, fires on any administrator who pastes a log line, and -- the part
    that matters -- withholding them edits the question the human asked from the
    conversation they are having.

    **TOOL RESULTS ARE NOT REPLAYED** because they are not stored (§3.2). The
    model sees what was asked and what it answered, which is what a conversation
    is; reconstituting old tool results would mean storing them.

    **REJECTED: wrap replayed answers in the untrusted envelope** (§0.8, and
    §17.11's objection stands). Wrapping a model's own prior answer contradicts
    the role it is replayed in and teaches the model the tags are noise, which
    degrades the FIRST-order defence to buy a weaker second-order one.
    **REJECTED: summarize prior turns with a second model call** -- cost, latency,
    and a second injection surface producing text nobody reviewed.
    """
    messages: list[Message] = []
    findings: list[TurnFinding] = []
    for row in rows:
        if row.role == "user":
            messages.append(Message(role="user", content=(TextBlock(text=row.content),)))
            continue

        hits = (
            list(
                detect(
                    {"id": str(row.id), "source_type": "ai.answer", "content": row.content},
                    tool=HISTORY_TOOL,
                )
            )
            if row.content
            else []
        )
        for hit in hits:
            if len(findings) >= MAX_FINDINGS:
                # The same cap §8.5 puts on one payload, for the same reason: a
                # long conversation must not write an unbounded number of flag
                # rows and audit sources per turn. **THE CAP BOUNDS THE FLAGS,
                # NOT THE STUBS** -- a conversation with 100 flagged answers must
                # still have all 100 neutralized, or the cap becomes a way to
                # smuggle the 21st one through.
                break
            findings.append(
                TurnFinding(
                    tool=HISTORY_TOOL,
                    source_type=hit.source_type,
                    source_id=hit.source_id,
                    matched_rule=hit.matched_rule,
                    excerpt=hit.excerpt,
                )
            )

        text = row.content
        if hits and policy == "neutralize_flagged":
            text = _stub(hits[0].matched_rule, hits[0].source_id)
        messages.append(Message(role="assistant", content=(TextBlock(text=text),)))
    return messages, findings


# --------------------------------------------------------------------------- #
# The partial-answer notices (§10.1).
# --------------------------------------------------------------------------- #


def _partial_notice(stop_reason: StopReason, budgets: TurnBudgets) -> str:
    kib = budgets.max_result_bytes // 1024
    return {
        "answer": "",
        "tool_budget": (
            f"I stopped early: this turn reached its budget of "
            f"{budgets.max_tool_calls} tool calls. The answer above is partial."
        ),
        "byte_budget": (
            f"I stopped early: this turn reached its budget of {kib} KiB of "
            f"tool-result data. The answer above is partial."
        ),
        "token_budget": (
            f"I stopped early: this turn reached its budget of "
            f"{budgets.max_output_tokens} output tokens. The answer above is partial."
        ),
        "deadline": (
            f"I stopped early: this turn reached its time limit of "
            f"{budgets.wall_clock_s:.0f} seconds. The answer above is partial."
        ),
        "malformed": (
            "I could not complete this request: I asked for the same information "
            "incorrectly several times in a row and stopped rather than keep trying."
        ),
        "nonce_leak": (
            "I stopped this turn: retrieved content contained an internal marker "
            "for this conversation, which should be impossible. Nothing was acted on."
        ),
        "provider_error": (
            "I could not complete this request: the language model service did not "
            "answer. Nothing was changed; please ask again."
        ),
        "session_expired": (
            "I stopped this turn: your signed-in session expired while I was working. "
            "Sign in again and ask once more."
        ),
        # The two the LOOP never sets. Both are reached by raising out of it, and
        # both are finished by the route so the turn is still recorded.
        "provider_rate_limited": (
            "I could not complete this request: the language model service is rate "
            "limiting this deployment. Nothing was changed; please ask again shortly."
        ),
        "internal_error": (
            "I could not complete this request: something went wrong on this server "
            "while I was working. Nothing was changed; please ask again."
        ),
    }[stop_reason]


def finish_turn(outcome: TurnOutcome, budgets: TurnBudgets, started: float) -> TurnOutcome:
    """Append the server-authored notice for `outcome.stop_reason` and stamp the
    duration.

    PUBLIC BECAUSE THE API LAYER FINISHES TURNS THIS LOOP NEVER GOT TO FINISH.
    A turn that ends by raising -- a provider 429, or something nobody predicted
    -- is still a turn that may have READ ESTATE DATA, and it is persisted and
    audited by the route (invariant #4). It must carry the same shape of answer
    and the same duration as one that ended here, from the same table of
    notices, rather than a second set of words that drift.
    """
    notice = _partial_notice(outcome.stop_reason, budgets)
    if notice:
        outcome.answer = f"{outcome.answer.rstrip()}\n\n{notice}".strip()
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    return outcome


# --------------------------------------------------------------------------- #
# One provider exchange, with §10.4's retry policy.
# --------------------------------------------------------------------------- #


@dataclass
class _Exchange:
    text: str = ""
    calls: list[ToolCallBlock] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


async def _stream_once(
    provider: ModelProvider,
    *,
    system: str,
    messages: Sequence[Message],
    tools: Sequence[Any],
    deadline: float,
    on_text: Callable[[str], Awaitable[None]] | None,
) -> _Exchange:
    exchange = _Exchange()
    stream = provider.stream_turn(
        system=system, messages=list(messages), tools=list(tools), deadline=deadline
    )
    async for block in stream:
        exchange, published = _absorb(exchange, block)
        if published is not None and on_text is not None:
            await on_text(published)
    return exchange


def _absorb(exchange: _Exchange, block: Block) -> tuple[_Exchange, str | None]:
    if isinstance(block, TextBlock):
        exchange.text += block.text
        return exchange, block.text
    if isinstance(block, ToolCallBlock):
        exchange.calls.append(block)
        return exchange, None
    if isinstance(block, UsageBlock):
        exchange.input_tokens += block.input_tokens
        exchange.output_tokens += block.output_tokens
    return exchange, None


async def _exchange_with_retry(
    provider: ModelProvider,
    *,
    system: str,
    messages: Sequence[Message],
    tools: Sequence[Any],
    deadline: float,
    on_text: Callable[[str], Awaitable[None]] | None,
) -> _Exchange:
    """§10.4's policy, expressed as control flow rather than as a comment.

    Connection error / 5xx: ONE retry. Timeout: NONE -- retrying a slow provider
    makes it slower and burns the administrator's deadline twice. 429: none, and
    it propagates so the API layer can answer 429 with the provider's own retry
    hint instead of inventing one.

    The retry re-runs the exchange from the same message list, so the ANSWER is
    whatever the successful attempt produced -- never a concatenation of a
    failed attempt and a successful one. A first attempt that failed after
    emitting some text will have published those deltas to the SSE side-channel;
    that is a cosmetic duplication on an optional channel, and the `POST`'s
    answer, which is authoritative, is unaffected.
    """
    try:
        return await _stream_once(
            provider,
            system=system,
            messages=messages,
            tools=tools,
            deadline=deadline,
            on_text=on_text,
        )
    except ProviderUnavailableError:
        logger.warning("ai provider unavailable; retrying once")
    return await _stream_once(
        provider,
        system=system,
        messages=messages,
        tools=tools,
        deadline=deadline,
        on_text=on_text,
    )


# --------------------------------------------------------------------------- #
# The loop.
# --------------------------------------------------------------------------- #


async def run_turn(
    *,
    provider: ModelProvider,
    profile: PersonaProfile,
    app: Any,
    bearer: str,
    messages: Sequence[Message],
    allowed_tools: Sequence[str],
    budgets: TurnBudgets = DEFAULTS,
    deadline: float | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    outcome: TurnOutcome | None = None,
    act_tools: Mapping[str, Tool] | None = None,
) -> TurnOutcome:
    """Spec §5 steps 4-5. Loading and persisting belong to the route.

    **THE CALLER MAY OWN THE OUTCOME, AND THE ROUTE DOES.** A turn that ends by
    RAISING -- a provider 429, or an exception nobody predicted -- would
    otherwise take its record of what it had already read out of the process
    with it, and the route would answer without persisting or auditing anything.
    Passing the outcome in means the tool calls already made survive the raise,
    so the route can finish the turn (`finish_turn`), persist it and write the
    `ai.query` event. That is invariant #4: a turn that read estate data is
    recorded whether or not it ended well.
    """
    started = time.monotonic()
    deadline = started + budgets.wall_clock_s if deadline is None else deadline
    outcome = TurnOutcome() if outcome is None else outcome
    if not outcome.model_id:
        outcome.model_id = getattr(provider, "id", "")

    # ONE nonce per turn (§8.2). Minted here, never cached, never derived from
    # anything stable -- a constant nonce is forgeable by any content that has
    # seen one previous turn's output.
    nonce = mint_nonce()
    # **THE ACT TOOL IS PASSED PER TURN, NOT REGISTERED IN `profile.tools`**
    # (§12, and the amendment at §18.9). A proposal needs the session id, the
    # turn's `message_seq`, the acting user and this turn's taint -- facts about
    # ONE TURN. A tool object carrying them inside the frozen profile would carry
    # one turn's binding into the next, and `message_seq` would stop describing
    # where a proposal came from. It also keeps `profile.tools` a mapping of
    # `ReadTool` ONLY, which `tests/test_ai_boundaries.py` asserts as a type-level
    # statement that a persona's registered capabilities cannot mutate anything.
    #
    # THE CALLER DECIDES WHETHER TO PASS ONE, and the caller is the persona's own
    # router, which passes one only when `profile.may_propose` AND the session is
    # act-capable. Nothing here can widen that: this loop offers what it is given.
    act = dict(act_tools or {})
    specs = [
        *tool_specs(app, allowed_tools, registry=profile.tools),
        *(tool.describe() for tool in act.values()),
    ]
    ctx = TurnContext(app=app, bearer=bearer, deadline=deadline)
    conversation: list[Message] = list(messages)

    calls_made = 0
    bytes_used = 0
    consecutive_malformed = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcome.stop_reason = "deadline"
            return finish_turn(outcome, budgets, started)

        try:
            # The wall clock bounds the WHOLE exchange, including a provider
            # that trickles one token at a time forever.
            async with asyncio.timeout(remaining):
                exchange = await _exchange_with_retry(
                    provider,
                    system=profile.system_prompt,
                    messages=conversation,
                    tools=specs,
                    deadline=deadline,
                    on_text=on_text,
                )
        except TimeoutError:
            # The WHOLE-TURN wall clock, not a per-phase timeout.
            outcome.stop_reason = "deadline"
            return finish_turn(outcome, budgets, started)
        except ProviderTimeoutError:
            # The provider's own timeout. NOT retried (§10.4), and reported as a
            # provider failure rather than as the turn's deadline, because the
            # administrator's 60 s were not what ran out.
            outcome.stop_reason = "provider_error"
            return finish_turn(outcome, budgets, started)
        except ProviderRateLimitedError:
            # Never retried here; the API layer turns this into a 429 carrying
            # the provider's own retry hint.
            raise
        except ProviderUnavailableError:
            outcome.stop_reason = "provider_error"
            return finish_turn(outcome, budgets, started)

        outcome.answer += exchange.text
        outcome.input_tokens += exchange.input_tokens
        outcome.output_tokens += exchange.output_tokens

        if budgets.max_output_tokens is not None and (
            outcome.output_tokens > budgets.max_output_tokens
        ):
            outcome.stop_reason = "token_budget"
            return finish_turn(outcome, budgets, started)

        if not exchange.calls:
            # A final text answer: the only path out of this loop that is not a
            # budget.
            return finish_turn(outcome, budgets, started)

        conversation.append(
            Message(role="assistant", content=tuple(exchange.calls))
            if not exchange.text
            else Message(
                role="assistant",
                content=(TextBlock(text=exchange.text), *exchange.calls),
            )
        )

        results: list[Block] = []
        for call in exchange.calls:
            if calls_made >= budgets.max_tool_calls:
                outcome.stop_reason = "tool_budget"
                return finish_turn(outcome, budgets, started)
            calls_made += 1

            # An act tool is NOT invoked through the loopback executor: it has no
            # route to call. It performs a server-side resolution over the
            # approved-action catalogue and returns a preview, and the ONE
            # loopback read it does make (the device visibility probe) it makes
            # itself, through the persona's own executor.
            if call.name in act:
                invoked = await act[call.name].invoke(ctx, call.arguments)
            else:
                invoked = await profile.executor(
                    tool_name=call.name,
                    raw_args=call.arguments,
                    app=app,
                    bearer=bearer,
                    deadline=deadline,
                    registry=profile.tools,
                )

            if isinstance(invoked, ToolError):
                outcome.tool_calls.append(
                    ToolCallRecord(
                        tool=call.name,
                        args=dict(call.arguments),
                        status=invoked.kind,
                        http_status=invoked.status,
                    )
                )
                if invoked.kind == "session_expired":
                    outcome.stop_reason = "session_expired"
                    return finish_turn(outcome, budgets, started)
                if invoked.kind in _MALFORMED_KINDS:
                    consecutive_malformed += 1
                    if consecutive_malformed >= budgets.max_consecutive_malformed:
                        outcome.stop_reason = "malformed"
                        return finish_turn(outcome, budgets, started)
                else:
                    consecutive_malformed = 0
                results.append(
                    ToolResultBlock(
                        tool_call_id=call.id,
                        name=call.name,
                        content=invoked.message,
                        is_error=True,
                    )
                )
                continue

            consecutive_malformed = 0
            # `.get` with the NAME as the fallback: an act tool is not in
            # `profile.tools`, and `detect` takes the tool only to attribute a
            # finding. Act-tool payloads are still scanned -- the preview carries
            # the catalogue entry's name and the device's own reported hostname,
            # neither of which this server wrote.
            tool = profile.tools.get(call.name, call.name)

            # Detect BEFORE wrapping: the heuristic runs over the scrubbed text
            # as retrieved, not over text carrying our own tags. It only flags
            # -- it never blocks the turn and never edits the payload (§8.4).
            for finding in detect(invoked.payload, tool=tool):
                outcome.findings.append(
                    TurnFinding(
                        tool=call.name,
                        source_type=finding.source_type,
                        source_id=finding.source_id,
                        matched_rule=finding.matched_rule,
                        excerpt=finding.excerpt,
                    )
                )

            try:
                wrapped = wrap(invoked.payload, nonce=nonce)
            except NonceLeakError:
                # There is no benign way for a freshly minted nonce to appear in
                # a row read microseconds later. Abort rather than degrade.
                logger.error("ai turn aborted: this turn's nonce appeared in retrieved content")
                outcome.stop_reason = "nonce_leak"
                return finish_turn(outcome, budgets, started)

            outcome.tool_calls.append(
                ToolCallRecord(
                    tool=call.name,
                    args=dict(call.arguments),
                    status="ok",
                    result_count=invoked.result_count,
                    truncated=invoked.truncated,
                    http_status=invoked.status,
                )
            )
            results.append(
                ToolResultBlock(tool_call_id=call.id, name=call.name, content=wrapped)
            )

            bytes_used += len(json.dumps(wrapped, default=str).encode("utf-8"))
            if bytes_used > budgets.max_result_bytes:
                outcome.stop_reason = "byte_budget"
                return finish_turn(outcome, budgets, started)

        conversation.append(Message(role="user", content=tuple(results)))

        # Checked HERE as well as before each call: the pre-call check catches a
        # single exchange that emitted more calls than the budget, and this one
        # stops the turn WITHOUT ASKING THE PROVIDER AGAIN once the budget is
        # spent. Without it, a model emitting one call per exchange costs
        # `max_tool_calls + 1` provider round trips to produce a partial answer.
        if calls_made >= budgets.max_tool_calls:
            outcome.stop_reason = "tool_budget"
            return finish_turn(outcome, budgets, started)

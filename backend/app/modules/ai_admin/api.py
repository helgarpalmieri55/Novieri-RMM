"""The chat API, the SSE side-channel, and the per-org rate limits (spec §5,
§6, §10.3, §10.6; the route table is §6.1, added by this build).

ELEVEN ROUTES, EVERY ONE `require_permission`-GATED DIRECTLY. Completeness is
DERIVED FROM THE ROUTER by `app.core.rbac.route_permission`
(`tests/test_ai_admin_api.py`), with the route count pinned exactly, rather than
from a hand-maintained list a later addition could silently miss.

**THE `POST` IS AUTHORITATIVE; SSE IS DECORATION.** §6 says the turn is
submitted by `POST` and streamed from a separate `GET`, and that "if SSE is
unavailable the `POST` alone returns the complete answer". So there is no
`ai_turn` table and no background job: the `POST` runs the turn inline and
returns the whole answer. While it runs it publishes text deltas to the Redis
channel `ai:turn:{session_id}:{turn_id}`; the `GET` subscribes to that channel.
A client that never opens the stream loses nothing, and **when Redis is down the
stream 503s and the `POST` still works** -- which is a tested property, not an
aspiration.

**THE CHANNEL IS NAMED FOR THE CONVERSATION, NOT FOR THE TURN ALONE** -- see
`_channel`. The conversation is the thing the stream route verifies, so it is
the thing the name has to carry; a turn id is a name a client chose, and on its
own it must reach nothing.

**THE ORCHESTRATOR HOLDS NO DATABASE SESSION** (§0.1's one hard implementation
constraint). This file opens `org_session` to load, CLOSES IT, runs the turn,
and reopens it to persist. A held session plus an inner loopback request that
opens its own would occupy two pool slots for as long as a third party's model
takes to answer -- so a pool of N would serve N/2 concurrent turns and then stop
answering for everyone, including administrators who never opened the chat.
`test_a_turn_does_not_block_an_ordinary_request` runs this with the pool set to
1 and is the only test that can see it.

**A TURN THAT READ ANYTHING IS PERSISTED AND AUDITED, ON EVERY EXIT PATH.**
Invariant #4. `create_turn` owns the `TurnOutcome` and passes it into
`run_turn`, so a turn that ends by raising -- a provider 429, or anything
unforeseen -- still leaves behind the tool calls it had already made, and step 7
still writes the transcript rows and the `ai.query` event. The failure is
recorded IN that event (`stop_reason`), not hidden by it. This is not
hypothetical tidiness: a tool call carrying a NUL byte in a `str` argument used
to raise out of the executor, out of the loop and out of this route, so a turn
that had read real estate data through the agent produced a 500 and NO `ai.query`
event at all.

**THE BEARER TOKEN IS READ FROM THIS REQUEST'S OWN HEADERS AND PASSED AS AN
ARGUMENT** (§0.1). It is never put on `AuthContext` -- that dataclass reaches
every module, and a token on it is one careless `logger.debug(ctx)` from living
in a log file forever -- never stored on a row, never logged, and never given to
the model.

**§0.9's 409 NEEDS A CROSS-ORG LOOK, AND THIS IS THE ONLY ONE IN THE MODULE.**
A conversation is pinned to one org at creation and a mismatch is a 409, never a
switch. Under `org_session` that case is unreachable: RLS returns no row, the
service raises "not found", and the console shows an empty transcript for a
conversation the user can see in their other organisation -- a silent wrong
answer, which is exactly what §0.9 rejects. `_owning_org` therefore reads ONE
COLUMN (`ai_session.org_id`) through `admin_session`, and nothing else ever. It
returns no content and cannot be made to; "another user in the same org" stays a
404, because that case has no legitimate 409 story and a 403 there would be a
cross-user existence oracle.

**RATE LIMITING FAILS OPEN, DELIBERATELY AND NARROWLY.** §10.3's counters are a
COST control. If Redis is unavailable the turn proceeds and a warning is logged,
because the alternative -- an org whose administrators cannot ask questions
because a cache is down -- trades a real outage for a billing risk. Nothing
security-relevant depends on Redis here: the allowlist, the loopback gate, RBAC
and the org pin are all enforced elsewhere and none of them consults it.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_extensions import uuid7

from app.api.deps import AuthContext, require_permission
from app.core.ai.act import (
    ActConflict,
    ActExpired,
    ActForbidden,
    ActRefused,
    ActTool,
    ProposeBinding,
    confirm_action,
    list_actions,
    load_preview,
    reject_action,
)
from app.core.ai.autonomy import list_ceilings, set_ceiling
from app.core.ai.errors import (
    AiDisabledError,
    AiNotFoundError,
    AiOrgMismatchError,
    AiValidationError,
)
from app.core.ai.orchestrator import (
    DEFAULTS,
    TurnOutcome,
    finish_turn,
    replay_history,
    replay_policy_for,
    run_turn,
    tools_for_permissions,
)
from app.core.ai.providers import (
    Message,
    ModelProvider,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    TextBlock,
    configured_provider,
    configured_providers,
    get_provider,
)
from app.core.ai.settings import EffectiveAiSettings, get_settings, update_settings
from app.core.ai.store import (
    create_session,
    delete_session,
    list_sessions,
    load_session,
    persist_turn,
)
from app.core.ai.types import TurnContext
from app.core.i18n import tr
from app.core.rbac import user_permission_codes
from app.core.tenancy import admin_session, org_session
from app.gateway.dispatch import publish_job
from app.models import AiInjectionFlag, AiMessage, AiSession
from app.modules.ai_admin.profile import ADMIN_PROFILE
from app.modules.ai_admin.schema import (
    ActionDispatchedOut,
    ActionOut,
    ActionPreviewOut,
    AiStatus,
    CeilingOut,
    CeilingUpdate,
    ConfirmIn,
    FlagOut,
    MessageOut,
    ProviderOut,
    RejectIn,
    SessionCreate,
    SessionOut,
    SettingsOut,
    SettingsUpdate,
    TranscriptOut,
    TurnOut,
    TurnRequest,
)

logger = logging.getLogger("its.ai_admin.api")

router = APIRouter(tags=["ai-admin"])

# §10.3's windows. The per-user window is a minute and the per-org window an
# hour, matching §3.4's two tunables; the token budget's window is a UTC day.
_USER_WINDOW_S = 60
_ORG_WINDOW_S = 3600
_DAY_WINDOW_S = 86400
_SOFT_BUDGET_FRACTION = 0.8

# How long the SSE generator waits for the turn that feeds it. One deadline
# longer than the turn's own, so a stream never outlives the work it is
# following, and never hangs a connection open on a turn that already ended.
_STREAM_GRACE_S = 10.0


def _channel(session_id: UUID, turn_id: UUID) -> str:
    """The Redis channel one turn's deltas travel on.

    **THE CONVERSATION ID IS IN THE NAME, AND THAT IS THE ACCESS CONTROL.** A
    subscriber reaches this channel by naming a conversation, and the stream
    route verifies the conversation belongs to the caller AND to the token's org
    before it subscribes -- so a turn id lifted from somewhere else names a
    channel under a conversation the caller does not own, which nobody publishes
    to. Keyed on the turn id alone, as it was, owning any conversation of your
    own was enough to subscribe to any turn in the deployment and read another
    organization's answer as it was written.

    THE OBVIOUS ALTERNATIVE DOES NOT WORK, WHICH IS WHY IT IS NOT USED: looking
    the turn up and requiring its `session_id` to match cannot bind an in-flight
    turn, because the transcript row is written when the turn ENDS. A client
    subscribes before it submits (that is the only ordering a stream can help),
    so at subscribe time there is nothing to look up -- and the live answer is
    exactly what leaks.
    """
    return f"ai:turn:{session_id}:{turn_id}"


# --------------------------------------------------------------------------- #
# Error translation. BY TYPE, never by matching message prose.
# --------------------------------------------------------------------------- #


def _http_error(exc: Exception) -> HTTPException:
    # **BY TYPE, AND EACH CODE MEANS SOMETHING DIFFERENT** (§6.4). A 409 says the
    # request was well formed and THE WORLD MOVED -- the catalogue was edited,
    # the dial was lowered, a script version was bumped, a fuse tripped, or this
    # action was already answered. A 410 says the preview's clock ran out. A 403
    # says this principal may not confirm THIS job type, which is §9.2's second
    # part and is not the same failure as not holding `ai.act.confirm` at all.
    if isinstance(exc, ActExpired):
        return HTTPException(status.HTTP_410_GONE, str(exc))
    if isinstance(exc, ActConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ActForbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, ActRefused):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, AiNotFoundError):
        # 404, NEVER 403: another user's or another org's conversation is
        # indistinguishable from one that never existed.
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, AiOrgMismatchError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, AiDisabledError | ProviderNotConfiguredError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# --------------------------------------------------------------------------- #
# Availability (§10.6). A disabled feature, not an error.
# --------------------------------------------------------------------------- #


def _provider_for() -> ModelProvider:
    """A named seam. Tests replace THIS, so every other line of the turn route
    -- the gates, the rate limits, the persistence -- runs exactly as it does in
    production.

    **NO ARGUMENT SINCE §0.14**: there is one provider per deployment and no
    per-org selection to pass in."""
    return get_provider()


def _availability() -> str:
    """`""` when the agent can answer, otherwise the reason, NAMING THE CAUSE.

    **ONE REASON REMAINS, AND IT BELONGS TO THE OPERATOR** (§0.14). There used
    to be three: the org had switched the assistant off, the org had selected no
    provider, or the server had configured none. The first two were the per-org
    kill switch and the per-org provider selection, and both are gone -- so an
    unavailable assistant now means exactly one thing, "this control plane has
    no `ITS_AI_*` configuration", and it is the person who runs the control
    plane who can fix it. That was always the reason with a different owner from
    the other two, which is why the distinction mattered; with the other two
    removed, no administrator can be sent to the wrong person.
    """
    try:
        _provider_for()
    except ProviderNotConfiguredError as exc:
        return str(exc)
    return ""


# --------------------------------------------------------------------------- #
# Rate limiting (§10.3). Follows `notification.api._enforce_test_send_rate_limit`
# exactly, including its own note that this codebase has no rate-limiting
# framework and that this pattern does not add one.
# --------------------------------------------------------------------------- #


async def _bump(redis: Any, key: str, window_s: int) -> int:
    count = int(await redis.incr(key))
    if count == 1:
        # ONLY on creation, so the window is a genuine rolling window. Calling
        # EXPIRE unconditionally pushes the TTL out on every request, and a
        # window that never rolls is a limit that never resets.
        await redis.expire(key, window_s)
    return count


def _day_key(org_id: UUID) -> str:
    return f"ai:tokens:org:{org_id}:{datetime.now(UTC):%Y%m%d}"


async def _enforce_rate_limits(
    redis: Any, effective: EffectiveAiSettings, *, org_id: UUID, user_id: UUID
) -> list[str]:
    """Three counters, and the request that trips a limit is itself counted and
    rejected. Returns any soft warnings for the response."""
    warnings: list[str] = []
    try:
        user_count = await _bump(redis, f"ai:rate:user:{user_id}", _USER_WINDOW_S)
        if user_count > effective.max_turns_per_user_per_minute:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                tr(
                    "you may ask at most {count} questions per minute; try again shortly",
                    count=effective.max_turns_per_user_per_minute,
                ),
            )
        org_count = await _bump(redis, f"ai:rate:org:{org_id}", _ORG_WINDOW_S)
        if org_count > effective.max_turns_per_org_per_hour:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                tr(
                    "this organization may ask at most {count} questions per hour; "
                    "try again shortly",
                    count=effective.max_turns_per_org_per_hour,
                ),
            )
        budget = effective.daily_token_budget
        if budget:
            used = int(await redis.get(_day_key(org_id)) or 0)
            if used >= budget:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    tr(
                        "this organization's daily token budget of {budget} is spent",
                        budget=budget,
                    ),
                )
            if used >= budget * _SOFT_BUDGET_FRACTION:
                warnings.append(
                    tr(
                        "{used} of this organization's daily budget of {budget} "
                        "tokens has been used.",
                        used=used,
                        budget=budget,
                    )
                )
    except RedisError:
        # FAIL OPEN, and say so in the log. See this module's header: these are
        # cost controls, and nothing security-relevant consults Redis.
        logger.warning("ai rate limiting unavailable; the turn proceeded uncounted")
    return warnings


async def _record_tokens(redis: Any, org_id: UUID, tokens: int) -> None:
    if tokens <= 0:
        return
    try:
        key = _day_key(org_id)
        total = int(await redis.incrby(key, tokens))
        if total == tokens:
            await redis.expire(key, _DAY_WINDOW_S)
    except RedisError:
        logger.warning("ai token accounting unavailable for org %s", org_id)


# --------------------------------------------------------------------------- #
# Cross-org pin (§0.9).
# --------------------------------------------------------------------------- #


async def _owning_org(admin_engine: AsyncEngine, session_id: UUID) -> UUID | None:
    """The org a conversation belongs to, or `None`.

    ONE COLUMN, through the explicit cross-org path. See this module's header
    for why the alternative -- letting RLS answer -- turns §0.9's 409 into a
    200 with an empty transcript.
    """
    async with admin_session(engine=admin_engine) as session:
        return (
            await session.execute(select(AiSession.org_id).where(AiSession.id == session_id))
        ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Transcript <-> provider message conversion.
# --------------------------------------------------------------------------- #


async def _turn_id_taken(session: Any, turn_id: UUID) -> bool:
    """Has this org already used this turn id?

    NO `org_id` PREDICATE, AND THAT IS CORRECT ONLY BECAUSE OF `_stored_turn_id`.
    This runs under `org_session`, so RLS answers for this tenant and no other --
    and a client-derived id is, by construction, in this org's own id space, so
    a row it cannot see cannot carry this id. The two facts are one mechanism:
    weaken the derivation and this query silently starts answering "free" for
    ids another tenant holds.
    """
    return (
        await session.execute(select(AiMessage.id).where(AiMessage.id == turn_id))
    ).scalar_one_or_none() is not None


# A fixed, arbitrary namespace for §6.1's client-supplied turn ids. It is a
# constant of the code, not a secret and not a tunable: two servers of the same
# deployment must derive the same id for the same (org, client id).
_TURN_ID_NAMESPACE = UUID("e0d6575f-de86-4b73-8dd4-1c35ec7ba738")


def _stored_turn_id(org_id: UUID, client_turn_id: UUID) -> UUID:
    """The id a CLIENT-SUPPLIED turn id is stored under, inside this org's own
    id space.

    **A CLIENT-CHOSEN VALUE MUST NOT BECOME A GLOBALLY UNIQUE PRIMARY KEY.**
    `ai_message.id` is unique across the whole deployment while every query that
    can see it is tenant-scoped by RLS, so a raw client id created three
    problems at once: an id already used in another tenant read as free, then
    violated the primary key at persist time -- a 500 after the provider had
    been paid -- and the three outcomes (200 free / 400 mine / 500 someone
    else's) formed a cross-tenant existence oracle over `ai_message.id`.

    Mapping the id into a per-org space removes all three at their cause rather
    than guarding each: two organizations naming the same turn id now produce
    two different rows, so nothing collides, nothing 500s, and the answer a
    caller sees depends only on their OWN org's rows. What could not work: a
    predicate on a query RLS has already narrowed (it cannot see what it must
    exclude), or asking the admin engine (which would answer 400 for another
    tenant's id -- the oracle, stated plainly). A composite `(org_id, id)`
    primary key would be the schema-level version of this and needs a migration;
    this is the same property without one.

    The consequence is deliberate and documented on `TurnRequest`: the id a
    client chooses names its STREAM, and the id it gets back names the STORED
    TURN. Server-allocated ids are `uuid7()` and are not passed through here --
    they are already unique and time-ordered, and there is no client to protect
    the id space from.
    """
    return uuid5(_TURN_ID_NAMESPACE, f"{org_id}:{client_turn_id}")


# **`_history` AND `_replayed_findings` MOVED TO `app/core/ai/orchestrator.py`**
# as `replay_history`, and merged into ONE function (§0.8, §7.4; §13 test 14).
#
# They moved because act mode made the replay a SECURITY CONTROL rather than a
# formatting step, and a security control that lives in one persona's router is a
# control the next two personas do not have. `_history`'s own closing paragraph
# said its reasoning "expires with read mode"; it has expired, and this comment
# is where a reader following that sentence lands.
#
# They merged because §7.4 requires the stub to be IN ADDITION TO the flag.
# Computed by two functions over two `detect()` passes they can disagree, and the
# way they disagree is the bad way round: a neutralization that fires while the
# flag does not is a mitigation that looks fine and leaves no record of the
# attack it absorbed.


# --------------------------------------------------------------------------- #
# Routes.
# --------------------------------------------------------------------------- #


@router.get("/ai/status", response_model=AiStatus)
async def ai_status(
    request: Request, ctx: AuthContext = Depends(require_permission("ai.use"))
) -> AiStatus:
    """Whether the assistant can answer here, and if not, why.

    §10.6: the console renders a disabled panel with an explanation -- not an
    error, not a spinner. Nothing about the server's configuration is returned:
    no provider key, no base URL, no model id. **This route reads no database
    row at all since §0.14** -- availability is now purely a property of the
    deployment's provider configuration.
    """
    reason = _availability()
    return AiStatus(enabled=not reason, reason=reason)


@router.post("/ai/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_ai_session(
    request: Request,
    body: SessionCreate | None = None,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> SessionOut:
    """Start a conversation. 503 when the assistant is unavailable, so a client
    cannot accumulate conversations it can never use.

    **`mode='act'` ADDITIONALLY REQUIRES `ai.act.propose`, CHECKED HERE AND NOT
    BY A ROUTE DEPENDENCY** (§9.2, §12). The route's own gate is `ai.use` because
    read sessions are the ordinary case; the second grant is conditional on the
    request body, which is precisely the shape §0.11 says cannot be a dependency.
    It is still server-side, still before any row is written, and the 403 leaves
    NO SESSION BEHIND -- a rejected act session that existed as a read session
    would be a promotion path with extra steps.

    **THE MODE IS FIXED HERE, FOREVER** (§0.8). No route on this surface accepts a
    mode change and `AiSession` refuses one at the ORM layer, because promoting a
    session imports a whole read-mode transcript -- assembled under read mode's
    replay rules -- into a context that can propose.
    """
    engine = request.app.state.app_engine
    mode = (body or SessionCreate()).mode

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        codes = await user_permission_codes(session, ctx.user_id)
    reason = _availability()
    if reason:
        raise _http_error(AiDisabledError(reason))
    if mode == "act" and "ai.act.propose" not in codes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            tr("opening an act-capable conversation requires ai.act.propose"),
        )

    provider = _provider_for()
    config = configured_provider()
    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        row = await create_session(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            persona=ADMIN_PROFILE.name,
            mode=mode,
            model_id=getattr(provider, "id", ""),
            # The audit event still records WHICH provider answered, because a
            # transcript is read months later and `.env` will have moved on.
            # It is a record of what happened, not a setting anybody chose.
            provider_key=config.key if config is not None else None,
        )
        return SessionOut.model_validate(row)


@router.get("/ai/sessions", response_model=list[SessionOut])
async def list_ai_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> list[SessionOut]:
    """The caller's OWN conversations. Another administrator's are not listed
    and not reachable."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_sessions(
            session, org_id=ctx.org_id, user_id=ctx.user_id, limit=limit
        )
        return [SessionOut.model_validate(row) for row in rows]


@router.get("/ai/sessions/{session_id}", response_model=TranscriptOut)
async def get_ai_session(
    session_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> TranscriptOut:
    """One conversation and its transcript."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row, messages = await load_session(
                session, org_id=ctx.org_id, user_id=ctx.user_id, session_id=session_id
            )
            return TranscriptOut(
                session=SessionOut.model_validate(row),
                messages=[MessageOut.model_validate(message) for message in messages],
            )
    except AiNotFoundError as exc:
        raise _http_error(exc) from exc


@router.delete("/ai/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_session(
    session_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> None:
    """Delete a conversation and, by cascade, its transcript and its flags. The
    audit events it produced remain: they are the record that it happened."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_session(
                session, org_id=ctx.org_id, user_id=ctx.user_id, session_id=session_id
            )
    except AiNotFoundError as exc:
        raise _http_error(exc) from exc


@router.post("/ai/sessions/{session_id}/turns", response_model=TurnOut)
async def create_turn(
    session_id: UUID,
    body: TurnRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> TurnOut:
    """Ask a question and get the whole answer (spec §5).

    The order below is the spec's, and it is load-bearing: the availability gate
    comes BEFORE the provider is ever called, so a disabled org cannot cause an
    outbound request whose answer is then discarded.
    """
    app_engine = request.app.state.app_engine
    admin_engine = request.app.state.admin_engine
    redis = request.app.state.redis

    # 1-2. Gate, then availability (§10.6).
    async with org_session(ctx.org_id, engine=app_engine, clients=ctx.clients) as session:
        effective = await get_settings(session, ctx.org_id)
    reason = _availability()
    if reason:
        raise _http_error(AiDisabledError(reason))
    provider = _provider_for()

    # 3. The org pin (§0.9) -- a 409, never a silently empty answer.
    owner = await _owning_org(admin_engine, session_id)
    if owner is None:
        raise _http_error(
            AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
        )
    if owner != ctx.org_id:
        raise _http_error(
            AiOrgMismatchError(
                tr(
                    "this conversation belongs to a different organization; "
                    "start a new one here"
                )
            )
        )

    # 4. Rate check (§10.3).
    warnings = await _enforce_rate_limits(
        redis, effective, org_id=ctx.org_id, user_id=ctx.user_id
    )

    # 5. Load, compute the prefilter ONCE, and CLOSE THE SESSION.
    #
    # TWO IDS, DELIBERATELY. `stream_id` is the name the client chose and the
    # name its SSE subscription already carries; `turn_id` is the id the stored
    # turn takes, derived into THIS ORG'S id space by `_stored_turn_id` so a
    # client-chosen value never becomes a globally unique primary key. When the
    # client chose nothing there is one id: a server-allocated `uuid7()`.
    stream_id = body.turn_id or uuid7()
    turn_id = (
        _stored_turn_id(ctx.org_id, body.turn_id) if body.turn_id is not None else stream_id
    )
    try:
        async with org_session(ctx.org_id, engine=app_engine, clients=ctx.clients) as session:
            ai_session, messages = await load_session(
                session, org_id=ctx.org_id, user_id=ctx.user_id, session_id=session_id
            )
            codes = await user_permission_codes(session, ctx.user_id)
            # ONE PASS PRODUCES BOTH, and the policy comes from the PROFILE AND
            # THE SESSION'S MODE together (§0.8): this persona's read sessions
            # replay verbatim, its act sessions replace flagged answers with the
            # server-authored stub. `replayed` is computed over the INPUT, here,
            # so it survives every way the turn can end -- including the failure
            # paths below, which are exactly the turns whose context nobody would
            # otherwise have looked at.
            history, replayed = replay_history(
                messages,
                policy=replay_policy_for(ADMIN_PROFILE, mode=ai_session.mode),
            )
            session_row_id = ai_session.id
            session_mode = ai_session.mode
            # The seq the ASSISTANT row of this turn will take, matching what
            # `persist_turn` computes and what `ai_injection_flag.message_seq`
            # already records. An `ai_action` names the turn it came from, so the
            # two must agree or "which turn proposed this" is unanswerable.
            next_seq = (max((row.seq for row in messages), default=-1) + 1) + 1
            if body.turn_id is not None and await _turn_id_taken(session, turn_id):
                # Refused here rather than colliding on the primary key at
                # persist time, which would surface as a 500 after the model had
                # already been paid for. THIS ORG'S OWN reuse is the only reuse
                # that exists now -- see `_stored_turn_id`.
                raise AiValidationError(
                    tr("turn id {turn_id} has already been used", turn_id=body.turn_id)
                )
    except (AiNotFoundError, AiValidationError) as exc:
        raise _http_error(exc) from exc

    # 6. Run the turn. NO DATABASE SESSION IS HELD PAST THIS LINE.
    history.append(Message(role="user", content=(TextBlock(text=body.question),)))
    bearer = request.headers.get("Authorization", "")

    channel = _channel(session_id, stream_id)

    async def publish(text: str) -> None:
        try:
            await redis.publish(channel, json.dumps({"type": "delta", "text": text}))
        except RedisError:
            # The stream is a side-channel. Losing it must never cost the answer.
            logger.debug("ai delta could not be published for turn %s", stream_id)

    # **THE OUTCOME IS OWNED HERE, NOT BY `run_turn`** -- so that a turn which
    # ends by RAISING still leaves behind the record of what it already read.
    # `run_turn` used to allocate it, and a raise therefore took the tool calls
    # with it: the route answered 429 or 500 having persisted nothing and
    # audited nothing, for a turn that had already read real estate data through
    # the agent. That is invariant #4, and this is the shape that fixes it.
    started = time.monotonic()
    outcome = TurnOutcome(model_id=getattr(provider, "id", ""))
    rate_limited: ProviderRateLimitedError | None = None

    # **THE ACT TOOL IS OFFERED ONLY WHEN ALL THREE HOLD**: the persona opts in
    # (`may_propose`), the SESSION was created act-capable (§0.8 -- fixed at
    # creation, no promotion), and the caller holds `ai.act.propose`. Any one of
    # them missing and the model is offered a read-only tool list, so a fully
    # persuaded model emits a name the executor's dict lookup does not have.
    #
    # The tool is built HERE, per turn, and thrown away with the turn. It carries
    # the session id, this turn's `message_seq` and the acting user, which is why
    # it cannot live in the frozen profile (§18.9).
    act_tools: dict[str, Any] = {}
    if ADMIN_PROFILE.may_propose and session_mode == "act" and "ai.act.propose" in codes:

        def _tainted() -> bool:
            """§7.3, read at INVOKE time rather than frozen at turn start: a
            proposal made after a hostile read in the SAME turn must carry the
            flag, and the findings that prove it do not exist yet when this
            closure is built."""
            return bool(replayed) or bool(outcome.findings)

        def _tenant_session() -> Any:
            return org_session(ctx.org_id, engine=app_engine, clients=ctx.clients)

        act_tools["actions.propose"] = ActTool(
            org_id=ctx.org_id,
            binding=ProposeBinding(
                persona=ADMIN_PROFILE.name,
                ai_session_id=session_row_id,
                message_seq=next_seq,
                actor_id=ctx.user_id,
                max_proposals_per_turn=effective.max_proposals_per_turn,
                action_expiry_s=effective.action_expiry_s,
            ),
            executor=ADMIN_PROFILE.executor,
            session_factory=_tenant_session,
            taint=_tainted,
        )

    try:
        await run_turn(
            provider=provider,
            profile=ADMIN_PROFILE,
            app=request.app,
            bearer=bearer,
            messages=history,
            allowed_tools=tools_for_permissions(codes, registry=ADMIN_PROFILE.tools),
            on_text=publish,
            outcome=outcome,
            act_tools=act_tools,
        )
    except ProviderRateLimitedError as exc:
        # Still a 429 with the provider's own hint (§10.4) -- but no longer a
        # 429 that erases a read.
        rate_limited = exc
        outcome.stop_reason = "provider_rate_limited"
        finish_turn(outcome, DEFAULTS, started)
    except Exception:
        # DELIBERATELY BROAD, AND IT ANSWERS 200 WITH A PARTIAL. Every other way
        # a turn can fail -- the deadline, a provider outage, an expired session
        # -- is already an honest partial answer rather than an error, and an
        # unforeseen failure is not a better reason to lose the turn than those.
        # It is also model-reachable: a tool call's arguments are model output,
        # so a crash here would otherwise be a per-turn denial of service AND an
        # unaudited read. The exception's own words go to the log and NOWHERE
        # ELSE: they can carry SQL, estate detail, and paths, and this answer is
        # shown to an administrator and stored in a transcript.
        logger.exception("ai turn failed unexpectedly for session %s", session_id)
        outcome.stop_reason = "internal_error"
        finish_turn(outcome, DEFAULTS, started)
    finally:
        try:
            await redis.publish(channel, json.dumps({"type": "end"}))
        except RedisError:
            logger.debug("ai end-of-turn could not be published for turn %s", stream_id)

    if rate_limited is not None and not outcome.tool_calls:
        # Nothing was read and nothing was answered, so there is no turn to
        # record: the 429 and §10.3's counters are the whole story. The rule is
        # "a turn that touched estate data is always recorded", not "every
        # rejected request becomes a transcript row".
        raise _rate_limited(rate_limited)

    # The replayed-history findings come FIRST: they are a property of what this
    # turn was given, not of what it went on to retrieve, and they are the ones
    # that must survive `store.MAX_AUDIT_SOURCES` truncation.
    outcome.findings = [*replayed, *outcome.findings]

    # 7. Reopen and persist -- one transaction (§5 step 6). ON EVERY PATH THAT
    #    REACHED THE PROVIDER, including the two failures above.
    async with org_session(ctx.org_id, engine=app_engine, clients=ctx.clients) as session:
        row = await session.get(AiSession, session_row_id)
        if row is None:  # pragma: no cover - deleted mid-turn
            raise _http_error(
                AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
            )
        _, assistant = await persist_turn(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            ai_session=row,
            question=body.question,
            outcome=outcome,
            turn_id=turn_id,
        )
        stored_tool_calls = list(assistant.tool_calls or [])

    await _record_tokens(redis, ctx.org_id, outcome.output_tokens + outcome.input_tokens)

    if rate_limited is not None:
        raise _rate_limited(rate_limited)
    return _turn_out(turn_id, outcome, stored_tool_calls, warnings)


def _rate_limited(exc: ProviderRateLimitedError) -> HTTPException:
    """§10.4's 429, carrying the PROVIDER's own retry hint rather than one this
    server invented."""
    headers = (
        {"Retry-After": str(int(exc.retry_after_s))} if exc.retry_after_s is not None else None
    )
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        tr("the model provider is rate limiting this deployment; try again shortly"),
        headers=headers,
    )


def _turn_out(
    turn_id: UUID,
    outcome: TurnOutcome,
    tool_calls: list[dict[str, Any]],
    warnings: list[str],
) -> TurnOut:
    return TurnOut(
        turn_id=turn_id,
        answer=outcome.answer,
        stop_reason=outcome.stop_reason,
        partial=outcome.partial,
        tool_calls=tool_calls,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        duration_ms=outcome.duration_ms,
        model_id=outcome.model_id,
        flagged=[
            {
                "tool": finding.tool,
                "source_type": finding.source_type,
                "source_id": finding.source_id,
                "matched_rule": finding.matched_rule,
            }
            for finding in outcome.findings
        ],
        warnings=warnings,
    )


@router.get("/ai/sessions/{session_id}/turns/{turn_id}/stream")
async def stream_turn(
    session_id: UUID,
    turn_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> StreamingResponse:
    """Server-Sent Events for one turn (§6).

    `turn_id` HERE IS THE ID THE CLIENT CHOSE AND SENT AS `TurnRequest.turn_id`
    -- the name of the stream, not the id the stored turn ends up with (see
    `_stored_turn_id`). This route reads no turn row and needs none: the turn it
    follows has not been written yet.

    **A TURN ID IS NOT A CAPABILITY, AND THE CHANNEL NAME IS WHAT MAKES THAT
    TRUE.** This route is gated on `ai.use` and verifies that the CONVERSATION
    belongs to the caller and to the token's org -- and then subscribes to a
    channel named for that verified conversation (`_channel`). The turn id from
    the path is only the second half of the name; on its own it reaches nothing.

    It was not always so, and the failure was not subtle: this route verified
    the conversation and then subscribed to `ai:turn:{turn_id}` taken straight
    from the path, so owning any conversation of your own let you read any turn
    in the deployment, across organizations, as the answer was written.
    `test_a_turn_id_alone_does_not_subscribe_to_another_orgs_live_answer` is
    what keeps that shut.

    When Redis is unavailable this returns 503 and the `POST` still works; the
    degradation is real, and `test_with_redis_down_the_stream_503s_and_the_post_
    still_answers` is what keeps it real.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await load_session(
                session, org_id=ctx.org_id, user_id=ctx.user_id, session_id=session_id
            )
    except AiNotFoundError as exc:
        raise _http_error(exc) from exc

    pubsub = request.app.state.redis.pubsub()
    try:
        await pubsub.subscribe(_channel(session_id, turn_id))
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "the answer stream is unavailable; the answer is still returned by "
                "the turn request itself"
            ),
        ) from exc

    deadline = time.monotonic() + DEFAULTS.wall_clock_s + _STREAM_GRACE_S
    return StreamingResponse(
        _stream_events(pubsub, deadline), media_type="text/event-stream"
    )


async def _stream_events(pubsub: Any, deadline: float) -> AsyncIterator[str]:
    """Republish the turn's deltas until it ends or the deadline passes.

    The deadline is what stops a stream outliving its turn: a `POST` that died
    without publishing its end marker would otherwise hold this connection open
    forever.
    """
    try:
        while time.monotonic() < deadline:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.5
                )
            except (RedisError, OSError):
                break
            if message is None:
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            if not isinstance(data, str):
                continue
            yield f"data: {data}\n\n"
            # PARSED, not substring-matched. An answer containing the literal
            # text `"end"` serializes to `\"end\"` inside the delta, and a
            # substring test would close the stream in the middle of the
            # sentence that mentioned it.
            try:
                if json.loads(data).get("type") == "end":
                    break
            except (ValueError, AttributeError):  # pragma: no cover - defensive
                continue
    except asyncio.CancelledError:  # pragma: no cover - client disconnected
        raise
    finally:
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 - teardown must not mask the response
            logger.debug("ai stream teardown failed")


# --------------------------------------------------------------------------- #
# Administration (`ai.admin`).
# --------------------------------------------------------------------------- #


@router.get("/ai/settings", response_model=SettingsOut)
async def get_ai_settings(
    request: Request, ctx: AuthContext = Depends(require_permission("ai.admin"))
) -> SettingsOut:
    """The effective settings. **This read creates no row.**"""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        effective = await get_settings(session, ctx.org_id)
    return SettingsOut(
        transcript_retention_days=effective.transcript_retention_days,
        max_turns_per_user_per_minute=effective.max_turns_per_user_per_minute,
        max_turns_per_org_per_hour=effective.max_turns_per_org_per_hour,
        daily_token_budget=effective.daily_token_budget,
        action_expiry_s=effective.action_expiry_s,
        max_proposals_per_turn=effective.max_proposals_per_turn,
    )


@router.put("/ai/settings", response_model=SettingsOut)
async def put_ai_settings(
    body: SettingsUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.admin")),
) -> SettingsOut:
    """Write this org's settings. Audited, with old and new values.

    There is no base-URL field and there never will be (§0.10), and since §0.14
    there is no `enabled`, no `provider_key` and no `egress_acknowledged`
    either: every field this route writes is a cost or retention dial. The
    audit event records the deployment's configured provider so that "where was
    this org's data going when this was written" still has an answer.
    """
    changes = body.model_dump(exclude_none=True)
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await update_settings(session, ctx.org_id, ctx.user_id, **changes)
            return SettingsOut.model_validate(row)
    except AiValidationError as exc:
        raise _http_error(exc) from exc


@router.get("/ai/providers", response_model=list[ProviderOut])
async def list_ai_providers(
    ctx: AuthContext = Depends(require_permission("ai.admin")),
) -> list[ProviderOut]:
    """The provider this SERVER is configured with -- key, kind and model name.

    **AT MOST ONE ENTRY, AND NOBODY PICKS FROM IT** (§0.14). The list shape is
    kept because the route is a published contract and an unconfigured
    deployment has to be able to answer "none"; what changed is that the console
    renders it as a statement of where this deployment sends, beside the egress
    disclosure, rather than as a dropdown.

    **NEVER THE BASE URL AND NEVER KEY MATERIAL.** A base URL in this response
    is a base URL in a browser's network tab, a bug report and a screenshot.
    """
    return [
        ProviderOut(key=config.key, kind=config.kind, model=config.model)
        for config in configured_providers().values()
    ]


@router.get("/ai/injection-flags", response_model=list[FlagOut])
async def list_injection_flags(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    ctx: AuthContext = Depends(require_permission("ai.admin")),
) -> list[FlagOut]:
    """§3.3's "for admin review": org-wide, most recent first.

    This is the ONE place stored retrieved text is served, and it is why the
    route is `ai.admin` rather than `ai.use` -- the excerpts are attacker-
    controlled prose from unauthenticated sources, and reading them across
    every conversation in the org is an administrative act.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(AiInjectionFlag)
                    .where(AiInjectionFlag.org_id == ctx.org_id)
                    .order_by(AiInjectionFlag.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [FlagOut.model_validate(row) for row in rows]


# --------------------------------------------------------------------------- #
# Act mode (docs/modules/ai-automation.md §6, §9.2, §12).
# --------------------------------------------------------------------------- #
#
# **THESE HANDLERS DELEGATE AND DO NOTHING ELSE.** They resolve the principal,
# call `app.core.ai.act`, and map an error type to a status code. No proposal
# logic, no digest arithmetic, no `JobEngine` call and no params construction
# lives in this file -- act mode belongs to the SHARED layer, not to this
# persona (§0.1), and `tests/test_ai_admin_boundaries.py` asserts that this
# package defines none of it.
#
# THE CONFIRM CARRIES AN ID AND A DIGEST AND NOTHING ELSE (§6.4). There is no
# request field here through which a job type, a parameter or a target could
# reach `JobEngine.create`; everything dispatched is read off the stored row.


def _turn_context(request: Request) -> TurnContext:
    """A `TurnContext` for the ONE loopback read the confirm path makes: "may
    this principal see the target device" (§9.2's third part).

    The bearer comes from THIS request's own headers and is passed as an
    argument, never put on `AuthContext` -- that dataclass reaches every module,
    and a token on it is one careless `logger.debug(ctx)` from living in a log
    file forever (§0.1).
    """
    return TurnContext(
        app=request.app,
        bearer=request.headers.get("Authorization", ""),
        deadline=time.monotonic() + DEFAULTS.wall_clock_s,
    )


async def _require_device_read(session: Any, ctx: AuthContext) -> None:
    """§12 as amended by §19.19: reading a PROPOSAL costs what reading a DEVICE
    costs.

    `ActionOut` carries `target_device_id`, `job_type` and
    `approved_action_key`; `ActionPreviewOut` adds the target's HOSTNAME, the
    rendered params and the model's rationale. All of that is device-identifying
    data about a machine the principal may not be allowed to see, served from a
    queue that lists every persona's proposals in the org. `ai.use` alone is not
    the right price for it.

    **`devices.read` IS EXACTLY THE RIGHT SECOND GATE** because device
    visibility in this platform IS org membership plus that code -- it is what
    the confirm path's loopback probe re-runs (`act._device_is_visible`) and
    what the `devices.health` read tool is gated on. When §11.1's device
    ownership eventually lands, "the devices this principal may see" stops being
    org-wide and this becomes a per-row filter; that is written down in §19.19
    rather than left for somebody to notice.

    **CHECKED IN THE HANDLER, NOT ON THE ROUTE**, for the reason
    `app/modules/ai_copilot/api.py` states at length: `require_permission`
    carries exactly one code and its shape is load-bearing --
    `app/core/rbac.py::route_permission` recovers that code by reflection over
    the closure, which is what makes RBAC completeness derivable from the
    router. The route keeps the gate §12 names; this is the second.
    """
    codes = await user_permission_codes(session, ctx.user_id)
    if "devices.read" not in codes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            tr(
                "reading an AI action additionally requires devices.read: it names a device, "
                "its hostname and what would run on it"
            ),
        )


@router.get("/ai/actions", response_model=list[ActionOut])
async def list_ai_actions(
    request: Request,
    session_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> list[ActionOut]:
    """This org's proposals, most recent first. **NO `rationale`** -- see
    `ActionOut`: a one-region list row cannot honour §6.3's separation.

    `ai.use` on the route, `devices.read` in the handler -- see
    `_require_device_read` for why a proposal costs what a device costs."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await _require_device_read(session, ctx)
        rows = await list_actions(
            session, org_id=ctx.org_id, ai_session_id=session_id, limit=limit
        )
        return [ActionOut.model_validate(row) for row in rows]


@router.get("/ai/actions/{ai_action_id}", response_model=ActionPreviewOut)
async def get_ai_action(
    ai_action_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.use")),
) -> ActionPreviewOut:
    """§6.2's preview, re-rendered from the stored catalogue row and the resolved
    target. A cross-org id is a 404, never another org's row (§13 test 17).

    `ai.use` on the route, `devices.read` in the handler, and the 403 comes
    BEFORE the row is loaded -- so a principal without it cannot use this route
    as an existence oracle over `ai_action.id` either."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await _require_device_read(session, ctx)
            preview = await load_preview(session, org_id=ctx.org_id, ai_action_id=ai_action_id)
    except AiNotFoundError as exc:
        raise _http_error(exc) from exc
    return ActionPreviewOut.model_validate(preview)


@router.post("/ai/actions/{ai_action_id}/confirm", response_model=ActionDispatchedOut)
async def confirm_ai_action(
    ai_action_id: UUID,
    body: ConfirmIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.act.confirm")),
) -> ActionDispatchedOut:
    """A human confirms a previewed act, and the job is created (§6.5, §9.2).

    **THE ROUTE'S GATE IS ONLY THE FIRST OF THREE.** `ai.act.confirm` is not a
    skeleton key: `confirm_action` additionally requires the permission the job
    type's OWN module dispatch route requires (`dispatch_permission_for`, and
    `None` means refuse), and that the principal can read the target device
    through the loopback. A technician who cannot run `patch.apply` by hand
    cannot confirm one here, which is invariant #6.

    **PUBLISHED AFTER THE TENANT TRANSACTION COMMITS.**
    `app/modules/deploy/scripts.py::run_script` states the contract and health's
    routes follow it: a job published to Redis before the caller's transaction
    commits could be handed to an agent with no durable server record if that
    commit later failed. The envelope is built inside the block; the publish is
    below it.
    """
    engine = request.app.state.app_engine
    try:
        async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
            codes = await user_permission_codes(session, ctx.user_id)
            outcome = await confirm_action(
                session,
                org_id=ctx.org_id,
                ai_action_id=ai_action_id,
                digest=body.digest,
                actor_id=ctx.user_id,
                permissions=codes,
                job_engine=request.app.state.job_engine,
                ctx=_turn_context(request),
                executor=ADMIN_PROFILE.executor,
            )
    except (AiNotFoundError, ActConflict, ActExpired, ActForbidden) as exc:
        raise _http_error(exc) from exc

    await publish_job(
        request.app.state.redis,
        UUID(outcome.envelope["job"]["device"]),
        outcome.envelope,
    )
    return ActionDispatchedOut(
        ai_action_id=outcome.ai_action_id, job_id=outcome.job_id, state="dispatched"
    )


@router.post("/ai/actions/{ai_action_id}/reject", response_model=ActionOut)
async def reject_ai_action(
    ai_action_id: UUID,
    body: RejectIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.act.propose")),
) -> ActionOut:
    """A human declines. Gated on `ai.act.propose` rather than `ai.act.confirm`
    (§12): declining creates no effect, so requiring the higher grant would leave
    proposals nobody may clear sitting until the sweep expires them."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            codes = await user_permission_codes(session, ctx.user_id)
            row = await reject_action(
                session,
                org_id=ctx.org_id,
                ai_action_id=ai_action_id,
                reason=body.reason,
                actor_id=ctx.user_id,
                permissions=codes,
            )
            return ActionOut.model_validate(row)
    except (AiNotFoundError, ActConflict, ActForbidden, AiValidationError) as exc:
        raise _http_error(exc) from exc


# --------------------------------------------------------------------------- #
# The autonomy dial (docs/modules/ai-automation.md §4, §12).
# --------------------------------------------------------------------------- #
#
# ON THIS PERSONA'S ROUTER, AND GATED ON `ai.admin` -- not on
# `health.action.admin`. That grant governs WHAT may be run automatically as
# SYSTEM/root, which is the approved-action catalogue; this one governs WHICH AI
# PERSONA may reach it. Both are admin tier and an org-admin holds both, so the
# split costs nothing today and keeps the two questions separable.
#
# The handlers are thin on purpose: the resolution, the vocabulary check, the
# upsert and the audit write all live in `app/core/ai/autonomy.py`, because the
# dial is kernel state that the technician and end-user personas' own routers
# will read through the same functions.


@router.get("/ai/autonomy", response_model=list[CeilingOut])
async def list_ai_ceilings(
    request: Request, ctx: AuthContext = Depends(require_permission("ai.admin"))
) -> list[CeilingOut]:
    """This org's STORED ceilings. Unset combinations are absent, not `off`
    rows: a materialized default reads as configuration somebody made."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_ceilings(session, org_id=ctx.org_id)
    return [CeilingOut.model_validate(row) for row in rows]


@router.put("/ai/autonomy", response_model=CeilingOut)
async def put_ai_ceiling(
    body: CeilingUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ai.admin")),
) -> CeilingOut:
    """Set one ceiling. Refuses `autonomous` with a 400 (§0.4) and audits the
    change with BOTH levels."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await set_ceiling(
                session,
                org_id=ctx.org_id,
                persona=body.persona,
                risk_class=body.risk_class,
                level=body.level,
                actor_id=ctx.user_id,
            )
            rows = [
                row
                for row in await list_ceilings(session, org_id=ctx.org_id)
                if row["persona"] == body.persona and row["risk_class"] == body.risk_class
            ]
    except AiValidationError as exc:
        raise _http_error(exc) from exc
    return CeilingOut.model_validate(rows[0])

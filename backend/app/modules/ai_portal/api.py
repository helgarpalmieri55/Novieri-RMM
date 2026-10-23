"""The resolver's two routes (§12).

    POST /api/v1/portal/ai/sessions
    POST /api/v1/portal/ai/sessions/{session_id}/turns

**TWO ROUTES, AND THE ABSENCE OF A THIRD IS A TEST** (§13 test 18). No action
route exists on this surface AT ALL -- not confirm, not reject, not propose --
and `tests/test_ai_portal.py` derives that from the FLATTENED router rather than
from this docstring, so a future addition fails a build instead of shipping.

**TWO SESSION KINDS, TWO DATABASE ROLES, AND THE SPLIT IS DELIBERATE.**

* The resolver's own tables (`ai_session`, `ai_message`, `ai_injection_flag`) are
  written through `org_session`, on the `app_user` role. `portal_session` runs on
  the `portal_user` role, which holds a deliberately tiny grant set aimed at the
  portal's own tables -- it is not, and must not become, a role that can write
  the AI layer's transcript.
* Every read OF PORTAL DATA goes through the loopback, which re-enters the portal
  routes and lets THEM open `portal_session` with both GUCs pinned. This layer
  never opens a `portal_session` of its own, so it never decides which GUCs get
  set, and `portal_session`'s fail-closed property stays the portal's.

The principal is a `PortalContext`, which is deliberately not an `AuthContext`,
so nothing in this file can be handed to a staff dependency by mistake.
"""

import logging
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.ai.errors import AiDisabledError, AiNotFoundError
from app.core.ai.orchestrator import (
    DEFAULTS,
    TurnOutcome,
    finish_turn,
    replay_history,
    replay_policy_for,
    run_turn,
)
from app.core.ai.providers import (
    Message,
    ProviderNotConfiguredError,
    TextBlock,
    configured_provider,
    get_provider,
)
from app.core.ai.store import create_session, load_session, persist_turn
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import AiSession
from app.modules.ai_portal.profile import RESOLVER_PROFILE
from app.modules.ai_portal.schema import (
    ResolverSessionOut,
    ResolverTurnOut,
    ResolverTurnRequest,
)
from app.modules.portal.principal import PortalContext, require_portal_permission

logger = logging.getLogger("its.ai_portal.api")

router = APIRouter(tags=["ai-portal"])

#: The resolver answers about this person's own tickets, so its turns are
#: shorter and cheaper than a staff turn's. Bounded HERE rather than inherited
#: because the population is larger, unauthenticated at the ingress that feeds
#: it, and has no reason to need eight tool calls to read three of its own rows.
_RESOLVER_BUDGETS = DEFAULTS.__class__(max_tool_calls=4, wall_clock_s=30.0)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AiNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, AiDisabledError | ProviderNotConfiguredError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _provider_for() -> Any:
    """A named seam, matching both staff surfaces'. Tests replace THIS, so the
    gates, the executor, the tenancy and the persistence all run as they do in
    production."""
    return get_provider()


def _provider_configured() -> bool:
    """Whether this DEPLOYMENT can answer at all (§0.14).

    The per-org `enabled` flag and the per-org provider selection this used to
    consult are gone: a control plane with `ITS_AI_*` configured has the
    assistant for every organization on it, and one without has it for none. It
    asks the same seam the turn asks, so availability and the turn can never
    disagree about whether a provider exists.
    """
    try:
        _provider_for()
    except ProviderNotConfiguredError:
        return False
    return True


def _configured_key() -> str | None:
    """The key of the provider that answered, for the `ai.session_started` audit
    metadata. A RECORD of what happened, not a setting anybody chose -- a
    transcript is read months after `.env` has moved on."""
    config = configured_provider()
    return config.key if config is not None else None


@router.post(
    "/portal/ai/sessions",
    response_model=ResolverSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_resolver_session(
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> ResolverSessionOut:
    """Start a resolver conversation.

    `mode` is not a parameter and `create_session`'s default is `read`. §0.9
    gives this persona no act at any dial position, so there is no request field
    through which an act-capable session could be asked for.
    """
    engine = request.app.state.app_engine
    if not _provider_configured():
        raise _http_error(
            AiDisabledError(tr("the assistant is not available for this organization"))
        )
    provider = _provider_for()

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        row = await create_session(
            session,
            org_id=ctx.org_id,
            user_id=ctx.requester_user_id,
            persona=RESOLVER_PROFILE.name,
            model_id=getattr(provider, "id", ""),
            provider_key=_configured_key(),
        )
        return ResolverSessionOut.model_validate(row)


@router.post("/portal/ai/sessions/{session_id}/turns", response_model=ResolverTurnOut)
async def create_resolver_turn(
    session_id: UUID,
    body: ResolverTurnRequest,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> ResolverTurnOut:
    """Ask the resolver.

    **NO ACT TOOL IS BUILT HERE, AND THERE IS NO BRANCH THAT COULD BUILD ONE.**
    `run_turn` is called without `act_tools`, so the resolver's tool list is its
    three portal reads and nothing else -- a model that emits `actions.propose`
    gets `unknown_tool` from the executor's dict lookup and ZERO HTTP REQUESTS,
    which is what `tests/test_ai_portal.py` asserts on the transport tape.

    The turn is persisted and audited on every exit path, including the broad
    failure below: invariant #4 does not care that the caller was a customer.
    """
    engine = request.app.state.app_engine
    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        try:
            ai_session, messages = await load_session(
                session,
                org_id=ctx.org_id,
                user_id=ctx.requester_user_id,
                session_id=session_id,
            )
        except AiNotFoundError as exc:
            raise _http_error(exc) from exc
        if ai_session.persona != RESOLVER_PROFILE.name:
            # A staff conversation is not continuable here: its transcript was
            # assembled under a staff persona's prompt and tool set, and it may
            # contain internal notes this principal must never see.
            raise _http_error(
                AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
            )
        history, replayed = replay_history(
            messages, policy=replay_policy_for(RESOLVER_PROFILE, mode=ai_session.mode)
        )
        session_row_id = ai_session.id

    if not _provider_configured():
        raise _http_error(
            AiDisabledError(tr("the assistant is not available for this organization"))
        )
    provider = _provider_for()
    history.append(Message(role="user", content=(TextBlock(text=body.question),)))

    started = time.monotonic()
    outcome = TurnOutcome(model_id=getattr(provider, "id", ""))
    try:
        await run_turn(
            provider=provider,
            profile=RESOLVER_PROFILE,
            app=request.app,
            bearer=request.headers.get("Authorization", ""),
            messages=history,
            # NO PREFILTER BY PERMISSION CODE. All three tools are gated
            # `portal.ticket.read`, which `require_portal_permission` already
            # required to reach this route -- and `user_permission_codes` is a
            # STAFF-shaped read this surface has no business making.
            allowed_tools=tuple(RESOLVER_PROFILE.tools),
            budgets=_RESOLVER_BUDGETS,
            outcome=outcome,
        )
    except Exception:
        # DELIBERATELY BROAD, ANSWERING 200 WITH A PARTIAL, and the exception's
        # own words go to the log and NOWHERE ELSE -- they can carry SQL, paths
        # and estate detail, and this answer is shown to a customer.
        logger.exception("resolver turn failed unexpectedly for session %s", session_id)
        outcome.stop_reason = "internal_error"
    finish_turn(outcome, _RESOLVER_BUDGETS, started)

    outcome.findings = [*replayed, *outcome.findings]

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        row = await session.get(AiSession, session_row_id)
        if row is None:  # pragma: no cover - deleted mid-turn
            raise _http_error(
                AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
            )
        await persist_turn(
            session,
            org_id=ctx.org_id,
            user_id=ctx.requester_user_id,
            ai_session=row,
            question=body.question,
            outcome=outcome,
        )

    return ResolverTurnOut.of(
        answer=outcome.answer, partial=outcome.partial, findings=list(outcome.findings)
    )


__all__ = ["router"]

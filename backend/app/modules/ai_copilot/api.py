"""The copilot's two routes, inside one ticket (§12).

    POST /api/v1/service-desk/tickets/{ticket_id}/ai/sessions
    POST /api/v1/service-desk/tickets/{ticket_id}/ai/sessions/{session_id}/turns

**TWO ROUTES, BOTH `POST`, NEITHER OF WHICH WRITES TO A TICKET.** That is the
§0.3 guarantee stated structurally: this module registers no route that mutates a
ticket and imports no ticket write service (`add_comment`, `change_status`,
`update_ticket`), and `tests/test_ai_copilot.py` asserts both -- over the
flattened router and over this file's imports, never as a string ban, so the
docstrings explaining why the copilot does not post survive.

**THE ROUTE GATE IS `ticket.read`; `ai.use` IS CHECKED IN THE HANDLER.** §12 asks
for both, and `require_permission` carries exactly one code -- its shape is
load-bearing (`app/core/rbac.py::route_permission` recovers the code by
reflection over the closure, which is what makes RBAC completeness derivable from
the router) and must not be refactored to take two. So the route declares the
one a route-walking completeness test can see, and the handler checks the other
before it does anything. Which way round matters: `ticket.read` is the gate that
decides whether this principal may see the ticket at all, so it is the one that
belongs on the route.

**THE TICKET IS RE-RESOLVED ON EVERY TURN, AND THE DEVICE BINDING WITH IT.**
`ai_session` carries no `ticket_id` -- §5.3 says the only changes to that table
are `mode` and `persona`, and this build does not add a column the spec forbids.
The consequence is recorded rather than hidden: the ticket binding is PER TURN,
not per session, so a technician can carry one conversation across two of their
own tickets.

**THAT IS AN ACCEPTABLE CONSEQUENCE, AND IT IS NOW AN OBSERVATION RATHER THAN AN
ARGUMENT** (§19.20). Every security-relevant decision in this route reads THIS
turn's ticket and nothing on the session: the route gate (`ticket.read`), the
org-predicated ticket read, `ticket_device_id`, and `ticket_bound`. So a
conversation carried across two tickets is bound to the second one the moment it
moves. `tests/test_ai_copilot.py::test_the_bound_is_this_turns_ticket_and_not_
the_one_the_session_opened_on` runs three turns of ONE session across two
tickets and asserts the two rows actually written; a companion test re-asks
§19.1's fail-open on a LATER turn, where a carried-forward `ticket_bound` would
be exactly the way it came back. What it costs is transcript hygiene, by the
technician's own hand.

**One thing WAS missing, and it was in the record rather than in the check.**
Neither `ai_session` nor `ai_action` could say which ticket authorized a
proposal's device -- the whole justification for the one device this persona may
name. `ProposeBinding.ticket_id` now rides into `ai.action_proposed`'s metadata:
per-event and per-turn, which is the scope the binding actually has, and no
column on the table §5.3 protects.
"""

import logging
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.ai.act import ActTool, ProposeBinding
from app.core.ai.errors import AiDisabledError, AiNotFoundError
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
    ProviderNotConfiguredError,
    TextBlock,
    configured_provider,
    get_provider,
)
from app.core.ai.settings import get_settings
from app.core.ai.store import create_session, load_session, persist_turn
from app.core.i18n import tr
from app.core.rbac import user_permission_codes
from app.core.tenancy import org_session
from app.models import AiAction, AiSession
from app.modules.ai_copilot.profile import COPILOT_PROFILE
from app.modules.ai_copilot.schema import (
    CopilotSessionCreate,
    CopilotSessionOut,
    CopilotTurnOut,
    CopilotTurnRequest,
)
from app.modules.service_desk.models import Ticket

logger = logging.getLogger("its.ai_copilot.api")

router = APIRouter(tags=["ai-copilot"])

_PREFIX = "/service-desk/tickets/{ticket_id}/ai"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AiNotFoundError):
        # 404, NEVER 403: another user's conversation is indistinguishable from
        # one that never existed.
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, AiDisabledError | ProviderNotConfiguredError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _provider_for() -> Any:
    """A named seam, matching the admin surface's. Tests replace THIS, so every
    other line of the turn route -- the gates, the ticket resolution, the device
    binding, the persistence -- runs exactly as it does in production."""
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


async def _require_ai_use(session: Any, ctx: AuthContext) -> set[str]:
    """§12's second gate. Returns the caller's codes, which the turn needs
    anyway for the tool prefilter -- so this is one query, not two."""
    codes = set(await user_permission_codes(session, ctx.user_id))
    if "ai.use" not in codes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires ai.use"))
    return codes


async def _ticket_for(session: AsyncSession, *, org_id: UUID, ticket_id: UUID) -> Ticket:
    """The ticket, with an EXPLICIT `org_id` predicate in addition to RLS (§9.1).

    The route already required `ticket.read`, which is a grant over the org's
    tickets rather than over this one; RLS is what scopes it to the tenant, and
    this predicate is what makes the read still correct if it is ever executed on
    the admin engine.
    """
    row = (
        await session.execute(
            select(Ticket).where(Ticket.org_id == org_id, Ticket.id == ticket_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AiNotFoundError(tr("no ticket {ticket_id}", ticket_id=ticket_id))
    return row


@router.post(
    f"{_PREFIX}/sessions",
    response_model=CopilotSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_copilot_session(
    ticket_id: UUID,
    request: Request,
    body: CopilotSessionCreate | None = None,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> CopilotSessionOut:
    """Open a copilot conversation about one ticket.

    `mode='act'` additionally requires `ai.act.propose` and is checked BEFORE any
    row is written, exactly as the admin surface does it -- a rejected act
    session that had been written as a read session first would be a promotion
    path with extra steps (§0.8).
    """
    engine = request.app.state.app_engine
    mode = (body or CopilotSessionCreate()).mode
    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        codes = await _require_ai_use(session, ctx)
        try:
            await _ticket_for(session, org_id=ctx.org_id, ticket_id=ticket_id)
        except AiNotFoundError as exc:
            raise _http_error(exc) from exc

    if not _provider_configured():
        raise _http_error(
            AiDisabledError(tr("the AI assistant is not available for this organization"))
        )
    if mode == "act" and "ai.act.propose" not in codes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            tr("opening an act-capable conversation requires ai.act.propose"),
        )
    provider = _provider_for()

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        row = await create_session(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            persona=COPILOT_PROFILE.name,
            mode=mode,
            model_id=getattr(provider, "id", ""),
            provider_key=_configured_key(),
        )
        return CopilotSessionOut.model_validate(row)


@router.post(f"{_PREFIX}/sessions/{{session_id}}/turns", response_model=CopilotTurnOut)
async def create_copilot_turn(
    ticket_id: UUID,
    session_id: UUID,
    body: CopilotTurnRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> CopilotTurnOut:
    """Ask the copilot about this ticket.

    **THE DEVICE THE COPILOT MAY PROPOSE AGAINST IS `ticket.device_id`, RESOLVED
    HERE, ON THE SERVER** (§3.2). A ticket with no linked device gets an act tool
    bound to `None`, which `propose` step 6 refuses with a reason the model can
    repeat -- so the copilot says why it cannot prepare anything rather than
    proposing against a device it chose. Letting the model choose an arbitrary
    device would make "the ticket" decorative, and `ticket.device_id` is itself
    asserted by whoever created the ticket -- for an email-created ticket, by a
    parser reading text an unauthenticated sender wrote -- so it is a BOUND, not
    an authorization. The authorization is still the loopback `devices.read`.

    The orchestrator holds no database session across the model call: this route
    loads, closes, runs the turn, and reopens to persist (§0.1). A held session
    plus an inner loopback request would occupy two pool slots for as long as a
    third party's model takes to answer.
    """
    engine = request.app.state.app_engine

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
        codes = await _require_ai_use(session, ctx)
        effective = await get_settings(session, ctx.org_id)
        try:
            ticket = await _ticket_for(session, org_id=ctx.org_id, ticket_id=ticket_id)
            ticket_device_id = ticket.device_id
            ai_session, messages = await load_session(
                session, org_id=ctx.org_id, user_id=ctx.user_id, session_id=session_id
            )
        except AiNotFoundError as exc:
            raise _http_error(exc) from exc
        if ai_session.persona != COPILOT_PROFILE.name:
            # A conversation started on another persona's surface is not
            # continuable here: its transcript was assembled under that persona's
            # prompt, tool set and replay policy.
            raise _http_error(
                AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
            )
        history, replayed = replay_history(
            messages, policy=replay_policy_for(COPILOT_PROFILE, mode=ai_session.mode)
        )
        session_row_id = ai_session.id
        session_mode = ai_session.mode
        next_seq = (max((row.seq for row in messages), default=-1) + 1) + 1

    if not _provider_configured():
        raise _http_error(
            AiDisabledError(tr("the AI assistant is not available for this organization"))
        )
    provider = _provider_for()
    history.append(Message(role="user", content=(TextBlock(text=body.question),)))

    started = time.monotonic()
    outcome = TurnOutcome(model_id=getattr(provider, "id", ""))

    act_tools: dict[str, Any] = {}
    if COPILOT_PROFILE.may_propose and session_mode == "act" and "ai.act.propose" in codes:

        def _tainted() -> bool:
            """§7.3, read at INVOKE time. This persona reads ticket threads, so
            `tainted` is the flag most likely to be raised mid-turn here."""
            return bool(replayed) or bool(outcome.findings)

        def _tenant_session() -> Any:
            return org_session(ctx.org_id, engine=engine, clients=ctx.clients)

        act_tools["actions.propose"] = ActTool(
            org_id=ctx.org_id,
            binding=ProposeBinding(
                persona=COPILOT_PROFILE.name,
                ai_session_id=session_row_id,
                message_seq=next_seq,
                actor_id=ctx.user_id,
                # THE BOUND. `ticket_bound=True` says the binding APPLIES, so a
                # ticket that names no device refuses rather than falling through
                # to "no ticket context" -- which is what one field alone did.
                ticket_bound=True,
                ticket_device_id=ticket_device_id,
                # FOR THE AUDIT, NOT FOR A CHECK (§19.20). No gate reads it;
                # `ai.action_proposed` does, so the immutable record can say
                # which ticket authorized the one device this turn could name.
                ticket_id=ticket_id,
                max_proposals_per_turn=effective.max_proposals_per_turn,
                action_expiry_s=effective.action_expiry_s,
            ),
            executor=COPILOT_PROFILE.executor,
            session_factory=_tenant_session,
            taint=_tainted,
        )

    try:
        await run_turn(
            provider=provider,
            profile=COPILOT_PROFILE,
            app=request.app,
            bearer=request.headers.get("Authorization", ""),
            messages=history,
            allowed_tools=tools_for_permissions(codes, registry=COPILOT_PROFILE.tools),
            outcome=outcome,
            act_tools=act_tools,
        )
    except Exception:
        # DELIBERATELY BROAD, AND IT ANSWERS 200 WITH A PARTIAL, for the reason
        # the admin surface states: a turn that read estate data is persisted and
        # audited whether or not it ended well (invariant #4), and the
        # exception's own words go to the log and nowhere else.
        logger.exception("copilot turn failed unexpectedly for session %s", session_id)
        outcome.stop_reason = "internal_error"
    finish_turn(outcome, DEFAULTS, started)

    outcome.findings = [*replayed, *outcome.findings]

    async with org_session(ctx.org_id, engine=engine, clients=ctx.clients) as session:
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
        )
        stored_tool_calls = list(assistant.tool_calls or [])
        proposed = list(
            (
                await session.execute(
                    select(AiAction.id).where(
                        AiAction.org_id == ctx.org_id,
                        AiAction.session_id == session_row_id,
                        AiAction.message_seq == next_seq,
                    )
                )
            ).scalars()
        )

    return CopilotTurnOut(
        answer=outcome.answer,
        stop_reason=outcome.stop_reason,
        partial=outcome.partial,
        tool_calls=stored_tool_calls,
        flagged=[
            {
                "tool": finding.tool,
                "source_type": finding.source_type,
                "source_id": finding.source_id,
                "matched_rule": finding.matched_rule,
            }
            for finding in outcome.findings
        ],
        proposed=proposed,
    )


__all__ = ["router"]

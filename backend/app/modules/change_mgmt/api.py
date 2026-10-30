"""Change management REST API (docs/modules/change_management/spec.md §11-12).

Routes under ``/api/v1/changes``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

Unlike ``provisioning``/``backup``, this module dispatches no agent jobs --
``register(app)`` mounts this router only, there is no job-type registration
and no gateway edit.

Every ``ChangeValidationError`` raised by the service layer is translated to
an HTTP 4xx here -- it must never surface as a 500, and the translation is
by TYPE, not by matching exception-message text. ``ChangeNotFoundError`` (a
``ChangeValidationError`` subclass, raised for a missing change or a missing
link row) becomes a 404; every other ``ChangeValidationError`` (an illegal
transition, the SoD guard, an edit-guard on a non-draft change, an unknown
action_type, a link target that doesn't resolve under this org, a duplicate
link) becomes a 400. This is also how the segregation-of-duties guard in
``app.modules.change_mgmt.state.next_state`` surfaces at the API boundary: an
org-admin who created a change and then tries to approve/reject it themself
gets a 400, not a 500.

``GET /changes/{id}/evidence`` additionally writes a ``change.evidence_exported``
audit event (invariant #4) before returning -- evidence export is itself a
privileged, audited action. ``GET /changes/{id}/approvals`` is the separate,
deliberately non-auditing read used to render a change's approval timeline
on view -- merely looking at a change must never be indistinguishable from
exporting its evidence in the audit log, so this route records no audit
event at all; only the explicit ``/evidence`` export does.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import ChangeApproval, ChangeLink, ChangeRequest
from app.modules.change_mgmt.gates import list_gates, remove_gate, set_gate
from app.modules.change_mgmt.links import add_link, list_links, remove_link
from app.modules.change_mgmt.reporting import (
    change_calendar,
    change_evidence,
    fleet_change_evidence,
    list_change_approvals,
)
from app.modules.change_mgmt.schema import (
    ApprovalDecisionRequest,
    ApprovalOut,
    CalendarConflictOut,
    CalendarEntryOut,
    CalendarOut,
    ChangeCreateRequest,
    ChangeOut,
    ChangeUpdateRequest,
    EvidenceOut,
    GateOut,
    GateSetRequest,
    LinkCreateRequest,
    LinkOut,
    TransitionRequest,
)
from app.modules.change_mgmt.service import (
    create_change,
    decide_change,
    get_change,
    list_changes,
    transition_change,
    update_change,
)
from app.modules.change_mgmt.state import (
    ChangeClientError,
    ChangeConflictError,
    ChangeNotFoundError,
    ChangeValidationError,
)
from app.modules.change_mgmt.ticketing import get_ticket_adapter

router = APIRouter(prefix="/changes", tags=["change-management"])


def _http_error(exc: ChangeValidationError) -> HTTPException:
    """Translate a `ChangeValidationError` to an HTTP 4xx -- it must never
    become a 500.

    Routing is by TYPE, never by matching exception-message prose (a wording
    tweak in the service layer must never silently mis-route a status code).
    `ChangeNotFoundError` -- raised for "the resource named in the URL path
    doesn't exist under this org" (the change itself, via `_load_change`/
    `_verify_change_exists`, and the advisory link row, via `remove_link`) --
    becomes a 404. Every other `ChangeValidationError` is a 400 -- including
    `add_link`'s "linked action not found", which names a *request body*
    field (`action_id`) rather than a path segment, and is how a cross-org
    link target is rejected (invariant #5); the SoD guard (`approve cannot
    be the change requester`), an illegal transition, the draft-only edit
    guard, an unknown action_type, and a duplicate link all fall through to
    400 as well. A client this caller cannot see is a 404 too;
    `ChangeConflictError` (an archived client, for a new change or the
    ticket a submit opens) is a 409, and `ChangeClientError` ("Sin asignar")
    a 422. A client without change management never reaches here: its
    `ModuleNotEnabled` is the app's own 404 handler (`app/main.py`).
    """
    if isinstance(exc, ChangeNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ChangeConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ChangeClientError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _change_out(row: ChangeRequest) -> ChangeOut:
    return ChangeOut(
        id=row.id,
        client_id=row.client_id,
        type=row.type,
        title=row.title,
        description=row.description,
        risk=row.risk,
        affected=row.affected,
        window_start=row.window_start,
        window_end=row.window_end,
        rollback_plan=row.rollback_plan,
        state=row.state,
        created_by=row.created_by,
        created_at=row.created_at,
        origin=row.origin,
    )


def _approval_out(row: ChangeApproval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        approver_id=row.approver_id,
        decision=row.decision,
        comment=row.comment,
        is_retrospective=row.is_retrospective,
        decided_at=row.decided_at,
    )


def _link_out(row: ChangeLink) -> LinkOut:
    return LinkOut(
        id=row.id,
        action_type=row.action_type,
        action_id=row.action_id,
        linked_at=row.linked_at,
        linked_by=row.linked_by,
    )


# --------------------------------------------------------------------------- #
# GET|POST /changes
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ChangeOut])
async def list_changes_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> list[ChangeOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_changes(session, ctx.org_id)
        return [_change_out(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ChangeOut)
async def create_change_route(
    body: ChangeCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.request")),
) -> ChangeOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            change = await create_change(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=body.client_id,
                type_=body.type,
                title=body.title,
                description=body.description,
                risk=body.risk,
                affected=body.affected,
                window_start=body.window_start,
                window_end=body.window_end,
                rollback_plan=body.rollback_plan,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _change_out(change)


# --------------------------------------------------------------------------- #
# GET /changes/calendar, GET /changes/evidence -- must be registered before
# GET /changes/{change_id}, or FastAPI would try to parse "calendar"/
# "evidence" as a UUID path parameter.
# --------------------------------------------------------------------------- #


@router.get("/calendar", response_model=CalendarOut)
async def calendar_route(
    request: Request,
    frm: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> CalendarOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await change_calendar(session, ctx.org_id, frm=frm, to=to)
        return CalendarOut(
            entries=[CalendarEntryOut(**e) for e in data["entries"]],
            conflicts=[CalendarConflictOut(**c) for c in data["conflicts"]],
        )


@router.get("/evidence", response_model=list[EvidenceOut])
async def fleet_evidence_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> list[EvidenceOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        records = await fleet_change_evidence(session, ctx.org_id)
        results: list[EvidenceOut] = []
        for data in records:
            change = await get_change(session, ctx.org_id, data["change"]["id"])
            if change is None:  # pragma: no cover - can't race under org_session
                continue
            results.append(
                EvidenceOut(
                    change=_change_out(change),
                    approvals=[ApprovalOut(**a) for a in data["approvals"]],
                    links=[LinkOut(**link) for link in data["links"]],
                    audit_refs=list(data["audit_refs"]),
                )
            )
        return results


# --------------------------------------------------------------------------- #
# /changes/gates -- registered before /changes/{change_id} for the same reason
# as /calendar and /evidence.
# --------------------------------------------------------------------------- #


@router.get("/gates", response_model=list[GateOut])
async def list_gates_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> list[GateOut]:
    """Where patch approvals and deployments need an approved change."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return [GateOut(**row) for row in await list_gates(session, ctx.org_id)]


@router.put("/gates", response_model=GateOut)
async def set_gate_route(
    body: GateSetRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.gate.manage")),
) -> GateOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            gate = await set_gate(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_group_id=body.device_group_id,
                applies_to_patches=body.applies_to_patches,
                applies_to_deployments=body.applies_to_deployments,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        for row in await list_gates(session, ctx.org_id):
            if row["id"] == gate.id:
                return GateOut(**row)
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, tr("gate not readable"))


@router.delete("/gates/{gate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_gate_route(
    gate_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.gate.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await remove_gate(session, org_id=ctx.org_id, actor_id=ctx.user_id, gate_id=gate_id)
        except ChangeValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET|PATCH /changes/{change_id}
# --------------------------------------------------------------------------- #


@router.get("/{change_id}", response_model=ChangeOut)
async def get_change_route(
    change_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> ChangeOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        change = await get_change(session, ctx.org_id, change_id)
        if change is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change"))
        return _change_out(change)


@router.patch("/{change_id}", response_model=ChangeOut)
async def update_change_route(
    change_id: UUID,
    body: ChangeUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.request")),
) -> ChangeOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            change = await update_change(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                type_=body.type,
                title=body.title,
                description=body.description,
                risk=body.risk,
                affected=body.affected,
                window_start=body.window_start,
                window_end=body.window_end,
                rollback_plan=body.rollback_plan,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _change_out(change)


# --------------------------------------------------------------------------- #
# POST /changes/{change_id}/submit|approve|reject|transition
# --------------------------------------------------------------------------- #


@router.post("/{change_id}/submit", response_model=ChangeOut)
async def submit_change_route(
    change_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.request")),
) -> ChangeOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            change = await transition_change(
                session,
                get_ticket_adapter(state.settings),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                action="submit",
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _change_out(change)


@router.post("/{change_id}/approve", response_model=ApprovalOut)
async def approve_change_route(
    change_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.approve")),
) -> ApprovalOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            approval = await decide_change(
                session,
                get_ticket_adapter(state.settings),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                decision="approved",
                comment=body.comment,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _approval_out(approval)


@router.post("/{change_id}/reject", response_model=ApprovalOut)
async def reject_change_route(
    change_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.approve")),
) -> ApprovalOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            approval = await decide_change(
                session,
                get_ticket_adapter(state.settings),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                decision="rejected",
                comment=body.comment,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _approval_out(approval)


@router.post("/{change_id}/transition", response_model=ChangeOut)
async def transition_change_route(
    change_id: UUID,
    body: TransitionRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.request")),
) -> ChangeOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            change = await transition_change(
                session,
                get_ticket_adapter(state.settings),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                action=body.action,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _change_out(change)


# --------------------------------------------------------------------------- #
# GET|POST /changes/{change_id}/links, DELETE .../links/{link_id}
# --------------------------------------------------------------------------- #


@router.get("/{change_id}/links", response_model=list[LinkOut])
async def list_links_route(
    change_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> list[LinkOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        change = await get_change(session, ctx.org_id, change_id)
        if change is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change"))
        rows = await list_links(session, ctx.org_id, change_id)
        return [_link_out(r) for r in rows]


@router.post("/{change_id}/links", status_code=status.HTTP_201_CREATED, response_model=LinkOut)
async def add_link_route(
    change_id: UUID,
    body: LinkCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.manage")),
) -> LinkOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            link = await add_link(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                action_type=body.action_type,
                action_id=body.action_id,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return _link_out(link)


@router.delete("/{change_id}/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_link_route(
    change_id: UUID,
    link_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await remove_link(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                change_id=change_id,
                link_id=link_id,
            )
        except ChangeValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /changes/{change_id}/approvals -- plain read, NO audit write. This is
# the route the frontend uses to render a change's approval timeline; the
# audited export stays exclusively behind /evidence below.
# --------------------------------------------------------------------------- #


@router.get("/{change_id}/approvals", response_model=list[ApprovalOut])
async def list_approvals_route(
    change_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> list[ApprovalOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            approvals = await list_change_approvals(session, ctx.org_id, change_id)
        except ChangeValidationError as exc:
            raise _http_error(exc) from None
        return [ApprovalOut(**a) for a in approvals]


# --------------------------------------------------------------------------- #
# GET /changes/{change_id}/evidence -- also writes the change.evidence_exported
# audit event (invariant #4)
# --------------------------------------------------------------------------- #


@router.get("/{change_id}/evidence", response_model=EvidenceOut)
async def change_evidence_route(
    change_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("change.read")),
) -> EvidenceOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await change_evidence(session, ctx.org_id, change_id)
        if data["change"] is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change"))

        change = await get_change(session, ctx.org_id, change_id)
        if change is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change"))

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="change.evidence_exported",
            target_type="change",
            target_id=str(change_id),
            client_id=change.client_id,
            metadata={},
        )

        return EvidenceOut(
            change=_change_out(change),
            approvals=[ApprovalOut(**a) for a in data["approvals"]],
            links=[LinkOut(**link) for link in data["links"]],
            audit_refs=list(data["audit_refs"]),
        )

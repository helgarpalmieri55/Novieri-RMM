"""Remote-control REST API (docs/modules/remote-control/spec.md §11).

Routes under ``/api/v1/remote``. Every route is gated server-side by
``require_permission`` (the frontend never gates security), and no response or
request model carries the relay token, the checked-out credential secret, or any
other secret material — only ``credential_id`` and non-sensitive metadata.

Session creation is a two-phase operation to keep the signed-job dispatch and
the relay registration OUT of the tenant transaction: ``start_session`` records
the row + audit + token hash and QUEUES the external side effects on
``session.info["remote_pending"]``; this handler drains them via
``dispatch_pending`` ONLY AFTER ``org_session`` has committed. A rolled-back
create therefore never dispatches a job or registers a relay session (mirrors
the ``queue_ui_event`` post-commit pattern).

One browser WebSocket carries media: ``/sessions/{id}/screen``, the screen
share (spec-screen-share.md), where the api joins the browser to the gateway's
relay. The guacd path (rdp/vnc/ssh) is retired and refused.
"""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthContext, bound_auth_context, narrowed_to_any, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.ws_auth import AUTH_SUBPROTOCOL, token_from_subprotocols
from app.models import Device, RemotePolicy, RemoteSession
from app.modules.remote.models import RECORDING_MAX_BYTES
from app.modules.remote.screen_tunnel import run_screen_tunnel
from app.modules.remote.service import SCREEN, InvalidSessionState, ScreenUnavailable
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel, owner_or_admin

router = APIRouter(prefix="/remote", tags=["remote"])

_READ_PERMS = ("remote.control", "remote.recordings.read")


# ``require_permission`` is imported from the kernel (app.api.deps) — this module
# no longer forks it. Only ``require_any_permission`` (not in the kernel) is
# defined locally, for the read routes shared by operators and auditors.
def require_any_permission(*codes: str) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    """Allow the request if the caller holds ANY of ``codes``. Used for the
    read routes, which serve both operators (``remote.control``) and auditors
    (``remote.recordings.read``).

    ON EACH SESSION'S OWN CLIENT (MSP clients plan 2026-10-26 Task 14, T4-C2
    item 13): the request's scope is the clients where the caller holds one
    of them (`narrowed_to_any`), so a session of another client does not
    exist for it -- the list leaves it out and a read is the 404 a missing
    one gets. Held on no client at all: the 403, as before."""

    async def dependency(
        request: Request, ctx: AuthContext = Depends(bound_auth_context)
    ) -> AuthContext:
        narrowed = await narrowed_to_any(request, ctx, codes)
        if narrowed is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires one of {items}", items=", ".join(codes))
            )
        return narrowed

    return dependency


# --------------------------------------------------------------------------- #
# Models — none carry the relay token or any secret.
# --------------------------------------------------------------------------- #


class SessionCreateRequest(BaseModel):
    device_id: UUID
    protocol: Literal["screen", "rdp", "vnc", "ssh"] = "screen"
    mode: Literal["attended", "unattended"] = "attended"
    credential_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=500)


class SessionCreateResponse(BaseModel):
    session_id: UUID
    state: str
    consent_mode: str


class SessionOut(BaseModel):
    id: UUID
    device_id: UUID
    technician_id: UUID
    protocol: str
    consent_mode: str
    state: str
    credential_id: UUID | None
    has_recording: bool
    started_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    created_at: datetime
    # Whether the technician may control (screen sessions): policy allow_input
    # at start, false for good once the endpoint user presses Stop control.
    input_enabled: bool = True


class SessionListResponse(BaseModel):
    items: list[SessionOut]
    total: int


class PolicyOut(BaseModel):
    id: UUID
    device_group_id: UUID | None
    allow_unattended: bool
    record_sessions: bool
    allow_file_transfer: bool
    allow_clipboard: bool
    recording_retention_days: int
    consent_timeout_s: int
    allow_input: bool
    allow_privacy_blank: bool
    recording_max_bytes: int


class PolicyUpsertRequest(BaseModel):
    device_group_id: UUID | None = None
    allow_unattended: bool = False
    record_sessions: bool = True
    allow_file_transfer: bool = False
    allow_clipboard: bool = False
    recording_retention_days: int = Field(default=90, ge=1, le=3650)
    consent_timeout_s: int = Field(default=30, ge=15, le=120)
    allow_input: bool = True
    allow_privacy_blank: bool = False
    # 1 MiB .. 64 GiB; the default is 2 GiB.
    recording_max_bytes: int = Field(default=RECORDING_MAX_BYTES, ge=1 << 20, le=64 << 30)


def _session_out(row: RemoteSession) -> SessionOut:
    return SessionOut(
        id=row.id,
        device_id=row.device_id,
        technician_id=row.technician_id,
        protocol=row.protocol,
        consent_mode=row.consent_mode,
        state=row.state,
        credential_id=row.credential_id,
        has_recording=row.recording_ref is not None,
        started_at=row.started_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        created_at=row.created_at,
        input_enabled=row.input_enabled,
    )


def _policy_out(row: RemotePolicy) -> PolicyOut:
    return PolicyOut(
        id=row.id,
        device_group_id=row.device_group_id,
        allow_unattended=row.allow_unattended,
        record_sessions=row.record_sessions,
        allow_file_transfer=row.allow_file_transfer,
        allow_clipboard=row.allow_clipboard,
        recording_retention_days=row.recording_retention_days,
        consent_timeout_s=row.consent_timeout_s,
        allow_input=row.allow_input,
        allow_privacy_blank=row.allow_privacy_blank,
        recording_max_bytes=row.recording_max_bytes,
    )


def _object_store(conn: Request | WebSocket) -> ObjectStore:
    """Lazily build (and cache) the object store on first use. Construction hits
    the object store to ensure the bucket exists, so it is deferred out of app
    startup — recording retrieval and the live tunnel are the only paths that
    need it. Accepts a Request or a WebSocket (both expose ``.app.state``)."""
    store = getattr(conn.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(conn.app.state.settings)
        conn.app.state.object_store = store
    return store


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionCreateResponse,
)
async def start_session(
    body: SessionCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("remote.control")),
) -> SessionCreateResponse:
    state = request.app.state
    service = state.remote_service
    if body.protocol != SCREEN:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            tr("{protocol} is retired; start a screen session instead", protocol=body.protocol),
        )
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        device = (
            await session.execute(select(Device).where(Device.id == body.device_id))
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        try:
            row = await service.start_session(
                session,
                org_id=ctx.org_id,
                technician_id=ctx.user_id,
                device_id=body.device_id,
                protocol=body.protocol,
                requested_mode=body.mode,
                credential_id=body.credential_id,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        except ScreenUnavailable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        out = SessionCreateResponse(
            session_id=row.id, state=row.state, consent_mode=row.consent_mode
        )
        # Capture the deferred side effects while still inside the tenant
        # session; they are dispatched only after the block commits below.
        pending = list(session.info.get("remote_pending", []))
    # Transaction committed: safe to register the relay and publish the job.
    await service.dispatch_pending(pending, state.redis)
    return out


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    ctx: AuthContext = Depends(require_any_permission(*_READ_PERMS)),
    state_filter: str | None = Query(default=None, alias="state"),
    device_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SessionListResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        query = select(RemoteSession)
        if state_filter:
            query = query.where(RemoteSession.state == state_filter)
        if device_id:
            query = query.where(RemoteSession.device_id == device_id)
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    query.order_by(RemoteSession.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return SessionListResponse(items=[_session_out(r) for r in rows], total=total)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_any_permission(*_READ_PERMS)),
) -> SessionOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(RemoteSession).where(RemoteSession.id == session_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such session"))
        return _session_out(row)


@router.post("/sessions/{session_id}/stop", response_model=SessionOut)
async def stop_session(
    session_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("remote.control")),
) -> SessionOut:
    state = request.app.state
    service = state.remote_service
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        row = (
            await session.execute(
                select(RemoteSession).where(RemoteSession.id == session_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such session"))
        # Spec §11, phase 3 design §5.3: stop is allowed only for the session
        # OWNER or someone holding ``remote.supervise`` on its client — the
        # ``remote.control`` permission alone (enforced above) is not enough.
        # Reuse the exact predicate the tunnel WS gate uses so the two paths
        # cannot diverge.
        if not await owner_or_admin(
            session,
            user_id=ctx.user_id,
            session_technician_id=row.technician_id,
            # Its machine's client (plan 2026-10-26 Task 14), loaded with the
            # row just SELECTed.
            client_id=row.client_id,
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("requires the session owner or remote.supervise")
            )
        try:
            # The person who stopped it is the actor (phase 3 design §5.3).
            # Left to its default, `end_session` names the session's owner,
            # which is right only when the owner is the one stopping it.
            await service.end_session(
                session, session_id=session_id, reason="completed", actor_id=ctx.user_id
            )
        except InvalidSessionState as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        await session.refresh(row)
        return _session_out(row)


@router.get("/sessions/{session_id}/recording")
async def get_recording(
    session_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("remote.recordings.read")),
) -> Response:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(RemoteSession).where(RemoteSession.id == session_id)
            )
        ).scalar_one_or_none()
        if row is None or row.recording_ref is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no recording for session"))
        ref = row.recording_ref
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="remote.recording_accessed",
            target_type="session",
            target_id=str(session_id),
            metadata={"device_id": str(row.device_id)},
        )
    data = await _object_store(request).get_remote_recording(ref)
    ext = "rsp" if ref.endswith(".rsp") else "guac"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{session_id}.{ext}"',
        },
    )


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    request: Request,
    ctx: AuthContext = Depends(require_permission("remote.control")),
) -> list[PolicyOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(RemotePolicy).order_by(RemotePolicy.id)
                )
            )
            .scalars()
            .all()
        )
        return [_policy_out(r) for r in rows]


@router.put("/policies", response_model=PolicyOut)
async def upsert_policy(
    body: PolicyUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("remote.policy.manage")),
) -> PolicyOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        # Locate the existing row to update. For the org-default (device_group_id
        # IS NULL) we must NOT create a second row: Postgres's UNIQUE allows
        # duplicate NULLs, so single-org-default is enforced here in code by
        # deterministically taking the first existing default (matches policy.py
        # and recording.py's order_by(id).limit(1)).
        query = select(RemotePolicy)
        if body.device_group_id is None:
            query = query.where(RemotePolicy.device_group_id.is_(None))
        else:
            query = query.where(RemotePolicy.device_group_id == body.device_group_id)
        row = (
            await session.execute(query.order_by(RemotePolicy.id).limit(1))
        ).scalar_one_or_none()

        created = row is None
        if row is None:
            row = RemotePolicy(org_id=ctx.org_id, device_group_id=body.device_group_id)
            session.add(row)
        row.allow_unattended = body.allow_unattended
        row.record_sessions = body.record_sessions
        row.allow_file_transfer = body.allow_file_transfer
        row.allow_clipboard = body.allow_clipboard
        row.recording_retention_days = body.recording_retention_days
        row.consent_timeout_s = body.consent_timeout_s
        row.allow_input = body.allow_input
        row.allow_privacy_blank = body.allow_privacy_blank
        row.recording_max_bytes = body.recording_max_bytes
        await session.flush()

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="remote.policy.changed",
            target_type="remote_policy",
            target_id=str(row.id),
            metadata={
                "device_group_id": (
                    str(body.device_group_id) if body.device_group_id else None
                ),
                "created": created,
            },
        )
        return _policy_out(row)


@router.websocket("/sessions/{session_id}/screen")
async def session_screen(websocket: WebSocket, session_id: UUID) -> None:
    """Browser-facing rsp/1 stream for a screen session.

    Admission: the bearer token from the ``Sec-WebSocket-Protocol`` header
    (never the query string, which access logs record), then
    ``remote.control`` and owner-or-admin, all server-side. The browser sends
    and receives rsp/1 frames as binary messages; text messages are ignored.
    """
    state = websocket.app.state
    token = token_from_subprotocols(websocket)
    try:
        org_id, _user_id, _clients = await authorize_tunnel(
            token=token,
            session_id=session_id,
            settings=state.settings,
            jwks=state.jwks,
            admin_engine=state.admin_engine,
            app_engine=state.app_engine,
        )
    except TunnelAuthError as exc:
        await websocket.close(code=exc.code)
        return

    service = state.remote_service
    params = service.connection_params(session_id)
    if params is None or params.get("protocol") != SCREEN:
        # Not a live screen session held by this api process.
        await websocket.close(code=4409)
        return

    await websocket.accept(subprotocol=AUTH_SUBPROTOCOL)

    async def browser_send(data: bytes) -> None:
        await websocket.send_bytes(data)

    async def browser_recv() -> bytes | None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return None
            data = message.get("bytes")
            if isinstance(data, bytes):
                return data

    try:
        await run_screen_tunnel(
            browser_send=browser_send,
            browser_recv=browser_recv,
            session_id=session_id,
            org_id=org_id,
            service=service,
            store=lambda: _object_store(websocket),
            app_engine=state.app_engine,
            relay_internal_url=state.settings.relay_internal_url,
            spool_dir=state.settings.remote_recording_spool_dir,
        )
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass

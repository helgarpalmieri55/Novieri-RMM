"""Agent self-update routes (spec-self-update.md §8.3).

`agent.update.read` reads; `agent.update.rollout` does everything a rollout
does; an `is_rollback` rollout additionally needs `agent.update.rollback`.
Promotion and release signatures write the global catalogue and stay with
`agent.release.manage` (super-admin). Nothing here dispatches a job: the
reconciler does, for rings a person started.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.core.tenancy import org_session
from app.modules.agent_update import service as svc
from app.modules.agent_update.models import AgentUpdateRollout
from app.modules.agent_update.schemas import (
    FleetVersionsOut,
    ManualUpdateDeviceOut,
    PreviewOut,
    PromoteIn,
    PromoteOut,
    ReleaseSignatureIn,
    RingOut,
    RingPreviewOut,
    RolloutIn,
    RolloutOut,
    TargetOut,
)

router = APIRouter(prefix="/agent", tags=["agent-update"])

_READ = "agent.update.read"
_ROLLOUT = "agent.update.rollout"
_ROLLBACK = "agent.update.rollback"
#: A rollout updates the agent across the organization's whole fleet -- one
#: `agent_update_rollouts` row, not one client's data -- so starting, steering
#: or previewing one needs a scope of every client (MSP clients spec §2.2).
_ROLLOUT_GATE = require_permission_on_every_client(_ROLLOUT)


def _http(exc: svc.RolloutError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


async def _require_rollback(request: Request, ctx: AuthContext) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        allowed = await has_permission(session, ctx.user_id, _ROLLBACK)
    if not allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, tr("requires {permission}", permission=_ROLLBACK)
        )


def _specs(body: RolloutIn) -> list[svc.RingSpec]:
    return [svc.RingSpec(r.name, r.device_group_id, r.is_pilot) for r in body.rings]


async def _rollout_out(session: AsyncSession, rollout: AgentUpdateRollout) -> RolloutOut:
    counts = await svc.target_counts(session, rollout.id)
    rings = await svc.rings_of(session, rollout.id)
    return RolloutOut(
        id=rollout.id,
        target_version=rollout.target_version,
        is_rollback=rollout.is_rollback,
        state=rollout.state,
        batch_size=rollout.batch_size,
        failure_threshold_pct=rollout.failure_threshold_pct,
        health_window_s=rollout.health_window_s,
        reason=rollout.reason,
        pause_reason=rollout.pause_reason,
        created_at=rollout.created_at,
        started_at=rollout.started_at,
        completed_at=rollout.completed_at,
        rings=[
            RingOut(
                id=r.id,
                ordinal=r.ordinal,
                name=r.name,
                is_pilot=r.is_pilot,
                device_group_id=r.device_group_id,
                state=r.state,
                activated_at=r.activated_at,
                counts=counts.get(r.id, {}),
            )
            for r in rings
        ],
    )


@router.get("/fleet-versions", response_model=FleetVersionsOut)
async def fleet_versions_route(
    request: Request, ctx: AuthContext = Depends(require_permission(_READ))
) -> FleetVersionsOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        fv = await svc.fleet_versions(session)
    return FleetVersionsOut(
        total=fv.total,
        versions=fv.versions,
        latest_stable=fv.latest_stable,
        update_available=fv.update_available,
        needs_manual_update=[
            ManualUpdateDeviceOut(
                device_id=d.device_id,
                hostname=d.hostname,
                version=d.version,
                reason="version_unknown" if not d.version else "no_agent_update",
            )
            for d in fv.needs_manual_update
        ],
    )


@router.get("/update-rollouts", response_model=list[RolloutOut])
async def list_rollouts_route(
    request: Request, ctx: AuthContext = Depends(require_permission(_READ))
) -> list[RolloutOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return [await _rollout_out(session, r) for r in await svc.list_rollouts(session)]


@router.get("/update-rollouts/{rollout_id}", response_model=RolloutOut)
async def get_rollout_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(require_permission(_READ))
) -> RolloutOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            return await _rollout_out(session, await svc.get_rollout(session, rollout_id))
        except svc.RolloutError as exc:
            raise _http(exc) from None


@router.get("/update-rollouts/{rollout_id}/targets", response_model=list[TargetOut])
async def list_targets_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(require_permission(_READ))
) -> list[TargetOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rows = await svc.list_targets(session, rollout_id)
        except svc.RolloutError as exc:
            raise _http(exc) from None
    return [
        TargetOut(
            id=t.id,
            ring_id=t.ring_id,
            device_id=t.device_id,
            hostname=hostname,
            from_version=t.from_version,
            state=t.state,
            reason=t.reason,
            attempts=t.attempts,
            job_id=t.job_id,
            updated_at=t.updated_at,
        )
        for t, hostname in rows
    ]


@router.post("/update-rollouts/preview", response_model=PreviewOut)
async def preview_route(
    body: RolloutIn, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> PreviewOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            pv = await svc.preview(
                session,
                target_version=body.target_version,
                is_rollback=body.is_rollback,
                rings=_specs(body),
            )
        except svc.RolloutError as exc:
            raise _http(exc) from None
    return PreviewOut(
        target_version=pv.target_version,
        is_rollback=pv.is_rollback,
        channel=pv.channel,
        rings=[RingPreviewOut(**vars(r)) for r in pv.rings],
    )


@router.post("/update-rollouts", status_code=status.HTTP_201_CREATED, response_model=RolloutOut)
async def create_rollout_route(
    body: RolloutIn, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    if body.is_rollback:
        await _require_rollback(request, ctx)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rollout = await svc.create_rollout(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                target_version=body.target_version,
                is_rollback=body.is_rollback,
                reason=body.reason,
                rings=_specs(body),
                batch_size=body.batch_size,
                failure_threshold_pct=body.failure_threshold_pct,
                health_window_s=body.health_window_s,
            )
        except svc.RolloutError as exc:
            raise _http(exc) from None
        return await _rollout_out(session, rollout)


async def _act(
    request: Request, ctx: AuthContext, rollout_id: UUID, action: str, ring_id: UUID | None = None
) -> RolloutOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rollout = await svc.get_rollout(session, rollout_id)
            if rollout.is_rollback and action in ("start", "resume", "advance"):
                await _require_rollback(request, ctx)
            if action == "start":
                await svc.start_rollout(
                    session, org_id=ctx.org_id, actor_id=ctx.user_id, rollout_id=rollout_id
                )
            elif action == "pause":
                await svc.pause_rollout(session, rollout_id=rollout_id, actor_id=ctx.user_id)
            elif action == "resume":
                await svc.resume_rollout(session, rollout_id=rollout_id, actor_id=ctx.user_id)
            elif action == "advance":
                await svc.advance_ring(session, rollout_id=rollout_id, actor_id=ctx.user_id)
            elif action == "cancel":
                await svc.cancel_rollout(session, rollout_id=rollout_id, actor_id=ctx.user_id)
            elif action == "close" and ring_id is not None:
                await svc.close_ring(
                    session, rollout_id=rollout_id, ring_id=ring_id, actor_id=ctx.user_id
                )
        except svc.RolloutError as exc:
            raise _http(exc) from None
        return await _rollout_out(session, await svc.get_rollout(session, rollout_id))


@router.post("/update-rollouts/{rollout_id}/start", response_model=RolloutOut)
async def start_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "start")


@router.post("/update-rollouts/{rollout_id}/pause", response_model=RolloutOut)
async def pause_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "pause")


@router.post("/update-rollouts/{rollout_id}/resume", response_model=RolloutOut)
async def resume_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "resume")


@router.post("/update-rollouts/{rollout_id}/advance", response_model=RolloutOut)
async def advance_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "advance")


@router.post("/update-rollouts/{rollout_id}/rings/{ring_id}/close", response_model=RolloutOut)
async def close_ring_route(
    rollout_id: UUID,
    ring_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_ROLLOUT_GATE),
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "close", ring_id)


@router.post("/update-rollouts/{rollout_id}/cancel", response_model=RolloutOut)
async def cancel_route(
    rollout_id: UUID, request: Request, ctx: AuthContext = Depends(_ROLLOUT_GATE)
) -> RolloutOut:
    return await _act(request, ctx, rollout_id, "cancel")


@router.post("/releases/versions/{version}/promote", response_model=PromoteOut)
async def promote_route(
    version: str,
    body: PromoteIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> PromoteOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rows = await svc.promote_version(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                version=version,
                channel=body.channel,
            )
        except svc.RolloutError as exc:
            raise _http(exc) from None
    return PromoteOut(version=version, channel=body.channel, rows=rows)


@router.put("/releases/{release_id}/release-signature", status_code=status.HTTP_204_NO_CONTENT)
async def release_signature_route(
    release_id: UUID,
    body: ReleaseSignatureIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await svc.set_release_signature(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                release_id=release_id,
                signature=body.release_signature,
            )
        except svc.RolloutError as exc:
            raise _http(exc) from None

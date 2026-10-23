"""BCP/DR REST API (docs/modules/bcp/spec.md §9).

Routes under ``/api/v1/bcp``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

Like ``change_mgmt``, this module dispatches no agent jobs -- ``register(app)``
mounts this router only, there is no job-type registration and no gateway
edit.

Every ``BcpValidationError`` raised by the service layer is translated to an
HTTP 4xx here -- it must never surface as a 500, and the translation is by
TYPE, not by matching exception-message text. ``BcpNotFoundError`` (a
``BcpValidationError`` subclass, raised for a missing plan/test/finding)
becomes a 404; every other ``BcpValidationError`` (an invalid scope/
procedure_ref, an invalid severity/remediation_state, a cross-org/nonexistent
``change_ref``) becomes a 400.

``GET /tests/{test_id}/report`` and ``GET /report/period`` additionally write
a ``bcp.report_exported`` audit event (invariant #4) -- the explicit export is
itself a privileged, audited action. Every plain list/detail read (``GET
/plans``, ``GET /plans/{id}``, ``GET /plans/status``, ``GET
/plans/{id}/tests``, ``GET /tests/{id}``, ``GET /tests/{id}/findings``)
records no audit event at all -- merely viewing must never be
indistinguishable from exporting in the audit log (the Phase-9 no-audit-on-
view lesson); the service layer's own mutations (create/update/review/record)
already audit themselves, so routes never double-audit.

Route registration order matters: ``GET /plans/status`` is declared before
``GET /plans/{plan_id}``, and ``GET /report/period`` is its own top-level
path -- otherwise FastAPI would try to parse ``"status"``/``"period"`` as a
UUID path parameter.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import BcpFinding, BcpPlan, BcpTest, BcpTestStep
from app.modules.bcp.errors import BcpNotFoundError, BcpValidationError
from app.modules.bcp.reporting import bcp_period_report, bcp_plan_status, bcp_test_report
from app.modules.bcp.schema import (
    FindingCreateRequest,
    FindingOut,
    FindingUpdateRequest,
    PeriodReportOut,
    PlanCreateRequest,
    PlanOut,
    PlanStatusOut,
    PlanUpdateRequest,
    StepOut,
    StepUpsertRequest,
    TestCreateRequest,
    TestOut,
    TestRecordRequest,
    TestReportOut,
)
from app.modules.bcp.service import (
    create_finding,
    create_plan,
    create_test,
    get_plan,
    get_test,
    list_findings,
    list_plan_tests,
    list_plans,
    record_test,
    review_plan,
    update_finding,
    update_plan,
    upsert_step,
)

router = APIRouter(prefix="/bcp", tags=["bcp"])


def _http_error(exc: BcpValidationError) -> HTTPException:
    """Translate a `BcpValidationError` to an HTTP 4xx -- it must never become
    a 500.

    Routing is by TYPE, never by matching exception-message prose (a wording
    tweak in the service layer must never silently mis-route a status code).
    `BcpNotFoundError` -- raised for "the resource named in the URL path
    doesn't exist under this org" (a plan, a test, a finding) -- becomes a
    404. Every other `BcpValidationError` is a 400 -- including an invalid
    `scope`/`procedure_refs` entry, an invalid `severity`/
    `remediation_state`, and a cross-org/nonexistent `change_ref` on
    `PATCH /findings/{id}`.
    """
    if isinstance(exc, BcpNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _plan_out(row: BcpPlan) -> PlanOut:
    return PlanOut(
        id=row.id,
        name=row.name,
        description=row.description,
        scope=row.scope,
        rto_minutes=row.rto_minutes,
        rpo_minutes=row.rpo_minutes,
        procedure_refs=row.procedure_refs,
        dependencies=row.dependencies,
        review_interval_days=row.review_interval_days,
        last_reviewed_at=row.last_reviewed_at,
        approved_by=row.approved_by,
        state=row.state,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _test_out(row: BcpTest) -> TestOut:
    return TestOut(
        id=row.id,
        plan_id=row.plan_id,
        type=row.type,
        scheduled_for=row.scheduled_for,
        executed_at=row.executed_at,
        executed_by=row.executed_by,
        outcome=row.outcome,
        rto_met=row.rto_met,
        rpo_met=row.rpo_met,
        evidence=row.evidence,
        notes=row.notes or "",
        created_at=row.created_at,
    )


def _step_out(row: BcpTestStep) -> StepOut:
    return StepOut(
        id=row.id,
        test_id=row.test_id,
        ordinal=row.ordinal,
        description=row.description,
        result=row.result,
        evidence_ref=row.evidence_ref,
    )


def _finding_out(row: BcpFinding) -> FindingOut:
    return FindingOut(
        id=row.id,
        test_id=row.test_id,
        description=row.description,
        severity=row.severity,
        remediation_state=row.remediation_state,
        change_ref=row.change_ref,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


# --------------------------------------------------------------------------- #
# GET|POST /plans
# --------------------------------------------------------------------------- #


@router.get("/plans", response_model=list[PlanOut])
async def list_plans_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> list[PlanOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_plans(session, ctx.org_id)
        return [_plan_out(r) for r in rows]


@router.post("/plans", status_code=status.HTTP_201_CREATED, response_model=PlanOut)
async def create_plan_route(
    body: PlanCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> PlanOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            plan = await create_plan(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                description=body.description,
                scope=body.scope,
                rto_minutes=body.rto_minutes,
                rpo_minutes=body.rpo_minutes,
                procedure_refs=body.procedure_refs,
                dependencies=body.dependencies,
                review_interval_days=body.review_interval_days,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _plan_out(plan)


# --------------------------------------------------------------------------- #
# GET /plans/status -- must be registered before GET /plans/{plan_id}, or
# FastAPI would try to parse "status" as a UUID path parameter.
# --------------------------------------------------------------------------- #


@router.get("/plans/status", response_model=list[PlanStatusOut])
async def plan_status_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> list[PlanStatusOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await bcp_plan_status(session, ctx.org_id, now=datetime.now(UTC))
        return [PlanStatusOut(**row) for row in data]


# --------------------------------------------------------------------------- #
# GET|PATCH /plans/{plan_id}, POST /plans/{plan_id}/review
# --------------------------------------------------------------------------- #


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan_route(
    plan_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> PlanOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        plan = await get_plan(session, ctx.org_id, plan_id)
        if plan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such bcp plan"))
        return _plan_out(plan)


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan_route(
    plan_id: UUID,
    body: PlanUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> PlanOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            plan = await update_plan(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                plan_id=plan_id,
                **body.model_dump(),
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _plan_out(plan)


@router.post("/plans/{plan_id}/review", response_model=PlanOut)
async def review_plan_route(
    plan_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.approve")),
) -> PlanOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            plan = await review_plan(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                plan_id=plan_id,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _plan_out(plan)


# --------------------------------------------------------------------------- #
# GET|POST /plans/{plan_id}/tests
# --------------------------------------------------------------------------- #


@router.get("/plans/{plan_id}/tests", response_model=list[TestOut])
async def list_plan_tests_route(
    plan_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> list[TestOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        plan = await get_plan(session, ctx.org_id, plan_id)
        if plan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such bcp plan"))
        rows = await list_plan_tests(session, ctx.org_id, plan_id)
        return [_test_out(r) for r in rows]


@router.post(
    "/plans/{plan_id}/tests", status_code=status.HTTP_201_CREATED, response_model=TestOut
)
async def create_test_route(
    plan_id: UUID,
    body: TestCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> TestOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            test = await create_test(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                plan_id=plan_id,
                type_=body.type,
                scheduled_for=body.scheduled_for,
                notes=body.notes,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _test_out(test)


# --------------------------------------------------------------------------- #
# GET /report/period -- registered as its own top-level path (never a
# `/report/{id}` shape) so it can never be shadowed by a dynamic segment.
# Writes the `bcp.report_exported` audit event (invariant #4).
# --------------------------------------------------------------------------- #


@router.get("/report/period", response_model=PeriodReportOut)
async def period_report_route(
    request: Request,
    frm: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> PeriodReportOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await bcp_period_report(session, ctx.org_id, frm=frm, to=to)

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="bcp.report_exported",
            target_type="bcp_report",
            target_id=str(ctx.org_id),
            metadata={"from": frm.isoformat(), "to": to.isoformat()},
        )

        return PeriodReportOut(**data)


# --------------------------------------------------------------------------- #
# GET /tests/{test_id}, PATCH /tests/{test_id}, PUT /tests/{test_id}/steps
# --------------------------------------------------------------------------- #


@router.get("/tests/{test_id}", response_model=TestOut)
async def get_test_route(
    test_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> TestOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        test = await get_test(session, ctx.org_id, test_id)
        if test is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such bcp test"))
        return _test_out(test)


@router.patch("/tests/{test_id}", response_model=TestOut)
async def record_test_route(
    test_id: UUID,
    body: TestRecordRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> TestOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            test = await record_test(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                test_id=test_id,
                executed_at=body.executed_at,
                rto_met=body.rto_met,
                rpo_met=body.rpo_met,
                outcome=body.outcome,
                notes=body.notes,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _test_out(test)


@router.put("/tests/{test_id}/steps", response_model=StepOut)
async def upsert_step_route(
    test_id: UUID,
    body: StepUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> StepOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            step = await upsert_step(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                test_id=test_id,
                ordinal=body.ordinal,
                description=body.description,
                result=body.result,
                evidence_ref=body.evidence_ref,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _step_out(step)


# --------------------------------------------------------------------------- #
# GET /tests/{test_id}/report -- also writes the `bcp.report_exported` audit
# event (invariant #4); the explicit export is a privileged, audited action.
# --------------------------------------------------------------------------- #


@router.get("/tests/{test_id}/report", response_model=TestReportOut)
async def test_report_route(
    test_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> TestReportOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await bcp_test_report(session, ctx.org_id, test_id)
        if data["test"] is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such bcp test"))

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="bcp.report_exported",
            target_type="bcp_test",
            target_id=str(test_id),
            metadata={},
        )

        return TestReportOut.model_validate(data)


# --------------------------------------------------------------------------- #
# GET|POST /tests/{test_id}/findings, PATCH /findings/{finding_id}
# --------------------------------------------------------------------------- #


@router.get("/tests/{test_id}/findings", response_model=list[FindingOut])
async def list_findings_route(
    test_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.read")),
) -> list[FindingOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        test = await get_test(session, ctx.org_id, test_id)
        if test is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such bcp test"))
        rows = await list_findings(session, ctx.org_id, test_id)
        return [_finding_out(r) for r in rows]


@router.post(
    "/tests/{test_id}/findings", status_code=status.HTTP_201_CREATED, response_model=FindingOut
)
async def create_finding_route(
    test_id: UUID,
    body: FindingCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> FindingOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            finding = await create_finding(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                test_id=test_id,
                description=body.description,
                severity=body.severity,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _finding_out(finding)


@router.patch("/findings/{finding_id}", response_model=FindingOut)
async def update_finding_route(
    finding_id: UUID,
    body: FindingUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("bcp.manage")),
) -> FindingOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            finding = await update_finding(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                finding_id=finding_id,
                remediation_state=body.remediation_state,
                change_ref=body.change_ref,
                description=body.description,
            )
        except BcpValidationError as exc:
            raise _http_error(exc) from None
        return _finding_out(finding)

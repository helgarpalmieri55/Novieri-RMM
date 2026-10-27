"""BCP/DR plan service (docs/modules/bcp/spec.md): create/edit a plan, drive
its manual review sign-off, and expand its `scope` to a concrete device set
for Task 5's evidence capture.

Mirrors `app.modules.change_mgmt.service`'s org-scoped-load + audit + flush
style and `app.modules.change_mgmt.links`'s org-scoped cross-reference
resolution guard: every `scope` entry (`{"kind": "device"|"group", "id": ...}`)
and every `procedure_refs` entry (a `doc_articles.id`) is resolved with an
explicit `WHERE Model.org_id == org_id AND Model.id == ref_id` existence
check. A ref from another org is indistinguishable from a nonexistent one --
both raise `BcpValidationError` (invariant #5: tenant scoping must never be
bypassable by guessing or enumerating another org's ids).

Every query in this module carries an explicit `org_id` predicate; a plan id
from another org is never reachable through any function here.

A plan belongs to ONE client (MSP clients plan 2026-10-26 Task 7), and so do
its tests, steps and findings (inherited by trigger, migration 0237). What a
plan points at is of that client too: `scope` devices and groups, `procedure_refs`
articles (or global ones, every client's), a finding's `change_ref`. Another
client's is a 422 (`BcpClientError`); a missing one stays the 400 it was.
Continuity is an optional module per client, so every write calls
`require_module` with the plan's client before it writes anything (a 404 when
the client does not use it), and every audit event carries that client.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import (
    BcpFinding,
    BcpPlan,
    BcpTest,
    BcpTestStep,
    ChangeRequest,
    Device,
    DeviceGroup,
    DocArticle,
)
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.backup.reporting import backup_evidence_rows
from app.modules.bcp.errors import (
    BcpClientError,
    BcpConflictError,
    BcpNotFoundError,
    BcpValidationError,
)
from app.services.client_modules import require_module
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

_VALID_PLAN_STATES = {"active", "archived"}
_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_REMEDIATION_STATES = frozenset({"open", "in_progress", "resolved"})

# Fields `update_plan` is allowed to apply. Only non-None values passed by the
# caller are actually written; `scope`/`procedure_refs` are re-validated
# org-scoped before being applied (see `_validate_scope`/`_validate_procedure_refs`).
_EDITABLE_FIELDS = (
    "name",
    "description",
    "scope",
    "rto_minutes",
    "rpo_minutes",
    "procedure_refs",
    "dependencies",
    "review_interval_days",
    "state",
)

_SCOPE_KIND_TO_MODEL: dict[str, type] = {
    "device": Device,
    "group": DeviceGroup,
}


async def _load_plan(session: AsyncSession, org_id: UUID, plan_id: UUID) -> BcpPlan:
    plan = (
        await session.execute(
            select(BcpPlan).where(BcpPlan.org_id == org_id, BcpPlan.id == plan_id)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise BcpNotFoundError(tr("bcp plan {plan_id} not found", plan_id=plan_id))
    return plan


async def _plan_for_write(session: AsyncSession, org_id: UUID, plan_id: UUID) -> BcpPlan:
    """The plan a write is about, of a client that uses continuity: a plan this
    session cannot see is the 404 it always was, and one whose client has the
    module off is `ModuleNotEnabled` (404) -- before anything is written."""
    plan = await _load_plan(session, org_id, plan_id)
    await require_module(session, org_id=org_id, client_id=plan.client_id, module="bcp")
    return plan


async def _client_for_new_plan(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """The client a new plan is filed under, loaded as every new row's is
    (`client_for_new_rows`, held FOR SHARE until commit): one this session
    cannot see is a 404, never a 403 (spec §3.2's existence oracle), an
    archived one a 409, "Sin asignar" a 422 (C10). Then the module: a client
    without continuity is a 404 too."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise BcpNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise BcpConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise BcpClientError(tr("a BCP plan cannot be filed under Unassigned"))
    await require_module(session, org_id=org_id, client_id=client_id, module="bcp")


async def _validate_scope(
    session: AsyncSession, org_id: UUID, client_id: UUID, scope: list[dict[str, Any]]
) -> None:
    """Org-scoped resolution guard for `scope` entries, held to the plan's
    client.

    Each entry is `{"kind": "device"|"group", "id": <uuid str>}`. An unknown
    `kind`, a malformed uuid, or an id that doesn't resolve under `org_id`
    (nonexistent OR belonging to another org -- indistinguishable by design)
    raises `BcpValidationError`. A device or group of another client of the
    org raises `BcpClientError`: a test of X's plan would freeze Y's backup
    evidence into X's DR pack.
    """
    for entry in scope:
        kind = entry.get("kind")
        model = _SCOPE_KIND_TO_MODEL.get(kind) if isinstance(kind, str) else None
        if model is None:
            raise BcpValidationError(tr("unknown scope kind {kind}", kind=repr(kind)))

        try:
            ref_id = UUID(str(entry.get("id")))
        except (ValueError, AttributeError, TypeError) as exc:
            raise BcpValidationError(
                tr("malformed scope id {scope_id}", scope_id=repr(entry.get("id")))
            ) from exc

        resolved = (
            await session.execute(
                select(model.client_id).where(model.org_id == org_id, model.id == ref_id)  # type: ignore[attr-defined]
            )
        ).one_or_none()
        if resolved is None:
            raise BcpValidationError(
                tr(
                    "scope ref not found: {kind}:{ref_id}",
                    # The wire kind, not the word: `kind:ref_id` is the identifier the
                    # API accepts, so it reads the same in every language.
                    kind=kind,
                    ref_id=ref_id,
                )
            )
        if resolved[0] != client_id:
            raise BcpClientError(
                tr("scope ref belongs to another client: {kind}:{ref_id}", kind=kind, ref_id=ref_id)
            )


async def _validate_procedure_refs(
    session: AsyncSession, org_id: UUID, client_id: UUID, procedure_refs: list[Any]
) -> None:
    """Org-scoped resolution guard for `procedure_refs` (each a `doc_articles.id`):
    a global article (no client) is every client's procedure; one of another
    client raises `BcpClientError`."""
    for ref in procedure_refs:
        try:
            ref_id = UUID(str(ref))
        except (ValueError, AttributeError, TypeError) as exc:
            raise BcpValidationError(tr("malformed procedure_ref {ref}", ref=repr(ref))) from exc

        resolved = (
            await session.execute(
                select(DocArticle.client_id).where(
                    DocArticle.org_id == org_id, DocArticle.id == ref_id
                )
            )
        ).one_or_none()
        if resolved is None:
            raise BcpValidationError(tr("procedure_ref not found: {ref_id}", ref_id=ref_id))
        if resolved[0] not in (None, client_id):
            raise BcpClientError(
                tr("procedure_ref belongs to another client: {ref_id}", ref_id=ref_id)
            )


async def create_plan(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    name: str,
    description: str,
    scope: list[dict[str, Any]],
    rto_minutes: int | None,
    rpo_minutes: int | None,
    procedure_refs: list[Any],
    dependencies: list[Any],
    review_interval_days: int,
) -> BcpPlan:
    """Create a new `active` BCP plan for `client_id` (`_client_for_new_plan`)
    and audit `bcp.plan_created`."""
    await _client_for_new_plan(session, org_id, client_id)
    await _validate_scope(session, org_id, client_id, scope)
    await _validate_procedure_refs(session, org_id, client_id, procedure_refs)

    plan = BcpPlan(
        org_id=org_id,
        client_id=client_id,
        name=name,
        description=description,
        scope=scope,
        rto_minutes=rto_minutes,
        rpo_minutes=rpo_minutes,
        procedure_refs=procedure_refs,
        dependencies=dependencies,
        review_interval_days=review_interval_days,
        state="active",
        created_by=actor_id,
    )
    session.add(plan)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.plan_created",
        target_type="bcp_plan",
        target_id=str(plan.id),
        client_id=plan.client_id,
        metadata={"name": name},
    )
    await session.flush()
    return plan


async def update_plan(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    plan_id: UUID,
    **fields: Any,
) -> BcpPlan:
    """Apply the given non-None fields to a plan, org-scoped-loaded.

    Re-validates `scope`/`procedure_refs` (if provided) org-scoped before
    applying anything, held to the plan's client.
    """
    plan = await _plan_for_write(session, org_id, plan_id)

    if fields.get("scope") is not None:
        await _validate_scope(session, org_id, plan.client_id, fields["scope"])
    if fields.get("procedure_refs") is not None:
        await _validate_procedure_refs(session, org_id, plan.client_id, fields["procedure_refs"])

    for field in _EDITABLE_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        if field == "state" and value not in _VALID_PLAN_STATES:
            raise BcpValidationError(tr("invalid plan state {value}", value=repr(value)))
        setattr(plan, field, value)

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.plan_updated",
        target_type="bcp_plan",
        target_id=str(plan.id),
        client_id=plan.client_id,
        metadata={},
    )
    await session.flush()
    return plan


async def review_plan(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    plan_id: UUID,
) -> BcpPlan:
    """Record a manual review sign-off. No segregation-of-duties in v1 -- the
    plan's author MAY review their own plan."""
    plan = await _plan_for_write(session, org_id, plan_id)

    plan.last_reviewed_at = datetime.now(UTC)
    plan.approved_by = actor_id
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.plan_reviewed",
        target_type="bcp_plan",
        target_id=str(plan.id),
        client_id=plan.client_id,
        metadata={},
    )
    await session.flush()
    return plan


async def get_plan(session: AsyncSession, org_id: UUID, plan_id: UUID) -> BcpPlan | None:
    return (
        await session.execute(
            select(BcpPlan).where(BcpPlan.org_id == org_id, BcpPlan.id == plan_id)
        )
    ).scalar_one_or_none()


async def list_plans(session: AsyncSession, org_id: UUID) -> list[BcpPlan]:
    return list(
        (await session.execute(select(BcpPlan).where(BcpPlan.org_id == org_id)))
        .scalars()
        .all()
    )


async def resolve_scope_devices(
    session: AsyncSession, org_id: UUID, scope: list[dict[str, Any]]
) -> set[UUID]:
    """Expand `scope` to a concrete, org-scoped device-id set.

    `device` refs are added directly (org-scoped defensively, even though
    callers pass already-validated scopes); `group` refs expand to their
    direct member devices only -- nested-group expansion is deferred. Used by
    Task 5's evidence capture.
    """
    device_ids: set[UUID] = set()

    for entry in scope:
        kind = entry.get("kind")
        ref_id = UUID(str(entry.get("id")))

        if kind == "device":
            resolved = (
                await session.execute(
                    select(Device.id).where(Device.org_id == org_id, Device.id == ref_id)
                )
            ).scalar_one_or_none()
            if resolved is not None:
                device_ids.add(resolved)
        elif kind == "group":
            members = (
                await session.execute(
                    select(Device.id).where(
                        Device.org_id == org_id, Device.device_group_id == ref_id
                    )
                )
            ).scalars().all()
            device_ids.update(members)

    return device_ids


async def _load_test(session: AsyncSession, org_id: UUID, test_id: UUID) -> BcpTest:
    test = (
        await session.execute(
            select(BcpTest).where(BcpTest.org_id == org_id, BcpTest.id == test_id)
        )
    ).scalar_one_or_none()
    if test is None:
        raise BcpNotFoundError(tr("bcp test {test_id} not found", test_id=test_id))
    return test


async def _test_for_write(session: AsyncSession, org_id: UUID, test_id: UUID) -> BcpTest:
    """The test a write is about, of a client that uses continuity (its
    client is its plan's, by trigger). Read `client_id` off the result before
    the write's flush: an UPDATE of the row expires it (`server_onupdate`)."""
    test = await _load_test(session, org_id, test_id)
    await require_module(session, org_id=org_id, client_id=test.client_id, module="bcp")
    return test


def _jsonable_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one `backup_evidence_rows` row (carrying a `UUID` and
    `datetime` objects) into a plain JSON-safe dict for storage in the
    `bcp_test.evidence` JSONB column. `device_id` -> `str`; each datetime ->
    `.isoformat()` or `None`; bool/str fields pass through unchanged.
    """
    return {
        "device_id": str(row["device_id"]),
        "hostname": row["hostname"],
        "covered": row["covered"],
        "last_backup_taken_at": (
            row["last_backup_taken_at"].isoformat()
            if row["last_backup_taken_at"] is not None
            else None
        ),
        "verified_at": (
            row["verified_at"].isoformat() if row["verified_at"] is not None else None
        ),
        "restore_tested_at": (
            row["restore_tested_at"].isoformat()
            if row["restore_tested_at"] is not None
            else None
        ),
        "restore_proven": row["restore_proven"],
    }


async def create_test(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    plan_id: UUID,
    type_: str,
    scheduled_for: datetime | None,
    notes: str | None,
) -> BcpTest:
    """Create a new `pending` DR test run against an org-scoped-verified plan
    and audit `bcp.test_created`."""
    plan = await _plan_for_write(session, org_id, plan_id)

    test = BcpTest(
        org_id=org_id,
        plan_id=plan_id,
        type=type_,
        scheduled_for=scheduled_for,
        outcome="pending",
        notes=notes,
        created_by=actor_id,
    )
    session.add(test)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.test_created",
        target_type="bcp_test",
        target_id=str(test.id),
        client_id=plan.client_id,
        metadata={"plan_id": str(plan_id), "type": type_},
    )
    await session.flush()
    return test


async def record_test(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    test_id: UUID,
    executed_at: datetime | None,
    rto_met: bool | None,
    rpo_met: bool | None,
    outcome: str,
    notes: str | None,
) -> BcpTest:
    """Record the outcome of a DR test run and capture a FROZEN snapshot of
    backup restore-test evidence for the plan's scope at this moment.

    The snapshot (`test.evidence`) is a stored value, never recomputed on
    read -- it is the point-in-time proof of what backup coverage looked
    like when this test was recorded. `device_id`/datetime fields are
    converted to plain str/None before storage since `evidence` is JSONB
    (see `_jsonable_evidence_row`).
    """
    test = await _test_for_write(session, org_id, test_id)
    plan = await _load_plan(session, org_id, test.plan_id)

    resolved_executed_at = executed_at if executed_at is not None else datetime.now(UTC)

    devices = await resolve_scope_devices(session, org_id, plan.scope)
    rows = await backup_evidence_rows(session, org_id)
    scoped_rows = [row for row in rows if row["device_id"] in devices]

    total_n = len(scoped_rows)
    covered_n = sum(1 for row in scoped_rows if row["covered"])
    restore_proven_n = sum(1 for row in scoped_rows if row["restore_proven"])

    evidence = {
        "captured_at": resolved_executed_at.isoformat(),
        "devices": [_jsonable_evidence_row(row) for row in scoped_rows],
        "summary": {
            "total_n": total_n,
            "covered_n": covered_n,
            "restore_proven_n": restore_proven_n,
        },
    }

    test.executed_at = resolved_executed_at
    test.executed_by = actor_id
    test.rto_met = rto_met
    test.rpo_met = rpo_met
    test.outcome = outcome
    test.notes = notes
    test.evidence = evidence
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.test_recorded",
        target_type="bcp_test",
        target_id=str(test.id),
        client_id=plan.client_id,
        metadata={
            "outcome": outcome,
            "total_n": total_n,
            "covered_n": covered_n,
            "restore_proven_n": restore_proven_n,
        },
    )
    await session.flush()
    return test


async def upsert_step(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    test_id: UUID,
    ordinal: int,
    description: str,
    result: str,
    evidence_ref: str | None,
) -> BcpTestStep:
    """Insert or update the step at `(test_id, ordinal)`, org-scoped, and
    audit `bcp.test_step_recorded` against the parent test (invariant #4:
    every privileged, RBAC-gated mutation is written to the hash-chained
    audit log). The step body itself is not secret; the metadata carries the
    step ordinal and result only."""
    client_id = (await _test_for_write(session, org_id, test_id)).client_id

    step = (
        await session.execute(
            select(BcpTestStep).where(
                BcpTestStep.org_id == org_id,
                BcpTestStep.test_id == test_id,
                BcpTestStep.ordinal == ordinal,
            )
        )
    ).scalar_one_or_none()

    if step is None:
        step = BcpTestStep(
            org_id=org_id,
            test_id=test_id,
            ordinal=ordinal,
            description=description,
            result=result,
            evidence_ref=evidence_ref,
        )
        session.add(step)
    else:
        step.description = description
        step.result = result
        step.evidence_ref = evidence_ref

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.test_step_recorded",
        target_type="bcp_test",
        target_id=str(test_id),
        client_id=client_id,
        metadata={"ordinal": ordinal, "result": result},
    )
    await session.flush()
    return step


async def get_test(session: AsyncSession, org_id: UUID, test_id: UUID) -> BcpTest | None:
    return (
        await session.execute(
            select(BcpTest).where(BcpTest.org_id == org_id, BcpTest.id == test_id)
        )
    ).scalar_one_or_none()


async def list_plan_tests(session: AsyncSession, org_id: UUID, plan_id: UUID) -> list[BcpTest]:
    return list(
        (
            await session.execute(
                select(BcpTest).where(BcpTest.org_id == org_id, BcpTest.plan_id == plan_id)
            )
        )
        .scalars()
        .all()
    )


async def _load_finding(session: AsyncSession, org_id: UUID, finding_id: UUID) -> BcpFinding:
    finding = (
        await session.execute(
            select(BcpFinding).where(BcpFinding.org_id == org_id, BcpFinding.id == finding_id)
        )
    ).scalar_one_or_none()
    if finding is None:
        raise BcpNotFoundError(tr("bcp finding {finding_id} not found", finding_id=finding_id))
    return finding


async def create_finding(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    test_id: UUID,
    description: str,
    severity: str,
) -> BcpFinding:
    """Create a new `open` finding on an org-scoped-verified test and audit
    `bcp.finding_created`."""
    if severity not in _VALID_SEVERITIES:
        raise BcpValidationError(tr("invalid severity {severity}", severity=repr(severity)))

    client_id = (await _test_for_write(session, org_id, test_id)).client_id

    finding = BcpFinding(
        org_id=org_id,
        test_id=test_id,
        description=description,
        severity=severity,
        remediation_state="open",
        created_by=actor_id,
    )
    session.add(finding)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.finding_created",
        target_type="bcp_finding",
        target_id=str(finding.id),
        client_id=client_id,
        metadata={"test_id": str(test_id), "severity": severity},
    )
    await session.flush()
    return finding


async def update_finding(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    finding_id: UUID,
    remediation_state: str | None = None,
    change_ref: UUID | None = None,
    description: str | None = None,
) -> BcpFinding:
    """Apply the given non-None fields to a finding, org-scoped-loaded.

    `change_ref`, if provided, is validated org-scoped against
    `change_request` before being set -- a cross-org or nonexistent id
    raises `BcpValidationError` (indistinguishable by design, invariant #5),
    and a change of another client of the org `BcpClientError`. The link is
    advisory: the referenced ChangeRequest row is only SELECTed, never
    mutated.

    `remediation_state`, if provided, is validated against
    `_VALID_REMEDIATION_STATES`. Transitioning TO `"resolved"` stamps
    `resolved_at`; moving AWAY from `"resolved"` to any other state clears
    it.
    """
    finding = await _load_finding(session, org_id, finding_id)
    # Read before the flush below: an UPDATE of the row expires it.
    client_id = finding.client_id
    await require_module(session, org_id=org_id, client_id=client_id, module="bcp")

    change_ref_set = change_ref is not None
    if change_ref is not None:
        resolved = (
            await session.execute(
                select(ChangeRequest.client_id).where(
                    ChangeRequest.org_id == org_id, ChangeRequest.id == change_ref
                )
            )
        ).one_or_none()
        if resolved is None:
            raise BcpValidationError(
                tr("change_ref not found: {change_ref}", change_ref=change_ref)
            )
        if resolved[0] != client_id:
            raise BcpClientError(tr("that change belongs to another client"))
        finding.change_ref = change_ref

    if remediation_state is not None:
        if remediation_state not in _VALID_REMEDIATION_STATES:
            raise BcpValidationError(
                tr(
                    "invalid remediation_state {remediation_state}",
                    remediation_state=repr(remediation_state),
                )
            )
        if remediation_state == "resolved" and finding.remediation_state != "resolved":
            finding.resolved_at = datetime.now(UTC)
        elif remediation_state != "resolved" and finding.remediation_state == "resolved":
            finding.resolved_at = None
        finding.remediation_state = remediation_state

    if description is not None:
        finding.description = description

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="bcp.finding_updated",
        target_type="bcp_finding",
        target_id=str(finding.id),
        client_id=client_id,
        metadata={
            "remediation_state": finding.remediation_state,
            "change_ref_set": change_ref_set,
        },
    )
    await session.flush()
    return finding


async def list_findings(session: AsyncSession, org_id: UUID, test_id: UUID) -> list[BcpFinding]:
    return list(
        (
            await session.execute(
                select(BcpFinding).where(
                    BcpFinding.org_id == org_id, BcpFinding.test_id == test_id
                )
            )
        )
        .scalars()
        .all()
    )

"""Tests for the BCP/DR findings service (docs/modules/bcp/spec.md):
remediation-state tracking plus the org-scoped, advisory link from a finding
to a `change_request`.

Mirrors `tests/test_bcp_test_service.py`'s fixture style (throwaway
org/user seeded via `admin_engine`, service calls through the
RLS-constrained `org_session`) and the minimal `ChangeRequest` seed used by
`tests/test_change_mgmt_links.py`.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, ChangeRequest, Organization, User
from app.modules.bcp.errors import BcpNotFoundError, BcpValidationError
from app.modules.bcp.service import (
    create_finding,
    create_plan,
    list_findings,
    update_finding,
)
from app.modules.bcp.service import (
    create_test as bcp_create_test,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"bcpfind-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_plan(app_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            client_id=org_id,
            actor_id=actor_id,
            name="DR Plan",
            description="Recover the core stack",
            scope=[],
            rto_minutes=60,
            rpo_minutes=15,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=180,
        )
        await s.commit()
        return plan.id


async def _seed_test(app_engine: AsyncEngine, org_id: UUID, actor_id: UUID, plan_id: UUID) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        return test.id


async def _seed_change_request(admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        change = ChangeRequest(
            org_id=org_id,
            client_id=org_id,
            type="normal",
            title="T",
            description="D",
            risk="low",
            affected=[],
            state="draft",
            created_by=actor_id,
        )
        s.add(change)
        await s.commit()
        return change.id


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: str
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == target_id,
                )
                # Callers assert on list ORDER, so the query must define one.
                # `id` is uuid7 and therefore chain-monotonic.
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# create_finding
# ---------------------------------------------------------------------------


async def test_create_finding_defaults_open_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="Failover took too long",
            severity="high",
        )
        await s.commit()
        finding_id = finding.id

    assert finding.remediation_state == "open"
    assert finding.resolved_at is None
    assert finding.created_by == actor_id

    actions = await _audit_actions(app_engine, org_id, "bcp_finding", str(finding_id))
    assert actions == ["bcp.finding_created"]


async def test_create_finding_missing_test_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await create_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                test_id=uuid4(),
                description="x",
                severity="low",
            )


async def test_create_finding_invalid_severity_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                test_id=test_id,
                description="x",
                severity="catastrophic",
            )


# ---------------------------------------------------------------------------
# update_finding -- remediation_state + resolved_at stamping
# ---------------------------------------------------------------------------


async def test_update_finding_remediation_state_transitions_stamp_resolved_at(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="medium",
        )
        await s.commit()
        finding_id = finding.id

    # open -> in_progress: resolved_at stays None
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding_id,
            remediation_state="in_progress",
        )
        await s.commit()
    assert updated.remediation_state == "in_progress"
    assert updated.resolved_at is None

    # in_progress -> resolved: resolved_at stamped
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding_id,
            remediation_state="resolved",
        )
        await s.commit()
    assert updated.remediation_state == "resolved"
    assert updated.resolved_at is not None

    # resolved -> in_progress: resolved_at cleared
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding_id,
            remediation_state="in_progress",
        )
        await s.commit()
    assert updated.remediation_state == "in_progress"
    assert updated.resolved_at is None

    actions = await _audit_actions(app_engine, org_id, "bcp_finding", str(finding_id))
    assert actions == [
        "bcp.finding_created",
        "bcp.finding_updated",
        "bcp.finding_updated",
        "bcp.finding_updated",
    ]


async def test_update_finding_invalid_remediation_state_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await update_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                finding_id=finding_id,
                remediation_state="closed_forever",
            )


async def test_update_finding_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await update_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                finding_id=uuid4(),
                remediation_state="in_progress",
            )


# ---------------------------------------------------------------------------
# update_finding -- change_ref linkage (org-scoped, advisory)
# ---------------------------------------------------------------------------


async def test_update_finding_links_own_org_change_ref_and_leaves_it_unchanged(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)
    change_id = await _seed_change_request(admin_engine, org_id, actor_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding_id,
            change_ref=change_id,
        )
        await s.commit()

    assert updated.change_ref == change_id

    # Advisory link: the ChangeRequest row itself is untouched.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        fresh_change = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert fresh_change.state == "draft"
        assert fresh_change.title == "T"

    actions = await _audit_actions(app_engine, org_id, "bcp_finding", str(finding_id))
    assert actions == ["bcp.finding_created", "bcp.finding_updated"]


async def test_update_finding_cross_org_change_ref_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, other_actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)
    foreign_change_id = await _seed_change_request(admin_engine, other_org_id, other_actor_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await update_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                finding_id=finding_id,
                change_ref=foreign_change_id,
            )


async def test_update_finding_change_ref_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `ChangeRequest.org_id == org_id` predicate in
    `update_finding`'s change-ref guard so it is the SOLE tenant check.

    The cross-org test above runs under `org_session(A)`, where RLS already
    hides org B's `change_request` -- so that assertion would still pass even
    if the explicit predicate regressed. Here we drive `update_finding` on an
    RLS-bypassing admin session (the `admin_engine` role bypasses RLS -- it is
    how the cross-org seed helpers insert), so org B's row IS visible on the
    connection and only the guard's own `org_id` predicate can reject the
    foreign id. Mutation-verified: deleting that predicate makes this fail.

    THE MESSAGE IS PART OF THE ASSERTION (MSP clients plan 2026-10-26 Task 7).
    The guard now also holds the change to the finding's client, and org B's
    change is org B's client's, so without the org predicate it would still
    be refused -- as "another client's", a different sentence. Matching "not
    found" keeps the org predicate the one that answers.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_a, actor_a)
    test_id = await _seed_test(app_engine, org_a, actor_a, plan_id)
    foreign_change_id = await _seed_change_request(admin_engine, org_b, actor_b)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_a,
            actor_id=actor_a,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(BcpValidationError, match="change_ref not found"):
            await update_finding(
                s,
                org_id=org_a,
                actor_id=actor_a,
                finding_id=finding_id,
                change_ref=foreign_change_id,
            )


async def test_update_finding_nonexistent_change_ref_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await update_finding(
                s,
                org_id=org_id,
                actor_id=actor_id,
                finding_id=finding_id,
                change_ref=uuid4(),
            )


# ---------------------------------------------------------------------------
# list_findings + tenancy
# ---------------------------------------------------------------------------


async def test_list_findings_returns_the_orgs_findings_and_writes_no_audit_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        findings = await list_findings(s, org_id, test_id)
        assert [f.id for f in findings] == [finding_id]

    # list_findings performs no writes/audits of its own.
    actions = await _audit_actions(app_engine, org_id, "bcp_finding", str(finding_id))
    assert actions == ["bcp.finding_created"]


async def test_list_findings_empty_for_missing_test(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        findings = await list_findings(s, org_id, uuid4())
        assert findings == []


async def test_another_orgs_finding_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    plan_a = await _seed_plan(app_engine, org_a, actor_a)
    test_a = await _seed_test(app_engine, org_a, actor_a, plan_a)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        finding = await create_finding(
            s,
            org_id=org_a,
            actor_id=actor_a,
            test_id=test_a,
            description="x",
            severity="low",
        )
        await s.commit()
        finding_id = finding.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        findings = await list_findings(s, org_b, test_a)
        assert findings == []

        with pytest.raises(BcpNotFoundError):
            await update_finding(
                s,
                org_id=org_b,
                actor_id=actor_b,
                finding_id=finding_id,
                remediation_state="in_progress",
            )

"""API-layer schema tests for the BCP/DR module (docs/modules/bcp/spec.md).

Mirrors `tests/test_change_mgmt_schema.py`'s style for the plain API-layer
models: construct each model, assert out-of-domain Literal values are
rejected, and assert required fields are enforced. These are NOT wire
contracts (no `extra="forbid"` base) since this module has no agent jobs.
"""

import pytest
from pydantic import ValidationError

from app.modules.bcp.errors import BcpNotFoundError, BcpValidationError
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


class TestErrors:
    def test_not_found_is_subclass_of_validation(self) -> None:
        assert issubclass(BcpNotFoundError, BcpValidationError)

    def test_validation_error_is_exception(self) -> None:
        assert issubclass(BcpValidationError, Exception)


class TestPlanCreateRequest:
    def test_construct_with_defaults(self) -> None:
        req = PlanCreateRequest(name="p")
        assert req.description == ""
        assert req.scope == []
        assert req.rto_minutes is None
        assert req.rpo_minutes is None
        assert req.procedure_refs == []
        assert req.dependencies == []
        assert req.review_interval_days == 365

    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            PlanCreateRequest()  # type: ignore[call-arg]


class TestPlanUpdateRequest:
    def test_construct_all_optional(self) -> None:
        req = PlanUpdateRequest()
        assert req.name is None
        assert req.description is None
        assert req.scope is None
        assert req.rto_minutes is None
        assert req.rpo_minutes is None
        assert req.procedure_refs is None
        assert req.dependencies is None
        assert req.review_interval_days is None


class TestPlanOut:
    def test_construct(self) -> None:
        out = PlanOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            name="p",
            description="d",
            scope=[],
            rto_minutes=60,
            rpo_minutes=30,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
            last_reviewed_at=None,
            approved_by=None,
            state="active",
            created_by="0198c1f4-0000-7000-8000-000000000001",
            created_at="2026-07-23T00:00:00Z",
        )
        assert out.state == "active"


class TestPlanStatusOut:
    def test_construct(self) -> None:
        out = PlanStatusOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            name="p",
            state="active",
            review_overdue=False,
            last_reviewed_at=None,
            last_test_outcome=None,
            open_findings=0,
            restore_proven_coverage={},
        )
        assert out.review_overdue is False
        assert out.open_findings == 0


class TestTestCreateRequest:
    def test_construct_with_defaults(self) -> None:
        req = TestCreateRequest(type="restore_drill")
        assert req.scheduled_for is None
        assert req.notes == ""

    def test_rejects_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            TestCreateRequest(type="foo")

    def test_requires_type(self) -> None:
        with pytest.raises(ValidationError):
            TestCreateRequest()  # type: ignore[call-arg]

    def test_accepts_each_valid_type(self) -> None:
        for t in ("restore_drill", "failover", "tabletop", "other"):
            assert TestCreateRequest(type=t).type == t


class TestTestRecordRequest:
    def test_construct_with_defaults(self) -> None:
        req = TestRecordRequest(outcome="pass")
        assert req.executed_at is None
        assert req.rto_met is None
        assert req.rpo_met is None
        assert req.notes == ""

    def test_rejects_invalid_outcome(self) -> None:
        with pytest.raises(ValidationError):
            TestRecordRequest(outcome="pending")

    def test_requires_outcome(self) -> None:
        with pytest.raises(ValidationError):
            TestRecordRequest()  # type: ignore[call-arg]

    def test_accepts_each_valid_outcome(self) -> None:
        for o in ("pass", "partial", "fail"):
            assert TestRecordRequest(outcome=o).outcome == o


class TestTestOut:
    def test_construct(self) -> None:
        out = TestOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            plan_id="0198c1f4-0000-7000-8000-000000000001",
            type="restore_drill",
            scheduled_for=None,
            executed_at=None,
            executed_by=None,
            outcome="pass",
            rto_met=True,
            rpo_met=True,
            evidence={},
            notes="",
            created_at="2026-07-23T00:00:00Z",
        )
        assert out.outcome == "pass"


class TestStepUpsertRequest:
    def test_construct_with_defaults(self) -> None:
        req = StepUpsertRequest(ordinal=1, description="do thing")
        assert req.result == "pending"
        assert req.evidence_ref is None

    def test_rejects_invalid_result(self) -> None:
        with pytest.raises(ValidationError):
            StepUpsertRequest(ordinal=1, description="do thing", result="huge")

    def test_requires_ordinal_and_description(self) -> None:
        with pytest.raises(ValidationError):
            StepUpsertRequest()  # type: ignore[call-arg]


class TestStepOut:
    def test_construct(self) -> None:
        out = StepOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            test_id="0198c1f4-0000-7000-8000-000000000001",
            ordinal=1,
            description="do thing",
            result="pass",
            evidence_ref=None,
        )
        assert out.result == "pass"


class TestFindingCreateRequest:
    def test_construct(self) -> None:
        req = FindingCreateRequest(description="d", severity="high")
        assert req.severity == "high"

    def test_rejects_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            FindingCreateRequest(description="d", severity="huge")

    def test_requires_description(self) -> None:
        with pytest.raises(ValidationError):
            FindingCreateRequest(severity="high")  # type: ignore[call-arg]

    def test_requires_severity(self) -> None:
        with pytest.raises(ValidationError):
            FindingCreateRequest(description="d")  # type: ignore[call-arg]


class TestFindingUpdateRequest:
    def test_construct_all_optional(self) -> None:
        req = FindingUpdateRequest()
        assert req.remediation_state is None
        assert req.change_ref is None
        assert req.description is None

    def test_rejects_invalid_remediation_state(self) -> None:
        with pytest.raises(ValidationError):
            FindingUpdateRequest(remediation_state="done")


class TestFindingOut:
    def test_construct(self) -> None:
        out = FindingOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            test_id="0198c1f4-0000-7000-8000-000000000001",
            description="d",
            severity="high",
            remediation_state="open",
            change_ref=None,
            created_at="2026-07-23T00:00:00Z",
            resolved_at=None,
        )
        assert out.remediation_state == "open"


class TestTestReportOut:
    def test_construct(self) -> None:
        report = TestReportOut(test=None, plan=None, steps=[], findings=[], evidence={})
        assert report.test is None
        assert report.plan is None
        assert report.steps == []
        assert report.findings == []
        assert report.evidence == {}


class TestPeriodReportOut:
    def test_construct(self) -> None:
        report = PeriodReportOut(
            outcome_counts={},
            plans_tested=[],
            open_findings_by_severity={},
            review_overdue_plans=[],
        )
        assert report.outcome_counts == {}
        assert report.plans_tested == []
        assert report.open_findings_by_severity == {}
        assert report.review_overdue_plans == []

"""`agent.uninstall` (install-link spec §1.5, §4): an ordinary signed job type,
classified in every kernel map, reachable only through Reject."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.approved_actions import non_approvable_reason
from app.jobs.direct_dispatch import DIRECTLY_DISPATCHABLE_JOB_TYPES, module_governed_reason
from app.jobs.dispatch_permissions import dispatch_permission_for
from app.jobs.registry import AgentUninstallParams, AgentUninstallResult, get_job_type
from app.jobs.review_gate import ALLOWED_WHILE_PENDING, ALLOWED_WHILE_REJECTED

REPO = Path(__file__).resolve().parents[2]


def test_it_is_registered_and_carries_no_parameters() -> None:
    job_type = get_job_type("agent.uninstall")
    assert job_type.params_model is AgentUninstallParams
    assert AgentUninstallParams().model_dump() == {}
    with pytest.raises(ValidationError):
        AgentUninstallParams(path="C:\\Windows")  # type: ignore[call-arg]
    assert job_type.result_handler is None
    assert job_type.cancellable is False
    assert job_type.retry_relinker is None


def test_the_result_says_the_removal_was_scheduled() -> None:
    assert AgentUninstallResult(scheduled=True).model_dump() == {"scheduled": True}


def test_the_result_tolerates_fields_a_newer_agent_adds() -> None:
    """agent -> server messages are ADDITIVE: an agent newer than this control
    plane may report more, and refusing its result would hang the rejection
    until the 24-hour timeout (the `AgentUpdateResult` rule)."""
    got = AgentUninstallResult.model_validate({"scheduled": True, "helper_pid": 4242})
    assert got.model_dump() == {"scheduled": True}


def test_only_reject_dispatches_it() -> None:
    assert "agent.uninstall" not in DIRECTLY_DISPATCHABLE_JOB_TYPES
    reason = module_governed_reason("agent.uninstall")
    assert reason is not None
    assert "/api/v1/devices/{device_id}/review/reject" in reason
    assert reason.rstrip().endswith("(devices.review)")
    assert dispatch_permission_for("agent.uninstall") == "devices.review"


def test_no_machine_or_ai_actor_may_name_it() -> None:
    reason = non_approvable_reason("agent.uninstall")
    assert reason is not None and "NEVER" in reason


def test_the_review_gate_lets_it_through_in_both_held_states() -> None:
    assert "agent.uninstall" in ALLOWED_WHILE_PENDING
    assert ALLOWED_WHILE_REJECTED == {"agent.uninstall"}


def test_the_exported_contract_is_committed() -> None:
    params = json.loads(
        (REPO / "docs/kernel/wire-protocol/AgentUninstallParams.schema.json").read_text()
    )
    result = json.loads(
        (REPO / "docs/kernel/wire-protocol/AgentUninstallResult.schema.json").read_text()
    )
    assert params["properties"] == {} and params["additionalProperties"] is False
    assert result["required"] == ["scheduled"]
    assert result.get("additionalProperties") is not False

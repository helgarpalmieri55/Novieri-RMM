"""The two health job types and the policy the server builds
(docs/modules/endpoint-health-alerting/spec.md §5.2, §5.3, §0.4).

READ THIS BEFORE ADDING A TEST HERE.

THE POLICY REACHES THE AGENT AS A SIGNED JOB, and that is not an implementation
detail. §0.4: `app/gateway/protocol.py` defines exactly ONE server->agent
message, `JobEnvelope`. Inventing a policy channel would be a new instruction
path into a root/SYSTEM process that the signed-job runner does not verify --
invariant #1. Threshold configuration therefore inherits the entire signed-job
safety story, and no new server->agent message kind exists.

`cadence` COMES FROM THE REGISTRY, NOT FROM THE ROW, and the schema is what
makes that structural: `health_check` has no `cadence` column at all. The tests
below assert a posture check ships `posture` and a tick check ships `tick`, so
a build that hardcoded either one is red.

The Pydantic bounds duplicate migration 0042's CHECK constraints deliberately.
The params model is what an agent parses; a bound that lives only in the
database does not travel with the job.
"""

import pytest
from pydantic import ValidationError

from app.jobs import registry
from app.modules.health.jobtypes import (
    HealthCheckNowParams,
    HealthCheckSpec,
    HealthPolicyApplyParams,
    register_job_types,
)


def _spec(**kw: object) -> dict[str, object]:
    values: dict[str, object] = {
        "check_id": "06a6f000-0000-7000-8000-000000000001",
        "check_type": "cpu.busy_pct",
        "selector": "",
        "comparator": "gt",
        "threshold": "95",
        "for_ticks": 3,
        "cadence": "tick",
    }
    values.update(kw)
    return values


def _params(**kw: object) -> dict[str, object]:
    values: dict[str, object] = {
        "policy_version": 7,
        "tick_interval_s": 300,
        "posture_interval_s": 3600,
        "summary_interval_s": 1800,
        "checks": [_spec()],
    }
    values.update(kw)
    return values


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_job_types_register_into_the_kernel_registry() -> None:
    """The engine refuses to create a job whose type is not registered, so an
    unregistered `health.policy.apply` is a policy that can never be pushed."""
    register_job_types()
    names = registry.registered_names()
    assert "health.policy.apply" in names
    assert "health.check.now" in names


def test_the_policy_apply_type_carries_a_result_handler() -> None:
    """§5.2: no handler is needed for CORRECTNESS -- the acknowledged version
    arrives in the next summary -- but one is registered so §12's
    reconciliation sweep does not re-push for a whole summary interval."""
    register_job_types()
    assert registry.get_job_type("health.policy.apply").result_handler is not None


def test_check_now_takes_no_parameters() -> None:
    """§5.3, exactly the shape of `logs.ship`: dispatching it tells the agent to
    sample now; the scheduled cadence comes from the policy. A params field here
    would be a second, unversioned way to configure the tick."""
    assert HealthCheckNowParams().model_dump() == {}
    with pytest.raises(ValidationError):
        HealthCheckNowParams(interval_s=60)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The params models
# ---------------------------------------------------------------------------


def test_a_well_formed_policy_parses() -> None:
    params = HealthPolicyApplyParams(**_params())  # type: ignore[arg-type]
    assert params.policy_version == 7
    assert params.checks[0].check_type == "cpu.busy_pct"


def test_the_threshold_is_a_string_on_the_wire() -> None:
    """§5.2, for the same reason `health_report.value` is (§2.4): the field must
    hold "95", "true" and "running" without the wire acquiring a numeric type
    that invites aggregation."""
    assert HealthCheckSpec.model_json_schema()["properties"]["threshold"]["type"] == "string"
    parsed = HealthCheckSpec(**_spec(threshold="running"))  # type: ignore[arg-type]
    assert parsed.threshold == "running"


def test_thirty_three_checks_are_rejected_by_the_params_model() -> None:
    """The database caps at 32 with a trigger (§0.11); this is the same cap on
    the wire, because a bound that lives only in the database does not travel
    with the job."""
    with pytest.raises(ValidationError):
        HealthPolicyApplyParams(**_params(checks=[_spec() for _ in range(33)]))  # type: ignore[arg-type]


def test_thirty_two_checks_are_accepted() -> None:
    params = HealthPolicyApplyParams(**_params(checks=[_spec() for _ in range(32)]))  # type: ignore[arg-type]
    assert len(params.checks) == 32


def test_an_empty_check_list_is_a_legal_policy() -> None:
    """§5.2: it means "run nothing", and it is how a device whose profile was
    unassigned is DISARMED rather than left running its last policy forever."""
    params = HealthPolicyApplyParams(**_params(checks=[]))  # type: ignore[arg-type]
    assert params.checks == []


@pytest.mark.parametrize(
    "override",
    [
        {"tick_interval_s": 59},
        {"tick_interval_s": 3601},
        {"posture_interval_s": 899},
        {"summary_interval_s": 299},
        {"summary_interval_s": 86401},
        {"policy_version": -1},
    ],
)
def test_interval_bounds_match_the_database_checks(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HealthPolicyApplyParams(**_params(**override))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        {"comparator": "like"},
        {"cadence": "hourly"},
        {"for_ticks": 0},
        {"for_ticks": 61},
    ],
)
def test_check_spec_domains_are_pinned(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HealthCheckSpec(**_spec(**override))  # type: ignore[arg-type]


def test_extra_fields_are_forbidden_on_both_models() -> None:
    """`extra="forbid"` throughout (§5.2). An unknown field silently accepted at
    the server is a field an agent may already be acting on."""
    with pytest.raises(ValidationError):
        HealthPolicyApplyParams(**_params(retry=True))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        HealthCheckSpec(**_spec(unit="percent"))  # type: ignore[arg-type]


def test_the_policy_names_no_device() -> None:
    """Identity comes from the certificate binding on the way UP (§5.1) and from
    `JobEnvelope.device` on the way DOWN. A device field in the params would be
    a second, unsigned-by-convention answer to "who is this for"."""
    properties = set(HealthPolicyApplyParams.model_json_schema()["properties"])
    assert "device" not in properties
    assert "device_id" not in properties
    assert "hostname" not in properties


def test_the_policy_carries_no_credential_or_script_body() -> None:
    """§9.1's rule applied to the push direction: the only strings that travel
    are a check id, a type name, a selector and a threshold."""
    spec_properties = set(HealthCheckSpec.model_json_schema()["properties"])
    assert spec_properties == {
        "check_id",
        "check_type",
        "selector",
        "comparator",
        "threshold",
        "for_ticks",
        "cadence",
    }

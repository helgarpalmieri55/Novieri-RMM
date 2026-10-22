"""The two health job types
(`docs/modules/endpoint-health-alerting/spec.md` §5.2, §5.3, §0.4).

WHY THE POLICY IS A JOB AT ALL. `app/gateway/protocol.py` defines exactly one
server->agent message, `JobEnvelope`. There is no policy channel, and inventing
one would be a new instruction path into a root/SYSTEM process that the
signed-job runner does not verify -- invariant #1, straightforwardly. Delivering
the threshold policy as a signed job means the agent verifies signature, target
identity, freshness and non-replay exactly as for any other job before it
installs anything, and no new server->agent message kind exists.

This module holds ONLY the params models and the registration. The build and
dispatch live in `policy_push.py`, so importing the wire contract (which
`scripts/export_wire_schema.py` does, from the repo root, outside a request)
does not drag in the resolver, the job engine or the ORM session machinery.

THE BOUNDS HERE DUPLICATE MIGRATION 0042's CHECK CONSTRAINTS DELIBERATELY. The
params model is what an agent parses; a bound that lives only in the database
does not travel with the job, and `agent/` builds its Go structs from
this model's exported JSON Schema.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.registry import JobType, register

__all__ = [
    "CHECK_NOW_JOB_TYPE",
    "POLICY_APPLY_JOB_TYPE",
    "HealthCheckNowParams",
    "HealthCheckSpec",
    "HealthPolicyApplyParams",
    "register_job_types",
]

POLICY_APPLY_JOB_TYPE = "health.policy.apply"
CHECK_NOW_JOB_TYPE = "health.check.now"


class HealthCheckSpec(BaseModel):
    """One threshold, as the agent receives it (§5.2).

    `threshold` is a STRING for the same reason `health_report.value` and
    `health_alert.observed_value` are (§2.4): it must carry "95", "true" and
    "running" without the wire acquiring a numeric type that invites
    aggregation. `cadence` is emitted from the check-type REGISTRY and not from
    the row -- `health_check` has no cadence column, which is what makes that
    structural rather than a convention.

    Carries no device, no credential and no script body. The only strings that
    travel are a check id, a registry type name, a selector and a threshold.
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str
    check_type: str
    selector: str = Field(default="", max_length=200)
    comparator: Literal["gt", "gte", "lt", "lte", "eq", "neq", "is"]
    threshold: str = Field(max_length=64)
    for_ticks: int = Field(ge=1, le=60)
    cadence: Literal["tick", "posture"]


class HealthPolicyApplyParams(BaseModel):
    """server -> agent: the device's whole check policy (§5.2).

    AN EMPTY `checks` LIST IS A LEGAL POLICY AND MEANS "RUN NOTHING". It is how
    a device whose profile was unassigned is DISARMED, rather than left running
    the last policy it was given forever: a disarm must be an explicit
    instruction, not the absence of one.

    `checks` is bounded at 32 to match §0.11's database cap, and the three
    intervals are bounded to match `health_profile`'s CHECK constraints -- they
    are what make §0.5's idle claim checkable, and a claim that depends on a
    value only the server validated is not a claim about the agent.
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: int = Field(ge=0)
    tick_interval_s: int = Field(ge=60, le=3600)
    posture_interval_s: int = Field(ge=900)
    summary_interval_s: int = Field(ge=300, le=86400)
    checks: list[HealthCheckSpec] = Field(max_length=32)


class HealthCheckNowParams(BaseModel):
    """server -> agent: sample every check now and emit a full summary (§5.3).

    Exactly the shape of `logs.ship`: dispatching it tells the agent to act now;
    the scheduled cadence comes from the policy. A parameter here would be a
    second, unversioned way to configure the tick.
    """

    model_config = ConfigDict(extra="forbid")


def register_job_types() -> None:
    """Register both types into the kernel registry.

    A `result_handler` is registered for `health.policy.apply` even though §5.2
    says none is needed for CORRECTNESS -- the acknowledged version arrives in
    the next summary either way. It exists for PROMPTNESS: without it §12's
    reconciliation sweep would re-push to every freshly-configured device for a
    whole summary interval. The import is function-local because `policy_push`
    imports this module for the params models.
    """
    from app.modules.health.policy_push import record_policy_ack

    register(
        JobType(
            POLICY_APPLY_JOB_TYPE, HealthPolicyApplyParams, result_handler=record_policy_ack
        )
    )
    register(JobType(CHECK_NOW_JOB_TYPE, HealthCheckNowParams))

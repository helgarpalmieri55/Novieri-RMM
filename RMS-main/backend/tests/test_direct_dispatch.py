"""`POST /api/v1/jobs` may not launder around a module's own gate.

The route is gated on `jobs.dispatch`, which the seeded `technician` role holds,
and it hands an operator-supplied job TYPE and params blob to
`JobEngine.create`. Without `app.jobs.direct_dispatch` that reaches every
registered job type -- including `net.sweep`, whose authorization lives in
ipam's `authorized_targets`, and `script.run`, whose body is supposed to come
from a pinned `scripts` row.

WHAT EACH TEST HERE ASSERTS IS AN OBSERVABLE VALUE, not just a status code: the
job row that must NOT exist, and the one that must.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approved_actions import APPROVABLE_JOB_TYPES, NON_APPROVABLE_REASONS
from app.jobs.direct_dispatch import (
    DIRECTLY_DISPATCHABLE_JOB_TYPES,
    MODULE_GOVERNED_REASONS,
    RESERVED_JOB_TYPES,
    module_governed_reason,
)


def _live_registry() -> set[str]:
    """Every job type an agent can be asked to run, in one process.

    THE REGISTRY IS PROCESS-GLOBAL, so `registered_names()` otherwise depends on
    which modules an earlier test happened to import. Both registration paths
    are called explicitly, exactly as `test_approved_actions_destructive.py`
    does -- a partition assertion that only holds when the file runs alone is
    the silence it exists to break.
    """
    from app.jobs import registry
    from app.modules.remote.jobtypes import register_job_type as register_remote_job_type
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote_job_type()
    return set(registry.registered_names())


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------


def test_the_two_sets_partition_the_live_job_type_registry() -> None:
    """THE PROPERTY THAT MAKES THIS AN ALLOWLIST RATHER THAN "THE ONES WE
    THOUGHT OF".

    Registering a job type and forgetting to decide whether a human holding
    `jobs.dispatch` may create it through the kernel route must BREAK A TEST,
    not default to either answer. Membership assertions cannot do that; a
    partition can.

    If this fails because a module registered a new job type, the fix is to
    decide -- does any module route own this type's authorization and record? --
    and put the name in one map or the other, not to widen the assertion.
    """
    registered = _live_registry()
    decided = DIRECTLY_DISPATCHABLE_JOB_TYPES | set(MODULE_GOVERNED_REASONS)

    assert registered - decided == set(), (
        "these registered job types have no direct-dispatch decision; deny-by-default already "
        "refuses them, but the operator would be shown the generic reason instead of the route "
        "that owns their job type"
    )
    assert decided - registered == RESERVED_JOB_TYPES, (
        "a named job type is neither registered nor reserved. `RESERVED_JOB_TYPES` holds the "
        "types this repository refuses BEFORE it registers them -- today the two push-install "
        "types, whose cycle B1 builds the refusals and dispatches nothing (spec §14, §2.3). "
        "Anything else here is a typo in one of the two maps, and on the day B2 registers the "
        "push types this fails until `RESERVED_JOB_TYPES` is emptied in the same commit"
    )
    assert DIRECTLY_DISPATCHABLE_JOB_TYPES & set(MODULE_GOVERNED_REASONS) == set()


@pytest.mark.parametrize("job_type", sorted(MODULE_GOVERNED_REASONS))
def test_every_governed_job_type_names_the_route_that_owns_it(job_type: str) -> None:
    """A refusal an operator cannot act on is one they will look for a way
    around -- and the way around this one is the correct path. Each reason is
    asserted separately, because a single "it is refused" test cannot tell a
    deliberate entry from a typo in the map.

    Four entries have no route to name and are required to SAY SO rather than
    quietly omit one, because "there is no route" and "somebody forgot the
    route" read identically to an operator otherwise:

    - `health.policy.apply` is owned by profile assignment and the reconcile
      sweep, which is itself the answer.
    - `agent.cert.renew` / `agent.cert.install` are the two legs of automatic
      certificate rotation (docs/modules/agent/cert-renewal.md). Rotation needs
      no operator action at all -- the kernel's state machine dispatches both
      and retries on every connect -- so a route would be a third caller with
      its own authorization story, and an operator-authored `agent.cert.install`
      would push an arbitrary certificate onto an endpoint.
    - `patch.reboot` is dispatched only by the patch module's restart sweep,
      inside the device's maintenance window.
    """
    no_route = {"agent.cert.renew", "agent.cert.install", "patch.reboot"}
    reason = module_governed_reason(job_type)
    assert reason is not None
    assert len(reason) > 60
    if job_type == "health.policy.apply":
        assert "profile" in reason
    elif job_type in no_route:
        assert "no route for it by design" in reason, (
            f"{job_type} has no operator route, and the refusal must say so explicitly -- "
            "silence reads as an omission"
        )
        assert "/api/v1/" not in reason, f"{job_type} names a route that does not exist"
    else:
        assert "/api/v1/" in reason, f"{job_type}'s refusal does not name a route"


def test_an_unclassified_job_type_is_refused_by_default() -> None:
    reason = module_governed_reason("service.restart")
    assert reason is not None
    assert "DIRECTLY_DISPATCHABLE_JOB_TYPES" in reason


def test_this_allowlist_is_not_the_approved_action_allowlist() -> None:
    """The two allowlists bound DIFFERENT audiences and must not be quietly
    collapsed into one.

    `APPROVABLE_JOB_TYPES` bounds what a MACHINE may dispatch (the health sweep
    firing an approved action); this one bounds what a HUMAN may dispatch
    through the kernel route. `script.run` and `patch.apply` are the proof that
    the sets differ in BOTH directions: approvable, because the registry pins a
    reviewed script version and a reviewed params template, yet not directly
    dispatchable, because the kernel route would take an arbitrary body and an
    arbitrary reboot mode with no run record behind either.
    """
    assert {"script.run", "patch.apply"} <= APPROVABLE_JOB_TYPES
    assert {"script.run", "patch.apply"} <= set(MODULE_GOVERNED_REASONS)
    # ...and the other direction: a type no machine may fire is still the
    # kernel route's to allow, because nothing else can dispatch it at all.
    assert "logs.ship" in DIRECTLY_DISPATCHABLE_JOB_TYPES
    assert "net.sweep" in NON_APPROVABLE_REASONS
    assert "net.sweep" in MODULE_GOVERNED_REASONS


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def _add_device(api_env, org_id: UUID, name: str = "dd-1") -> UUID:
    from app.models import Device

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=name, os_family="windows")
        session.add(device)
        await session.commit()
        return device.id


async def _job_count(api_env, org_id: UUID) -> int:
    from app.models import Job

    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.org_id == org_id)
            )
        ).scalar_one()


async def test_a_technician_cannot_dispatch_a_sweep_through_the_kernel_route(
    api_env,
) -> None:
    """THE FINDING, END TO END. `technician` holds `jobs.dispatch` (migration
    0003) and `net.sweep` is registered, so before the allowlist this call
    returned 201 and a SIGNED envelope went to the device -- with no
    `ipam_scan_authorization` row in the org, no `ipam_sweep_run` to check the
    result against and no `ipam.sweep_dispatched` audit event.

    The assertion that falsifies is the JOB COUNT, not the status code: the
    point is that no signed job exists, not that the caller got a particular
    number back.
    """
    org_id = await api_env.create_org("dd")
    device_id = await _add_device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    response = await api_env.client.post(
        "/api/v1/jobs",
        json={
            "type": "net.sweep",
            "device_id": str(device_id),
            "params": {
                "subnet_id": str(uuid4()),
                "sweep_run_id": str(uuid4()),
                "targets": ["10.0.0.0/24"],
                "scope_digest": "0" * 64,
                "method": "icmp",
                "max_targets": 256,
            },
        },
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert "ipam" in response.json()["detail"]
    assert await _job_count(api_env, org_id) == 0, (
        "a net.sweep job row exists: the kernel route created and SIGNED a sweep with no "
        "authorization row behind it"
    )


async def test_an_org_admin_is_refused_too_because_this_is_not_about_the_role(
    api_env,
) -> None:
    """`org-admin` holds every permission in the platform, including
    `ipam.scope.manage`. They are still refused HERE -- the refusal is about the
    route lacking the module's record, not about the caller lacking authority.
    An admin who wants a sweep has ipam's endpoint, which writes the run row.
    """
    org_id = await api_env.create_org("dd-admin")
    device_id = await _add_device(api_env, org_id, "dd-2")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    # A COMPLETE, VALID `ScriptRunParams`, invented entirely in the request
    # body: no `scripts` row has this id, nothing was ever version-pinned, and
    # `script.dispatched` names nothing. Before the allowlist this was a 201 and
    # `whoami > /tmp/pwned` acquired the server's signature.
    response = await api_env.client.post(
        "/api/v1/jobs",
        json={
            "type": "script.run",
            "device_id": str(device_id),
            "params": {
                "interpreter": "bash",
                "body": "whoami > /tmp/pwned",
                "script_id": str(uuid4()),
                "script_version": 1,
                "timeout_s": 60,
            },
        },
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert "SYSTEM/root" in response.json()["detail"]
    assert await _job_count(api_env, org_id) == 0, (
        "a script.run job row exists: the kernel route signed an operator-authored script body "
        "with no scripts row and no script_run record behind it"
    )


async def test_the_kernel_job_types_a_technician_actually_needs_still_dispatch(
    api_env,
) -> None:
    """THE OTHER HALF, and the one that makes the refusals meaningful. A gate
    that refused everything would pass every test above and break the platform.
    Each allowed type is asserted to produce a REAL signed job row.
    """
    from app.models import Job

    org_id = await api_env.create_org("dd-ok")
    device_id = await _add_device(api_env, org_id, "dd-3")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    created: dict[str, str] = {}
    for job_type, params in (
        ("hello", {"message": "yo"}),
        ("inventory.collect", {}),
        ("logs.ship", {}),
    ):
        response = await api_env.client.post(
            "/api/v1/jobs",
            json={"type": job_type, "device_id": str(device_id), "params": params},
            headers=headers,
        )
        assert response.status_code == 201, f"{job_type}: {response.text}"
        created[job_type] = response.json()["id"]

    assert sorted(created) == ["hello", "inventory.collect", "logs.ship"]
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            (await session.execute(select(Job).where(Job.org_id == org_id))).scalars().all()
        )
    assert sorted(row.type for row in rows) == ["hello", "inventory.collect", "logs.ship"]
    assert all(row.signature for row in rows), "every created job must be signed"


async def test_an_unregistered_type_is_still_a_422_and_not_a_403(api_env) -> None:
    """The two refusals must stay distinguishable. "That job type does not
    exist" and "that job type is not yours to dispatch here" send an operator to
    two different places, and collapsing them into one status code loses the
    difference.
    """
    org_id = await api_env.create_org("dd-422")
    device_id = await _add_device(api_env, org_id, "dd-4")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    response = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "not.a.thing", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert "unknown job type" in response.json()["detail"]

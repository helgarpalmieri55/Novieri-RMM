"""SECURITY GATE: §20's two-permission remediation check
(`docs/modules/endpoint-health-alerting/spec.md` §20; `docs/modules/ai-automation.md` §0.11).

**THE CLAIM.** `POST /health/alerts/{id}/remediate` and
`POST /health/alerts/{id}/actions/{action_id}/approve` dispatch a signed job
whose TYPE comes from the resolved `approved_action` row. Each therefore requires
`health.remediate` AND the permission that job type's own module dispatch route
requires (`app/jobs/dispatch_permissions.py`). Gated on `health.remediate` alone,
its holder could fire a `patch.apply` job here while being refused the same job
on `POST /api/v1/patch/apply` -- privilege escalation across a module boundary
and invariant #6 violated: authorization was server-side, but it was the wrong
authorization. This is the same hole `app/core/ai/act.py::confirm_action` closed
on its own dispatch path and `23df039` closed on the kernel route.

**EVERY TEST HERE ASSERTS THE `jobs` TABLE AND THE ACTION ROW, NOT ONLY THE
STATUS.** "403" and "403 but it also dispatched" are different facts and only the
second is a breach; "403" and "403 and it burned the approval request" are
different facts too. The assertions are tuples of observable values -- status,
job count, job types, row state -- so a mutation prints what it did rather than
`DID NOT RAISE`.

**WHAT EVERY FIXTURE HERE VARIES, because a constant parameter is an untested
one:**

* **The job type in the catalogue row.** `patch.apply`, `script.run`,
  `inventory.collect` and `health.policy.apply` all appear, and they map to four
  DIFFERENT answers from `dispatch_permission_for` (`patch.apply`,
  `deploy.script.run`, `jobs.dispatch`, and `None`). A file that only ever
  seeded `inventory.collect` would pass against a check hard-coded to
  `jobs.dispatch`.
* **WHICH dispatch grant the caller holds, not merely whether they hold one.**
  `test_holding_another_modules_dispatch_grant_is_not_enough` gives the caller
  `deploy.script.run` against a `patch.apply` action, so an implementation that
  accepted any member of the map passes every other test here and fails that
  one.
* **Whether the stored job type is the one at dispatch.** One test edits the
  catalogue row after it was approved, because a gate that read a value the
  write path set earlier is the defect `4f4e05b` fixed for `destructive`.
* **Whether the route creates a job at all.** `refuse` is exercised with
  `health.remediate` ALONE and must still succeed: a fix that gated every
  remediation route would leave approval requests nobody may clear.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.dispatch_permissions import NO_HUMAN_DISPATCH_PATH
from app.models import (
    ApprovedAction,
    HealthAlertAction,
    Job,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from tests.health_action_fixtures import seed_approved_action, seed_script
from tests.health_fixtures import seed_alert, seed_check, seed_org, seed_profile

HEALTH = "/api/v1/health"

# The permissions the four job types below are gated on, per
# `app/jobs/dispatch_permissions.py`. Named as constants so each test reads as
# the claim it is making, and typed by hand rather than looked up through
# `dispatch_permission_for`: a fixture that asked the implementation which grant
# it wanted would agree with any answer it gave, including a wrong one.
PATCH_APPLY_GATE = "patch.apply"
SCRIPT_RUN_GATE = "deploy.script.run"
INVENTORY_COLLECT_GATE = "jobs.dispatch"

# §14.1-shaped `patch.apply` params. The engine validates params against the
# registered job type, so a plausible-looking blob would fail at
# `JobEngine.create` -- i.e. AFTER the gate under test -- and the positive half
# of each pair would then pass for the wrong reason.
PATCH_PARAMS: dict[str, Any] = {"patches": [{"kb": "KB5000001"}], "reboot": "never"}


# --------------------------------------------------------------------------- #
# Seeding.
# --------------------------------------------------------------------------- #


async def _holder(api_env: Any, org_id: UUID, *codes: str) -> dict[str, str]:
    """Headers for a user holding EXACTLY `codes`.

    Never `create_user(org_id, "org-admin")`: an org-admin holds every code in
    the platform, so the two-permission check and a one-permission check look
    identical to them and every test in this file would pass against the defect.
    """
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
        session.add_all([user, role])
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


async def _everything(api_env: Any, org_id: UUID) -> dict[str, str]:
    """A caller holding EVERY permission this platform defines.

    Only used where the refusal must not be attributable to a missing grant --
    the `NO_HUMAN_DISPATCH_PATH` test, where `dispatch_permission_for` answers
    `None` and there is no code that would satisfy it.
    """
    async with AsyncSession(api_env.admin_engine) as session:
        codes = list((await session.execute(select(Permission.code))).scalars().all())
    return await _holder(api_env, org_id, *codes)


async def _world(api_env: Any, tag: str) -> tuple[Any, UUID]:
    """An org with a profile, a check and one open alert on its device."""
    org = await seed_org(api_env, tag)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    return org, alert_id


async def _jobs(api_env: Any, org_id: UUID) -> list[Job]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(Job).where(Job.org_id == org_id))).scalars().all()
        )


async def _action_row_state(api_env: Any, row_id: UUID) -> tuple[str, UUID | None]:
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        return row.state, row.job_id


async def _awaiting_row(
    api_env: Any, org_id: UUID, alert_id: UUID, approved_action_id: UUID
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = HealthAlertAction(
            org_id=org_id,
            alert_id=alert_id,
            kind="remediate",
            state="awaiting_approval",
            approved_action_id=approved_action_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(row)
        await session.commit()
        return row.id


# --------------------------------------------------------------------------- #
# 1. POST /health/alerts/{id}/remediate -- the route §20 names.
# --------------------------------------------------------------------------- #


async def test_health_remediate_alone_cannot_dispatch_a_patch_apply(api_env) -> None:
    """**THE GOVERNING CLAIM.** The caller holds `health.remediate` and NOT
    `patch.apply`, so they may not fire a `patch.apply` remediation -- because
    they could not run one by hand on `POST /api/v1/patch/apply`.

    Deleting `_require_dispatch_grant`'s call in `dispatch_manual_remediation`
    makes this print `(200, 1, ['patch.apply'])`: a `patch.apply` job created by
    a caller who cannot dispatch one, which is invariant #6 violated in one
    observable value.
    """
    org, alert_id = await _world(api_env, "authz-remediate")
    headers = await _holder(api_env, org.org_id, "health.remediate")
    await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="suggest",
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "patch-now"},
    )
    jobs = await _jobs(api_env, org.org_id)

    assert (response.status_code, len(jobs), [job.type for job in jobs]) == (403, 0, [])
    assert PATCH_APPLY_GATE in response.json()["detail"]


async def test_the_same_caller_holding_patch_apply_dispatches_exactly_one_job(
    api_env,
) -> None:
    """The other half, with the SAME seeded action. Without it, the 403 above is
    equally consistent with a route that refuses everybody -- and a fix that
    refuses everybody is not a fix, it is an outage with a good excuse."""
    org, alert_id = await _world(api_env, "authz-remediate-ok")
    headers = await _holder(api_env, org.org_id, "health.remediate", PATCH_APPLY_GATE)
    await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="suggest",
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "patch-now"},
    )
    jobs = await _jobs(api_env, org.org_id)

    assert (response.status_code, [job.type for job in jobs]) == (200, ["patch.apply"])
    # Invariant #1: it still went through the signed engine, and #4's record is
    # still the kernel's plus health's own.
    assert jobs[0].signature is not None
    assert str(jobs[0].id) == response.json()["job_id"]


async def test_holding_another_modules_dispatch_grant_is_not_enough(api_env) -> None:
    """The caller holds `deploy.script.run` -- a real member of the map, and the
    gate on a real dispatch route -- against a `patch.apply` action.

    This is the test an implementation that checked "does this principal hold ANY
    permission in `DISPATCH_PERMISSIONS`" would fail, and it is the only one:
    every other case in this file is satisfied by that weaker check.
    """
    org, alert_id = await _world(api_env, "authz-wrong-grant")
    headers = await _holder(api_env, org.org_id, "health.remediate", SCRIPT_RUN_GATE)
    await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="suggest",
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "patch-now"},
    )
    jobs = await _jobs(api_env, org.org_id)

    assert (response.status_code, len(jobs)) == (403, 0)
    assert PATCH_APPLY_GATE in response.json()["detail"]


async def test_a_script_run_remediation_is_gated_on_deploy_script_run(api_env) -> None:
    """A SECOND job type with a DIFFERENT answer, both ways round in one test.

    `script.run`'s own route is gated on `deploy.script.run`, and the map is the
    only place that is written down. A file that only tested `patch.apply` --
    where the job type and its permission share a name -- would pass against an
    implementation that derived the permission from the job type's own spelling.

    The refused caller holds `deploy.execute`, the SIBLING code in the same
    module (it gates `app.deploy`), so the refusal cannot be explained by "the
    caller holds nothing from deploy".
    """
    org, alert_id = await _world(api_env, "authz-script")
    script_id = await seed_script(api_env, org.org_id, org.user_id)
    await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="restart-spooler",
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        autonomy="suggest",
        risk_class="medium",
    )

    refused = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=await _holder(api_env, org.org_id, "health.remediate", "deploy.execute"),
        json={"action_key": "restart-spooler"},
    )
    assert (refused.status_code, len(await _jobs(api_env, org.org_id))) == (403, 0)
    assert SCRIPT_RUN_GATE in refused.json()["detail"]

    allowed = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=await _holder(api_env, org.org_id, "health.remediate", SCRIPT_RUN_GATE),
        json={"action_key": "restart-spooler"},
    )
    assert (allowed.status_code, [j.type for j in await _jobs(api_env, org.org_id)]) == (
        200,
        ["script.run"],
    )


# --------------------------------------------------------------------------- #
# 2. `None` IS A REFUSAL, NOT A PASS.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("job_type", sorted(NO_HUMAN_DISPATCH_PATH))
async def test_a_job_type_with_no_human_dispatch_route_is_refused_holding_everything(
    api_env, job_type: str
) -> None:
    """`dispatch_permission_for` returns `None` for these, and `None` MEANS
    REFUSE.

    **ALL THREE ENTRIES, PARAMETRIZED OFF THE LIVE MAP**, because "no human
    dispatch path" is not a property of one job type and a fourth entry added
    later must land here without anyone remembering to add it. Every one of them
    is dispatched by a kernel state machine from state the kernel itself minted;
    there is no route a human can call, so the question "does this human hold
    what the route requires" has no route to ask about.

    Reading `None` as "no permission needed" inverts the map and turns this fix
    into a bypass. It is the one mutation a caller-holds-nothing fixture cannot
    catch, which is why this caller holds EVERY permission the platform defines
    -- the refusal cannot be attributed to a missing grant, because no grant
    exists that would satisfy it.

    What the mutation actually buys an attacker, for each entry:
    `health.policy.apply` with `checks: []` is the platform's own monitoring
    kill switch; `agent.cert.renew` and `agent.cert.install` are the two legs of
    mTLS identity rotation. `agent.cert.renew` takes EMPTY params, so under the
    inverted reading it does not merely 500 on validation -- it dispatches, and
    this test prints `(200, 1)`.
    """
    org, alert_id = await _world(api_env, "authz-noroute")
    headers = await _everything(api_env, org.org_id)
    await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="no-route",
        job_type=job_type,
        autonomy="suggest",
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "no-route"},
    )
    jobs = await _jobs(api_env, org.org_id)

    assert (response.status_code, len(jobs)) == (403, 0), response.text
    assert "no human dispatch route" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 3. The job type at DISPATCH is the authority, not the one that was approved.
# --------------------------------------------------------------------------- #


async def test_the_gate_reads_the_job_type_at_dispatch_not_the_one_that_was_stored(
    api_env,
) -> None:
    """`4f4e05b`'s reasoning, applied to `job_type`.

    A catalogue row authored as `inventory.collect` -- through the audited
    `health.action.admin` write path, where a `jobs.dispatch` holder is a
    perfectly ordinary dispatcher -- is edited to `patch.apply` afterwards. The
    row is read fresh in the same transaction that creates the job, so it is
    gated as `patch.apply` and the `jobs.dispatch` holder is refused.

    The caller holds the grant the STORED value would have wanted, which is what
    makes this discriminate: a gate that had cached, precomputed or trusted the
    approved-at value would answer 200 here and dispatch a `patch.apply`.
    """
    org, alert_id = await _world(api_env, "authz-edited")
    headers = await _holder(api_env, org.org_id, "health.remediate", INVENTORY_COLLECT_GATE)
    action_id = await seed_approved_action(
        api_env, org.org_id, org.user_id, key="drifted", autonomy="suggest"
    )
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(ApprovedAction).where(ApprovedAction.id == action_id))
        ).scalar_one()
        assert row.job_type == "inventory.collect", "the premise: it was approved as this"
        row.job_type = "patch.apply"
        row.params_template = PATCH_PARAMS
        session.add(row)
        await session.commit()

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "drifted"},
    )
    jobs = await _jobs(api_env, org.org_id)

    assert (response.status_code, len(jobs)) == (403, 0), response.text
    assert PATCH_APPLY_GATE in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 4. The approve route has the identical shape and the identical gate.
# --------------------------------------------------------------------------- #


async def test_approving_a_patch_apply_remediation_needs_patch_apply_too(api_env) -> None:
    """§20: "The same applies to `POST /health/alerts/{id}/actions/{id}/approve`,
    which has the identical shape."

    THE ROW STATE IS ASSERTED, not only the job count. A refusal that left the
    request `dispatched`, `skipped` or `blocked_rate_limit` would have consumed
    the alert's one and only `remediate` slot -- `UNIQUE (alert_id, kind)` means
    there is no second attempt to make -- so a caller who may not approve could
    permanently deny one who may. Deleting the check in `approve_action` prints
    `(200, 1, 'dispatched')`.
    """
    org, alert_id = await _world(api_env, "authz-approve")
    headers = await _holder(api_env, org.org_id, "health.remediate")
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="require_approval",
    )
    row_id = await _awaiting_row(api_env, org.org_id, alert_id, action_id)

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/actions/{row_id}/approve", headers=headers
    )
    jobs = await _jobs(api_env, org.org_id)
    state, job_id = await _action_row_state(api_env, row_id)

    assert (response.status_code, len(jobs), state, job_id) == (
        403,
        0,
        "awaiting_approval",
        None,
    ), response.text
    assert PATCH_APPLY_GATE in response.json()["detail"]


async def test_the_same_approver_holding_patch_apply_dispatches_it(api_env) -> None:
    """The positive half of the approve pair, same seeded request."""
    org, alert_id = await _world(api_env, "authz-approve-ok")
    headers = await _holder(api_env, org.org_id, "health.remediate", PATCH_APPLY_GATE)
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="require_approval",
    )
    row_id = await _awaiting_row(api_env, org.org_id, alert_id, action_id)

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/actions/{row_id}/approve", headers=headers
    )
    jobs = await _jobs(api_env, org.org_id)
    state, job_id = await _action_row_state(api_env, row_id)

    assert (response.status_code, [j.type for j in jobs], state) == (
        200,
        ["patch.apply"],
        "dispatched",
    ), response.text
    assert job_id == jobs[0].id


# --------------------------------------------------------------------------- #
# 5. The refusal path is deliberately NOT gated this way.
# --------------------------------------------------------------------------- #


async def test_refusing_a_remediation_still_needs_only_health_remediate(api_env) -> None:
    """The bound on the fix, asserted rather than assumed.

    `refuse` creates no job, so there is no dispatch to authorize and nothing for
    the second permission to be about. Requiring it would leave approval requests
    that nobody in the org may decline sitting until `approval_ttl_s` expires
    them -- the same argument `POST /ai/actions/{id}/reject` makes for being
    gated on `ai.act.propose` rather than `ai.act.confirm`.

    The action is `patch.apply` on purpose: this caller is refused DISPATCHING it
    by the test above, with the same permission set.
    """
    org, alert_id = await _world(api_env, "authz-refuse")
    headers = await _holder(api_env, org.org_id, "health.remediate")
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        key="patch-now",
        job_type="patch.apply",
        params_template=PATCH_PARAMS,
        destructive=True,
        autonomy="require_approval",
    )
    row_id = await _awaiting_row(api_env, org.org_id, alert_id, action_id)

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/actions/{row_id}/refuse",
        headers=headers,
        json={"reason": "we are rebuilding this box"},
    )
    state, job_id = await _action_row_state(api_env, row_id)

    assert (response.status_code, state, job_id, len(await _jobs(api_env, org.org_id))) == (
        200,
        "skipped",
        None,
        0,
    ), response.text

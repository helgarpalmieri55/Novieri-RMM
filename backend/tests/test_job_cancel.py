"""`POST /api/v1/jobs/{job_id}/cancel` -- withdrawal, and honest refusals.

Cycle 1 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. The
one thing this route may assert on its own authority is that a QUEUED job
never reached a device; everything else is a 409 that names the state, because
a no-op reported as a success is the defect class this programme exists to
close.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Device,
    Job,
    JobStatus,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

JOBS = "/api/v1/jobs"


async def _device(api_env: Any, org_id: UUID, hostname: str = "cx-1") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="linux")
        session.add(device)
        await session.commit()
        return device.id


async def _grant(api_env: Any, org_id: UUID, *codes: str) -> tuple[UUID, str]:
    """A user holding EXACTLY `codes`, as `(user_id, oidc_sub)`.

    Never a seeded `org-admin`: they hold every code in the platform, so a
    check for two permissions and a check for one look identical to them and
    the escalation tests below would pass against the defect.
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
        return user.id, sub


async def _holder(api_env: Any, org_id: UUID, *codes: str) -> dict[str, str]:
    """Auth headers for `_grant`'s user."""
    _, sub = await _grant(api_env, org_id, *codes)
    return await api_env.headers(org_id, sub)


async def _job(
    api_env: Any,
    org_id: UUID,
    device_id: UUID,
    *,
    type: str = "hello",
    status: JobStatus = JobStatus.queued,
    finished: bool = False,
) -> UUID:
    """A job row written on the admin engine, in whatever state the test needs.

    Written directly rather than driven through the engine's transitions on
    purpose: the refusals under test are a function of the STORED state, and a
    fixture that could only reach a state by the route the engine allows could
    not produce, say, a `refused` job without also exercising three unrelated
    code paths.
    """
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type=type,
            target_type="device",
            target_id=device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=status,
            signature="sig",
            finished_at=now if finished else None,
        )
        session.add(job)
        await session.commit()
        return job.id


async def _row(api_env: Any, job_id: UUID) -> Job:
    async with AsyncSession(api_env.admin_engine) as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _jobs_in(api_env: Any, org_id: UUID) -> list[Job]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(Job).where(Job.org_id == org_id))).scalars().all()
        )


# ---------------------------------------------------------------------------
# The one thing the platform may assert
# ---------------------------------------------------------------------------


async def test_a_queued_job_is_withdrawn_and_said_to_be(api_env: Any) -> None:
    org_id = await api_env.create_org("cancel-ok")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancel_outcome"] == "withdrawn"
    assert body["cancel_requested_at"] is not None

    row = await _row(api_env, job_id)
    assert row.status is JobStatus.cancelled
    assert row.cancel_outcome == "withdrawn"
    assert row.finished_at is not None


async def test_withdrawal_creates_no_job_and_sends_nothing(api_env: Any) -> None:
    """§7 test 1, and the invariant-1 half of it.

    Cycle 1 sends NO command to any endpoint. If this route ever mints a
    second job row, it has crossed into cycle 2 and belongs in a review that
    knows it.
    """
    org_id = await api_env.create_org("cancel-nojob")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    assert (
        await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    ).status_code == 200

    assert [job.id for job in await _jobs_in(api_env, org_id)] == [job_id]


async def test_the_requester_is_recorded_in_the_chain_and_not_in_the_response(
    api_env: Any,
) -> None:
    """Both audit events land, and neither actor leaks into `JobOut`.

    `cancel_requested_by` is a real column and a real foreign key, and the
    design is explicit that no API route returns it: the actor belongs in the
    hash-chained log, which is where an investigator looks, and `JobOut` has
    never disclosed a user id.
    """
    org_id = await api_env.create_org("cancel-audit")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    body = (await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)).json()
    assert "cancel_requested_by" not in body

    row = await _row(api_env, job_id)
    assert row.cancel_requested_by is not None

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        events = {
            r.action: r
            for r in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.target_id == str(job_id))
                )
            ).scalars()
        }
    assert set(events) == {"jobs.cancel_requested", "jobs.cancelled"}
    assert events["jobs.cancel_requested"].actor_id == row.cancel_requested_by
    assert events["jobs.cancel_requested"].meta["mechanism"] == "withdrawn"
    assert events["jobs.cancelled"].meta["cancel_outcome"] == "withdrawn"


# ---------------------------------------------------------------------------
# The refusals -- each names the state rather than saying "conflict"
# ---------------------------------------------------------------------------


async def test_a_job_that_reached_the_endpoint_is_a_409_saying_why(api_env: Any) -> None:
    """`dispatched` and `running` are refused, and the message is the point.

    Cycle 1 cannot stop a job that is already on a machine, and saying so is
    the honest answer. A 409 reading "conflict" would leave the operator with
    a dead button and no idea what to do instead; this one names what the job
    will do next.
    """
    org_id = await api_env.create_org("cancel-inflight")
    device_id = await _device(api_env, org_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    for state, phrase in (
        (JobStatus.dispatched, "already been sent to the device"),
        (JobStatus.running, "executing on the device"),
    ):
        job_id = await _job(api_env, org_id, device_id, status=state)
        response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert phrase in detail
        assert "will run or expire at" in detail
        assert (await _row(api_env, job_id)).status is state
        assert (await _row(api_env, job_id)).cancel_requested_at is None


async def test_a_finished_job_is_a_409_naming_its_outcome(api_env: Any) -> None:
    org_id = await api_env.create_org("cancel-finished")
    device_id = await _device(api_env, org_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    for state, phrase in (
        (JobStatus.succeeded, "finished successfully"),
        (JobStatus.failed, "failed"),
        (JobStatus.expired, "expired"),
        (JobStatus.refused, "was refused by the endpoint"),
    ):
        job_id = await _job(api_env, org_id, device_id, status=state, finished=True)
        response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
        assert response.status_code == 409, response.text
        assert phrase in response.json()["detail"]
        assert (await _row(api_env, job_id)).status is state


async def test_a_second_cancel_names_the_first_request(api_env: Any) -> None:
    org_id = await api_env.create_org("cancel-twice")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    assert (
        await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    ).status_code == 200
    first_request = (await _row(api_env, job_id)).cancel_requested_at
    assert first_request is not None

    again = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert "already requested at" in detail
    assert first_request.isoformat() in detail
    # The first request is not restamped by the second attempt.
    assert (await _row(api_env, job_id)).cancel_requested_at == first_request


async def test_an_unknown_job_is_a_404(api_env: Any) -> None:
    org_id = await api_env.create_org("cancel-404")
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")
    response = await api_env.client.post(f"{JOBS}/{uuid4()}/cancel", headers=headers)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# RBAC -- design §9.2: whoever may start a job may stop it
# ---------------------------------------------------------------------------


async def test_reading_a_job_does_not_authorize_stopping_it(api_env: Any) -> None:
    org_id = await api_env.create_org("cancel-readonly")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read")

    response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert response.status_code == 403, response.text
    assert "jobs.dispatch" in response.json()["detail"]
    assert (await _row(api_env, job_id)).status is JobStatus.queued


async def test_jobs_dispatch_does_not_reach_a_module_governed_type(api_env: Any) -> None:
    """The escalation §9.2 forecloses, asserted against a job that exists.

    A holder of `jobs.dispatch` could never have STARTED a `patch.apply` --
    that route is gated on `patch.apply` -- so letting them stop one would
    hand them authority over fleet patching through a route nobody reviewed
    for it. A fleet-wide cancel is a denial of service on patching, which is
    exactly why cancel is not the lesser act a weaker gate would imply.
    """
    org_id = await api_env.create_org("cancel-escalate")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, type="patch.apply")
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert response.status_code == 403, response.text
    assert "patch.apply" in response.json()["detail"]
    assert (await _row(api_env, job_id)).status is JobStatus.queued

    # NOT VACUOUS: the same caller, the same route, a type they may dispatch.
    theirs = await _job(api_env, org_id, device_id, type="inventory.collect")
    assert (
        await api_env.client.post(f"{JOBS}/{theirs}/cancel", headers=headers)
    ).status_code == 200


async def test_a_kernel_minted_type_is_uncancellable_by_anyone(api_env: Any) -> None:
    """`NO_HUMAN_DISPATCH_PATH` means `None`, and `None` means refuse.

    The caller here holds EVERY permission the platform defines, so the 403
    cannot be read as a missing grant: there is no code that would satisfy
    this, because there is no route a human calls to start one of these. An
    operator cancelling a certificate rotation is cancelling the thing that
    keeps the endpoint reachable.
    """
    org_id = await api_env.create_org("cancel-kernel")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, type="agent.cert.renew")
    async with AsyncSession(api_env.admin_engine) as session:
        codes = list((await session.execute(select(Permission.code))).scalars().all())
    headers = await _holder(api_env, org_id, *codes)

    response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert response.status_code == 403, response.text
    assert "no human dispatch route" in response.json()["detail"]
    assert (await _row(api_env, job_id)).status is JobStatus.queued


# ---------------------------------------------------------------------------
# Tenancy -- idiom 3 of 2026-08-21-tenancy-test-efficacy.md
# ---------------------------------------------------------------------------


async def test_a_neighbours_job_cannot_be_cancelled_without_rls_to_mask_it(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """Drive the REAL route with `app.state.app_engine` repointed at the
    BYPASSRLS admin engine, so the service's own `Job.org_id == ctx.org_id` is
    the only thing between the caller and a neighbour's job.

    THIS IS THE ONLY SHAPE THAT CAN FAIL. Everything served through
    `org_session` runs as a NOBYPASSRLS role, so with RLS in place a test that
    asks for a neighbour's job gets a 404 whether or not the application
    carries a predicate -- which is why the audit found that deleting one
    turns no route test red. Falsified rather than assumed: removing
    `Job.org_id == org_id` from `app/jobs/cancel.py::request_cancellation` makes this
    a 200 that cancels the victim's job.
    """
    victim = await api_env.create_org("cancel-victim")
    mine = await api_env.create_org("cancel-mine")
    victim_job = await _job(api_env, victim, await _device(api_env, victim, "v-1"))
    headers = await _holder(api_env, mine, "jobs.read", "jobs.dispatch")

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(f"{JOBS}/{victim_job}/cancel", headers=headers)
    finally:
        api_env.app.state.app_engine = original

    assert response.status_code == 404, response.text
    row = await _row(api_env, victim_job)
    assert row.status is JobStatus.queued
    assert row.cancel_requested_at is None


async def test_rls_hides_a_neighbours_job_from_an_unfiltered_select(
    api_env: Any, app_engine: AsyncEngine
) -> None:
    """The DATABASE half of invariant #5 for the column this cycle writes."""
    victim = await api_env.create_org("cancel-rls-victim")
    mine = await api_env.create_org("cancel-rls-mine")
    victim_job = await _job(api_env, victim, await _device(api_env, victim, "v-2"))

    async with org_session(mine, engine=app_engine, clients=ClientScope.all()) as session:
        visible = {row.id for row in (await session.execute(select(Job))).scalars()}
    assert victim_job not in visible


# ---------------------------------------------------------------------------
# §7 test 6: a cancel and a delivery on the same queued row
# ---------------------------------------------------------------------------


async def test_a_cancel_racing_a_delivery_never_produces_both(
    api_env: Any, tmp_path: Any
) -> None:
    """Exactly one winner, and it is the one that took the row lock first.

    The gateway marks delivery in another process. Without
    `with_for_update()` in `request_cancellation` both transactions read `queued`
    under READ COMMITTED, both write, and the later one wins silently --
    producing a job the console reports as withdrawn and the endpoint has in
    hand, which is the single outcome this cycle exists to make impossible.

    The competing transaction here does exactly what `app/gateway/main.py::
    _on_delivered` does -- lock the row, `mark_dispatched` -- because that
    function opens and closes its own session and so cannot be paused
    mid-transaction. Its own half of the lock is asserted from the other side
    in `tests/test_dispatch.py::test_on_delivered_reads_the_status_under_a_row_lock`.

    Deleting `with_for_update()` from `request_cancellation` turns this red twice: the
    cancel is not blocked, and it writes `cancelled` over a delivered job.
    """
    import asyncio

    from app.core.signing import LocalEd25519Signer
    from app.jobs.cancel import CancelConflict, request_cancellation
    from app.jobs.engine import JobEngine

    org_id = await api_env.create_org("cancel-race")
    device_id = await _device(api_env, org_id, "race-1")
    job_id = await _job(api_env, org_id, device_id)
    actor, _ = await _grant(api_env, org_id, "jobs.read", "jobs.dispatch")

    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "race.key"))
    locked, released = asyncio.Event(), asyncio.Event()

    async def delivery() -> None:
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            job = (
                await session.execute(
                    select(Job).where(Job.id == job_id).with_for_update()
                )
            ).scalar_one()
            locked.set()
            await released.wait()
            await engine.mark_dispatched(session, job)

    async def cancel() -> Exception | None:
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            try:
                await request_cancellation(
                    session,
                    engine=engine,
                    org_id=org_id,
                    user_id=actor,
                    job_id=job_id,
                )
            except CancelConflict as exc:
                return exc
            return None

    deliver_task = asyncio.create_task(delivery())
    await locked.wait()
    cancel_task = asyncio.create_task(cancel())
    await asyncio.sleep(0.2)
    assert not cancel_task.done(), "the cancel read the row without taking its lock"
    released.set()
    await deliver_task
    refusal = await cancel_task

    assert isinstance(refusal, CancelConflict)
    row = await _row(api_env, job_id)
    assert row.status is JobStatus.dispatched
    assert row.cancel_requested_at is None
    assert row.cancel_outcome is None

"""Interrupting a job that has already reached an endpoint.

Cycle 2 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. Cycle 1
could withdraw a `queued` job -- one the server can prove it never sent -- and
refused everything else. This is the half that needs the endpoint to answer for
itself, and it is the security-relevant half: **the mechanism that stops a job
on a machine is a signed job**, verified on that machine before anything acts
on it, because a mechanism that is not is a fleet-wide denial-of-service
primitive.

Every test here is written so that it can fail. Where the assertion is about
something NOT happening -- a target row untouched, no second job minted -- the
test also exercises the case where it should, so a check that has quietly
stopped reaching the code under test shows up as a pair that both pass.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.cancel import (
    AGENT_CANCELLED_REFUSAL,
    CANCEL_JOB_TYPE,
    handle_cancel_refusal,
    handle_cancel_result,
    is_agent_cancellation,
)
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    Device,
    Job,
    JobLog,
    JobStatus,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

JOBS = "/api/v1/jobs"

#: A registered type that IS interruptible on day one (design §9.1's option
#: (b)), and the permission its own dispatch route requires (§9.2). Both are
#: read from the platform's own maps in `test_the_day_one_cancellable_set_is_the_specs`
#: rather than assumed here.
CANCELLABLE_TYPE = "script.run"
CANCELLABLE_GRANT = "deploy.script.run"


# --------------------------------------------------------------------------- #
# Fixtures, all written on the admin engine so a test can produce any state.
# --------------------------------------------------------------------------- #


async def _device(api_env: Any, org_id: UUID, hostname: str = "cx-1") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="linux")
        session.add(device)
        await session.commit()
        return device.id


async def _grant(api_env: Any, org_id: UUID, *codes: str) -> tuple[UUID, str]:
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
    _, sub = await _grant(api_env, org_id, *codes)
    return await api_env.headers(org_id, sub)


async def _job(
    api_env: Any,
    org_id: UUID,
    device_id: UUID,
    *,
    type: str = CANCELLABLE_TYPE,
    status: JobStatus = JobStatus.dispatched,
    cancel_requested: bool = False,
    cancel_outcome: str | None = None,
) -> UUID:
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
            cancel_requested_at=now if cancel_requested else None,
            cancel_outcome=cancel_outcome,
        )
        session.add(job)
        await session.commit()
        return job.id


async def _row(api_env: Any, job_id: UUID) -> Job:
    async with AsyncSession(api_env.admin_engine) as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _jobs_of_type(api_env: Any, org_id: UUID, type: str) -> list[Job]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(Job).where(Job.org_id == org_id, Job.type == type)
                )
            )
            .scalars()
            .all()
        )


async def _audit(api_env: Any, org_id: UUID, target_id: UUID) -> dict[str, AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.target_id == str(target_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.action: row for row in rows}


# --------------------------------------------------------------------------- #
# §7 test 2 -- the route mints a SIGNED job and does not touch the status.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state", [JobStatus.dispatched, JobStatus.running])
async def test_cancelling_a_job_on_a_device_mints_one_signed_cancel(
    api_env: Any, state: JobStatus
) -> None:
    """The whole of §7 test 2, plus the signature, plus the publish.

    THE STATUS IS UNCHANGED AND THAT IS THE ASSERTION THAT MATTERS. A platform
    that wrote `cancelled` here would be claiming something only the endpoint
    can know. What it may say is that a human asked, at a time, and that it
    passed the request on as a named signed job.
    """
    org_id = await api_env.create_org(f"interrupt-{state.value}")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, status=state)
    headers = await _holder(api_env, org_id, "jobs.read", CANCELLABLE_GRANT)

    import redis.asyncio as aioredis

    listener = aioredis.from_url(api_env.settings.redis_url, decode_responses=True)
    pubsub = listener.pubsub()
    await pubsub.subscribe(f"dispatch:{device_id}")
    assert await pubsub.get_message(timeout=2.0) is not None, "subscribe not confirmed"

    response = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == state.value, "the target is still where it was"
    assert body["cancel_requested_at"] is not None
    assert body["cancel_outcome"] == "requested"

    row = await _row(api_env, job_id)
    assert row.status is state
    assert row.finished_at is None

    cancels = await _jobs_of_type(api_env, org_id, CANCEL_JOB_TYPE)
    assert len(cancels) == 1, "exactly one signed cancel, never two"
    cancel = cancels[0]
    assert cancel.target_id == device_id, "signed for the SAME device"
    assert cancel.params == {"target_job_id": str(job_id)}
    # INVARIANT 1. The cancel carries a server signature over its own canonical
    # bytes, exactly like every other job; there is no unsigned command path.
    assert cancel.signature, "a cancel that reaches an endpoint is a SIGNED job"
    assert cancel.status is JobStatus.queued

    # INVARIANT 2. It is PUBLISHED for the gateway to write down the connection
    # the agent opened. Nothing here dials an endpoint.
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
    await pubsub.aclose()
    await listener.aclose()
    assert message is not None, "the signed cancel was never published"
    envelope = json.loads(message["data"])
    assert envelope["kind"] == "job"
    assert envelope["job"]["id"] == str(cancel.id)
    assert envelope["job"]["type"] == CANCEL_JOB_TYPE
    assert envelope["job"]["device"] == str(device_id)
    assert envelope["sig"] == cancel.signature


async def test_the_cancel_is_signed_over_its_own_bytes_and_verifies(
    api_env: Any, tmp_path: Any
) -> None:
    """The signature is not merely PRESENT -- it verifies.

    `assert cancel.signature` above would pass against a column holding the
    string "sig". This re-derives the canonical bytes the engine signs and
    checks them against the very key the API server signed with, so a cancel
    minted with a stale or absent payload fails here rather than on an
    endpoint.
    """
    from app.core.signing import verify_signature
    from app.jobs.canonical import canonical_bytes

    org_id = await api_env.create_org("interrupt-sig")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", CANCELLABLE_GRANT)
    assert (
        await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    ).status_code == 200

    cancel = (await _jobs_of_type(api_env, org_id, CANCEL_JOB_TYPE))[0]
    engine: JobEngine = api_env.app.state.job_engine
    envelope = await engine.signed_envelope(cancel)

    signer = LocalEd25519Signer(api_env.settings.signing_key_path, allow_generate=False)
    assert verify_signature(
        await signer.public_key_pem(), canonical_bytes(envelope["job"]), envelope["sig"]
    ), "the cancel's signature does not verify against the platform's own key"

    # NOT VACUOUS: the same check against tampered bytes must fail, or this
    # test would pass against a `verify_signature` that returns True blindly.
    tampered = dict(envelope["job"])
    tampered["params"] = {"target_job_id": str(uuid4())}
    assert not verify_signature(
        await signer.public_key_pem(), canonical_bytes(tampered), envelope["sig"]
    )


async def test_the_request_is_audited_as_asked_and_not_as_done(api_env: Any) -> None:
    """`mechanism` is the field that stops "we asked" reading like "we stopped it".

    A withdrawal writes `mechanism: withdrawn` AND a `jobs.cancelled` event in
    the same breath, because the server did the thing. An interruption writes
    `mechanism: agent_asked` and NO `jobs.cancelled` at all -- there is nothing
    to record yet.
    """
    org_id = await api_env.create_org("interrupt-audit")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", CANCELLABLE_GRANT)
    assert (
        await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    ).status_code == 200

    events = await _audit(api_env, org_id, job_id)
    assert set(events) == {"jobs.cancel_requested"}, (
        "an interruption has not stopped anything yet, so nothing may say it has"
    )
    meta = events["jobs.cancel_requested"].meta
    assert meta["mechanism"] == "agent_asked"
    assert meta["from_status"] == "dispatched"
    cancel = (await _jobs_of_type(api_env, org_id, CANCEL_JOB_TYPE))[0]
    assert meta["cancel_job_id"] == str(cancel.id)
    assert events["jobs.cancel_requested"].actor_type == "user"


async def test_a_second_interrupt_mints_no_second_command(api_env: Any) -> None:
    org_id = await api_env.create_org("interrupt-twice")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", CANCELLABLE_GRANT)
    assert (
        await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    ).status_code == 200

    again = await api_env.client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
    assert again.status_code == 409, again.text
    assert "already requested at" in again.json()["detail"]
    assert len(await _jobs_of_type(api_env, org_id, CANCEL_JOB_TYPE)) == 1


# --------------------------------------------------------------------------- #
# §9.1 -- the cancellable flag, deny by default.
# --------------------------------------------------------------------------- #


async def test_a_type_that_is_not_cancellable_is_a_409_naming_why(api_env: Any) -> None:
    """Deny by default, and the refusal is a sentence rather than "conflict".

    NOT VACUOUS: the same caller, the same route and the same state, on a type
    that IS cancellable, succeeds -- so this cannot pass because the route is
    broken for everybody.
    """
    org_id = await api_env.create_org("interrupt-denied")
    device_id = await _device(api_env, org_id)
    denied = await _job(api_env, org_id, device_id, type="inventory.collect")
    headers = await _holder(
        api_env, org_id, "jobs.read", "jobs.dispatch", CANCELLABLE_GRANT
    )

    response = await api_env.client.post(f"{JOBS}/{denied}/cancel", headers=headers)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "may not be interrupted" in detail
    assert "inventory.collect" in detail
    assert (await _row(api_env, denied)).cancel_requested_at is None
    assert await _jobs_of_type(api_env, org_id, CANCEL_JOB_TYPE) == []

    allowed = await _job(api_env, org_id, device_id)
    assert (
        await api_env.client.post(f"{JOBS}/{allowed}/cancel", headers=headers)
    ).status_code == 200


def test_the_day_one_cancellable_set_is_the_specs() -> None:
    """§9.1's recommendation (b), as an assertion over the LIVE registry.

    The three named types are the ones whose agent capability already checks
    `ctx.Err()` in its own loop, so the flag claims something the code does.
    Anything else acquiring the flag -- `patch.apply`, `backup.restore`,
    `provision.enroll`, `inventory.collect` -- is a product decision about what
    a half-finished one leaves behind, and it breaks this test rather than
    shipping quietly.
    """
    from app.jobs import registry
    from app.modules.remote.jobtypes import register_job_type as register_remote
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote()
    cancellable = {
        name for name in registry.registered_names() if registry.get_job_type(name).cancellable
    }
    # Cycle B2 adds the two push types: a probe or an install in flight must be
    # interruptible (spec-push-install.md §4.3), and their agent capability
    # honours `ctx` -- it dials and runs under the job's context, so the flag
    # claims something the code does. `job.cancel` reaching `pushinstall` closes
    # the SSH session and the job ends.
    assert cancellable == {
        "script.run",
        "net.sweep",
        "backup.run",
        "agent.push_install",
        "agent.push_probe",
    }


# --------------------------------------------------------------------------- #
# §3.5 -- the target's OWN result, once a cancel has been requested.
# --------------------------------------------------------------------------- #


async def test_a_failed_result_on_a_cancelled_job_is_stored_cancelled_with_the_body(
    api_env: Any, tmp_path: Any
) -> None:
    """§7 test 4. The agent's body survives the reinterpretation intact."""
    org_id = await api_env.create_org("interrupt-failed")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, status=JobStatus.running, cancel_requested=True)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    body = {"error": "context canceled", "partial_output": "3 of 40 hosts"}
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.complete(session, job, state="failed", result=body)

    row = await _row(api_env, job_id)
    assert row.status is JobStatus.cancelled
    assert row.cancel_outcome == "interrupted"
    assert row.result == body, "the endpoint's own words are evidence, not ours to edit"
    assert (await _audit(api_env, org_id, job_id))["jobs.cancelled"].actor_type == "agent"


async def test_a_succeeded_result_on_a_cancelled_job_is_stored_succeeded(
    api_env: Any, tmp_path: Any
) -> None:
    """The case this design exists to tell the truth about.

    The cancel arrived too late and the job completed. Saying `cancelled` here
    would be a lie about a machine that did the work; the console reads
    `succeeded` plus `cancel_requested_at` and says exactly that.
    """
    org_id = await api_env.create_org("interrupt-too-late")
    device_id = await _device(api_env, org_id)
    # `hello` rather than a cancellable type: this test is about the ENGINE's
    # succeeded path, and a type whose `result_handler` rejects the body would
    # convert the outcome to `failed` for a reason that has nothing to do with
    # cancellation.
    job_id = await _job(
        api_env, org_id, device_id, type="hello",
        status=JobStatus.running, cancel_requested=True,
    )
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.complete(session, job, state="succeeded", result={"exit_code": 0})

    row = await _row(api_env, job_id)
    assert row.status is JobStatus.succeeded
    assert row.cancel_requested_at is not None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (f"{AGENT_CANCELLED_REFUSAL}: this job was cancelled before it started", "dropped"),
        (AGENT_CANCELLED_REFUSAL, "dropped"),
        ("queue_full: this capability already has queued jobs", "interrupted"),
    ],
)
async def test_a_refusal_distinguishes_never_started_from_stopped_mid_flight(
    api_env: Any, tmp_path: Any, reason: str, expected: str
) -> None:
    """§3.5's `dropped` vs `interrupted`, which the console renders differently.

    The token is matched against the HEAD of the reason and never as a
    substring, so a capability quoting the constant in its own refusal text
    cannot promote itself to `dropped`.
    """
    org_id = await api_env.create_org(f"interrupt-refused-{expected}-{len(reason)}")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, cancel_requested=True)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.refuse(session, job, reason=reason)

    row = await _row(api_env, job_id)
    assert row.status is JobStatus.cancelled
    assert row.cancel_outcome == expected
    assert row.result == {"refused": reason}


def test_the_cancellation_token_is_not_matched_as_a_substring() -> None:
    assert is_agent_cancellation(AGENT_CANCELLED_REFUSAL)
    assert is_agent_cancellation(f"{AGENT_CANCELLED_REFUSAL}: detail")
    assert not is_agent_cancellation(f"failed while {AGENT_CANCELLED_REFUSAL}")
    assert not is_agent_cancellation("unknown_type: no capability")


async def test_a_refusal_on_a_job_nobody_cancelled_is_still_a_refusal(
    api_env: Any, tmp_path: Any
) -> None:
    """The guard is `cancel_requested_at`, not the reason. Without this the
    reinterpretation could swallow ordinary refusals."""
    org_id = await api_env.create_org("interrupt-plain-refusal")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.refuse(session, job, reason="unknown_type: no capability")

    row = await _row(api_env, job_id)
    assert row.status is JobStatus.refused
    assert row.cancel_outcome is None


# --------------------------------------------------------------------------- #
# §3.5 -- the `job.cancel` result handler.
# --------------------------------------------------------------------------- #


async def _a_cancel_pair(
    api_env: Any, org_id: UUID, device_id: UUID, *, target_status: JobStatus
) -> tuple[UUID, UUID]:
    """A target with a cancel requested, and the `job.cancel` that asked."""
    target = await _job(
        api_env, org_id, device_id, status=target_status, cancel_requested=True,
        cancel_outcome="requested",
    )
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        cancel = Job(
            org_id=org_id,
            type=CANCEL_JOB_TYPE,
            target_type="device",
            target_id=device_id,
            params={"target_job_id": str(target)},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=JobStatus.running,
            signature="sig",
        )
        session.add(cancel)
        await session.commit()
        return target, cancel.id


@pytest.mark.parametrize(
    ("outcome", "closes", "expected_outcome"),
    [
        ("dropped_before_start", True, "dropped"),
        ("interrupted", False, "interrupted"),
        ("already_finished", False, "too_late"),
        ("unknown", False, "too_late"),
    ],
)
async def test_the_cancel_result_closes_the_target_only_when_nothing_else_will_speak(
    api_env: Any, outcome: str, closes: bool, expected_outcome: str
) -> None:
    """§3.5's table, in full.

    ONE RULE: close the target only when nothing else will ever speak for it. A
    job the endpoint says it never started has no result coming; every other
    outcome leaves the target to answer for itself.
    """
    org_id = await api_env.create_org(f"cancel-result-{outcome}")
    device_id = await _device(api_env, org_id)
    target, cancel_id = await _a_cancel_pair(
        api_env, org_id, device_id, target_status=JobStatus.running
    )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cancel = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_result(
            session, cancel, {"target_job_id": str(target), "outcome": outcome}
        )

    row = await _row(api_env, target)
    assert row.cancel_outcome == expected_outcome
    if closes:
        assert row.status is JobStatus.cancelled
        assert row.finished_at is not None
        assert (await _audit(api_env, org_id, target))["jobs.cancelled"].actor_type == "agent"
    else:
        assert row.status is JobStatus.running, "only the endpoint's own result closes this"
        assert "jobs.cancelled" not in await _audit(api_env, org_id, target)


async def test_a_late_cancel_result_does_not_downgrade_better_evidence(api_env: Any) -> None:
    """`requested` is the only value the handler overwrites.

    The ordering race is real: the target's own refusal can land first and
    write `dropped`, and the cancel job's `unknown` answer then arrives behind
    it. Downgrading to `too_late` would replace what the endpoint said with a
    weaker guess.
    """
    org_id = await api_env.create_org("cancel-result-late")
    device_id = await _device(api_env, org_id)
    target, cancel_id = await _a_cancel_pair(
        api_env, org_id, device_id, target_status=JobStatus.running
    )
    async with AsyncSession(api_env.admin_engine) as session:
        row = (await session.execute(select(Job).where(Job.id == target))).scalar_one()
        row.status = JobStatus.cancelled
        row.cancel_outcome = "dropped"
        await session.commit()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cancel = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_result(
            session, cancel, {"target_job_id": str(target), "outcome": "unknown"}
        )

    assert (await _row(api_env, target)).cancel_outcome == "dropped"


async def test_the_target_comes_from_the_signed_params_and_never_the_result(
    api_env: Any,
) -> None:
    """The falsifier for "cancel is unforgeable" (§7's table).

    A compromised endpoint answers about a DIFFERENT job. The answer is
    discarded and audited; neither job is written.
    """
    org_id = await api_env.create_org("cancel-forged")
    device_id = await _device(api_env, org_id)
    target, cancel_id = await _a_cancel_pair(
        api_env, org_id, device_id, target_status=JobStatus.running
    )
    victim = await _job(api_env, org_id, device_id, status=JobStatus.running)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cancel = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_result(
            session,
            cancel,
            {"target_job_id": str(victim), "outcome": "dropped_before_start"},
        )

    assert (await _row(api_env, victim)).status is JobStatus.running
    assert (await _row(api_env, victim)).cancel_outcome is None
    assert (await _row(api_env, target)).status is JobStatus.running
    assert (await _row(api_env, target)).cancel_outcome == "requested"
    assert "jobs.cancel_target_mismatch" in await _audit(api_env, org_id, cancel_id)


async def test_a_cancel_result_cannot_reach_another_device(api_env: Any) -> None:
    """§3.5's second predicate. A compromised device may not close a job
    belonging to a different device, even inside its own org."""
    org_id = await api_env.create_org("cancel-other-device")
    mine = await _device(api_env, org_id, "mine")
    theirs = await _device(api_env, org_id, "theirs")
    victim = await _job(api_env, org_id, theirs, status=JobStatus.running)

    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        cancel = Job(
            org_id=org_id,
            type=CANCEL_JOB_TYPE,
            target_type="device",
            target_id=mine,
            params={"target_job_id": str(victim)},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=JobStatus.running,
            signature="sig",
        )
        session.add(cancel)
        await session.commit()
        cancel_id = cancel.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_result(
            session, row, {"target_job_id": str(victim), "outcome": "dropped_before_start"}
        )

    assert (await _row(api_env, victim)).status is JobStatus.running
    assert "jobs.cancel_target_mismatch" in await _audit(api_env, org_id, cancel_id)


async def test_a_cancel_result_cannot_reach_another_org(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """Invariant #5's application half, on the engine that cannot hide a
    neighbour's row.

    Driven through the ADMIN (BYPASSRLS) engine on purpose: under RLS the
    neighbour's job is invisible whether or not the handler carries an `org_id`
    predicate, so a test that used the app engine would pass against the
    defect (2026-08-21-tenancy-test-efficacy.md §0, idiom 3). Deleting
    `Job.org_id == cancel_job.org_id` from `_target_of` turns this red.
    """
    victim_org = await api_env.create_org("cancel-victim-org")
    attacker_org = await api_env.create_org("cancel-attacker-org")
    victim_device = await _device(api_env, victim_org, "v-1")
    attacker_device = await _device(api_env, attacker_org, "a-1")
    victim = await _job(api_env, victim_org, victim_device, status=JobStatus.running)

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cancel = Job(
            org_id=attacker_org,
            type=CANCEL_JOB_TYPE,
            target_type="device",
            target_id=attacker_device,
            params={"target_job_id": str(victim)},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=JobStatus.running,
            signature="sig",
        )
        session.add(cancel)
        await session.commit()
        cancel_id = cancel.id

    async with AsyncSession(admin_engine) as session:
        row = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_result(
            session, row, {"target_job_id": str(victim), "outcome": "dropped_before_start"}
        )
        await session.commit()

    assert (await _row(api_env, victim)).status is JobStatus.running
    assert (await _row(api_env, victim)).cancel_outcome is None


# --------------------------------------------------------------------------- #
# §3.7 -- the old fleet, on the record.
# --------------------------------------------------------------------------- #


async def test_an_old_agent_refusing_unknown_type_marks_the_target_unsupported(
    api_env: Any, tmp_path: Any
) -> None:
    """§7 test 19's server half, and the single most important mixed-version case.

    An agent built before this cycle has no `job.cancel` capability. Its
    verifier refuses `unknown_type` BEFORE anything runs and sends an ordinary
    `job_result`, which is exactly why cancel is a job TYPE and not a new wire
    `kind` -- an unknown kind is dropped in silence, and the control plane would
    have to tell "old agent" from "already finished" by timeout.

    THE TARGET IS UNTOUCHED apart from the outcome word: it still runs or
    expires as it would have.
    """
    org_id = await api_env.create_org("cancel-old-agent")
    device_id = await _device(api_env, org_id)
    target, cancel_id = await _a_cancel_pair(
        api_env, org_id, device_id, target_status=JobStatus.running
    )
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cancel = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await engine.refuse(
            session, cancel, reason="unknown_type: no capability is registered for this job type"
        )

    assert (await _row(api_env, cancel_id)).status is JobStatus.refused
    row = await _row(api_env, target)
    assert row.status is JobStatus.running, "the target runs on, exactly as it would have"
    assert row.cancel_outcome == "unsupported"

    events = await _audit(api_env, org_id, target)
    assert "jobs.cancel_unsupported" in events
    assert events["jobs.cancel_unsupported"].actor_type == "agent"
    assert events["jobs.cancel_unsupported"].meta["cancel_job_id"] == str(cancel_id)

    async with AsyncSession(api_env.admin_engine) as session:
        lines = list(
            (await session.execute(select(JobLog).where(JobLog.job_id == target)))
            .scalars()
            .all()
        )
    assert len(lines) == 1
    assert "does not support being interrupted" in lines[0].message


async def test_any_other_refusal_leaves_the_request_standing(
    api_env: Any, tmp_path: Any
) -> None:
    """A `queue_full` or a replayed nonce says nothing about whether this
    endpoint understands cancellation. Writing `unsupported` for them would put
    a false sentence in front of the operator."""
    org_id = await api_env.create_org("cancel-other-refusal")
    device_id = await _device(api_env, org_id)
    target, cancel_id = await _a_cancel_pair(
        api_env, org_id, device_id, target_status=JobStatus.running
    )
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cancel = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await engine.refuse(session, cancel, reason="queue_full: too many queued jobs")

    assert (await _row(api_env, target)).cancel_outcome == "requested"
    assert "jobs.cancel_unsupported" not in await _audit(api_env, org_id, target)


async def test_handle_cancel_refusal_ignores_a_cancel_with_no_reachable_target(
    api_env: Any,
) -> None:
    """A `job.cancel` whose signed target has been purged: audited, not raised.

    The refusal path runs inside the gateway's tenant transaction; raising here
    would take down the handling of an ordinary agent message.
    """
    org_id = await api_env.create_org("cancel-no-target")
    device_id = await _device(api_env, org_id)
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        cancel = Job(
            org_id=org_id,
            type=CANCEL_JOB_TYPE,
            target_type="device",
            target_id=device_id,
            params={"target_job_id": str(uuid4())},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=JobStatus.running,
            signature="sig",
        )
        session.add(cancel)
        await session.commit()
        cancel_id = cancel.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = (await session.execute(select(Job).where(Job.id == cancel_id))).scalar_one()
        await handle_cancel_refusal(session, row, reason="unknown_type: nope")

    assert "jobs.cancel_target_mismatch" in await _audit(api_env, org_id, cancel_id)


# --------------------------------------------------------------------------- #
# Expiry, unchanged (§3.5's last line).
# --------------------------------------------------------------------------- #


async def test_a_cancelled_job_that_expires_in_silence_is_expired_not_cancelled(
    api_env: Any, tmp_path: Any
) -> None:
    """Nobody ever told us it stopped, so nothing may say it did."""
    org_id = await api_env.create_org("cancel-expiry")
    device_id = await _device(api_env, org_id)
    job_id = await _job(api_env, org_id, device_id, cancel_requested=True)
    async with AsyncSession(api_env.admin_engine) as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await engine.expire_due(session) == 1

    assert (await _row(api_env, job_id)).status is JobStatus.expired

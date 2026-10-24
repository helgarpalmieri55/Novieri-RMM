"""`POST /api/v1/jobs/{job_id}/retry` -- a NEW dispatch, never a replay.

Cycle 3 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. An
operator whose deployment failed overnight for a laptop that was asleep had no
way to run it again; the only thing in the platform called "retry" was the
stuck-dispatch sweep, which refuses any job that reached a terminal state --
i.e. exactly the case an operator has (finding B41's other half).

TWO PROPERTIES CARRY THE SECURITY WEIGHT HERE AND EACH HAS ITS OWN TEST.

1. **A retry is signed afresh.** New id, new nonce, a signature over different
   canonical bytes. If this route could ever re-send stored signed bytes it
   would be a replay primitive with a permission check in front of it.
2. **A retry is authorized as an original dispatch is**, by the TARGET job
   type's own dispatch permission -- so `jobs.dispatch` cannot re-run a
   `patch.apply` nobody holding it could have started.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
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


async def _device(api_env: Any, org_id: UUID, hostname: str = "rx-1") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id, hostname=hostname, os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.commit()
        return device.id


async def _grant(api_env: Any, org_id: UUID, *codes: str) -> tuple[UUID, str]:
    """A user holding EXACTLY `codes`.

    Never a seeded `org-admin`: they hold every code in the platform, so a
    check for the right permission and a check for any permission look
    identical to them and the escalation test below would pass against the
    defect.
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
    _, sub = await _grant(api_env, org_id, *codes)
    return await api_env.headers(org_id, sub)


#: Params that satisfy each type's own model. THE RETRY ROUTE RE-VALIDATES
#: rather than trusting the stored row (`JobEngine.create`), so a fixture
#: carrying `{}` would fail for a reason that has nothing to do with what these
#: tests assert -- and the case where stored params genuinely no longer
#: validate has its own test below.
_VALID_PARAMS: dict[str, dict[str, Any]] = {
    "app.deploy": {"action": "install", "installer_type": "deb"},
    "script.run": {
        "interpreter": "bash",
        "body": "echo hello",
        "script_id": "11111111-2222-4333-8444-555555555555",
        "script_version": 1,
    },
}


async def _job(
    api_env: Any,
    org_id: UUID,
    device_id: UUID,
    *,
    type: str = "inventory.collect",
    status: JobStatus = JobStatus.failed,
    result: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> UUID:
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type=type,
            target_type="device",
            target_id=device_id,
            params=params if params is not None else _VALID_PARAMS.get(type, {}),
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=status,
            signature="original-signature",
            attempts=1,
            finished_at=now,
            result=result if result is not None else {"error": "the disk was full"},
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


async def _audit(api_env: Any, org_id: UUID, target_id: UUID) -> dict[str, AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.target_id == str(target_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.action: row for row in rows}


# --------------------------------------------------------------------------- #
# §7 test 9 -- a new row, and the original untouched.
# --------------------------------------------------------------------------- #


async def test_retrying_a_failed_job_creates_a_new_signed_job(api_env: Any) -> None:
    """The whole of §7 test 9, and the replay property alongside it.

    NEW ID, NEW NONCE, DIFFERENT SIGNATURE. These are not cosmetic: they are
    what makes this a dispatch rather than a re-send of bytes somebody already
    authorized once. The agent's own nonce store would refuse the original
    envelope anyway; the point is that this route cannot produce one.
    """
    org_id = await api_env.create_org("retry-ok")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    listener = api_env.app.state.redis
    pubsub = listener.pubsub()
    await pubsub.subscribe(f"dispatch:{device_id}")
    assert await pubsub.get_message(timeout=2.0) is not None

    before = await _row(api_env, original_id)
    snapshot = (
        before.status,
        before.nonce,
        before.signature,
        before.result,
        before.attempts,
        before.issued_at,
        before.expires_at,
        before.finished_at,
    )

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != str(original_id)
    assert body["status"] == "queued"
    assert body["retry_of_job_id"] == str(original_id)
    assert body["signed"] is True

    retry = await _row(api_env, UUID(body["id"]))
    assert retry.retry_of_job_id == original_id
    assert retry.nonce != snapshot[1]
    assert retry.signature != snapshot[2]
    assert retry.type == before.type
    assert retry.target_id == device_id
    assert retry.attempts == 0
    assert retry.result is None

    # THE ORIGINAL IS BYTE-FOR-BYTE UNCHANGED. Its `result` is the failure body
    # the operator retried FROM, and its terminal status is what makes its
    # outcome final.
    after = await _row(api_env, original_id)
    assert (
        after.status,
        after.nonce,
        after.signature,
        after.result,
        after.attempts,
        after.issued_at,
        after.expires_at,
        after.finished_at,
    ) == snapshot
    assert after.retry_of_job_id is None

    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
    await pubsub.aclose()
    assert message is not None, "the retry was never published"
    envelope = json.loads(message["data"])
    assert envelope["job"]["id"] == str(retry.id)
    assert envelope["job"]["nonce"] == retry.nonce
    assert envelope["sig"] != snapshot[2]


async def test_the_retry_signature_verifies_over_the_new_bytes(
    api_env: Any,
) -> None:
    """`signature != original` would pass against a column holding junk.

    This re-derives the canonical bytes and checks them against the very key
    the API signed with, and then checks that the ORIGINAL's signature does not
    verify over them -- which is the assertion that a retry is not a replay.
    """
    from app.core.signing import LocalEd25519Signer, verify_signature
    from app.jobs.canonical import canonical_bytes

    org_id = await api_env.create_org("retry-sig")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    body = (
        await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    ).json()
    retry = await _row(api_env, UUID(body["id"]))
    envelope = await api_env.app.state.job_engine.signed_envelope(retry)

    signer = LocalEd25519Signer(api_env.settings.signing_key_path, allow_generate=False)
    public = await signer.public_key_pem()
    assert verify_signature(public, canonical_bytes(envelope["job"]), envelope["sig"])
    # THE FALSIFIER FOR "THIS IS NOT A REPLAY": the original's signature is not
    # a signature over these bytes.
    assert not verify_signature(
        public, canonical_bytes(envelope["job"]), "original-signature"
    )


async def test_both_acts_and_their_relationship_are_on_the_chain(api_env: Any) -> None:
    """`jobs.retry_requested` names the human; `jobs.dispatched` names the lineage.

    TWO EVENTS BECAUSE THEY ANSWER DIFFERENT QUESTIONS, and one does not
    replace the other. The design (§4.1) specifies `retry_of` in the dispatch
    event's metadata -- but that event is `actor_type="system"` and is written
    by the GATEWAY at delivery, which for a device that is off is hours away or
    never. Invariant #4 wants the privileged act itself, with its actor, at the
    moment it happened.
    """
    org_id = await api_env.create_org("retry-audit")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id)
    actor, sub = await _grant(api_env, org_id, "jobs.read", "jobs.dispatch")
    headers = await api_env.headers(org_id, sub)

    body = (
        await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    ).json()
    retry_id = UUID(body["id"])

    events = await _audit(api_env, org_id, retry_id)
    assert "jobs.retry_requested" in events
    requested = events["jobs.retry_requested"]
    assert requested.actor_type == "user"
    assert requested.actor_id == actor
    assert requested.meta["retry_of"] == str(original_id)
    assert requested.meta["retry_of_status"] == "failed"

    # And the dispatch event carries the lineage the design names, once the
    # gateway marks delivery.
    from app.jobs.engine import JobEngine

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == retry_id))).scalar_one()
        engine: JobEngine = api_env.app.state.job_engine
        await engine.mark_dispatched(session, job)

    dispatched = (await _audit(api_env, org_id, retry_id))["jobs.dispatched"]
    assert dispatched.meta["retry_of"] == str(original_id)
    # AND IT IS ABSENT FOR AN ORDINARY DISPATCH -- a key that is always there
    # and usually empty reads as a field somebody forgot to fill in.
    ordinary = await api_env.client.post(
        f"{JOBS}",
        headers=headers,
        json={"type": "inventory.collect", "device_id": str(device_id)},
    )
    assert ordinary.status_code == 201, ordinary.text
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = (
            await session.execute(select(Job).where(Job.id == UUID(ordinary.json()["id"])))
        ).scalar_one()
        await api_env.app.state.job_engine.mark_dispatched(session, job)
    plain = (await _audit(api_env, org_id, UUID(ordinary.json()["id"])))["jobs.dispatched"]
    assert "retry_of" not in plain.meta


# --------------------------------------------------------------------------- #
# §7 test 10 -- which states may be retried.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state",
    [JobStatus.failed, JobStatus.expired, JobStatus.refused, JobStatus.cancelled],
)
async def test_every_unhappy_terminal_state_can_be_retried(
    api_env: Any, state: JobStatus
) -> None:
    org_id = await api_env.create_org(f"retry-from-{state.value}")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, status=state)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 201, response.text
    assert (await _row(api_env, original_id)).status is state


async def test_a_succeeded_job_is_a_409_that_says_why_it_is_not_a_retry(
    api_env: Any,
) -> None:
    """Re-running work that WORKED is a new dispatch, not a retry.

    The refusal says so rather than reading "conflict": it belongs on the route
    that owns the record of what was asked for and the bounds on it, not on a
    verb whose name says "again".
    """
    org_id = await api_env.create_org("retry-succeeded")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, status=JobStatus.succeeded)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    assert "new dispatch rather than a retry" in response.json()["detail"]
    assert len(await _jobs_in(api_env, org_id)) == 1


@pytest.mark.parametrize(
    "state", [JobStatus.queued, JobStatus.dispatched, JobStatus.running]
)
async def test_an_unfinished_job_is_a_409_naming_the_sweep(
    api_env: Any, state: JobStatus
) -> None:
    org_id = await api_env.create_org(f"retry-unfinished-{state.value}")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, status=state)
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "has not finished" in detail
    assert "stuck-dispatch sweep" in detail
    assert len(await _jobs_in(api_env, org_id)) == 1


async def test_an_unknown_job_is_a_404(api_env: Any) -> None:
    org_id = await api_env.create_org("retry-404")
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")
    assert (
        await api_env.client.post(f"{JOBS}/{uuid4()}/retry", headers=headers)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# §7 tests 11-13 -- what may be retried here, and by whom.
# --------------------------------------------------------------------------- #


async def test_a_governed_type_with_no_relinker_is_a_403_naming_its_route(
    api_env: Any,
) -> None:
    """§7 test 11. The refusal is about a RECORD, not a permission.

    A retry is a new job id, and `patch`'s record of the run is keyed on the
    old one -- so a retry from here would apply patches on the endpoint and
    leave the patch run pointing at a job that will never speak again. The
    caller here holds `patch.apply`, so the 403 cannot be read as a missing
    grant.
    """
    org_id = await api_env.create_org("retry-governed")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, type="patch.apply")
    headers = await _holder(api_env, org_id, "jobs.read", "patch.apply")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert "cannot be re-run from the jobs route" in detail
    assert "POST /api/v1/patch/apply" in detail, "the message must name where it CAN be re-run"
    assert len(await _jobs_in(api_env, org_id)) == 1


async def test_a_governed_type_that_declares_a_relinker_is_retryable(
    api_env: Any,
) -> None:
    """§9.3(b): `app.deploy` and `script.run` ship relinkers in this cycle.

    NOT VACUOUS beside the test above: the same route, the same shape of job,
    and the only difference is that this type's module has said in code how to
    repoint its own record.
    """
    org_id = await api_env.create_org("retry-relinked")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, type="script.run")
    headers = await _holder(api_env, org_id, "jobs.read", "deploy.script.run")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["retry_of_job_id"] == str(original_id)


async def test_holding_jobs_dispatch_does_not_reach_another_types_permission(
    api_env: Any,
) -> None:
    """§7 test 12. The escalation §5 forecloses.

    A holder of `jobs.dispatch` could never have STARTED a `script.run` -- that
    route is gated on `deploy.script.run` -- so letting them re-run one would
    hand them arbitrary code execution as SYSTEM/root through a route nobody
    reviewed for it.
    """
    org_id = await api_env.create_org("retry-escalate")
    device_id = await _device(api_env, org_id)
    theirs = await _job(api_env, org_id, device_id, type="script.run")
    headers = await _holder(api_env, org_id, "jobs.read", "jobs.dispatch")

    response = await api_env.client.post(f"{JOBS}/{theirs}/retry", headers=headers)
    assert response.status_code == 403, response.text
    assert "deploy.script.run" in response.json()["detail"]

    # NOT VACUOUS: the same caller, the same route, a type they may dispatch.
    mine = await _job(api_env, org_id, device_id, type="inventory.collect")
    assert (
        await api_env.client.post(f"{JOBS}/{mine}/retry", headers=headers)
    ).status_code == 201


async def test_reading_a_job_does_not_authorize_re_running_it(api_env: Any) -> None:
    org_id = await api_env.create_org("retry-readonly")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id)
    headers = await _holder(api_env, org_id, "jobs.read")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 403, response.text
    assert "jobs.dispatch" in response.json()["detail"]
    assert len(await _jobs_in(api_env, org_id)) == 1


async def test_a_kernel_minted_type_is_unretryable_by_anyone(api_env: Any) -> None:
    """`NO_HUMAN_DISPATCH_PATH` means `None`, and `None` means refuse.

    The caller holds EVERY permission the platform defines, so the 403 cannot
    be read as a missing grant: there is no code that would satisfy this,
    because there is no route a human calls to start one of these.
    """
    org_id = await api_env.create_org("retry-kernel")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, type="agent.cert.renew")
    async with AsyncSession(api_env.admin_engine) as session:
        codes = list((await session.execute(select(Permission.code))).scalars().all())
    headers = await _holder(api_env, org_id, *codes)

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 403, response.text
    assert "no human dispatch route" in response.json()["detail"]


def test_the_retryable_set_is_a_partition_over_the_live_registry() -> None:
    """§7 test 13. DENY BY DEFAULT, asserted as a partition rather than a list.

    A membership-only test would let the next registered job type pick a side
    by accident, which is precisely how the first version of the direct-dispatch
    hole was born. Every registered type is either retryable through the kernel
    route -- because no module governs it, or because its module declared a
    relinker -- or it is not, and this names both sides explicitly so that
    adding a job type breaks a test instead of defaulting to either answer.
    """
    from app.jobs import registry
    from app.jobs.retry import retryable_job_types
    from app.modules.remote.jobtypes import register_job_type as register_remote
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote()
    live = set(registry.registered_names())
    retryable = retryable_job_types()

    assert retryable <= live, f"named here but not registered: {sorted(retryable - live)}"
    assert retryable == {
        # No module governs these, so the kernel route IS their owning route.
        "hello",
        "inventory.collect",
        "logs.ship",
        # Module-governed, and their module declared how to repoint its record
        # (design §9.3, the operator's option (b)).
        "app.deploy",
        "script.run",
    }
    # The other side of the partition, named so it cannot shrink into silence.
    assert live - retryable == {
        "agent.cert.install",
        "agent.cert.renew",
        # Cycle B2: pushes are NOT retried automatically (spec-push-install.md
        # §4.3, §5.3) -- the credential would have to be re-decrypted and
        # re-signed, and a failed auth repeated across a batch is how an AD
        # account gets locked out. Neither declares a `retry_relinker`.
        "agent.push_install",
        "agent.push_probe",
        # Reject's uninstall (install-link spec §1.5): not retried by the
        # kernel route. A lost one is covered by the 24-hour completion, which
        # revokes the certificate whether or not the agent confirmed.
        "agent.uninstall",
        # Self-update: retried only by its rollout's reconciler, which mints a
        # fresh presigned URL; no `retry_relinker` (spec-self-update.md §8.1).
        "agent.update",
        "backup.restore",
        "backup.run",
        "health.check.now",
        "health.policy.apply",
        "job.cancel",
        "net.sweep",
        "patch.apply",
        "patch.reboot",
        "patch.scan",
        "provision.enroll",
        "remote.session.open",
        # A retry would re-sign a relay token that died with connect_by; a
        # new session is a new start, with a new consent prompt.
        "remote.screen.open",
        "site.wake",
    }


# --------------------------------------------------------------------------- #
# §4.4's falsifier: a retried deploy whose record never leaves pending.
# --------------------------------------------------------------------------- #


async def test_a_retried_deploy_repoints_its_own_record_at_the_new_job(
    api_env: Any,
) -> None:
    """THE FALSIFIER FROM §7's TABLE, and the whole reason `retry_relinker` exists.

    Without the relinker `ingest_deploy_result` looks the row up by the OLD job
    id, finds nothing, and "is skipped gracefully" -- so the retry installs the
    package on the endpoint and the deployment shows pending forever, with
    nothing anywhere going red. Deleting `retry_relinker=relink_deployment`
    from `app/modules/deploy/handler.py` turns this red twice: the route 403s,
    and the record is never repointed.
    """
    from app.modules.deploy.models import Deployment, DeploymentResult, Package

    org_id = await api_env.create_org("retry-deploy-record")
    device_id = await _device(api_env, org_id)
    original_id = await _job(api_env, org_id, device_id, type="app.deploy")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        package = Package(
            org_id=org_id,
            name="thing",
            version="1.0",
            os_family="linux",
            installer_type="deb",
        )
        session.add(package)
        await session.flush()
        deployment = Deployment(
            org_id=org_id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device_id,
            state="failed",
        )
        session.add(deployment)
        await session.flush()
        row = DeploymentResult(
            org_id=org_id,
            deployment_id=deployment.id,
            device_id=device_id,
            job_id=original_id,
            state="failed",
            exit_code=1,
            log_ref="it went wrong",
            finished_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        result_id = row.id

    headers = await _holder(api_env, org_id, "jobs.read", "deploy.execute")
    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 201, response.text
    retry_id = UUID(response.json()["id"])

    async with AsyncSession(api_env.admin_engine) as session:
        after = (
            await session.execute(select(DeploymentResult).where(DeploymentResult.id == result_id))
        ).scalar_one()
    assert after.job_id == retry_id, "the module's record still points at the old job"
    # Back to `pending`, because the row has always meant "the latest attempt"
    # and a fresh one is now in flight. The first attempt's evidence lives on
    # the ORIGINAL job row, which a retry never touches.
    assert after.state == "pending"
    assert after.exit_code is None
    assert after.finished_at is None
    assert (await _row(api_env, original_id)).result == {"error": "the disk was full"}


# --------------------------------------------------------------------------- #
# Tenancy -- idiom 3 of 2026-08-21-tenancy-test-efficacy.md
# --------------------------------------------------------------------------- #


async def test_a_neighbours_job_cannot_be_retried_without_rls_to_mask_it(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """Drive the REAL route with `app.state.app_engine` repointed at the
    BYPASSRLS admin engine, so the service's own `Job.org_id == ctx.org_id` is
    the only thing between the caller and a neighbour's job.

    THIS IS THE ONLY SHAPE THAT CAN FAIL. Everything served through
    `org_session` runs as a NOBYPASSRLS role, so with RLS in place a test that
    asks for a neighbour's job gets a 404 whether or not the application
    carries a predicate. Removing `Job.org_id == org_id` from
    `app/jobs/retry.py::retry_job` makes this a 201 that re-runs the victim's
    job on the victim's device.
    """
    victim = await api_env.create_org("retry-victim")
    mine = await api_env.create_org("retry-mine")
    victim_job = await _job(api_env, victim, await _device(api_env, victim, "v-1"))
    headers = await _holder(api_env, mine, "jobs.read", "jobs.dispatch")

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(f"{JOBS}/{victim_job}/retry", headers=headers)
    finally:
        api_env.app.state.app_engine = original

    assert response.status_code == 404, response.text
    assert len(await _jobs_in(api_env, victim)) == 1
    assert len(await _jobs_in(api_env, mine)) == 0


async def test_stored_params_the_current_model_rejects_are_a_409_not_a_500(
    api_env: Any,
) -> None:
    """A job written before its type gained a required field.

    `JobEngine.create` re-validates rather than trusting the stored row, and
    that is right: signing params the current model would reject would put a
    job on an endpoint that the platform can no longer describe. What must not
    happen is a 500 -- it is a fact about this job, and the operator's next
    move (dispatch a fresh one through the module's route) is in the message.
    """
    org_id = await api_env.create_org("retry-stale-params")
    device_id = await _device(api_env, org_id)
    original_id = await _job(
        api_env, org_id, device_id, type="script.run", params={"interpreter": "bash"}
    )
    headers = await _holder(api_env, org_id, "jobs.read", "deploy.script.run")

    response = await api_env.client.post(f"{JOBS}/{original_id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    assert "no longer satisfy" in response.json()["detail"]
    assert len(await _jobs_in(api_env, org_id)) == 1


async def test_rls_hides_a_neighbours_job_from_an_unfiltered_select(
    api_env: Any, app_engine: AsyncEngine
) -> None:
    """The DATABASE half of invariant #5 for the column this cycle writes."""
    victim = await api_env.create_org("retry-rls-victim")
    mine = await api_env.create_org("retry-rls-mine")
    victim_job = await _job(api_env, victim, await _device(api_env, victim, "v-2"))

    async with org_session(mine, engine=app_engine, clients=ClientScope.all()) as session:
        visible = {row.id for row in (await session.execute(select(Job))).scalars()}
    assert victim_job not in visible

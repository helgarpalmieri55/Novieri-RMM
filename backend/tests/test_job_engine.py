import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer, verify_signature
from app.core.tenancy import org_session
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import InvalidTransition, JobEngine, UnknownJobType
from app.jobs.registry import get_job_type
from app.models import AuditEvent, Device, JobStatus, Organization


@pytest.fixture()
def engine_factory(tmp_path: Path):
    def make() -> JobEngine:
        return JobEngine(signer=LocalEd25519Signer(tmp_path / "sign.key"))

    return make


async def _org_and_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"jobs-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="h1", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def _actor_in(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    """A real `users` row -- `jobs.cancel_requested_by` is a foreign key, and a
    made-up uuid would let a test pass against a schema that had lost it."""
    from app.models import User

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        user = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"{uuid4().hex[:8]}@test.local",
        )
        session.add(user)
        await session.commit()
        return user.id


def test_registry_has_phase0_job_types() -> None:
    assert get_job_type("hello").name == "hello"
    assert get_job_type("inventory.collect").name == "inventory.collect"
    with pytest.raises(UnknownJobType):
        get_job_type("nope.nothing")


async def test_create_validates_type_and_params(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    with pytest.raises(UnknownJobType):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await engine.create(
                session, org_id=org_id, type="bogus.type", target_device_id=device_id, params={}
            )
    with pytest.raises(ValueError):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await engine.create(
                session,
                org_id=org_id,
                type="hello",
                target_device_id=device_id,
                params={"message": 123 ** 300},  # not a string-coercible sane param
            )


async def test_envelope_signature_verifies_and_tamper_fails(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session,
            org_id=org_id,
            type="hello",
            target_device_id=device_id,
            params={"message": "hi"},
        )
        envelope = await engine.signed_envelope(job)

    assert envelope["v"] == 1 and envelope["kind"] == "job"
    pub = await engine.signer.public_key_pem()
    assert verify_signature(pub, canonical_bytes(envelope["job"]), envelope["sig"])

    for field, value in [
        ("device", str(uuid4())),
        ("type", "patch.apply"),
        ("params", {"message": "evil"}),
        ("expires_at", "2999-01-01T00:00:00Z"),
        ("nonce", "replayed"),
    ]:
        tampered = json.loads(json.dumps(envelope["job"]))
        tampered[field] = value
        assert not verify_signature(pub, canonical_bytes(tampered), envelope["sig"]), field


async def test_lifecycle_transitions_enforced(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.mark_running(session, job)
        await engine.complete(session, job, state="succeeded", result={"echo": "hi"})
        assert job.status is JobStatus.succeeded
        assert job.finished_at is not None
        # terminal states never transition
        with pytest.raises(InvalidTransition):
            await engine.mark_dispatched(session, job)
        with pytest.raises(InvalidTransition):
            await engine.mark_running(session, job)

        # an agent's report is authoritative even before delivery bookkeeping:
        # queued -> succeeded is legal (result raced ahead of mark_dispatched)
        early = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.complete(session, early, state="succeeded", result={})
        assert early.status is JobStatus.succeeded


async def test_refusal_and_audit_trail(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.refuse(session, job, reason="signature invalid on agent")
        assert job.status is JobStatus.refused
        actions = [
            r.action
            for r in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            ).scalars()
        ]
    assert "jobs.dispatched" in actions
    assert "jobs.refused" in actions


async def test_expire_due_and_resign_for_retry(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        stale = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}, ttl_s=1
        )
        fresh = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        stale.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        session.add(stale)
        await session.flush()

        expired_count = await engine.expire_due(session)
        assert expired_count == 1
        assert stale.status is JobStatus.expired
        assert fresh.status is JobStatus.queued

        old_nonce, old_sig_envelope = fresh.nonce, await engine.signed_envelope(fresh)
        await engine.resign_for_retry(session, fresh)
        assert fresh.nonce != old_nonce
        assert fresh.attempts == 1
        assert fresh.expires_at > datetime.now(UTC)
        new_envelope = await engine.signed_envelope(fresh)
        assert new_envelope["job"]["nonce"] != old_sig_envelope["job"]["nonce"]
        pub = await engine.signer.public_key_pem()
        assert verify_signature(pub, canonical_bytes(new_envelope["job"]), new_envelope["sig"])


# ---------------------------------------------------------------------------
# Withdrawal -- cycle 1 of
# docs/superpowers/specs/2026-08-22-job-lifecycle-design.md.
# ---------------------------------------------------------------------------


async def test_withdraw_takes_a_queued_job_to_a_terminal_cancelled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    """`queued -> cancelled`, and nothing transitions out of it again.

    `withdrawn` is the ONE outcome this platform can assert on its own
    authority: `queued` means the envelope was never written to a socket
    (`app/gateway/dispatch.py` §3.3), so no endpoint has to be consulted and
    no endpoint can contradict it.
    """
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    actor = await _actor_in(admin_engine, org_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.withdraw(session, job, requested_by=actor)
        assert job.status is JobStatus.cancelled
        assert job.cancel_outcome == "withdrawn"
        assert job.cancel_requested_at is not None
        assert job.cancel_requested_by == actor
        assert job.finished_at is not None

        for forbidden in (engine.mark_dispatched, engine.mark_running):
            with pytest.raises(InvalidTransition):
                await forbidden(session, job)
        with pytest.raises(InvalidTransition):
            await engine.complete(session, job, state="succeeded", result={})
        with pytest.raises(InvalidTransition):
            await engine.refuse(session, job, reason="too late")
        with pytest.raises(InvalidTransition):
            await engine.withdraw(session, job, requested_by=actor)


async def test_withdraw_refuses_a_job_that_already_left_the_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    """A `dispatched` job is beyond withdrawal, and the engine says so.

    Interrupting a job already on an endpoint is cycle 2 and needs the
    endpoint's own answer. Until then the engine refuses rather than writing a
    `cancelled` the platform cannot back up.
    """
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        with pytest.raises(InvalidTransition):
            await engine.withdraw(session, job, requested_by=uuid4())
        assert job.status is JobStatus.dispatched
        assert job.cancel_requested_at is None


async def test_withdraw_writes_both_halves_of_the_record(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    """Two audit events, and they answer different questions.

    `jobs.cancel_requested` says a NAMED HUMAN asked, and carries `mechanism`
    -- the record of whether the platform stopped this itself or merely passed
    the request on. `jobs.cancelled` says the platform actually stopped it, and
    its actor is `system` because for a withdrawal the server is the thing that
    acted. Cycle 2's endpoint confirmations will write the same event with
    `actor_type="agent"`, which is why the two are separate rather than one.
    """
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    actor = await _actor_in(admin_engine, org_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        job_id = job.id
        await engine.withdraw(session, job, requested_by=actor)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        events = {
            r.action: r
            for r in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.target_id == str(job_id))
                )
            ).scalars()
        }
    assert set(events) == {"jobs.cancel_requested", "jobs.cancelled"}

    requested = events["jobs.cancel_requested"]
    assert requested.actor_type == "user"
    assert requested.actor_id == actor
    assert requested.meta["mechanism"] == "withdrawn"
    assert requested.meta["from_status"] == "queued"
    assert requested.meta["type"] == "hello"
    assert requested.meta["device"] == str(device_id)

    cancelled = events["jobs.cancelled"]
    assert cancelled.actor_type == "system"
    assert cancelled.actor_id is None
    assert cancelled.meta["cancel_outcome"] == "withdrawn"


async def test_a_withdrawn_job_is_never_drained_to_a_reconnecting_agent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    """§7 test 1. This is the property the whole cycle rests on: an agent that
    comes back online must not be handed a job the console says was withdrawn.
    """
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    actor = await _actor_in(admin_engine, org_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        withdrawn = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        kept = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.withdraw(session, withdrawn, requested_by=actor)
        queued = await engine.queued_for_device(session, device_id)
    assert [job.id for job in queued] == [kept.id]


async def test_the_expiry_sweep_leaves_a_cancelled_job_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine_factory
) -> None:
    """A terminal state does not expire. `expire_due` selects `queued` and
    `dispatched` only, so this asserts the consequence rather than the query --
    an overdue cancelled job keeps saying it was cancelled.
    """
    org_id, device_id = await _org_and_device(admin_engine)
    engine = engine_factory()
    actor = await _actor_in(admin_engine, org_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.withdraw(session, job, requested_by=actor)
        job.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        session.add(job)
        await session.flush()
        assert await engine.expire_due(session) == 0
        assert job.status is JobStatus.cancelled

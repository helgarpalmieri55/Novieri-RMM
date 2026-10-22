"""Tests for the remote-session lifecycle service (docs/modules/remote-control/spec.md).

The service is the security-central integration point of the module: it resolves
policy, downgrades unattended to attended when policy forbids it, checks out the
remote-control credential (whose secret must NEVER touch the persisted row, a
log, or any return value), mints the relay token, and audits every privileged
transition. Every terminal path must unregister the relay session so a
registered session never leaks (the relay has no self-eviction).

The external side effects (relay registration, in-memory params, signed-job
publish) are DEFERRED: ``start_session`` only queues them on
``session.info["remote_pending"]``; the caller drains them via
``dispatch_pending`` ONLY AFTER its transaction commits (mirroring the
``queue_ui_event`` post-commit pattern). A rolled-back start therefore leaves no
relay registration and dispatches no job.
"""

import datetime as _dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Credential,
    Device,
    Organization,
    RemotePolicy,
    RemoteSession,
    User,
)
from app.modules.remote.relay import SessionRelay
from app.modules.remote.service import InvalidSessionState, RemoteSessionService


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        from app.models import Job

        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
        )
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": "x"}


class _FakeVault:
    def __init__(self) -> None:
        self.checkouts: list[UUID] = []

    async def checkout(self, session, *, org_id, credential_id) -> str:
        self.checkouts.append(credential_id)
        return "SUPER-SECRET"


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple] = []

    async def publish(self, ch, data) -> None:
        self.published.append((ch, data))


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rc", slug=f"rc-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux")
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_unattended_requires_policy_and_never_stores_secret(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=True))
        cred = Credential(org_id=org_id, name="win", type="windows_admin", vault_ref="v")
        s.add(cred)
        await s.flush()
        cred_id = cred.id
    engine, vault, redis = _FakeJobEngine(), _FakeVault(), _FakeRedis()
    svc = RemoteSessionService(engine, SessionRelay(), vault)
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="unattended",
            credential_id=cred_id,
            reason="fix",
        )
        assert row.consent_mode == "unattended" and row.state == "awaiting_consent"
        assert row.relay_token_hash and "SUPER-SECRET" not in str(row.__dict__)
        # the secret was checked out and stashed in-memory, never persisted
        assert vault.checkouts == [cred_id]
        sid = row.id
        pending = list(s.info.get("remote_pending", []))
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "remote.session.requested" in actions
    # BEFORE the post-commit drain: no in-memory params and nothing published
    assert svc.connection_params(sid) is None
    assert redis.published == []
    # draining post-commit installs the params and dispatches the signed job
    await svc.dispatch_pending(pending, redis)
    assert svc.connection_params(sid)["secret"] == "SUPER-SECRET"
    assert len(redis.published) == 1


async def test_unattended_start_audits_credential_used(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant #4 / spec §7,§12: an UNATTENDED start checks out the vault
    credential, and that credential ACCESS must itself be audited as
    ``remote.session.credential_used`` referencing the credential — and the
    plaintext secret must NEVER appear in the audit metadata."""
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=True))
        cred = Credential(org_id=org_id, name="win", type="windows_admin", vault_ref="v")
        s.add(cred)
        await s.flush()
        cred_id = cred.id
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="unattended",
            credential_id=cred_id,
            reason="fix",
        )
        sid = row.id
        events = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id)
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
    cred_used = [e for e in events if e.action == "remote.session.credential_used"]
    assert len(cred_used) == 1
    ev = cred_used[0]
    assert ev.target_type == "credential"
    assert ev.target_id == str(cred_id)
    assert ev.meta.get("session_id") == str(sid)
    # the secret string must never leak into the audit metadata
    assert "SUPER-SECRET" not in str(ev.meta)


async def test_attended_start_does_not_audit_credential_used(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The ATTENDED path checks out no credential, so it must NOT emit
    ``remote.session.credential_used`` (only ``remote.session.requested``)."""
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="attended",
            credential_id=None,
            reason="help",
        )
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "remote.session.requested" in actions
    assert "remote.session.credential_used" not in actions


async def test_unattended_downgraded_when_policy_forbids(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    vault = _FakeVault()
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), vault)
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="unattended",
            credential_id=uuid4(),
            reason="x",
        )
        assert row.consent_mode == "attended"  # policy default forbids unattended
    # downgraded to attended => no credential checkout happened
    assert vault.checkouts == []


async def test_attended_dispatches_signed_job_and_registers_relay(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    engine, relay, redis = _FakeJobEngine(), SessionRelay(), _FakeRedis()
    svc = RemoteSessionService(engine, relay, _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="ssh",
            requested_mode="attended",
            credential_id=None,
            reason="help",
        )
        sid = row.id
        pending = list(s.info.get("remote_pending", []))
    # nothing dispatched until the post-commit drain
    assert redis.published == []
    await svc.dispatch_pending(pending, redis)
    # a signed envelope was published to the device's dispatch channel
    assert len(redis.published) == 1
    assert str(device_id) in redis.published[0][0]
    assert engine.created[0]["type"] == "remote.session.open"
    # relay knows about the session: a wrong-token attach is rejected, not unknown
    ok = await relay.attach_agent(sid, device_id, "wrong-token", object(), object())
    assert ok is False  # False because token mismatch, but the session IS registered


async def test_rollback_leaves_no_relay_registration_or_publish(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Regression for the Task 8 review finding: if the caller's transaction
    rolls back and dispatch_pending is never called, NO signed job may have been
    published and the relay must hold NO registration for the phantom session."""
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    relay, redis = SessionRelay(), _FakeRedis()
    svc = RemoteSessionService(_FakeJobEngine(), relay, _FakeVault())

    class _Boom(Exception):
        pass

    sid: UUID | None = None
    with pytest.raises(_Boom):
        async with org_session(org_id, engine=app_engine) as s:
            row = await svc.start_session(
                s,
                org_id=org_id,
                technician_id=user_id,
                device_id=device_id,
                protocol="rdp",
                requested_mode="attended",
                credential_id=None,
                reason="r",
            )
            sid = row.id
            raise _Boom()  # roll back WITHOUT draining remote_pending

    assert sid is not None
    # no job was dispatched to the agent
    assert redis.published == []
    # the relay holds no registration for the session that never committed
    assert await relay.attach_agent(sid, device_id, "t", object(), object()) is False
    # no in-memory params leaked
    assert svc.connection_params(sid) is None
    # and the row itself was rolled back — it never persisted
    async with org_session(org_id, engine=app_engine) as s:
        gone = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == sid))
        ).scalar_one_or_none()
    assert gone is None


async def test_consent_reject_ends_and_unregisters_relay(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    relay, redis = SessionRelay(), _FakeRedis()
    svc = RemoteSessionService(_FakeJobEngine(), relay, _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="attended",
            credential_id=None,
            reason="r",
        )
        sid = row.id
        pending = list(s.info.get("remote_pending", []))
    # register the relay post-commit, then reject consent and prove cleanup
    await svc.dispatch_pending(pending, redis)
    async with org_session(org_id, engine=app_engine) as s:
        await svc.on_consent(s, session_id=sid, accepted=False)
        got = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == sid))
        ).scalar_one()
        assert got.state == "ended" and got.end_reason == "denied_consent"
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
        assert "remote.session.denied" in actions
    # relay session was cleaned up: attach now returns False due to unknown session
    assert await relay.attach_agent(sid, device_id, "t", object(), object()) is False
    # and the in-memory params entry is gone
    assert svc.connection_params(sid) is None


async def test_full_lifecycle_and_end_unregisters_relay(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    relay, redis = SessionRelay(), _FakeRedis()
    svc = RemoteSessionService(_FakeJobEngine(), relay, _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        row = await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="attended",
            credential_id=None,
            reason="r",
        )
        sid = row.id
        pending = list(s.info.get("remote_pending", []))
    await svc.dispatch_pending(pending, redis)
    # the session is registered and its in-memory params installed
    assert svc.connection_params(sid) is not None

    async with org_session(org_id, engine=app_engine) as s:
        await svc.on_consent(s, session_id=sid, accepted=True)
        assert (
            await s.execute(select(RemoteSession.state).where(RemoteSession.id == sid))
        ).scalar_one() == "connecting"

        await svc.mark_active(s, session_id=sid, audit_ref="ref-123")
        got = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == sid))
        ).scalar_one()
        assert got.state == "active" and got.started_at is not None
        assert got.audit_event_id is not None

        await svc.end_session(s, session_id=sid, reason="completed")
        got = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == sid))
        ).scalar_one()
        assert got.state == "ended" and got.ended_at is not None
        assert got.end_reason == "completed"

        actions = [
            a.action
            for a in (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        ]
    assert actions == [
        "remote.session.requested",
        "remote.session.started",
        "remote.session.ended",
    ]
    # terminal end unregistered the relay and dropped params
    assert await relay.attach_agent(sid, device_id, "t", object(), object()) is False
    assert svc.connection_params(sid) is None


async def test_illegal_transition_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    async with org_session(org_id, engine=app_engine) as s:
        await svc.start_session(
            s,
            org_id=org_id,
            technician_id=user_id,
            device_id=device_id,
            protocol="rdp",
            requested_mode="attended",
            credential_id=None,
            reason="r",
        )
        sid = (
            await s.execute(select(RemoteSession.id).where(RemoteSession.org_id == org_id))
        ).scalar_one()
        # awaiting_consent -> active is not a legal transition (must go via connecting)
        with pytest.raises(InvalidSessionState):
            await svc.mark_active(s, session_id=sid, audit_ref="x")


async def test_unattended_requires_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=True))
        await s.flush()
    svc = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(ValueError):
            await svc.start_session(
                s,
                org_id=org_id,
                technician_id=user_id,
                device_id=device_id,
                protocol="rdp",
                requested_mode="unattended",
                credential_id=None,  # missing credential for unattended
                reason="r",
            )

import asyncio
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import (
    GENESIS_HASH,
    AuditService,
    MissingAuditKeyError,
    load_audit_hmac_key,
)
from app.core.config import Settings
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization
from tests.conftest import PgCluster

_KEY_A = b"audit-hmac-key-a-0123456789abcdef"
_KEY_B = b"audit-hmac-key-b-fedcba9876543210"


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def test_appends_chain_and_verify_ok(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _make_org(admin_engine, "audit-a")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine) as session:
        e1 = await audit.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=None,
            action="test.first",
            target_type="thing",
            target_id="t1",
            metadata={"n": 1},
        )
        e2 = await audit.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="test.second",
            target_type="thing",
            target_id="t2",
            metadata={"n": 2},
        )
    assert e1.prev_hash == GENESIS_HASH
    assert e2.prev_hash == e1.hash
    async with org_session(org_id, engine=app_engine) as session:
        result = await audit.verify(session, org_id)
    assert result.ok and result.count == 2 and result.broken_at is None


async def test_tamper_breaks_verification(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: PgCluster
) -> None:
    org_id = await _make_org(admin_engine, "audit-b")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine) as session:
        e1 = await audit.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=None,
            action="test.a",
            target_type="",
            target_id="",
            metadata={"amount": 10},
        )
        await audit.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=None,
            action="test.b",
            target_type="",
            target_id="",
            metadata={},
        )
    # tamper as superuser with the immutability trigger disabled — simulating
    # an attacker with database write access trying to rewrite history
    subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-d", pg_cluster.dbname, "-c",
            "ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable;"
            f"UPDATE audit_events SET metadata = '{{\"amount\": 99999}}' WHERE id = '{e1.id}';"
            "ALTER TABLE audit_events ENABLE TRIGGER audit_events_immutable;",
        ],
        check=True,
        capture_output=True,
    )
    async with org_session(org_id, engine=app_engine) as session:
        result = await audit.verify(session, org_id)
    assert not result.ok
    assert result.broken_at == e1.id


async def test_update_and_delete_blocked(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _make_org(admin_engine, "audit-c")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine) as session:
        await audit.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=None,
            action="test.x",
            target_type="",
            target_id="",
            metadata={},
        )
    with pytest.raises(DBAPIError):
        async with org_session(org_id, engine=app_engine) as session:
            await session.execute(text("UPDATE audit_events SET action = 'rewritten'"))
    with pytest.raises(DBAPIError):
        async with org_session(org_id, engine=app_engine) as session:
            await session.execute(text("DELETE FROM audit_events"))
    # the admin (BYPASSRLS) role hits the trigger as well
    with pytest.raises(DBAPIError):
        async with AsyncSession(admin_engine) as session:
            await session.execute(text("DELETE FROM audit_events"))
            await session.commit()


async def test_concurrent_appends_stay_linear(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _make_org(admin_engine, "audit-d")
    audit = AuditService()

    async def append(n: int) -> None:
        async with org_session(org_id, engine=app_engine) as session:
            await audit.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action=f"test.concurrent.{n}",
                target_type="",
                target_id="",
                metadata={"n": n},
            )

    await asyncio.gather(*(append(n) for n in range(10)))

    async with org_session(org_id, engine=app_engine) as session:
        events = (
            (await session.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
        )
        result = await audit.verify(session, org_id)
    assert len(events) == 10
    assert result.ok
    for prev, curr in zip(events, events[1:], strict=False):
        assert curr.prev_hash == prev.hash


async def _record_two(audit: AuditService, org_id: UUID, app_engine: AsyncEngine) -> None:
    async with org_session(org_id, engine=app_engine) as session:
        await audit.record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="test.a", target_type="", target_id="", metadata={"n": 1},
        )
        await audit.record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="test.b", target_type="", target_id="", metadata={"n": 2},
        )


async def test_hmac_authenticated_chain_verifies(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # Deferred finding A: with a key configured, the chain is authenticated and
    # still verifies for the party holding the key.
    org_id = await _make_org(admin_engine, "audit-hmac")
    audit = AuditService(hmac_key=_KEY_A)
    await _record_two(audit, org_id, app_engine)
    async with org_session(org_id, engine=app_engine) as session:
        result = await audit.verify(session, org_id)
    assert result.ok and result.count == 2


async def test_wrong_key_fails_verification(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # An attacker without the HMAC key cannot produce a chain that verifies:
    # the same events under a different key fail.
    org_id = await _make_org(admin_engine, "audit-hmac-wrong")
    await _record_two(AuditService(hmac_key=_KEY_A), org_id, app_engine)
    async with org_session(org_id, engine=app_engine) as session:
        result = await AuditService(hmac_key=_KEY_B).verify(session, org_id)
    assert not result.ok


async def test_forged_unkeyed_event_fails_hmac_verify(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # The core of finding A: an attacker knows the algorithm and the readable
    # head hash, and appends a correctly-chained event — but computes it with
    # plain SHA-256 because they lack the HMAC key. Keyed verify catches it.
    org_id = await _make_org(admin_engine, "audit-hmac-forge")
    keyed = AuditService(hmac_key=_KEY_A)
    async with org_session(org_id, engine=app_engine) as session:
        await keyed.record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="legit", target_type="", target_id="", metadata={},
        )
    # attacker: no key, chains the forged event off the real head hash
    async with org_session(org_id, engine=app_engine) as session:
        forged = await AuditService().record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="credential.checked_out", target_type="user", target_id="victim",
            metadata={"forged": True},
        )
    async with org_session(org_id, engine=app_engine) as session:
        result = await keyed.verify(session, org_id)
    assert not result.ok and result.broken_at == forged.id


def test_load_audit_hmac_key_prod_requires_key(tmp_path: Path) -> None:
    base = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=str(tmp_path),
    )
    with pytest.raises(MissingAuditKeyError):
        load_audit_hmac_key(Settings(**base, environment="production"))
    # dev without a key path runs unkeyed
    assert load_audit_hmac_key(Settings(**base, environment="dev")) is None
    # a configured key file is loaded (dev or prod)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(_KEY_A)
    loaded = load_audit_hmac_key(
        Settings(**base, environment="production", audit_hmac_key_path=key_path)
    )
    assert loaded == _KEY_A

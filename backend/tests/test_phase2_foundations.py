"""Phase-2 foundations (plan 2026-10-26, Task 1): the internal client is born
with the three optional modules (C2), an audit event can name its subject's
client without breaking the chain of the events that do not (C6), and a
domain event carries its subject's client."""

import hashlib
import subprocess
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import AuditEntry, AuditService
from app.core.client_scope import ClientScope
from app.core.events import publish
from app.core.tenancy import org_session
from app.jobs.canonical import canonical_bytes, rfc3339
from app.models import AuditEvent, Client, DomainEvent, Organization
from tests.conftest import PgCluster


async def _org(admin_engine: AsyncEngine) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="F2", slug=f"f2-{uuid4().hex[:8]}")
        s.add(org)
        await s.commit()
        return org.id


async def _customer(admin_engine: AsyncEngine, org: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org, name=f"C {uuid4().hex[:6]}", kind="customer")
        s.add(client)
        await s.commit()
        return client.id


async def test_a_new_org_s_internal_client_has_the_three_optional_modules(
    admin_engine: AsyncEngine,
) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        rows = dict(
            (
                await s.execute(
                    select(Client.kind, Client.enabled_modules).where(Client.org_id == org)
                )
            ).all()
        )
    assert sorted(rows["internal"]) == ["bcp", "budget", "change"]
    assert rows["unassigned"] == []


async def test_a_customer_client_starts_with_no_optional_module(
    admin_engine: AsyncEngine,
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    async with AsyncSession(admin_engine) as s:
        modules = (
            await s.execute(select(Client.enabled_modules).where(Client.id == customer))
        ).scalar_one()
    assert modules == []


async def test_events_with_and_without_a_client_verify_in_one_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    audit = AuditService()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        plain = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.plain"
        )
        named = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.named",
            client_id=customer,
        )
        batch = await audit.record_many(
            s, org_id=org,
            entries=[AuditEntry(actor_type="system", actor_id=None, action="test.batch",
                                client_id=customer)],
        )
    assert plain.client_id is None and named.client_id == customer
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        result = await audit.verify(s, org)
        # Read back, not the returned object: a batch that hashed the client
        # but did not STORE it would still verify -- the row is what the log
        # filters on (review of Task 1).
        stored = (
            await s.execute(select(AuditEvent.client_id).where(AuditEvent.id == batch[0].id))
        ).scalar_one()
    assert result.ok and result.count == 3
    assert stored == customer


async def test_an_event_without_a_client_hashes_exactly_as_it_did_before_the_column(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """C6, the trap the plan names: every event stored before 0233 has a NULL
    client and was linked over EIGHT fields. A `"client_id": None` key added
    unconditionally would change every stored link, and `verify` would fail
    from the first event. The link is recomputed here from the legacy layout,
    independently of `_canonical_fields`, so both writers and `verify` moving
    together cannot hide it."""
    org = await _org(admin_engine)
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        event = await AuditService().record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.legacy",
            target_type="thing", target_id="1", metadata={"k": "v"},
        )
    legacy = {
        "org_id": str(org),
        "ts": rfc3339(event.ts),
        "actor_type": "system",
        "actor_id": None,
        "action": "test.legacy",
        "target_type": "thing",
        "target_id": "1",
        "metadata": {"k": "v"},
    }
    material = event.prev_hash.encode("ascii") + canonical_bytes(legacy)
    assert event.client_id is None
    assert event.hash == hashlib.sha256(material).hexdigest()


async def test_rewriting_an_event_s_client_breaks_the_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: PgCluster
) -> None:
    """C6: the client is HASHED when it is set, so moving an event to another
    client is tampering, exactly as rewriting its metadata is
    (`tests/test_audit.py::test_tamper_breaks_verification`)."""
    org = await _org(admin_engine)
    x, y = await _customer(admin_engine, org), await _customer(admin_engine, org)
    audit = AuditService()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        event = await audit.record(
            s, org_id=org, actor_type="system", actor_id=None, action="test.x", client_id=x
        )
    subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-d", pg_cluster.dbname, "-c",
            "ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable;"
            f"UPDATE audit_events SET client_id = '{y}' WHERE id = '{event.id}';"
            "ALTER TABLE audit_events ENABLE TRIGGER audit_events_immutable;",
        ],
        check=True,
        capture_output=True,
    )
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        result = await audit.verify(s, org)
    assert not result.ok and result.broken_at == event.id


async def test_an_audit_client_of_another_organisation_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, other = await _org(admin_engine), await _org(admin_engine)
    foreign = await _customer(admin_engine, other)
    audit = AuditService()
    try:
        async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
            await audit.record(
                s, org_id=org, actor_type="system", actor_id=None, action="t",
                client_id=foreign,
            )
    except Exception as exc:  # noqa: BLE001 -- the FK violation, whatever wraps it
        assert "fk_audit_events_client_org" in str(exc)
    else:
        raise AssertionError("an event named another organisation's client")


async def test_publish_stores_the_subject_s_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    customer = await _customer(admin_engine, org)
    subject = uuid4()
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        await publish(
            s, org_id=org, event_type="device.joined_via_link", subject_type="device",
            subject_id=subject, payload={}, actor_user_id=None, client_id=customer,
        )
    async with AsyncSession(admin_engine) as s:
        stored = (
            await s.execute(
                select(DomainEvent.client_id).where(DomainEvent.subject_id == subject)
            )
        ).scalar_one()
    assert stored == customer


async def test_the_audit_column_is_nullable_and_unindexed_by_policy(
    admin_engine: AsyncEngine,
) -> None:
    """C6: informative only -- no client RLS on `audit_events` (spec §2.5)."""
    async with admin_engine.connect() as c:
        policies = set(
            (
                await c.execute(
                    text(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = 'audit_events' AND policyname LIKE 'client%'"
                    )
                )
            ).scalars()
        )
        nullable = (
            await c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'audit_events' AND column_name = 'client_id'"
                )
            )
        ).scalar_one()
    assert policies == set() and nullable == "YES"

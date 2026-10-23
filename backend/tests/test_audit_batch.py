"""`AuditService.record_many`: N audited writes, ONE advisory lock, one chain.

**WHY A BATCH APPEND EXISTS AT ALL.** `record` takes a per-org advisory
transaction lock, reads the org's head hash and flushes -- three round trips per
event. That is the right shape for the privileged actions the audit log was
built for, which arrive one at a time. It is the wrong shape for the one caller
that does not: since the operator's ruling of 2026-08-26 a `net.sweep` records
every host it finds, so a /24 writes 254 audited creations in one transaction
and a /16 far more. Measured, 4096 of them took 14.7s.

**THE LOCK CANNOT SIMPLY BE DROPPED, AND THIS DOES NOT DROP IT.** The chain is
hash-linked: each event's hash covers the previous event's hash, so two
transactions appending to the same org concurrently would read the same head and
fork it. The lock is what serialises that, and it is `pg_advisory_xact_lock`, so
it is held until the transaction ends whichever way it is taken. Taking it ONCE
and then computing N links in Python is therefore exactly as serialised as
taking it N times -- and the run of N events written under one lock is, if
anything, a stronger guarantee than N independent acquisitions.

WHAT THESE TESTS DELIBERATELY VARY:

* **The lock count is asserted as a NUMBER against a batch of 50**, not as
  "at least one". An implementation that kept the per-event lock would still
  produce a correct chain and pass every verification test in this file.
* **A batch is written into a chain that ALREADY HAS EVENTS, and a single
  `record` follows it.** A `record_many` that ignored the existing head, or that
  left the head unreadable for the next `record`, verifies only against an empty
  table.
* **Two orgs' batches are interleaved.** A batch that locked or read the head
  without an `org_id` predicate joins two tenants' chains, which verification
  inside one org cannot see.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import GENESIS_HASH, AuditEntry, AuditService
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization

_KEY = b"audit-hmac-key-batch-0123456789ab"


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


def _entries(count: int, *, action: str = "test.batch") -> list[AuditEntry]:
    return [
        AuditEntry(
            actor_type="system",
            actor_id=None,
            action=action,
            target_type="thing",
            target_id=f"t{n}",
            metadata={"n": n},
        )
        for n in range(count)
    ]


class _LockCountingSession:
    """A session proxy that counts `pg_advisory_xact_lock` statements.

    Counted from the SQL that is actually sent rather than from a call to a
    helper, so an implementation that inlines the lock, or takes it through a
    different helper, is counted just the same.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.locks = 0

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "pg_advisory_xact_lock" in str(statement):
            self.locks += 1
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


async def test_a_batch_takes_one_lock_and_writes_one_unbroken_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FIFTY EVENTS, ONE LOCK, and the number is the assertion.

    *If `record_many` looped over `record`* the chain still verifies, the events
    are still all present, and the only thing that changes is `locks == 50`.
    That is why this test counts rather than checking `>= 1`: the correctness
    properties below cannot distinguish the batch from the loop it replaces.
    """
    org_id = await _make_org(admin_engine, "audit-batch")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        counter = _LockCountingSession(session)
        written = await audit.record_many(counter, org_id=org_id, entries=_entries(50))
    assert counter.locks == 1
    assert len(written) == 50

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (
                await session.execute(select(AuditEvent).order_by(AuditEvent.id))
            ).scalars()
        )
        result = await audit.verify(session, org_id)
    assert result.ok and result.count == 50 and result.broken_at is None
    assert [row.meta["n"] for row in rows] == list(range(50))
    assert rows[0].prev_hash == GENESIS_HASH
    assert all(b.prev_hash == a.hash for a, b in zip(rows, rows[1:], strict=False))


async def test_a_batch_continues_an_existing_chain_and_leaves_it_continuable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`record` -> `record_many` -> `record`, all in one org, all verifying.

    THE BATCH IS IN THE MIDDLE ON PURPOSE. A batch that ignored the org's head
    hash starts a second chain from GENESIS and verification catches it; a batch
    whose last event is not the head the NEXT `record` reads leaves a fork the
    first test would never see, because there the batch is the whole table.
    """
    org_id = await _make_org(admin_engine, "audit-mixed")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await audit.record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="test.before", target_type="thing", target_id="before",
        )
        batch = await audit.record_many(session, org_id=org_id, entries=_entries(5))
        last = await audit.record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="test.after", target_type="thing", target_id="after",
        )
    assert first.prev_hash == GENESIS_HASH
    assert batch[0].prev_hash == first.hash
    assert last.prev_hash == batch[-1].hash

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = await audit.verify(session, org_id)
    assert result.ok and result.count == 7


async def test_an_empty_batch_writes_nothing_and_locks_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A sweep that found nothing must not serialise the org's audit chain.

    *If the guard were dropped* this observes `locks == 1` -- an org-wide lock
    taken, and held to the end of the transaction, to write zero rows.
    """
    org_id = await _make_org(admin_engine, "audit-empty")
    audit = AuditService()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        counter = _LockCountingSession(session)
        assert await audit.record_many(counter, org_id=org_id, entries=[]) == []
    assert counter.locks == 0

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert (await audit.verify(session, org_id)).count == 0


async def test_two_orgs_batches_do_not_join_each_others_chains(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant #5 on the batch path, interleaved so a missing predicate shows.

    Each org's batch is written while the other org already has events, and each
    org's first event must still link to GENESIS. *If the head read dropped its
    `org_id` predicate* the second org's batch chains onto the first org's head
    and its first `prev_hash` is not GENESIS -- which is a fork, and which
    `verify` inside that org reports as broken.
    """
    org_a = await _make_org(admin_engine, "audit-two-a")
    org_b = await _make_org(admin_engine, "audit-two-b")
    audit = AuditService()
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        await audit.record_many(session, org_id=org_a, entries=_entries(3))
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        b_batch = await audit.record_many(session, org_id=org_b, entries=_entries(3))
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        await audit.record_many(session, org_id=org_a, entries=_entries(3))

    assert b_batch[0].prev_hash == GENESIS_HASH
    for org_id, expected in ((org_a, 6), (org_b, 3)):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            result = await audit.verify(session, org_id)
        assert result.ok and result.count == expected, org_id


async def test_a_keyed_batch_is_hmac_linked_like_a_keyed_single_append(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The batch must use the SAME link function, key and all.

    `_link` falls back to a bare SHA-256 when no key is configured, so a batch
    that reimplemented the hash rather than calling `_link` would pass every
    unkeyed test in this file and silently write forgeable events in
    production, where a key is mandatory. Verifying under the key -- and
    failing to verify under a service that has none -- is what pins it.
    """
    org_id = await _make_org(admin_engine, "audit-keyed")
    keyed = AuditService(hmac_key=_KEY)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await keyed.record_many(session, org_id=org_id, entries=_entries(4))

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert (await keyed.verify(session, org_id)).ok
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert not (await AuditService().verify(session, org_id)).ok


async def test_the_verify_route_still_passes_over_a_batched_chain(api_env) -> None:
    """`GET /api/v1/audit/verify` is the only view of this an operator has.

    The tests above call `AuditService.verify` directly. This one goes through
    the route -- same service, but through `org_session`, RLS, and the
    `audit.read` gate -- over a chain whose middle is a 300-event batch written
    by the sweep's own auto-creation path. A batch that broke the chain reports
    `ok: false` here and names the first offending id, which is exactly what an
    operator would see.
    """
    from app.modules.ipam.candidates import SweepSighting, auto_create_from_sweep

    org_id = await api_env.create_org("audit-verify")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await AuditService().record(
            session, org_id=org_id, actor_type="user", actor_id=None,
            action="test.before", target_type="thing", target_id="before",
        )
        await auto_create_from_sweep(
            session,
            org_id,
            sightings=[
                SweepSighting(
                    address=f"10.91.{n // 256}.{n % 256}",
                    mac=None,
                    hostname="",
                    confidence="low",
                )
                for n in range(300)
            ],
            sweep_run_id=uuid4(),
        )

    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    verified = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"ok": True, "count": 301, "broken_at": None}

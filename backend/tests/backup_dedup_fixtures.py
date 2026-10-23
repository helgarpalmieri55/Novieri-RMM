"""Seed helpers shared by the server-side dedup-exchange tests
(`test_backup_chunk_migration.py`, `test_backup_dedup.py`).

Same shape and reasoning as `tests/ipam_fixtures.py`: THIN inserts on the admin
(RLS-bypassing) engine that return ids, with no service-layer call anywhere, so
a test that exercises the dedup service is never seeded through the code path it
is testing. In particular nothing here goes through
`app.modules.backup.dedup`, and the chunk-index rows a test wants pre-existing
are inserted directly rather than by asking the service to record them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupChunk, BackupRepository, BackupRun, Device, Job, Organization, User
from app.models.core import JobStatus
from app.modules.backup.repository import create_repository

# The clock every dedup fixture and every dedup test shares. A seed that used
# `datetime.now()` while the tests pinned a fixed `now` would make the job's
# remaining lifetime depend on the wall clock — which is how a test suite comes
# to report `job_expired` for a test about something else, only after noon.
#
# THE FIX FOR THAT IS "ONE SHARED CLOCK", NOT "A LITERAL DATE", and the
# difference is not academic: this was `datetime(2026, 8, 9, 12, 0)` and the
# seeded job's `expires_at` is `SEED_NOW + 1h`, so from 13:00 UTC on
# 2026-08-09 onwards, `test_the_reply_goes_down_the_devices_own_channel` failed
# on every run, for ever. That test is the only one here that goes through
# `handle_agent_message`, which offers no `now` seam and therefore reads the
# REAL clock — so it saw a job that expired hours ago and got the deliberate
# silence, exactly the `job_expired` symptom the paragraph above predicted.
# Measured, not deduced: the audited refusal reason was `job_expired`, and the
# failure reproduces at the commit before it was found.
#
# Anchoring to the real clock keeps the property that comment is about — seed
# and tests still share ONE value, so no test's `now` can drift from the
# fixture's — while making "after noon" mean nothing. Every use is relative
# (`_NOW ± timedelta`); nothing asserts an absolute date. The one standing
# assumption is that a dedup test runs within the seeded job's TTL of module
# import. That TTL is 24h (not the whole backend suite's own ~23 min runtime)
# on purpose: a 1-hour TTL leaves only a one-hour margin between import and the
# last dedup test, which a slow or parallel run can burn through, reintroducing
# exactly the intermittent `job_expired` this fixture exists to rule out. The
# seeded job's `expires_at` is a fixture detail, not the real job-expiry
# policy under test, so widening it costs nothing.
SEED_NOW = datetime.now(UTC).replace(microsecond=0)


# A deterministic BLAKE3-256-shaped id. `n` varies the LAST characters so two
# ids differ everywhere a prefix comparison would look, and the first two
# characters (the object-key shard) vary too.
def chunk_id(n: int) -> str:
    return f"{n:064x}"


@dataclass(frozen=True)
class DedupOrg:
    """One org with everything a `backup.run` dedup query needs behind it."""

    org_id: UUID
    user_id: UUID
    device_id: UUID
    repository_id: UUID
    storage_ref: str
    job_id: UUID
    run_id: UUID


def admin_engine_of(env_or_engine: Any) -> AsyncEngine:
    engine = getattr(env_or_engine, "admin_engine", env_or_engine)
    assert isinstance(engine, AsyncEngine)
    return engine


async def seed_dedup_org(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    tag: str = "dd",
    *,
    device_count: int = 1,
) -> tuple[DedupOrg, list[UUID]]:
    """(org, user, device(s), repository, a running `backup.run` job + its
    `BackupRun` row). Returns the primary bundle and the full device id list."""
    slug = f"{tag}-{uuid4().hex[:8]}"
    now = SEED_NOW
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        devices = [
            Device(org_id=org.id, hostname=f"host-{slug}-{i}", os_family="linux")
            for i in range(device_count)
        ]
        for d in devices:
            session.add(d)
        await session.commit()
        org_id, user_id = org.id, user.id
        device_ids = [d.id for d in devices]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=user_id, name=f"repo-{slug}"
        )
        await session.flush()
        repository_id, storage_ref = repo.id, repo.storage_ref

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type="backup.run",
            target_type="device",
            target_id=device_ids[0],
            params={},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=24),
            status=JobStatus.running,
        )
        session.add(job)
        await session.flush()
        run = BackupRun(
            org_id=org_id,
            device_id=device_ids[0],
            repository_id=repository_id,
            job_id=job.id,
            state="running",
            actor_type="user",
            requested_by=user_id,
            started_at=now,
        )
        session.add(run)
        await session.commit()
        job_id, run_id = job.id, run.id

    return (
        DedupOrg(
            org_id=org_id,
            user_id=user_id,
            device_id=device_ids[0],
            repository_id=repository_id,
            storage_ref=storage_ref,
            job_id=job_id,
            run_id=run_id,
        ),
        device_ids,
    )


async def seed_index_rows(
    admin_engine: AsyncEngine, org_id: UUID, repository_id: UUID, chunk_ids: list[str]
) -> None:
    """Pre-populate the chunk index directly, bypassing the service."""
    async with AsyncSession(admin_engine) as session:
        for cid in chunk_ids:
            session.add(
                BackupChunk(org_id=org_id, repository_id=repository_id, chunk_id=cid)
            )
        await session.commit()


async def index_rows(
    admin_engine: AsyncEngine, repository_id: UUID
) -> list[tuple[UUID, str]]:
    """Every (org_id, chunk_id) recorded for `repository_id`, READ ON THE ADMIN
    ENGINE so RLS cannot hide a row that landed under the wrong org."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(BackupChunk.org_id, BackupChunk.chunk_id)
                .where(BackupChunk.repository_id == repository_id)
                .order_by(BackupChunk.chunk_id)
            )
        ).all()
    return [(r.org_id, r.chunk_id) for r in rows]


async def all_index_rows(admin_engine: AsyncEngine) -> list[tuple[UUID, UUID, str]]:
    """Every chunk-index row in the database, on the admin engine."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(BackupChunk.org_id, BackupChunk.repository_id, BackupChunk.chunk_id)
            )
        ).all()
    return [(r.org_id, r.repository_id, r.chunk_id) for r in rows]


async def repository_of(admin_engine: AsyncEngine, repository_id: UUID) -> BackupRepository:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(
                select(BackupRepository).where(BackupRepository.id == repository_id)
            )
        ).scalar_one()

"""Backup job lifetime (spec-server-backup.md §3.3, defect D3).

Jobs used to expire after an hour and the upload exchange refused expired
jobs, so every backup longer than an hour failed. A backup job now lives for
its start deadline plus a full run, and the exchange accepts a running job's
queries until it finishes, bounded by when it actually started: a start after
`not_after_start` gets nothing, and nothing is granted past start + the
maximum runtime.
"""

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from moto import mock_aws
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.storage import MAX_PRESIGNED_TTL_S, ObjectStore
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupChunkQueryMsg
from app.models import AuditEvent
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.schedule import MAX_RUNTIME, START_DEADLINE, rfc3339
from tests.backup_dedup_fixtures import SEED_NOW, chunk_id, seed_dedup_org

_NOW = SEED_NOW


@pytest.fixture()
def object_store(tmp_path: Path):
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


@pytest.fixture()
def vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


def _query(job_id: UUID) -> BackupChunkQueryMsg:
    return BackupChunkQueryMsg(
        v=1, kind="backup_chunk_query", job_id=str(job_id), query_id="q1",
        check=[chunk_id(1)], stored=[],
    )


async def _set_job(admin_engine: AsyncEngine, job_id: UUID, **values: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in values)
    async with AsyncSession(admin_engine) as session:
        await session.execute(text(f"UPDATE jobs SET {sets} WHERE id = :job"),
                              {"job": job_id, **values})
        await session.commit()


async def _reasons(admin_engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(admin_engine) as session:
        rows = (await session.execute(
            select(AuditEvent).where(AuditEvent.org_id == org_id,
                                     AuditEvent.action == "backup.dedup_refused")
        )).scalars().all()
    return [str(r.meta.get("reason")) for r in rows]


async def _ask(admin_engine: AsyncEngine, store: ObjectStore, org, now):
    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(session, store, _query(org.job_id),
                                         org_id=org.org_id, device_id=org.device_id, now=now)
        await session.commit()
    return reply


async def _params_with_deadline(admin_engine: AsyncEngine, job_id: UUID, deadline) -> None:
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE jobs SET params = jsonb_build_object('not_after_start', "
                 "cast(:d as text)) WHERE id = :job"),
            {"job": job_id, "d": rfc3339(deadline)},
        )
        await session.commit()


async def test_a_run_twenty_hours_in_still_gets_upload_grants(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    org, _ = await seed_dedup_org(admin_engine, app_engine, vault, "life-ok")
    started = _NOW - timedelta(hours=20)
    await _params_with_deadline(admin_engine, org.job_id, started + START_DEADLINE)
    await _set_job(admin_engine, org.job_id, started_at=started,
                   expires_at=started + START_DEADLINE + MAX_RUNTIME)

    reply = await _ask(admin_engine, object_store, org, _NOW)

    assert reply is not None
    # The grant ends when the run's time is up, never later.
    remaining = min(int((started + MAX_RUNTIME - _NOW).total_seconds()), MAX_PRESIGNED_TTL_S)
    assert reply.expires_at == rfc3339(_NOW + timedelta(seconds=remaining))


async def test_a_run_past_its_maximum_runtime_gets_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    org, _ = await seed_dedup_org(admin_engine, app_engine, vault, "life-long")
    started = _NOW - MAX_RUNTIME - timedelta(minutes=1)
    await _params_with_deadline(admin_engine, org.job_id, started + START_DEADLINE)
    await _set_job(admin_engine, org.job_id, started_at=started,
                   expires_at=_NOW + timedelta(hours=5))

    assert await _ask(admin_engine, object_store, org, _NOW) is None
    assert await _reasons(admin_engine, org.org_id) == ["run_time_exceeded"]


async def test_a_run_that_started_after_its_deadline_gets_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    org, _ = await seed_dedup_org(admin_engine, app_engine, vault, "life-late")
    dispatched = _NOW - timedelta(hours=10)
    await _params_with_deadline(admin_engine, org.job_id, dispatched + START_DEADLINE)
    await _set_job(admin_engine, org.job_id, started_at=_NOW - timedelta(minutes=5),
                   expires_at=dispatched + START_DEADLINE + MAX_RUNTIME)

    assert await _ask(admin_engine, object_store, org, _NOW) is None
    assert await _reasons(admin_engine, org.org_id) == ["started_after_deadline"]


async def test_a_job_without_a_start_deadline_keeps_the_old_rule(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    # Dispatched by a control plane before this change: params carry no
    # deadline, and its own `expires_at` stays the only bound.
    org, _ = await seed_dedup_org(admin_engine, app_engine, vault, "life-legacy")
    await _set_job(admin_engine, org.job_id, started_at=_NOW - timedelta(hours=30))
    assert await _ask(admin_engine, object_store, org, _NOW) is not None

"""The daily remote-recording retention sweep, exercised via the all-orgs entry
point (mirrors tests/test_security_retention.py's split between the per-org
function and the worker-level fan-out, but for retention_all_orgs-style
wrappers there's no existing direct test because the throwaway Postgres
cluster is shared session-wide across every test file -- other tests' orgs
are already sitting in the same database. So this test doesn't assert an
absolute total; it asserts the sweep's own summation invariant (total equals
the sum of what apply_recording_retention returned per org visited) and that
the two orgs it creates are among the ones visited.
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.core.storage import ObjectStore
from app.models import Organization


class _DummyStore:
    """Stand-in for ObjectStore: construction must not touch real S3/MinIO."""

    def __init__(self, settings: Settings) -> None:
        pass


async def _make_org(admin_engine: AsyncEngine) -> UUID:
    slug = f"rr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def test_retention_worker_sweeps_all_orgs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path, monkeypatch
) -> None:
    from app import worker

    calls: list[UUID] = []

    async def fake_apply(
        org_id: UUID, admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore
    ) -> int:
        calls.append(org_id)
        return 2

    monkeypatch.setattr(worker, "apply_recording_retention", fake_apply, raising=False)
    monkeypatch.setattr(worker, "ObjectStore", _DummyStore, raising=False)

    org_a = await _make_org(admin_engine)
    org_b = await _make_org(admin_engine)

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
    )
    with override_settings(settings):
        total = await worker.remote_recording_retention_all_orgs()

    assert total == 2 * len(calls)
    assert {org_a, org_b} <= set(calls)

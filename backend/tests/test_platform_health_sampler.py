"""The sampler writes one row per probe; the sweep prunes beyond 48h
(docs/superpowers/specs/2026-08-15-platform-health-design.md §6.1).

FIXTURE PATTERN. The task brief for this file sketched a `db_session`
fixture -- the same note recurs across this suite
(`test_notification_migration.py`, `test_portal_migration.py`,
`test_notification_worker.py`): no such fixture exists in `tests/conftest.py`.
The real convention is `AsyncSession(<engine fixture>, expire_on_commit=...)`
built directly in the test file, so `db_session` here is a small local
fixture over conftest's `app_engine`. It is deliberately `app_engine`, not
`admin_engine`: `platform_health_sample`'s only RLS policy grants `app_user`
(Task 7's migration 0055), and `admin_engine` is `BYPASSRLS` -- writing
through it would pass even if the app-role policy were wrong.

`sample_health_once` also runs `WORKER_SAMPLED_PROBES` -- seven of the eight
probes, all but `jwks` (see `app.api.probes`) -- against real `Settings`
(Postgres, Redis, a local CA/signer/vault rooted under `tmp_path`), so this
file overrides the process-wide settings singleton the same way
`test_worker_audit_chain.py` and `test_worker_beat_expiry.py` do.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.probes import WORKER_SAMPLED_PROBES
from app.core.config import Settings, override_settings
from app.core.health.probes import probe_jwks
from app.models import PlatformHealthSample
from app.worker import RETENTION_HOURS, prune_health_samples, sample_health_once

REDIS_URL = "redis://localhost:6379/9"


def _settings(pg_cluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        vault_root_key_path=tmp_path / "vault-root.key",
        redis_url=REDIS_URL,
    )


@pytest.fixture(autouse=True)
def _worker_settings(pg_cluster, tmp_path: Path) -> Iterator[None]:
    with override_settings(_settings(pg_cluster, tmp_path)):
        yield


@pytest.fixture()
async def db_session(app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        yield session


async def _insert(session: AsyncSession, *, sampled_at: datetime) -> None:
    session.add(
        PlatformHealthSample(
            probe="postgres.app",
            status="ok",
            duration_ms=1,
            reason=None,
            sampled_at=sampled_at,
        )
    )
    await session.commit()


async def test_one_row_per_probe_is_written(db_session: AsyncSession) -> None:
    written = await sample_health_once(db_session)
    # One short of the registry's size: the sampler runs
    # `WORKER_SAMPLED_PROBES`, every probe but `jwks` -- see
    # `test_the_sampler_writes_no_jwks_row` below for why. Re-measured when
    # `keycloak_events` was added, and again for the three backup probes.
    assert written == 11, "one row per worker-sampled probe (every probe minus jwks)"


async def test_the_sampler_writes_no_jwks_row(db_session: AsyncSession) -> None:
    """The worker constructs no `JwksCache`, so sampling `jwks` from this
    process would not describe the IdP -- it would fabricate a `down` for an
    attribute that was never wired up here. Assert the exact set written,
    not just its size, so a probe silently swapped in for another still
    fails this."""
    await sample_health_once(db_session)
    names = set((await db_session.execute(select(PlatformHealthSample.probe))).scalars().all())
    assert names == WORKER_SAMPLED_PROBES
    assert "jwks" not in names


async def test_jwks_is_excluded_because_a_worker_reading_would_be_a_false_down() -> None:
    """Pins the COST of the exclusion above, not just the fact of it.

    `probe_jwks` reads `state.jwks._keys` unconditionally (see
    `app.core.health.probes.probe_jwks`) -- there is no `getattr` fallback
    for a missing `jwks` itself, only for `_keys` on top of it. A
    worker-shaped state, exactly like the one `sample_health_once` builds,
    has no `jwks` attribute at all, so calling the probe on it is not a
    graceful DEGRADED -- it is an `AttributeError`. Left uncaught, the
    registry's own catch-all (`ProbeRegistry._run_one`) turns any such
    exception into `Status.DOWN` / `ReasonCode.UNREACHABLE`, which is exactly
    the fabricated, permanent `jwks / down / unreachable` row this whole fix
    exists to stop writing every 60 seconds. If a future edit drops `jwks`
    from `WORKER_SAMPLED_PROBES`, or has `sample_health_once` fall back to
    calling `probe_jwks` directly instead of going through the registry, this
    test shows exactly what that costs instead of raising a mysterious
    `AttributeError` in production.
    """
    worker_shaped_state = SimpleNamespace()  # no `jwks` attribute, by design
    with pytest.raises(AttributeError):
        await probe_jwks(worker_shaped_state)


async def test_the_sweep_deletes_beyond_the_window_and_keeps_the_rest(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _insert(db_session, sampled_at=now - timedelta(hours=RETENTION_HOURS + 1))
    await _insert(db_session, sampled_at=now - timedelta(hours=1))
    deleted = await prune_health_samples(db_session, now=now)
    assert deleted == 1

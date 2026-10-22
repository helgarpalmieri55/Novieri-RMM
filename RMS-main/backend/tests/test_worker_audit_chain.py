"""The Celery worker process must authenticate the audit chain it appends to.

Invariant #4 (CLAUDE.md) requires a tamper-evident audit log. Tamper-evidence
comes from the HMAC key, not from the chaining: an unkeyed SHA-256 link is a
checksum that anyone with write access to `audit_events` can recompute, so
unkeyed entries can be forged or rewritten undetected. `app/main.py` and
`app/gateway/main.py` load the key at process wiring; `create_celery()` is the
worker's equivalent, and these tests hold it to that.

The worker writes audit events on live paths (SLA breach escalation and
auto-close, provisioning timeouts, patch/deploy/backup dispatch), so this is not
hypothetical.
"""

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import worker
from app.core.audit import AuditService, audit_service
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from app.models import Organization

REDIS_URL = "redis://localhost:6379/9"

_KEY = b"worker-audit-hmac-key-0123456789"


def _settings(pg_cluster, tmp_path: Path, *, key_path: Path | None) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
        vault_root_key_path=tmp_path / "vault-root.key",
        audit_hmac_key_path=key_path,
    )


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _record_like_a_sweep(org_id: UUID, app_engine: AsyncEngine):
    """Append one event exactly the way worker.py's sweeps do -- through the
    process-wide `audit_service` singleton, system-attributed."""
    async with org_session(org_id, engine=app_engine) as session:
        return await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="test.worker_sweep",
            target_type="device",
            target_id=str(uuid4()),
            metadata={"from": "worker"},
        )


async def test_create_celery_keys_the_audit_chain(
    pg_cluster, tmp_path: Path, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """With an HMAC key configured, an event written by the worker after
    `create_celery()` must carry an HMAC-SHA256 link, not a plain SHA-256 one.

    FALSIFICATION: the keyed and unkeyed paths are made to produce different
    observable values by verifying the same stored row twice -- once with a
    verifier holding the key (as `GET /api/v1/audit/verify` does in a keyed
    deployment) and once with an unkeyed verifier. Exactly one can pass.
    Delete the `audit_service.set_hmac_key(...)` line from `create_celery()`
    and the observations invert: the row is written with a plain SHA-256 link,
    so `keyed.verify()` returns ok=False with `broken_at == event.id` and the
    first assertion below fails, while `unkeyed.verify()` returns ok=True and
    the third assertion fails. A test that only asserted "verify passes" would
    pass either way -- that is the defect class this file exists to catch.
    """
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(_KEY)
    org_id = await _make_org(admin_engine, "wrk-audit-keyed")

    # a fresh worker process starts with no key on the singleton
    audit_service.set_hmac_key(None)
    try:
        with override_settings(_settings(pg_cluster, tmp_path, key_path=key_path)):
            worker.create_celery()
        event = await _record_like_a_sweep(org_id, app_engine)

        async with org_session(org_id, engine=app_engine) as session:
            keyed = await AuditService(hmac_key=_KEY).verify(session, org_id)
            unkeyed = await AuditService().verify(session, org_id)
    finally:
        audit_service.set_hmac_key(None)

    assert keyed.ok, "worker-written event does not verify under the deployment's HMAC key"
    assert keyed.count == 1
    # The link is genuinely keyed: recomputing it without the key -- which is
    # all a database-write attacker can do -- does not reproduce the stored hash.
    assert not unkeyed.ok
    assert unkeyed.broken_at == event.id


async def test_create_celery_clears_a_stale_key_when_none_configured(
    pg_cluster, tmp_path: Path, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`set_hmac_key` must be called unconditionally, with None when no key is
    configured -- not skipped. Otherwise a key left on the singleton by another
    context (an in-process `create_app()`, a previous test) would silently key
    events that this deployment's verifier will read unkeyed.

    FALSIFICATION: guard the call with `if key is not None:` and the stale
    `_KEY` survives, so the event below is written with an HMAC link and
    `unkeyed.ok` becomes False.
    """
    org_id = await _make_org(admin_engine, "wrk-audit-unkeyed")

    audit_service.set_hmac_key(_KEY)  # stale key from another context
    try:
        with override_settings(_settings(pg_cluster, tmp_path, key_path=None)):
            worker.create_celery()
        event = await _record_like_a_sweep(org_id, app_engine)

        async with org_session(org_id, engine=app_engine) as session:
            unkeyed = await AuditService().verify(session, org_id)
            keyed = await AuditService(hmac_key=_KEY).verify(session, org_id)
    finally:
        audit_service.set_hmac_key(None)

    assert unkeyed.ok and unkeyed.count == 1
    assert not keyed.ok and keyed.broken_at == event.id

import base64
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, BackupRepository, Organization
from app.modules.backup.repository import create_repository, open_repo_key


async def _org(admin_engine: AsyncEngine, actor_id: object = None) -> tuple[object, object]:
    slug = f"bkrepo-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id, uuid4()


async def test_create_repository_sets_encryption_key_ref(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=actor_id, name="primary"
        )
        await session.commit()
    assert repo.encryption_key_ref
    assert repo.storage_ref == f"{org_id}/backup/{repo.id}"
    assert repo.state == "active"


async def test_open_repo_key_roundtrips_the_sealed_master_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=actor_id, name="primary"
        )
        repo_id = repo.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        key1 = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        key2 = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)

    assert key1 == key2
    assert len(key1) == 32


async def test_db_column_holds_sealed_string_not_the_raw_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=actor_id, name="primary"
        )
        repo_id = repo.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        raw_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        row = (
            await session.execute(
                select(BackupRepository).where(BackupRepository.id == repo_id)
            )
        ).scalar_one()

    assert raw_key.hex() not in row.encryption_key_ref
    assert base64.b64encode(raw_key).decode() not in row.encryption_key_ref
    assert raw_key not in row.encryption_key_ref.encode()


async def test_repository_created_audit_has_no_key_in_metadata(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=actor_id, name="primary"
        )
        repo_id = repo.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        raw_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "backup.repository_created",
                )
            )
        ).scalar_one()

    assert event.actor_type == "user"
    assert event.actor_id == actor_id
    assert event.target_id == str(repo_id)
    assert "key" not in str(event.meta).lower() or all(
        raw_key.hex() not in str(v) for v in event.meta.values()
    )
    assert raw_key.hex() not in str(event.meta)
    assert base64.b64encode(raw_key).decode() not in str(event.meta)


async def test_org_b_cannot_open_org_as_repo_key_because_rls_hides_the_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_a, actor_id=actor_a, name="primary"
        )
        repo_id = repo.id
        await session.commit()

    with pytest.raises(Exception):  # noqa: B017 - org-scoped select finds no row (RLS+predicate)
        async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
            await open_repo_key(session, vault, org_id=org_b, repository_id=repo_id)


async def test_the_repo_key_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession, tmp_path: Path
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    The test above runs `open_repo_key` inside `org_session` on the NOBYPASSRLS
    `app_user` role, where `backup_repository` carries a forced
    `tenant_isolation` policy and org A's row is gone before
    `BackupRepository.org_id == org_id` is consulted -- it raises with that
    predicate deleted just the same, and its own `# noqa` comment concedes the
    ambiguity ("RLS+predicate"). `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here the predicate is the only thing between the
    two tenants.

    WHAT IS ON THE OTHER SIDE OF IT IS A BACKUP ENCRYPTION KEY (invariant 3):
    not a row, not an identifier, the material that decrypts another customer's
    entire backup set. `scalar_one` is what makes the refusal loud -- a missing
    row raises rather than returning `None` for `vault.open_bytes` to be handed.

    BOTH ORGS HOLD A REPOSITORY, and org B's own key is opened at the end, so
    the refusal cannot be a vault or fixture failure wearing a tenancy costume.

    FALSIFIED, AND THE FAILURE MODE IS WORTH RECORDING. Deleting
    `BackupRepository.org_id == org_id` turns the `NoResultFound` into org A's
    repository row, and `vault.open_bytes` is then called with org B's `org_id`
    against org A's sealed key -- at which point the test goes red with
    `cryptography.exceptions.InvalidTag` instead. That is the vault's own
    second rail: `org_id` is bound into the AEAD's associated data, so the key
    does not open for the wrong tenant even when the query hands it over. The
    assertion names `NoResultFound` specifically rather than bare `Exception`
    for exactly this reason -- a test that accepted any exception would score
    the vault's refusal as the query's, and the predicate would be untested
    while looking covered.
    """
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org(admin_engine)
    org_b, actor_b = await _org(admin_engine)

    repos: dict[str, object] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        repo = await create_repository(
            bypassrls_session, vault, org_id=org_id, actor_id=actor_id, name=f"primary-{key}"
        )
        repos[key] = repo.id
    await bypassrls_session.commit()

    with pytest.raises(NoResultFound):
        await open_repo_key(
            bypassrls_session, vault, org_id=org_b, repository_id=repos["a"]
        )

    # Non-vacuity: org B's own key still opens, so the refusal above is the
    # tenant boundary and not a broken vault.
    assert await open_repo_key(
        bypassrls_session, vault, org_id=org_b, repository_id=repos["b"]
    )

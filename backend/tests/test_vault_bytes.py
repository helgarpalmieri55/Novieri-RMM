import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import Organization, OrgVaultKey


async def _org(admin_engine: AsyncEngine) -> object:
    slug = f"vltb-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def test_seal_open_roundtrip_arbitrary_bytes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    key_id = uuid4()
    plaintext = b"\x00\x01arbitrary\xffbytes\x02"
    async with org_session(org_id, engine=app_engine) as session:
        sealed = await vault.seal_bytes(
            session, org_id=org_id, key_id=key_id, plaintext=plaintext
        )
    async with org_session(org_id, engine=app_engine) as session:
        opened = await vault.open_bytes(session, org_id=org_id, key_id=key_id, sealed=sealed)
    assert opened == plaintext


async def test_seal_open_roundtrip_repository_master_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    key_id = uuid4()
    repo_key = os.urandom(32)
    async with org_session(org_id, engine=app_engine) as session:
        sealed = await vault.seal_bytes(session, org_id=org_id, key_id=key_id, plaintext=repo_key)
    async with org_session(org_id, engine=app_engine) as session:
        opened = await vault.open_bytes(session, org_id=org_id, key_id=key_id, sealed=sealed)
    assert opened == repo_key


async def test_sealed_string_does_not_contain_plaintext(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    key_id = uuid4()
    repo_key = os.urandom(32)
    async with org_session(org_id, engine=app_engine) as session:
        sealed = await vault.seal_bytes(session, org_id=org_id, key_id=key_id, plaintext=repo_key)
    assert repo_key not in sealed.encode()
    assert repo_key.hex() not in sealed


async def test_open_bytes_wrong_key_id_fails(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    key_id = uuid4()
    other_key_id = uuid4()
    plaintext = b"repo-master-key-bytes-000000000"
    async with org_session(org_id, engine=app_engine) as session:
        sealed = await vault.seal_bytes(
            session, org_id=org_id, key_id=key_id, plaintext=plaintext
        )
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag from cryptography (AAD mismatch)
        async with org_session(org_id, engine=app_engine) as session:
            await vault.open_bytes(session, org_id=org_id, key_id=other_key_id, sealed=sealed)


async def test_open_bytes_wrong_org_id_fails(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    other_org_id = await _org(admin_engine)
    key_id = uuid4()
    plaintext = b"repo-master-key-bytes-000000000"
    async with org_session(org_id, engine=app_engine) as session:
        sealed = await vault.seal_bytes(
            session, org_id=org_id, key_id=key_id, plaintext=plaintext
        )
    with pytest.raises(Exception):  # noqa: B017 - wrong org DEK / AAD mismatch
        async with org_session(other_org_id, engine=app_engine) as session:
            await vault.open_bytes(session, org_id=other_org_id, key_id=key_id, sealed=sealed)


async def test_seal_bytes_reuses_org_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine) as session:
        await vault.seal_bytes(session, org_id=org_id, key_id=uuid4(), plaintext=b"a")
        await vault.seal_bytes(session, org_id=org_id, key_id=uuid4(), plaintext=b"b")
        count = (
            await session.execute(
                select(func.count())
                .select_from(OrgVaultKey)
                .where(OrgVaultKey.org_id == org_id)
            )
        ).scalar_one()
    assert count == 1

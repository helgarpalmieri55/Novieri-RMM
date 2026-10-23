"""Recovery keys (spec-server-backup.md §6.5; operator decision 2026-09-23:
mandatory before off-site copying is enabled).

The private key is generated offline and never reaches the server; possession
is proven by decrypting a challenge. These tests drive the server with the
standalone `scripts/itms_recover.py`, so the two implementations of the wrap
format are checked against each other rather than each against itself.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent
from app.modules.backup.recovery import (
    RecoveryKeyError,
    chunk_name_key,
    fingerprint,
    offsite_chunk_name,
    parse_public_key,
    prove_recovery_key,
    proven_recovery_keys,
    register_recovery_key,
    repository_key_object,
    unwrap,
)
from tests.backup_offsite_fixtures import load_recover_tool, seed_org

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_the_tool_unwraps_what_the_server_wraps() -> None:
    tool = load_recover_tool()
    private_text, public_text = tool.keygen()
    private = tool.load_private_key(private_text)
    public = parse_public_key(public_text)
    repo_id = uuid4()
    repo_key = b"k" * 32
    obj = json.loads(repository_key_object(public, repo_key, repo_id))
    assert obj["recipient"] == fingerprint(public) == tool.fingerprint(public)
    assert tool.unwrap(private, obj, str(repo_id).encode()) == repo_key
    assert unwrap(private, obj, str(repo_id).encode()) == repo_key


def test_a_key_object_does_not_open_under_another_repository_id() -> None:
    tool = load_recover_tool()
    private_text, public_text = tool.keygen()
    obj = json.loads(repository_key_object(parse_public_key(public_text), b"k" * 32, uuid4()))
    with pytest.raises(Exception):  # noqa: B017 - AES-GCM authentication failure
        tool.unwrap(tool.load_private_key(private_text), obj, str(uuid4()).encode())


def test_offsite_chunk_names_are_keyed_and_agree_with_the_tool() -> None:
    tool = load_recover_tool()
    cid = "ab" * 32
    name = offsite_chunk_name(chunk_name_key(b"r" * 32), cid)
    assert cid not in name
    assert name == tool._chunk_name(b"r" * 32, cid)
    assert name != offsite_chunk_name(chunk_name_key(b"s" * 32), cid)


@pytest.mark.parametrize("text", ["", "not base64!", base64.b64encode(b"short").decode()])
def test_a_malformed_public_key_is_refused(text: str) -> None:
    with pytest.raises(RecoveryKeyError):
        parse_public_key(text)


async def test_register_prove_and_list(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    tool = load_recover_tool()
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "rk")
    private_text, public_text = tool.keygen()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row, challenge = await register_recovery_key(
            session, org_id=org.org_id, actor_id=org.user_id, public_key_text=public_text,
            now=NOW,
        )
        assert await proven_recovery_keys(session, org.org_id) == []
        answer = tool.prove(tool.load_private_key(private_text), challenge)
        await prove_recovery_key(session, row, answer=answer, actor_id=org.user_id, now=NOW)
        proven = await proven_recovery_keys(session, org.org_id)
    assert [k.id for k in proven] == [row.id]
    assert row.challenge_sha256 is None
    # The server stores the public key and nothing that is the private key.
    assert private_text.split(":", 1)[1] not in (row.public_key + row.fingerprint)


async def test_a_wrong_answer_is_refused_and_audited(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    tool = load_recover_tool()
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "rkw")
    _other_private, public_text = tool.keygen()
    wrong_private, _ = tool.keygen()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row, challenge = await register_recovery_key(
            session, org_id=org.org_id, actor_id=org.user_id, public_key_text=public_text,
            now=NOW,
        )
        key_id = row.id
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (await session.execute(select(type(row)).where(type(row).id == key_id))).scalar_one()
        # The wrong private key cannot even open the challenge.
        with pytest.raises(Exception):  # noqa: B017
            tool.prove(tool.load_private_key(wrong_private), challenge)
        with pytest.raises(RecoveryKeyError):
            await prove_recovery_key(session, row, answer=base64.b64encode(b"x" * 32).decode(),
                                     actor_id=org.user_id, now=NOW)
    async with AsyncSession(admin_engine) as s:
        actions = (await s.execute(select(AuditEvent.action).where(
            AuditEvent.org_id == org.org_id))).scalars().all()
    assert "backup.recovery_key_proof_failed" in actions
    assert "backup.recovery_key_proven" not in actions


async def test_an_expired_challenge_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    tool = load_recover_tool()
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "rke")
    private_text, public_text = tool.keygen()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row, challenge = await register_recovery_key(
            session, org_id=org.org_id, actor_id=org.user_id, public_key_text=public_text,
            now=NOW,
        )
        answer = tool.prove(tool.load_private_key(private_text), challenge)
        with pytest.raises(RecoveryKeyError):
            await prove_recovery_key(session, row, answer=answer, actor_id=org.user_id,
                                     now=NOW + timedelta(days=2))


async def test_the_same_key_cannot_be_registered_twice(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    tool = load_recover_tool()
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "rkd")
    _p, public_text = tool.keygen()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await register_recovery_key(session, org_id=org.org_id, actor_id=org.user_id,
                                    public_key_text=public_text, now=NOW)
        with pytest.raises(RecoveryKeyError):
            await register_recovery_key(session, org_id=org.org_id, actor_id=org.user_id,
                                        public_key_text=public_text, now=NOW)

import base64
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService, OpenBaoVaultService, get_vault
from app.models import Credential, Organization, OrgVaultKey


async def _org_credentials(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    slug = f"vlt-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        c1 = Credential(org_id=org.id, name="admin1", type="windows_admin", vault_ref="")
        c2 = Credential(org_id=org.id, name="admin2", type="windows_admin", vault_ref="")
        session.add_all([c1, c2])
        await session.commit()
        return org.id, c1.id, c2.id


async def test_local_roundtrip(admin_engine, app_engine, tmp_path: Path) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, cred_id, _ = await _org_credentials(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        stored = await vault.store(
            session, org_id=org_id, credential_id=cred_id, plaintext="hunter2"
        )
        credential = (
            await session.execute(select(Credential).where(Credential.id == cred_id))
        ).scalar_one()
        credential.ciphertext = stored.ciphertext_b64
        credential.wrapped_dek = stored.wrapped_dek
        credential.key_version = stored.key_version
        await session.flush()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        plaintext = await vault.checkout(session, org_id=org_id, credential_id=cred_id)
    assert plaintext == "hunter2"
    # ciphertext does not contain the plaintext bytes
    assert b"hunter2" not in base64.b64decode(stored.ciphertext_b64)


async def test_two_secrets_differ_and_aad_binds(admin_engine, app_engine, tmp_path: Path) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, cred_a, cred_b = await _org_credentials(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        stored_a = await vault.store(
            session, org_id=org_id, credential_id=cred_a, plaintext="same-secret"
        )
        stored_b = await vault.store(
            session, org_id=org_id, credential_id=cred_b, plaintext="same-secret"
        )
        # identical plaintext -> different ciphertext (random nonce)
        assert stored_a.ciphertext_b64 != stored_b.ciphertext_b64
        # AAD binds ciphertext to its credential id: A's ciphertext under B's id fails
        cred = (
            await session.execute(select(Credential).where(Credential.id == cred_b))
        ).scalar_one()
        cred.ciphertext = stored_a.ciphertext_b64
        cred.wrapped_dek = stored_a.wrapped_dek
        cred.key_version = stored_a.key_version
        await session.flush()
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag from cryptography
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await vault.checkout(session, org_id=org_id, credential_id=cred_b)


async def test_one_org_key_reused(admin_engine, app_engine, tmp_path: Path) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, cred_a, cred_b = await _org_credentials(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await vault.store(session, org_id=org_id, credential_id=cred_a, plaintext="a")
        await vault.store(session, org_id=org_id, credential_id=cred_b, plaintext="b")
        count = (
            await session.execute(select(func.count()).select_from(OrgVaultKey))
        ).scalar_one()
    assert count == 1


async def test_openbao_backend_roundtrip(admin_engine, app_engine, tmp_path: Path) -> None:
    # OpenBao transit wraps/unwraps the DEK; emulate it with a fixed XOR pad so
    # the mock is deterministic and the AES-GCM secret layer is exercised for real.
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if "/transit/keys/" in request.url.path:
            return httpx.Response(204)
        body = _json.loads(request.content)
        if "/transit/encrypt/org-" in request.url.path:
            return httpx.Response(
                200, json={"data": {"ciphertext": f"vault:v1:{body['plaintext']}"}}
            )
        if "/transit/decrypt/org-" in request.url.path:
            token = body["ciphertext"].split(":", 2)[2]
            return httpx.Response(200, json={"data": {"plaintext": token}})
        return httpx.Response(404)

    vault = OpenBaoVaultService(
        "https://bao.test",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    org_id, cred_id, _ = await _org_credentials(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        stored = await vault.store(
            session, org_id=org_id, credential_id=cred_id, plaintext="bao-secret"
        )
        cred = (
            await session.execute(select(Credential).where(Credential.id == cred_id))
        ).scalar_one()
        cred.ciphertext, cred.wrapped_dek, cred.key_version = (
            stored.ciphertext_b64,
            stored.wrapped_dek,
            stored.key_version,
        )
        await session.flush()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await vault.checkout(session, org_id=org_id, credential_id=cred_id) == "bao-secret"


async def test_org_dek_concurrent_first_calls_leave_both_secrets_decryptable(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two colleagues in a brand-new org both store a credential for the
    first time at once (e.g. two admins provisioning credentials right
    after onboarding). `org_id` is `OrgVaultKey`'s PK, so a naive
    check-then-insert would let the loser's `_org_dek` call raise
    `IntegrityError` on `org_vault_keys_pkey` -- a 500 on the
    credential-storage path -- AFTER it had already generated and wrapped
    its own (now-orphaned) DEK.

    The property that actually matters is stronger than "no
    IntegrityError": a fix that let the loser return its own
    locally-generated DEK while `org_vault_keys` ends up holding the
    winner's would leave the loser's secret encrypted under a DEK nothing
    on disk reflects -- decryptable today only by accident (because
    `checkout()` currently reads `Credential.wrapped_dek` directly), and
    silently unrecoverable the moment anything ever re-derives a
    credential's DEK from the org's canonical row instead. So this test
    asserts BOTH secrets remain decryptable after the race, AND that both
    credentials' `wrapped_dek` equals the canonical `org_vault_keys` row --
    proving the loser returned the SAME dek the winner's row holds, not a
    discarded one.

    Deterministic interleave, the repo's established technique (see
    `test_budget_fx.py::test_get_settings_concurrent_first_calls_do_not_race`):
    session A's `_org_dek` SELECT against `org_vault_keys` is intercepted;
    right after it misses (org has no key yet), session B runs a FULL
    concurrent `store()` -- generating its own DEK, creating the org's key,
    and persisting its own credential's ciphertext -- and commits, before A
    gets to insert its own `OrgVaultKey` row.
    """
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, cred_a, cred_b = await _org_credentials(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "org_vault_keys" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    stored_b = await vault.store(
                        session_b, org_id=org_id, credential_id=cred_b, plaintext="secret-b"
                    )
                    cred_b_row = (
                        await session_b.execute(
                            select(Credential).where(Credential.id == cred_b)
                        )
                    ).scalar_one()
                    cred_b_row.ciphertext = stored_b.ciphertext_b64
                    cred_b_row.wrapped_dek = stored_b.wrapped_dek
                    cred_b_row.key_version = stored_b.key_version
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        stored_a = await vault.store(
            session_a, org_id=org_id, credential_id=cred_a, plaintext="secret-a"
        )
        cred_a_row = (
            await session_a.execute(select(Credential).where(Credential.id == cred_a))
        ).scalar_one()
        cred_a_row.ciphertext = stored_a.ciphertext_b64
        cred_a_row.wrapped_dek = stored_a.wrapped_dek
        cred_a_row.key_version = stored_a.key_version
        await session_a.commit()

    # Exactly one OrgVaultKey row was ever created for this org.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        canonical = (
            await session.execute(select(OrgVaultKey).where(OrgVaultKey.org_id == org_id))
        ).scalar_one()
        count = (
            await session.execute(
                select(func.count())
                .select_from(OrgVaultKey)
                .where(OrgVaultKey.org_id == org_id)
            )
        ).scalar_one()
        assert count == 1

        cred_rows = (
            await session.execute(select(Credential).where(Credential.org_id == org_id))
        ).scalars().all()
        # Neither credential is encrypted under a DEK that org_vault_keys
        # does not reflect -- both wrapped_dek values equal the canonical row.
        assert {c.wrapped_dek for c in cred_rows} == {canonical.wrapped_dek}

    # And both secrets -- winner's and loser's -- remain independently
    # decryptable.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await vault.checkout(session, org_id=org_id, credential_id=cred_a) == "secret-a"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await vault.checkout(session, org_id=org_id, credential_id=cred_b) == "secret-b"


def test_factory_guard(tmp_path: Path) -> None:
    base = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=str(tmp_path),
    )
    local = Settings(**base, vault_backend="local", vault_root_key_path=tmp_path / "r.key")
    assert isinstance(get_vault(local), LocalVaultService)
    with pytest.raises(ValueError):
        get_vault(Settings(**base, vault_backend="openbao"))

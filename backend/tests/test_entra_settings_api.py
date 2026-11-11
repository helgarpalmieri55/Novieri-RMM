"""Settings → Microsoft sign-in (plan 2026-11-06, Task 5, decisions 5-6; C4,
D33, D34): the application and Novieri's tenant; a certificate the platform
makes, its private key into the vault and never back out; a renewal that
waits beside the certificate in use until a person switches to it, the key
in use signing meanwhile; every save audited; nothing switched on half
configured; nothing written by a save that changes nothing; `orgs.manage` on
every client to save."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import entra, entra_settings
from app.core.auth.entra import EntraRefused
from app.core.auth.entra_settings import ENTRA_CREDENTIAL_TYPE
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Client, Credential, Role, User, UserRole
from app.models.auth import AuthEntraSettings
from tests.entra_support import APP, CONSUMERS, TENANT, FakeMicrosoft

API = "/api/v1/settings/microsoft-sign-in"
OFF = {"enabled": False, "app_client_id": None, "staff_tenant_id": None}
ON = {"enabled": True, "app_client_id": str(APP), "staff_tenant_id": str(TENANT)}


async def _admin(env: Any) -> tuple[Any, dict[str, str]]:
    org = await env.create_org("entra-settings")
    sub = await env.create_user(org, "org-admin")
    return org, await env.headers(org, sub)


async def _keys(env: Any, org: Any) -> list[Any]:
    """The organization's sign-in-with-Microsoft keys in the vault."""
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(Credential.id).where(
                        Credential.org_id == org, Credential.type == ENTRA_CREDENTIAL_TYPE
                    )
                )
            ).scalars()
        )


async def _exchange_passes(env: Any, org: Any, microsoft: FakeMicrosoft) -> bool:
    """One code exchange, its assertion signed as Task 9 signs it: with the
    key `signing_key` hands out, naming the certificate in use."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
        private_pem = await entra_settings.signing_key(
            session, env.app.state.vault, org_id=org, config=config, actor_id=None
        )
    assert config.app_client_id is not None and config.certificate_thumbprint is not None
    async with microsoft.client() as client:
        try:
            await entra.exchange(
                client=client,
                client_id=config.app_client_id,
                client_assertion=entra.client_assertion(
                    client_id=config.app_client_id,
                    private_pem=private_pem,
                    thumbprint=config.certificate_thumbprint,
                ),
                code=microsoft.issue_code(nonce="n-rotation"),
                redirect_uri="https://itms.test/sign-in/microsoft",
                code_verifier="v" * 43,
            )
        except EntraRefused:
            return False
    return True


async def test_nothing_is_set_until_somebody_saves(api_env: Any) -> None:
    _org, headers = await _admin(api_env)
    got = await api_env.client.get(API, headers=headers)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "enabled": False,
        "app_client_id": None,
        "staff_tenant_id": None,
        "configured": False,
        "certificate": None,
        "certificate_thumbprint": None,
        "certificate_expires_at": None,
        "next_certificate": None,
        "next_certificate_thumbprint": None,
        "next_certificate_expires_at": None,
        "redirect_uri": "https://itms.test/sign-in/microsoft",
        "updated_at": None,
        "updated_by": None,
    }


async def test_a_first_save_that_changes_nothing_writes_nothing(api_env: Any) -> None:
    """A2-25: the Settings tab saving before anything is filled in."""
    org, headers = await _admin(api_env)
    saved = await api_env.client.put(API, headers=headers, json=OFF)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is False
    async with AsyncSession(api_env.admin_engine) as s:
        rows = await s.scalar(
            select(func.count())
            .select_from(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
        )
        events = await s.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.org_id == org, AuditEvent.action == "auth.entra_settings_updated")
        )
    assert (rows, events) == (0, 0)


async def test_switching_on_needs_a_certificate_the_platform_makes(api_env: Any) -> None:
    org, headers = await _admin(api_env)
    first = await api_env.client.put(API, headers=headers, json=ON)
    assert first.status_code == 422, "no certificate yet: decision 5's 422"
    saved = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True and body["enabled"] is True
    certificate = x509.load_pem_x509_certificate(body["certificate"].encode())
    assert body["certificate_expires_at"].startswith(
        certificate.not_valid_after_utc.date().isoformat()
    )
    assert "PRIVATE" not in saved.text
    async with AsyncSession(api_env.admin_engine) as s:
        credential = (
            await s.execute(
                select(Credential).where(
                    Credential.org_id == org, Credential.type == ENTRA_CREDENTIAL_TYPE
                )
            )
        ).scalar_one()
        [event] = (
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org, AuditEvent.action == "auth.entra_settings_updated"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert credential.ciphertext and "PRIVATE" not in credential.ciphertext
    assert event.meta["certificate"]["thumbprint"] == body["certificate_thumbprint"]
    assert "PRIVATE" not in str(event.meta)

    kept = await api_env.client.put(API, headers=headers, json={**ON, "staff_tenant_id": None})
    assert kept.status_code == 200
    assert kept.json()["certificate_thumbprint"] == body["certificate_thumbprint"]


async def test_another_application_needs_a_certificate_of_its_own(api_env: Any) -> None:
    """A2-29: a certificate is never "configured" for an app nobody uploaded it to."""
    _org, headers = await _admin(api_env)
    first = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    other = {**ON, "app_client_id": str(uuid4())}
    refused = await api_env.client.put(API, headers=headers, json=other)
    assert refused.status_code == 422
    renewed = await api_env.client.put(
        API, headers=headers, json={**other, "new_certificate": True}
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["certificate_thumbprint"] != first.json()["certificate_thumbprint"]
    assert renewed.json()["next_certificate"] is None, "a new application's first goes into use"


async def test_a_new_certificate_waits_beside_the_one_in_use(api_env: Any) -> None:
    """Decision 5 (f2-2 I-1): after `new_certificate` the key in use keeps
    signing -- an exchange still passes a Microsoft that trusts only the old
    certificate -- until `activate_certificate`; then the new key signs, and
    the old one is gone from the vault."""
    org, headers = await _admin(api_env)
    first = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    old = first.json()
    [old_key] = await _keys(api_env, org)
    microsoft = FakeMicrosoft()
    microsoft.trust(old["certificate"])

    made = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert made.status_code == 200, made.text
    waiting = made.json()
    assert waiting["certificate_thumbprint"] == old["certificate_thumbprint"]
    assert waiting["next_certificate_thumbprint"] not in (None, old["certificate_thumbprint"])
    assert waiting["next_certificate_expires_at"] is not None
    assert "PRIVATE" not in made.text
    assert await _exchange_passes(api_env, org, microsoft), "the key in use still signs"

    switched = await api_env.client.put(
        API, headers=headers, json={**ON, "activate_certificate": True}
    )
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert body["certificate"] == waiting["next_certificate"]
    assert body["certificate_thumbprint"] == waiting["next_certificate_thumbprint"]
    assert (body["next_certificate"], body["next_certificate_thumbprint"]) == (None, None)
    assert not await _exchange_passes(api_env, org, microsoft), "Entra holds only the old one"
    microsoft.trust(waiting["next_certificate"])
    assert await _exchange_passes(api_env, org, microsoft)
    keys = await _keys(api_env, org)
    assert old_key not in keys and len(keys) == 1
    async with AsyncSession(api_env.admin_engine) as s:
        acts = [
            e.meta["certificate"]
            for e in (
                await s.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org,
                        AuditEvent.action == "auth.entra_settings_updated",
                    )
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        ]
    assert [act.get("made") for act in acts] == ["in_use", "waiting", None]
    assert acts[2] == {
        "activated": waiting["next_certificate_thumbprint"],
        "retired": old["certificate_thumbprint"],
    }


async def test_switching_needs_a_waiting_certificate_and_a_save_of_its_own(
    api_env: Any,
) -> None:
    _org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    nothing = await api_env.client.put(
        API, headers=headers, json={**ON, "activate_certificate": True}
    )
    assert nothing.status_code == 422, "nothing waits to be switched to"
    both = await api_env.client.put(
        API, headers=headers, json={**ON, "new_certificate": True, "activate_certificate": True}
    )
    assert both.status_code == 422, "the upload to Entra goes between the two"
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    other = await api_env.client.put(
        API,
        headers=headers,
        json={**ON, "app_client_id": str(uuid4()), "activate_certificate": True},
    )
    assert other.status_code == 422, "the waiting certificate is this application's"
    again = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert again.status_code == 200, "a second renewal replaces the waiting one"


async def test_clearing_the_application_drops_its_certificates_and_keys(api_env: Any) -> None:
    """f2-2 M7: an admin can unconfigure. Both pairs were made for that
    application, so both go, keys included."""
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert len(await _keys(api_env, org)) == 2
    waiting = (await api_env.client.get(API, headers=headers)).json()
    cleared = await api_env.client.put(API, headers=headers, json=OFF)
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["configured"] is False
    assert (body["certificate"], body["next_certificate"]) == (None, None)
    assert await _keys(api_env, org) == []
    async with AsyncSession(api_env.admin_engine) as s:
        last = (
            (
                await s.execute(
                    select(AuditEvent.meta)
                    .where(
                        AuditEvent.org_id == org,
                        AuditEvent.action == "auth.entra_settings_updated",
                    )
                    .order_by(AuditEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
    assert last is not None
    assert last["certificate"] == {
        "dropped": [waiting["certificate_thumbprint"], waiting["next_certificate_thumbprint"]]
    }


async def test_two_saves_at_once_take_turns(api_env: Any) -> None:
    """N-M2 (second re-review): an activation and a renewal at the same
    moment. `save` locks the row, so the renewal waits for the activation and
    reads what it wrote; the key in use always matches the certificate in
    use. Unlocked, the renewal reads the row as it was: here, with the
    activation's write already made, its own write then fails on
    `ck_auth_entra_settings_next_certificate` (red); had both read before
    either wrote, it would have re-encrypted the credential being promoted,
    leaving its key under the activated certificate, which Entra refuses."""
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    async with AsyncSession(api_env.admin_engine) as s:
        actor = await s.scalar(select(User.id).where(User.org_id == org).limit(1))
    vault = api_env.app.state.vault
    values: dict[str, Any] = {
        "org_id": org,
        "actor_id": actor,
        "enabled": True,
        "app_client_id": APP,
        "staff_tenant_id": TENANT,
    }

    async def renew() -> entra_settings.EntraConfig:
        async with org_session(
            org, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            return await entra_settings.save(session, vault, new_certificate=True, **values)

    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as first:
        await entra_settings.save(first, vault, activate_certificate=True, **values)
        second = asyncio.create_task(renew())
        await asyncio.sleep(0.5)
        assert not second.done(), "the renewal waits for the activation's lock"
    renewed = await second
    assert renewed.pending
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
        private_pem = await entra_settings.signing_key(
            session, vault, org_id=org, config=config, actor_id=None
        )
    assert config.certificate_pem is not None
    certificate = x509.load_pem_x509_certificate(config.certificate_pem.encode())
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    spki = (serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert private.public_key().public_bytes(*spki) == certificate.public_key().public_bytes(*spki)


def test_an_expired_certificate_is_not_usable() -> None:
    """Decision 5: with the certificate in use expired, Task 9 refuses before
    sending anyone to Microsoft (its 409 `microsoft_unavailable`)."""
    live = entra_settings.EntraConfig(
        enabled=True,
        app_client_id=APP,
        credential_id=uuid4(),
        certificate_pem="-----BEGIN CERTIFICATE-----",
        certificate_thumbprint="x",
        certificate_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert live.usable
    expired = replace(live, certificate_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert expired.configured and not expired.usable


async def test_the_key_cannot_be_checked_out_rotated_or_deleted_elsewhere(api_env: Any) -> None:
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    async with AsyncSession(api_env.admin_engine) as s:
        credential_id = await s.scalar(
            select(Credential.id).where(
                Credential.org_id == org, Credential.type == ENTRA_CREDENTIAL_TYPE
            )
        )
    path = f"/api/v1/credentials/{credential_id}"
    checkout = await api_env.client.post(f"{path}/checkout", headers=headers, json={"reason": "x"})
    assert checkout.status_code == 409
    assert "PRIVATE" not in checkout.text
    rotated = await api_env.client.put(
        path, headers=headers, json={"name": "entra-sign-in", "secret": "replaced"}
    )
    assert rotated.status_code == 409
    deleted = await api_env.client.delete(path, headers=headers)
    assert deleted.status_code == 409


async def test_a_personal_account_tenant_is_refused(api_env: Any) -> None:
    _org, headers = await _admin(api_env)
    refused = await api_env.client.put(
        API,
        headers=headers,
        json={**ON, "staff_tenant_id": str(CONSUMERS), "new_certificate": True},
    )
    assert refused.status_code == 422


async def test_reading_needs_orgs_manage(api_env: Any) -> None:
    org = await api_env.create_org("entra-settings-read")
    sub = await api_env.create_user(org, "technician")
    got = await api_env.client.get(API, headers=await api_env.headers(org, sub))
    assert got.status_code == 403


async def test_orgs_manage_on_one_client_is_not_enough_to_save(api_env: Any) -> None:
    """B2-08: the write needs the code on EVERY client, like Email and
    Sessions. An org-admin of one client reads, and is refused the save."""
    org = await api_env.create_org("entra-settings-one-client")
    sub = await api_env.create_user(org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org, name="Acme", kind="customer")
        s.add(client)
        await s.flush()
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "org-admin", Role.org_id.is_(None))
        )
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role_id, client_id=client.id))
        await s.commit()
    headers = await api_env.headers(org, sub)
    got = await api_env.client.get(API, headers=headers)
    assert got.status_code == 200, got.text
    put = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert put.status_code == 403


# --------------------------------------------------------------------------- #
# The three properties the review of Task 1 raised (0246's `credential_id`,
# its CHECKs, and a NULL `staff_tenant_id`), each shown refusing.
# --------------------------------------------------------------------------- #


async def _used(env: Any, org: Any) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org,
                        AuditEvent.action == entra_settings.USED_ACTION,
                    )
                )
            ).scalars()
        )


async def test_each_use_of_the_key_is_audited(api_env: Any) -> None:
    """Invariant 4: the private key is opened for one assertion and the
    opening is recorded, with the certificate it signs for and no key."""
    org, headers = await _admin(api_env)
    saved = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    assert await _used(api_env, org) == []
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
        pem = await entra_settings.signing_key(
            session, api_env.app.state.vault, org_id=org, config=config, actor_id=None
        )
    assert "BEGIN PRIVATE KEY" in pem
    [event] = await _used(api_env, org)
    assert (event.actor_type, event.target_id) == ("system", str(config.credential_id))
    assert event.meta == {
        "purpose": "sign_in",
        "thumbprint": saved.json()["certificate_thumbprint"],
    }


async def test_a_key_of_another_organization_fails_closed(api_env: Any) -> None:
    """`credential_id` references `credentials.id` with no organization check
    (a foreign key ignores RLS). The key is read only under the
    organization's own RLS session, by organization and type, so a row
    naming another organization's key opens nothing and records nothing."""
    org, headers = await _admin(api_env)
    other = await api_env.create_org("entra-settings-other")
    other_headers = await api_env.headers(other, await api_env.create_user(other, "org-admin"))
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    await api_env.client.put(API, headers=other_headers, json={**ON, "new_certificate": True})
    [foreign] = await _keys(api_env, other)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
            .values(credential_id=foreign)
        )
        await s.commit()
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
        assert config.credential_id == foreign
        with pytest.raises(LookupError):
            await entra_settings.signing_key(
                session, api_env.app.state.vault, org_id=org, config=config, actor_id=None
            )
    assert await _used(api_env, org) == []
    assert await _used(api_env, other) == []


async def test_a_credential_that_is_not_a_sign_in_key_is_not_opened(api_env: Any) -> None:
    """The type half: a row naming another credential of the same
    organization -- here a stored password -- does not open it."""
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    made = await api_env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": "router", "type": "password", "secret": "not-a-key"},
    )
    assert made.status_code == 201, made.text
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
            .values(credential_id=made.json()["id"])
        )
        await s.commit()
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
        with pytest.raises(LookupError):
            await entra_settings.signing_key(
                session, api_env.app.state.vault, org_id=org, config=config, actor_id=None
            )
    assert await _used(api_env, org) == []


async def test_a_session_that_is_not_the_organizations_is_refused(api_env: Any) -> None:
    """Never through the admin (RLS-bypassing) engine: a session that is not
    an `org_session` is refused before anything is read; another
    organization's `org_session` finds nothing (RLS)."""
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    async with AsyncSession(api_env.admin_engine) as session:
        config = await entra_settings.read(session, org)
        assert config.credential_id is not None
        with pytest.raises(RuntimeError):
            await entra_settings.signing_key(
                session, api_env.app.state.vault, org_id=org, config=config, actor_id=None
            )
    async with org_session(
        await api_env.create_org("entra-settings-elsewhere"),
        engine=api_env.app_engine,
        clients=ClientScope.all(),
    ) as session:
        with pytest.raises(LookupError):
            await entra_settings.signing_key(
                session, api_env.app.state.vault, org_id=org, config=config, actor_id=None
            )
    assert await _used(api_env, org) == []


async def test_neither_key_is_edited_or_deleted_through_credentials(api_env: Any) -> None:
    """Deleting the key in use or the waiting one would fail on a CHECK by
    design (0246; decisions 3 and 5). `/credentials` refuses both -- a rename
    too -- with a 409, never a 500, and the settings stay as they were."""
    org, headers = await _admin(api_env)
    await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    before = await api_env.client.put(API, headers=headers, json={**ON, "new_certificate": True})
    keys = await _keys(api_env, org)
    assert len(keys) == 2
    for credential_id in keys:
        path = f"/api/v1/credentials/{credential_id}"
        renamed = await api_env.client.put(path, headers=headers, json={"name": "renamed"})
        assert renamed.status_code == 409, renamed.text
        deleted = await api_env.client.delete(path, headers=headers)
        assert deleted.status_code == 409, deleted.text
        shown = await api_env.client.get(path, headers=headers)
        assert shown.status_code == 200 and shown.json()["name"] == "entra-sign-in"
    assert sorted(await _keys(api_env, org)) == sorted(keys)
    assert (await api_env.client.get(API, headers=headers)).json() == before.json()


async def test_a_null_staff_tenant_stays_null(api_env: Any) -> None:
    """Switched on with no tenant for technicians is a valid save (contacts
    only), and reads back null: nothing fills it in or widens it."""
    org, headers = await _admin(api_env)
    saved = await api_env.client.put(
        API, headers=headers, json={**ON, "staff_tenant_id": None, "new_certificate": True}
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["enabled"], saved.json()["staff_tenant_id"]) == (True, None)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        config = await entra_settings.read(session, org)
    assert config.usable and config.staff_tenant_id is None

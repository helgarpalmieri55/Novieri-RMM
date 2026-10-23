"""Tests for the provisioning registry service (docs/modules/provisioning/spec.md).

Mirrors backend/tests/test_backup_repository.py's fixtures and style. The
security-critical assertions are the secret-absence ones (c): the plaintext
domain-join / local-admin passwords must never land in the persisted
`post_config` JSON nor in any audit event's metadata — only the
`{"__sealed__": ...}` vault reference may appear.
"""

import copy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Organization
from app.modules.provisioning.repository import (
    SEALED_MARKER,
    ProvisioningValidationError,
    create_image,
    get_image,
    get_profile,
    list_images,
    list_profiles,
    upsert_profile,
)


async def _org(admin_engine: AsyncEngine) -> tuple[object, object]:
    slug = f"prov-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id, uuid4()


# ---------------------------------------------------------------------------
# create_image: per-OS kind validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "os_family,kind",
    [("windows", "image"), ("linux", "build"), ("macos", "enrollment")],
)
async def test_create_image_accepts_matching_kind(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, os_family: str, kind: str
) -> None:
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family=os_family,
            name=f"{os_family} image",
            kind=kind,
            params={"v": 1},
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
    assert image.os_family == os_family
    assert image.kind == kind
    assert image.state == "active"


@pytest.mark.parametrize(
    "os_family,kind",
    [("windows", "build"), ("linux", "image"), ("macos", "image")],
)
async def test_create_image_rejects_mismatched_kind(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, os_family: str, kind: str
) -> None:
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(ProvisioningValidationError):
            await create_image(
                session,
                org_id=org_id,
                os_family=os_family,
                name="bad",
                kind=kind,
                params={},
                actor_id=actor_id,
            )


async def test_create_image_rejects_unmapped_os_family(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(ProvisioningValidationError):
            await create_image(
                session,
                org_id=org_id,
                os_family="bsd",
                name="bad",
                kind="image",
                params={},
                actor_id=actor_id,
            )


async def test_create_image_audits_without_secrets(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="windows",
            name="Windows 11 Pro",
            kind="image",
            params={"version": "21h2"},
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "provisioning.image_created",
                )
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == actor_id
    assert event.target_id == str(image.id)
    assert event.meta["name"] == "Windows 11 Pro"
    assert event.meta["os_family"] == "windows"
    assert event.meta["kind"] == "image"


# ---------------------------------------------------------------------------
# upsert_profile: sealing, secret-absence, round-trip, no-mutate, idempotent
# ---------------------------------------------------------------------------


async def _make_image(session: AsyncSession, org_id: object, actor_id: object) -> object:
    image = await create_image(
        session,
        org_id=org_id,
        os_family="windows",
        name="Windows 11 Pro",
        kind="image",
        params={},
        actor_id=actor_id,
        actor_type="user",
    )
    return image


def _post_config() -> dict:
    return {
        "domain_join": {"user": "svc", "password": "s3cret"},
        "local_admin": {"password": "adminpw"},
        "hostname": "WK-{n}",
    }


async def test_upsert_profile_seals_credential_fields(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        post_config = _post_config()
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config=post_config,
            target_scope={"all": True},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(
                select(type(profile)).where(
                    type(profile).org_id == org_id, type(profile).id == profile_id
                )
            )
        ).scalar_one()

    dj_pw = row.post_config["domain_join"]["password"]
    la_pw = row.post_config["local_admin"]["password"]
    assert isinstance(dj_pw, dict) and list(dj_pw.keys()) == [SEALED_MARKER]
    assert isinstance(la_pw, dict) and list(la_pw.keys()) == [SEALED_MARKER]
    assert dj_pw[SEALED_MARKER] != "s3cret"
    assert la_pw[SEALED_MARKER] != "adminpw"
    # non-secret fields untouched
    assert row.post_config["domain_join"]["user"] == "svc"
    assert row.post_config["hostname"] == "WK-{n}"


async def test_upsert_profile_secret_absence_in_audit_and_persisted_json(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.org_id == org_id)
            )
        ).scalars().all()
        row = (
            await session.execute(
                select(type(profile)).where(type(profile).id == profile_id)
            )
        ).scalar_one()

    for event in events:
        meta_str = str(event.meta)
        assert "s3cret" not in meta_str
        assert "adminpw" not in meta_str

    persisted_str = str(row.post_config)
    assert "s3cret" not in persisted_str
    assert "adminpw" not in persisted_str


async def test_upsert_profile_seal_roundtrips(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id
        dj_sealed = profile.post_config["domain_join"]["password"][SEALED_MARKER]
        la_sealed = profile.post_config["local_admin"]["password"][SEALED_MARKER]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        dj_plain = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=dj_sealed
        )
        la_plain = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=la_sealed
        )
    assert dj_plain == b"s3cret"
    assert la_plain == b"adminpw"


async def test_upsert_profile_does_not_mutate_caller_dict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        original = _post_config()
        original_copy = copy.deepcopy(original)
        await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config=original,
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

    assert original == original_copy
    assert original["domain_join"]["password"] == "s3cret"
    assert original["local_admin"]["password"] == "adminpw"


async def test_upsert_profile_idempotent_reseal_on_already_sealed_marker(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id
        first_marker = profile.post_config["domain_join"]["password"][SEALED_MARKER]
        # simulate loading the persisted profile back and re-saving it (e.g. an
        # edit that only touches an unrelated field) — the already-sealed
        # marker must be left as-is, not re-sealed.
        reloaded_post_config = copy.deepcopy(profile.post_config)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        updated = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11 v2",
            image_id=image.id,
            post_config=reloaded_post_config,
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
            profile_id=profile_id,
        )
        await session.commit()

    second_marker = updated.post_config["domain_join"]["password"][SEALED_MARKER]
    assert second_marker == first_marker
    assert updated.name == "Standard Win11 v2"


async def test_upsert_profile_rejects_non_string_credential_dict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        with pytest.raises(ProvisioningValidationError):
            await upsert_profile(
                session,
                vault,
                org_id=org_id,
                name="Standard Win11",
                image_id=image.id,
                post_config={"domain_join": {"password": {"note": "x"}}},
                target_scope={},
                enabled=True,
                actor_id=actor_id,
                actor_type="user",
            )
        await session.rollback()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await list_profiles(session, org_id) == []


async def test_upsert_profile_rejects_non_string_credential_int(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        with pytest.raises(ProvisioningValidationError):
            await upsert_profile(
                session,
                vault,
                org_id=org_id,
                name="Standard Win11",
                image_id=image.id,
                post_config={"local_admin": {"password": 123456}},
                target_scope={},
                enabled=True,
                actor_id=actor_id,
                actor_type="user",
            )
        await session.rollback()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await list_profiles(session, org_id) == []


async def test_upsert_profile_edit_preserves_sealed_credential_when_password_not_resupplied(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Editing settings (e.g. renaming, changing the domain) without
    re-typing the domain-join / local-admin password must NOT wipe the
    previously-sealed credential. The API redacts sealed markers from
    ProfileOut.post_config, so the editor round-trips a payload where the
    outer credential container is present but the password field is simply
    absent — that must be treated as "keep the existing credential", not
    "clear it"."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp", "username": "svc", "password": "s3cret"},
                "local_admin": {"password": "adminpw"},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    # Simulate the API+editor round-trip: sealed markers are redacted from
    # ProfileOut, so the PUT body has the containers present but no password.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        updated = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11 v2",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp2", "username": "svc"},
                "local_admin": {},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
            profile_id=profile_id,
        )
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(
                select(type(updated)).where(
                    type(updated).org_id == org_id, type(updated).id == profile_id
                )
            )
        ).scalar_one()

    dj_pw = row.post_config["domain_join"]["password"]
    la_pw = row.post_config["local_admin"]["password"]
    assert isinstance(dj_pw, dict) and list(dj_pw.keys()) == [SEALED_MARKER]
    assert isinstance(la_pw, dict) and list(la_pw.keys()) == [SEALED_MARKER]
    assert row.post_config["domain_join"]["domain"] == "corp2"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        dj_plain = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=dj_pw[SEALED_MARKER]
        )
        la_plain = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=la_pw[SEALED_MARKER]
        )
    assert dj_plain == b"s3cret"
    assert la_plain == b"adminpw"


async def test_upsert_profile_edit_can_replace_credential_with_new_password(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Supplying a new plaintext password on edit reseals to the new value,
    it does not preserve the old sealed credential."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp", "username": "svc", "password": "s3cret"},
                "local_admin": {"password": "adminpw"},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        updated = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp", "username": "svc", "password": "newpw"},
                "local_admin": {"password": "adminpw"},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
            profile_id=profile_id,
        )
        await session.commit()

    dj_sealed = updated.post_config["domain_join"]["password"][SEALED_MARKER]
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        dj_plain = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=dj_sealed
        )
    assert dj_plain == b"newpw"


async def test_upsert_profile_edit_dropping_container_does_not_resurrect_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """If the operator intentionally removes the whole `local_admin`
    container on edit, the credential must stay gone — it must not be
    resurrected from the old stored config."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_id, actor_id)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp", "username": "svc", "password": "s3cret"},
                "local_admin": {"password": "adminpw"},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        updated = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={
                "domain_join": {"domain": "corp", "username": "svc"},
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
            profile_id=profile_id,
        )
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(
                select(type(updated)).where(
                    type(updated).org_id == org_id, type(updated).id == profile_id
                )
            )
        ).scalar_one()

    assert "local_admin" not in row.post_config


async def test_upsert_profile_update_missing_or_cross_org_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org(admin_engine)
    org_b, actor_b = await _org(admin_engine)

    # a random, wholly nonexistent profile_id
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_a, actor_a)
        with pytest.raises(ProvisioningValidationError):
            await upsert_profile(
                session,
                vault,
                org_id=org_a,
                name="Standard Win11",
                image_id=image.id,
                post_config=_post_config(),
                target_scope={},
                enabled=True,
                actor_id=actor_a,
                actor_type="user",
                profile_id=uuid4(),
            )
        await session.rollback()

    # a real profile_id, but belonging to a different org
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        image_a = await _make_image(session, org_a, actor_a)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_a,
            name="Standard Win11",
            image_id=image_a.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_a,
            actor_type="user",
        )
        await session.commit()
        profile_id = profile.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        image_b = await _make_image(session, org_b, actor_b)
        with pytest.raises(ProvisioningValidationError):
            await upsert_profile(
                session,
                vault,
                org_id=org_b,
                name="Hijacked",
                image_id=image_b.id,
                post_config=_post_config(),
                target_scope={},
                enabled=True,
                actor_id=actor_b,
                actor_type="user",
                profile_id=profile_id,
            )
        await session.rollback()


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_another_orgs_image_and_profile_are_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        image = await _make_image(session, org_a, actor_a)
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_a,
            name="Standard Win11",
            image_id=image.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_a,
            actor_type="user",
        )
        await session.commit()
        image_id = image.id
        profile_id = profile.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        assert await get_image(session, org_b, image_id) is None
        assert await get_profile(session, org_b, profile_id) is None
        assert await list_images(session, org_b) == []
        assert await list_profiles(session, org_b) == []

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        assert await get_image(session, org_a, image_id) is not None
        assert await get_profile(session, org_a, profile_id) is not None
        assert len(await list_images(session, org_a)) == 1
        assert len(await list_profiles(session, org_a)) == 1


async def test_the_image_and_profile_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession, tmp_path: Path
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    The test above drives the same four reads through `org_session` on the
    NOBYPASSRLS `app_user` role: `0002_rls.py`'s forced `tenant_isolation`
    policy answers before `OsImage.org_id == org_id` or
    `ProvisionProfile.org_id == org_id` is consulted, so it is green with all
    four predicates deleted. `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here they are the only scoping.

    BOTH ORGS HOLD A ROW, so each assertion is a difference between two tenants
    rather than a query that returned nothing -- an empty answer is what a
    broken fixture looks like too.

    A PROFILE IS NOT AN INERT RECORD. `upsert_profile` seals provisioning
    credentials into it (invariant 3), and the profile is what decides how a
    machine gets built; a read that crosses tenants here is a read of another
    customer's build recipe.

    FALSIFIED, one at a time: deleting the predicate from `list_images`,
    `get_image`, `list_profiles` or `get_profile` turns exactly one of the four
    assertion pairs below red.
    """
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org(admin_engine)
    org_b, actor_b = await _org(admin_engine)

    images: dict[str, object] = {}
    profiles: dict[str, object] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        image = await _make_image(bypassrls_session, org_id, actor_id)
        images[key] = image.id  # type: ignore[attr-defined]
        profile = await upsert_profile(
            bypassrls_session,
            vault,
            org_id=org_id,
            name=f"Standard {key}",
            image_id=image.id,  # type: ignore[attr-defined]
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        profiles[key] = profile.id
    await bypassrls_session.commit()

    assert [i.id for i in await list_images(bypassrls_session, org_a)] == [images["a"]]
    assert [i.id for i in await list_images(bypassrls_session, org_b)] == [images["b"]]
    assert await get_image(bypassrls_session, org_a, images["b"]) is None
    assert await get_image(bypassrls_session, org_a, images["a"]) is not None

    assert [p.id for p in await list_profiles(bypassrls_session, org_a)] == [profiles["a"]]
    assert [p.id for p in await list_profiles(bypassrls_session, org_b)] == [profiles["b"]]
    assert await get_profile(bypassrls_session, org_a, profiles["b"]) is None
    assert await get_profile(bypassrls_session, org_a, profiles["a"]) is not None

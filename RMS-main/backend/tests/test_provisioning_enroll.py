"""Tests for enrollment correlation + signed `provision.enroll` post-config
dispatch (docs/modules/provisioning/spec.md §7/§9).

(a) drives the REAL `app.services.enrollment.enroll` path (mirrors
`test_enrollment.py::test_enroll_happy_path`'s CSR/DeviceFacts construction)
with a token issued by `start_provision_run`, and asserts the matching
`ProvisionRun` advances `awaiting_enrollment -> enrolled` with `device_id`
bound and a `provisioning.enrolled` audit recorded.

(b) is the additive-hook regression: an ORDINARY token (issued directly via
`create_token`, not tied to any run) must leave `bind_provisioned_device`
returning `None` and touch no `ProvisionRun` row, while the real enrollment
still succeeds unchanged.

(c) drives `dispatch_provision_enroll` on the enrolled run: exactly one
signed `provision.enroll` job targeting `run.device_id`, `run.job_id` set, a
`provisioning.enroll_dispatched` audit, and — the security-critical
assertion — the dispatched job's `post_config` carries neither a
`{"__sealed__": ...}` reference nor the plaintext credentials planted on the
profile.

(d) is tenancy: neither function may reach across org boundaries.

A `_FakeJobEngine` (mirrors `tests/test_backup_dispatch.py`) stands in for
the kernel job engine so these tests exercise `enroll.py`'s own dispatch
logic without requiring `provision.enroll` to be registered in the job-type
registry (that registration is a later task's scope, like `backup.restore`'s
in the backup module).
"""

import datetime as _dt
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ca import LocalCA
from app.core.config import Settings
from app.core.signing import LocalEd25519Signer
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Device, Job, Organization, ProvisionRun, User
from app.modules.provisioning.dispatch import start_provision_run
from app.modules.provisioning.enroll import bind_provisioned_device, dispatch_provision_enroll
from app.modules.provisioning.repository import create_image, upsert_profile
from app.services.enrollment import DeviceFacts, create_token, enroll

ADMIN_PASSWORD = "s3cret-admin-pw"
DOMAIN_PASSWORD = "domain-s3cret"


def make_csr_pem() -> str:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "enrolling-agent")]))
        .sign(key, None)
    )
    return csr.public_bytes(Encoding.PEM).decode()


FACTS = DeviceFacts(
    hostname="wks-001",
    os_family="windows",
    os_version="10",
    architecture="x86_64",
)


class _FakeJobEngine:
    """Mirrors `tests/test_backup_dispatch.py::_FakeJobEngine`: a minimal
    stand-in for `app.jobs.engine.JobEngine` that satisfies `_JobEngineLike`
    without requiring `provision.enroll` to be registered in the job-type
    registry."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session: AsyncSession, **kw):
        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        job.signature = "fake-signature"
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job: Job) -> dict:
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


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
        "domain_join": {"user": "svc", "password": DOMAIN_PASSWORD},
        "local_admin": {"password": ADMIN_PASSWORD},
        "hostname": "WK-0001",
    }


async def _org_and_admin(admin_engine: AsyncEngine) -> tuple[object, object]:
    slug = f"provenr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        session.add(user)
        await session.flush()
        await session.commit()
        return org.id, user.id


async def _make_windows_profile(
    session: AsyncSession, vault: LocalVaultService, org_id: object, actor_id: object
) -> object:
    image = await create_image(
        session,
        org_id=org_id,
        os_family="windows",
        name="windows image",
        kind="image",
        params={},
        actor_id=actor_id,
        actor_type="user",
    )
    return await upsert_profile(
        session,
        vault,
        org_id=org_id,
        name="windows profile",
        image_id=image.id,
        post_config=_post_config(),
        target_scope={},
        enabled=True,
        actor_id=actor_id,
        actor_type="user",
    )


def _extract_token_from_unattend(answer_bytes: bytes) -> str:
    """The rendered windows unattend.xml embeds the FULL issued token
    (`"<token_id>.<secret>"`) verbatim in its enroll command line — this is
    the one place the plaintext token exists outside the (hashed) DB row, so
    the test recovers it from there rather than needing `start_provision_run`
    to return it directly (it deliberately does not — the token is scoped to
    the answer object only, per invariant #3)."""
    match = re.search(r"--token ([^<\s]+)", answer_bytes.decode())
    assert match, "token not found in rendered answer file"
    return match.group(1)


async def _start_windows_run(
    app_engine: AsyncEngine,
    object_store: ObjectStore,
    vault: LocalVaultService,
    signer: LocalEd25519Signer,
    *,
    org_id: object,
    actor_id: object,
    now: datetime,
) -> tuple[object, str]:
    """Create a windows profile, start a provisioning run, and return
    (run_id, plaintext_token) with the run committed in `awaiting_enrollment`."""
    async with org_session(org_id, engine=app_engine) as session:
        profile = await _make_windows_profile(session, vault, org_id, actor_id)
        await session.commit()
        profile_id = profile.id

    async with org_session(org_id, engine=app_engine) as session:
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
        answer_ref = run.answer_ref

    answer_bytes = await object_store.get_provision_object(answer_ref)
    token = _extract_token_from_unattend(answer_bytes)
    return run_id, token


# ---------------------------------------------------------------------------
# (a) real enroll() with a provisioning token advances the run
# ---------------------------------------------------------------------------


async def test_bind_provisioned_device_advances_run_on_real_enroll(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    signer = LocalEd25519Signer(tmp_path / "sign.key")
    ca = LocalCA(tmp_path / "ca")
    org_id, actor_id = await _org_and_admin(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        run_id, token = await _start_windows_run(
            app_engine, object_store, vault, signer, org_id=org_id, actor_id=actor_id, now=now
        )

    result = await enroll(
        admin_engine=admin_engine,
        app_engine=app_engine,
        ca=ca,
        signer=signer,
        gateway_url="wss://gw.test/agent",
        token=token,
        csr_pem=make_csr_pem(),
        facts=FACTS,
    )

    async with org_session(org_id, engine=app_engine) as session:
        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
        ).scalar_one()
        assert run.state == "enrolled"
        assert run.device_id == result.device_id

        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "provisioning.enrolled",
                )
            )
        ).scalar_one()
        assert event.meta["provision_run_id"] == str(run_id)
        assert event.meta["device_id"] == str(result.device_id)


# ---------------------------------------------------------------------------
# (b) ordinary (non-provisioning) token: strict no-op
# ---------------------------------------------------------------------------


async def test_ordinary_token_enrollment_is_a_noop_for_provisioning(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    signer = LocalEd25519Signer(tmp_path / "sign.key")
    ca = LocalCA(tmp_path / "ca")
    org_id, actor_id = await _org_and_admin(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    async with org_session(org_id, engine=app_engine) as session:
        issued = await create_token(
            session,
            org_id=org_id,
            created_by=actor_id,
            name="ordinary",
            expires_in_hours=24,
            # `max_uses=None` UNTIL TODAY, and `None` meant unlimited. The
            # kernel route now requires a finite, capped value and
            # `create_token` refuses anything else, so an "ordinary" token is
            # the smallest one that still enrols a machine.
            max_uses=1,
        )
        await session.commit()
        token_id = issued.id

    result = await enroll(
        admin_engine=admin_engine,
        app_engine=app_engine,
        ca=ca,
        signer=signer,
        gateway_url="wss://gw.test/agent",
        token=issued.token,
        csr_pem=make_csr_pem(),
        facts=FACTS,
    )

    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.org_id == org_id))
        ).scalar_one()
        assert device.id == result.device_id

        # No ProvisionRun exists in this org at all: the hook found nothing
        # to bind, and calling it again directly confirms the None contract.
        run_count = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.org_id == org_id))
        ).scalars().all()
        assert run_count == []

        bound = await bind_provisioned_device(
            session,
            org_id=org_id,
            enrollment_token_id=token_id,
            device_id=result.device_id,
            now=now,
        )
        assert bound is None

        assert (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "provisioning.enrolled",
                )
            )
        ).scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# (c) dispatch_provision_enroll: one signed job, non-secret post_config
# ---------------------------------------------------------------------------


async def test_dispatch_provision_enroll_builds_signed_job_without_secrets(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    signer = LocalEd25519Signer(tmp_path / "sign.key")
    ca = LocalCA(tmp_path / "ca")
    org_id, actor_id = await _org_and_admin(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        run_id, token = await _start_windows_run(
            app_engine, object_store, vault, signer, org_id=org_id, actor_id=actor_id, now=now
        )

    result = await enroll(
        admin_engine=admin_engine,
        app_engine=app_engine,
        ca=ca,
        signer=signer,
        gateway_url="wss://gw.test/agent",
        token=token,
        csr_pem=make_csr_pem(),
        facts=FACTS,
    )

    job_engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine) as session:
        pending = await dispatch_provision_enroll(
            session,
            job_engine,
            org_id=org_id,
            run_id=run_id,
            now=now,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()

    assert len(pending) == 1
    job_id, device_id, envelope = pending[0]
    assert device_id == result.device_id
    assert envelope["job"]["id"] == str(job_id)

    async with org_session(org_id, engine=app_engine) as session:
        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
        ).scalar_one()
        assert run.job_id == job_id

        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert job.type == "provision.enroll"
        assert job.target_id == device_id

        post_config = job.params["post_config"]
        post_config_str = str(post_config)
        assert "__sealed__" not in post_config_str
        assert ADMIN_PASSWORD not in post_config_str
        assert DOMAIN_PASSWORD not in post_config_str
        # non-secret fields survive the strip
        assert post_config["hostname"] == "WK-0001"
        assert "user" in post_config["domain_join"]
        assert "password" not in post_config["domain_join"]
        assert "password" not in post_config["local_admin"]

        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "provisioning.enroll_dispatched",
                )
            )
        ).scalar_one()
        assert event.meta["provision_run_id"] == str(run_id)
        assert event.meta["job_id"] == str(job_id)
        meta_str = str(event.meta)
        assert ADMIN_PASSWORD not in meta_str
        assert DOMAIN_PASSWORD not in meta_str
        assert "__sealed__" not in meta_str


# ---------------------------------------------------------------------------
# (d) tenancy: cross-org run/token is unreachable
# ---------------------------------------------------------------------------


async def test_bind_and_dispatch_find_nothing_across_the_boundary_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    signer = LocalEd25519Signer(tmp_path / "sign.key")
    org_a, actor_a = await _org_and_admin(admin_engine)
    org_b, actor_b = await _org_and_admin(admin_engine)
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with mock_aws():
        object_store = _object_store(tmp_path)
        run_id, _token = await _start_windows_run(
            app_engine, object_store, vault, signer, org_id=org_a, actor_id=actor_a, now=now
        )

        async with org_session(org_a, engine=app_engine) as session:
            run_a = (
                await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
            ).scalar_one()
            token_id = run_a.enrollment_token_id

        # org B can't bind org A's token to some device id of its own.
        async with org_session(org_b, engine=app_engine) as session:
            bound = await bind_provisioned_device(
                session,
                org_id=org_b,
                enrollment_token_id=token_id,
                device_id=uuid4(),
                now=now,
            )
            assert bound is None

        # org A's run is untouched: still awaiting_enrollment.
        async with org_session(org_a, engine=app_engine) as session:
            reloaded = (
                await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
            ).scalar_one()
            assert reloaded.state == "awaiting_enrollment"
            assert reloaded.device_id is None

        # org B can't dispatch org A's run either (not found under org B).
        job_engine = _FakeJobEngine()
        async with org_session(org_b, engine=app_engine) as session:
            pending = await dispatch_provision_enroll(
                session,
                job_engine,
                org_id=org_b,
                run_id=run_id,
                now=now,
                actor_id=actor_b,
                actor_type="user",
            )
            assert pending == []

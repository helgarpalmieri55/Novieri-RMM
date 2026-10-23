"""Cycle B2 dispatch: the signed jobs, the credential, the host key, the scrub
(spec §4, §6.4, §7.1, §7.3; `CLAUDE.md` invariants #1, #3, 2a; falsifiers 12,
13, 19, 21).

**EACH INVARIANT HAS A TEST THAT CAN FAIL, AND EACH REMOVES EXACTLY ONE THING.**
The dispatch path is exercised at the service boundary (`dispatch_push_batch`)
with a real `JobEngine` signing over canonical bytes, a fake vault whose
`checkout` returns the credential JSON, and a fake object store that presigns --
so a test needs neither S3 nor Redis but the signature, the params and the audit
are the real ones.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.jobs.registry import get_job_type
from app.models import EnrollmentToken, Job, JobStatus
from app.modules.agent_push import host_key as host_key_service
from app.modules.agent_push.dispatch import (
    _parse_credential_secret,
    dispatch_push_batch,
    record_probe_result,
)
from app.modules.agent_push.errors import PushValidationError
from app.modules.agent_push.jobtypes import INSTALL_JOB_TYPE, PROBE_JOB_TYPE
from tests.agent_push_fixtures import (
    seed_agent_release,
    seed_asset,
    seed_confirmed_host_key,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)


@pytest.fixture(autouse=True)
def _register_push_job_types():
    """The kernel registry is process-global and idempotent; the app/worker
    register these at startup, but a service-level test builds no app, so it
    registers them itself."""
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


TARGET = "10.70.0.17"
SEGMENT = "10.70.0.0/24"
FINGERPRINT = "SHA256:AAAAtestkeyfingerprintAAAA"
CRED_JSON = json.dumps({"username": "svc-push", "secret": "hunter2"})
#: `settings.public_console_url` (ITS_PUBLIC_CONSOLE_URL): where a pushed
#: computer enrolls, over SSH as over WinRM (plan W2, superseded 2026-09-29).
CONSOLE = "https://itms.example.com"


class _FakeVault:
    """`checkout` returns the credential's plaintext JSON. Nothing else is used
    by the dispatch path."""

    def __init__(self, plaintext: str) -> None:
        self.plaintext = plaintext
        self.calls = 0

    async def checkout(self, session, *, org_id, credential_id) -> str:
        self.calls += 1
        return self.plaintext


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def presign_agent_release_get(self, version, os_name, arch, *, expires_in) -> str:
        self.calls.append((version, os_name, arch, expires_in))
        return f"https://store.test/{version}/{os_name}/{arch}?sig=x&expires={expires_in}"


def _engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key"))


async def _grant_org_admin(engine, org) -> None:
    """The service checks `credentials.checkout` on the actor (spec §8); grant
    it through `org-admin`, which holds it, on the admin engine."""
    from app.models import Role, UserRole

    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.org_id, user_id=org.user_id, role_id=role.id))
        await session.commit()


async def _world(
    engine,
    tag: str,
    *,
    with_release: bool = True,
    os_family: str = "linux",
    release_os: str = "linux",
    architecture: str = "amd64",
):
    org = await seed_org(engine, tag)
    await _grant_org_admin(engine, org)
    credential_id = await seed_credential(engine, org)
    device_id = await seed_asset(
        engine,
        org.org_id,
        primary_ip=TARGET,
        os_family=os_family,
        architecture=architecture,
    )
    await seed_push_authorization(engine, org, SEGMENT)
    await seed_push_credential_scope(engine, org, credential_id, SEGMENT)
    _ss, ss_device = await seed_push_site_server(engine, org.org_id, segments=[SEGMENT])
    if with_release:
        await seed_agent_release(engine, os_name=release_os, arch=architecture)
    return org, credential_id, device_id, ss_device


async def _dispatch(
    app_engine, org, credential_id, *, kind, vault=None, store=None, tmp_path,
    public_console_url: str | None = CONSOLE,
):
    async with org_session(org.org_id, engine=app_engine) as session:
        return await dispatch_push_batch(
            session,
            _engine(tmp_path),
            (lambda: store) if store is not None else None,
            vault or _FakeVault(CRED_JSON),
            org_id=org.org_id,
            actor_id=org.user_id,
            credential_id=credential_id,
            targets=[TARGET],
            kind=kind,
            public_console_url=public_console_url,
        )


# --------------------------------------------------------------------------- #
# Invariant #1: an install is an ordinary signed job, and it contains no code
# --------------------------------------------------------------------------- #


async def test_an_install_is_a_signed_job_carrying_the_confirmed_key_and_no_more(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, _device_id, ss_device = await _world(admin_engine, "d-ok")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    store = _FakeStore()
    vault = _FakeVault(CRED_JSON)

    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, store=store, tmp_path=tmp_path
    )
    assert result.refused == {}
    assert len(result.dispatched) == 1

    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.type == INSTALL_JOB_TYPE
    # Invariant #1: signed over canonical bytes.
    assert job.signature
    # Targets the SITE SERVER'S device, never the machine being installed onto.
    assert str(job.target_id) == str(ss_device)
    p = job.params
    # The whole instruction, and no code (§4.2).
    assert p["target_address"] == TARGET
    assert p["transport"] == "ssh"
    assert p["credential"] == {"username": "svc-push", "secret": "hunter2"}
    assert p["host_key_fingerprint"] == FINGERPRINT
    assert p["release"]["sha256"] == "a" * 64
    assert p["release"]["url"].startswith("https://store.test/")
    assert p["enroll_token"]
    # Where the pushed machine enrolls: the configured console, never the
    # presigned release URL's origin (W2 superseded). `its-agent install`
    # writes it into /etc/its-agent/agent.env or the launchd plist.
    assert p["enroll_url"] == CONSOLE
    assert not p["release"]["url"].startswith(CONSOLE)
    # Nothing that could carry a command.
    assert set(p) == {
        "target_address", "expected_mac", "transport", "os", "arch",
        "release", "enroll_token", "credential", "host_key_fingerprint", "dns_name",
        "enroll_url",
    }
    # The credential was decrypted exactly once for the whole batch.
    assert vault.calls == 1
    # The presigned window is the 300s install TTL, not the kernel default,
    # and it names the release the server selected for this OS/arch.
    assert len(store.calls) == 1
    _version, os_name, arch, expires = store.calls[0]
    assert (os_name, arch, expires) == ("linux", "amd64", 300)


# --------------------------------------------------------------------------- #
# 2a clause (a): the job targets the chosen site server; §7.1: token bounds
# --------------------------------------------------------------------------- #


async def test_the_enroll_token_is_single_use_and_expires_within_thirty_minutes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Spec §7.1, falsifier 19: a token minted for a push has max_uses 1 and an
    expiry <= 30 minutes -- tighter than the console default because a machine
    delivers it, not a human."""
    from datetime import UTC, datetime, timedelta

    org, credential_id, *_ = await _world(admin_engine, "d-token")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )

    async with AsyncSession(admin_engine) as session:
        token = (
            await session.execute(
                select(EnrollmentToken).where(EnrollmentToken.org_id == org.org_id)
            )
        ).scalar_one()
    assert token.max_uses == 1
    assert token.name.startswith("push:")
    remaining = token.expires_at - datetime.now(UTC)
    assert remaining <= timedelta(minutes=30)
    assert remaining > timedelta(minutes=25)


# --------------------------------------------------------------------------- #
# §7.3, falsifier 21: no confirmed host key => refused, not prompted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("configured", [None, "", "http://itms.example.com"])
async def test_an_ssh_install_with_no_usable_console_url_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, configured
) -> None:
    """An SSH-pushed machine enrolls at ITS_PUBLIC_CONSOLE_URL too. With it
    unset there is nowhere to send the machine, so nothing is decrypted,
    presigned or created."""
    tag = {None: "d-nocon-none", "": "d-nocon-empty"}.get(configured, "d-nocon-http")
    org, credential_id, *_ = await _world(admin_engine, tag)
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    vault = _FakeVault(CRED_JSON)
    store = _FakeStore()
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, store=store,
        tmp_path=tmp_path, public_console_url=configured,
    )
    # Review Minor 2: refused and RECORDED (push.refused), not raised.
    assert result.refused == {TARGET: ["no_console_url"]} and result.dispatched == []
    assert vault.calls == 0
    assert store.calls == []
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).first() is None


async def test_an_install_with_no_confirmed_host_key_is_refused_and_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "d-nokey")
    # No host key confirmed.
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    assert result.refused == {TARGET: ["host_key_unconfirmed"]}
    async with AsyncSession(admin_engine) as session:
        jobs = (
            (await session.execute(select(Job).where(Job.org_id == org.org_id)))
            .scalars()
            .all()
        )
    assert jobs == []


async def test_an_install_against_a_changed_host_key_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Confirmed once, but the latest observation now disagrees: refused, not
    warned about (§7.3)."""
    org, credential_id, device_id, _ss = await _world(admin_engine, "d-changed")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    # A later probe observes a DIFFERENT key.
    async with org_session(org.org_id, engine=app_engine) as session:
        await host_key_service.record_observation(
            session,
            org.org_id,
            address=TARGET,
            transport="ssh",
            fingerprint="SHA256:DIFFERENTkeyBBBB",
            key_type="ssh-ed25519",
            device_id=device_id,
            job_id=None,
        )
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    assert result.refused == {TARGET: ["host_key_changed"]}


# --------------------------------------------------------------------------- #
# The probe fetches the key, a human confirms it, then the install proceeds
# --------------------------------------------------------------------------- #


async def test_a_probe_carries_no_credential_and_its_result_pins_the_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, device_id, _ss = await _world(admin_engine, "d-probe")
    result = await _dispatch(
        app_engine, org, credential_id, kind="probe", tmp_path=tmp_path
    )
    assert result.refused == {}
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.type == PROBE_JOB_TYPE
    assert "credential" not in job.params
    assert "enroll_token" not in job.params

    # The gateway completes the probe; its result pins the observed key.
    async with org_session(org.org_id, engine=app_engine) as session:
        j = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
        await record_probe_result(
            session,
            j,
            {
                "target_address": TARGET,
                "transport": "ssh",
                "host_key_fingerprint": FINGERPRINT,
                "host_key_type": "ssh-ed25519",
            },
        )
    async with org_session(org.org_id, engine=app_engine) as session:
        row = await host_key_service.get_host_key(session, org.org_id, TARGET)
        assert row is not None
        assert row.observed_fingerprint == FINGERPRINT
        assert row.confirmed_fingerprint is None  # not yet confirmed

    # An install is still refused until a human confirms.
    refused = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    assert refused.refused == {TARGET: ["host_key_unconfirmed"]}

    # Confirm, then the install proceeds.
    async with org_session(org.org_id, engine=app_engine) as session:
        await host_key_service.confirm_host_key(
            session, org.org_id, actor_id=org.user_id, address=TARGET, fingerprint=FINGERPRINT
        )
    ok = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    assert ok.refused == {}
    assert len(ok.dispatched) == 1


# --------------------------------------------------------------------------- #
# Invariant #3: redacted on read, scrubbed on terminal
# --------------------------------------------------------------------------- #


async def test_the_redactor_removes_every_bearer_field() -> None:
    """Falsifier 12: `GET /jobs/{id}` must not return the credential, the token
    or the presigned URL. The job type declares the redactor; here it is applied."""
    redactor = get_job_type(INSTALL_JOB_TYPE).params_redactor
    assert redactor is not None
    params = {
        "target_address": TARGET,
        "credential": {"username": "u", "secret": "s"},
        "enroll_token": "T",
        "release": {"version": "1", "sha256": "a" * 64, "url": "https://store/x"},
    }
    safe = redactor(params)
    assert "credential" not in safe
    assert "enroll_token" not in safe
    assert "url" not in safe["release"]
    # The original is untouched: the redactor is pure.
    assert params["credential"] == {"username": "u", "secret": "s"}


async def test_the_credential_is_scrubbed_from_jobs_params_on_a_terminal_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Falsifier 13, spec §6.4: a terminal `agent.push_install` row must retain
    no credential material in `jobs.params`. The scrubber runs at EVERY terminal
    writer; here `complete` (succeeded) is exercised, and the credential is gone.
    """
    org, credential_id, *_ = await _world(admin_engine, "d-scrub")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    job_id = result.dispatched[0].job_id

    # Before terminal: the credential IS in the row (the §6.4 residual, stated).
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert job.params["credential"]["secret"] == "hunter2"

    engine = _engine(tmp_path)
    async with org_session(org.org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.complete(
            session,
            job,
            state="succeeded",
            result={"target_address": TARGET, "outcome": "installed"},
        )

    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.succeeded
    assert "credential" not in job.params, "the credential survived the terminal state"
    assert "enroll_token" not in job.params
    # A SELECT for credential material over terminal push rows returns nothing.
    assert "hunter2" not in json.dumps(job.params)


async def test_the_credential_is_scrubbed_on_a_refused_terminal_state_too(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The scrubber is not hooked to `succeeded` alone: a job the endpoint
    REFUSES must also drop the credential. This is the falsifier the §6.4 residual
    would fail if the scrub lived at one terminal writer instead of all of them.
    """
    org, credential_id, *_ = await _world(admin_engine, "d-scrub-refuse")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
    )
    job_id = result.dispatched[0].job_id
    engine = _engine(tmp_path)
    async with org_session(org.org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.refuse(session, job, reason="target unreachable")
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.refused
    assert "credential" not in job.params


# --------------------------------------------------------------------------- #
# The credential shape, and the empty catalogue
# --------------------------------------------------------------------------- #


def test_a_non_json_credential_secret_is_refused_plainly() -> None:
    with pytest.raises(PushValidationError):
        _parse_credential_secret("not json")
    with pytest.raises(PushValidationError):
        _parse_credential_secret(json.dumps({"username": "u"}))  # no secret
    assert _parse_credential_secret(CRED_JSON) == ("svc-push", "hunter2")


async def test_an_install_with_no_published_release_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Spec §1.3 item 4: a push has nothing to install until an artefact is
    published for the target's OS."""
    # A darwin/arm64 asset with no darwin/arm64 release published. `agent_releases`
    # is global and accumulates, so the ABSENT combination must be one nothing
    # else in the suite seeds -- the push tests only ever seed linux releases.
    org, credential_id, *_ = await _world(
        admin_engine,
        "d-norelease",
        with_release=False,
        os_family="macos",
        architecture="arm64",
    )
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint=FINGERPRINT)
    with pytest.raises(PushValidationError) as exc:
        await _dispatch(
            app_engine, org, credential_id, kind="install", store=_FakeStore(), tmp_path=tmp_path
        )
    assert "no published agent release" in str(exc.value)


def test_transport_accepts_winrm_and_dns_name_is_optional() -> None:
    from app.modules.agent_push.jobtypes import (
        AgentPushInstallParams,
        AgentPushProbeParams,
        PushCredential,
        PushRelease,
    )

    probe = AgentPushProbeParams(
        target_address="10.0.0.9", transport="winrm", dns_name="pc1.corp.example"
    )
    assert probe.transport == "winrm"
    assert probe.dns_name == "pc1.corp.example"

    # dns_name is optional and defaults to None; the ssh path never sets it.
    ssh_probe = AgentPushProbeParams(target_address="10.0.0.9", transport="ssh")
    assert ssh_probe.dns_name is None

    install = AgentPushInstallParams(
        target_address="10.0.0.9",
        transport="winrm",
        dns_name="pc1.corp.example",
        os="windows",
        arch="amd64",
        release=PushRelease(version="1.6.0", sha256="a" * 64, url="https://c/x"),
        enroll_token="t",
        credential=PushCredential(username="CORP\\svc", secret="p"),
        host_key_fingerprint="kerberos",
    )
    assert install.transport == "winrm" and install.os == "windows"


def test_transport_rejects_anything_but_ssh_or_winrm() -> None:
    import pytest
    from pydantic import ValidationError

    from app.modules.agent_push.jobtypes import AgentPushProbeParams

    with pytest.raises(ValidationError):
        AgentPushProbeParams(target_address="10.0.0.9", transport="smb")


def test_probe_result_carries_winrm_readiness() -> None:
    from app.modules.agent_push.jobtypes import AgentPushProbeResult

    r = AgentPushProbeResult(
        target_address="10.0.0.9",
        transport="winrm",
        host_key_fingerprint="",
        host_key_type="",
        winrm_https=True,
        winrm_http=False,
        kdc_reachable=True,
    )
    assert r.winrm_https and r.kdc_reachable and not r.winrm_http

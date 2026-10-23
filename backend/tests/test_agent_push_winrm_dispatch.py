"""Windows push dispatch (design §2.1-2.3): a Windows target goes over winrm,
carries its DNS name, needs no confirmed host key (Kerberos authenticates it),
and a Windows target with no DNS name is refused before the credential is
decrypted.

The batch tests run the REAL `dispatch_push_batch` -- the ladder, a real
`JobEngine` signing over canonical bytes -- with a fake vault that counts its
checkouts, so "refused before the credential is decrypted" is asserted as
`vault.calls == 0`, not inferred.
"""

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import AuditEvent, Job
from app.modules.agent_push.dispatch import (
    DISPATCH_REFUSAL_REASONS,
    _device_target,
    _dns_name_refusal,
    _parse_credential_secret,
    dispatch_push_batch,
)
from app.modules.agent_push.errors import PushValidationError
from app.modules.agent_push.jobtypes import INSTALL_JOB_TYPE, PROBE_JOB_TYPE

# The REAL helper is seed_asset (tests/agent_push_fixtures.py), extended with
# `fqdn` and `review_state`; there is NO seed_device_asset.
from tests.agent_push_fixtures import (
    seed_agent_release,
    seed_asset,
    seed_confirmed_host_key,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
)
from tests.ipam_fixtures import seed_agent, seed_device
from tests.site_server_fixtures import seed_site_server

TARGET = "10.71.0.17"
SEGMENT = "10.71.0.0/24"
FQDN = "pc1.corp.example.com"
WD_JSON = json.dumps(
    {
        "username": "svc-push@corp.example.com",
        "secret": "hunter2",
        "kdc": "dc1.corp.example.com",
        "ca_bundle": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    }
)
SSH_JSON = json.dumps({"username": "svc-push", "secret": "hunter2"})
#: `settings.public_console_url` (ITS_PUBLIC_CONSOLE_URL): where a pushed
#: computer enrolls (plan W2, superseded 2026-09-29).
CONSOLE = "https://itms.example.com"


@pytest.fixture(autouse=True)
def _register_push_job_types():
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


class _FakeVault:
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
        return f"https://console.test/{version}/{os_name}/{arch}?sig=x"


async def _grant_org_admin(engine, org) -> None:
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
    os_family: str = "windows",
    fqdn: str | None = FQDN,
    credential_type: str = "windows_domain",
    architecture: str = "amd64",
    realm: str | None = "CORP.EXAMPLE.COM",
):
    """One org with every bound satisfied for TARGET, and a site server whose
    agent advertises the WinRM push capability (design §4)."""
    org = await seed_org(engine, tag)
    await _grant_org_admin(engine, org)
    credential_id = await seed_credential(
        engine, org, credential_type=credential_type, realm=realm
    )
    device_id = await seed_asset(
        engine,
        org.org_id,
        primary_ip=TARGET,
        os_family=os_family,
        architecture=architecture,
        fqdn=fqdn,
    )
    await seed_push_authorization(engine, org, SEGMENT)
    await seed_push_credential_scope(engine, org, credential_id, SEGMENT)
    ss_device = await seed_device(engine, org.org_id)
    await seed_agent(
        engine,
        org.org_id,
        ss_device,
        capabilities={
            "agent.push_probe": True,
            "agent.push_install": True,
            "agent.push_install.winrm": True,
            "agent.push_install.enroll_url": True,
        },
    )
    await seed_site_server(engine, org.org_id, device_id=ss_device, segments=[SEGMENT])
    await seed_agent_release(engine, os_name="windows", arch="amd64")
    await seed_agent_release(engine, os_name="linux", arch="amd64")
    return org, credential_id, device_id, ss_device


async def _dispatch(
    app_engine, org, credential_id, *, kind, vault, tmp_path, store=None,
    public_console_url: str | None = CONSOLE,
):
    async with org_session(org.org_id, engine=app_engine) as session:
        return await dispatch_push_batch(
            session,
            JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key")),
            lambda: store or _FakeStore(),
            vault,
            org_id=org.org_id,
            actor_id=org.user_id,
            credential_id=credential_id,
            targets=[TARGET],
            kind=kind,
            public_console_url=public_console_url,
        )


async def _actions(admin_engine: AsyncEngine, org) -> list[str]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action.like("push.%"),
                    )
                )
            )
            .scalars()
            .all()
        )


async def _assert_refused(admin_engine, org, result, vault, reason: str) -> dict:
    """Review Minor 2: a pre-decrypt refusal is a RESULT, not an exception --
    recorded as a `push.refused` row (the route raises 422 after it commits),
    nothing decrypted, nothing signed. Returns the audit row's metadata."""
    assert result.dispatched == []
    assert reason in result.refused.get(TARGET, []), result.refused
    assert vault.calls == 0
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id, AuditEvent.action == "push.refused"
                )
            )
        ).scalars().all()
        assert (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).first() is None
    matching = [r.meta for r in rows if reason in r.meta.get("reasons", [])]
    assert matching, [r.meta for r in rows]
    return matching[0]


# --------------------------------------------------------------------------- #
# _device_target: the OS -> transport rule, in one place
# --------------------------------------------------------------------------- #


async def test_device_target_maps_windows_to_winrm_with_the_fqdn(admin_engine) -> None:
    org = await seed_org(admin_engine, "wd")
    device_id = await seed_asset(
        admin_engine, org.org_id, os_family="windows", architecture="amd64",
        fqdn="pc1.corp.example", primary_ip="10.0.0.9", review_state="approved",
        provenance="promoted",
    )
    async with AsyncSession(admin_engine) as session:
        os_name, arch, transport, fqdn = await _device_target(session, org.org_id, device_id)
    assert (os_name, arch, transport, fqdn) == ("windows", "amd64", "winrm", "pc1.corp.example")


async def test_linux_and_macos_stay_on_ssh(admin_engine) -> None:
    org = await seed_org(admin_engine, "wd")
    lin = await seed_asset(admin_engine, org.org_id, primary_ip="10.0.0.8",
        os_family="linux", architecture="amd64", provenance="promoted", review_state="approved",
        fqdn="lin1.corp.example")
    mac = await seed_asset(admin_engine, org.org_id, primary_ip="10.0.0.7",
        os_family="macos", architecture="arm64", provenance="promoted", review_state="approved")
    async with AsyncSession(admin_engine) as session:
        _, _, transport, fqdn = await _device_target(session, org.org_id, lin)
        assert transport == "ssh" and fqdn is None
        os_name, _, transport, fqdn = await _device_target(session, org.org_id, mac)
        assert (os_name, transport, fqdn) == ("darwin", "ssh", None)


def test_a_missing_dns_name_is_named_as_such() -> None:
    assert _dns_name_refusal(None) == (None, "no_dns_name")
    assert _dns_name_refusal("  ") == (None, "no_dns_name")


@pytest.mark.parametrize(
    "bad", ["10.0.0.9", "fe80::1", "pc1", "pc_1.corp.example.com", "pc1.corp.example.123"]
)
def test_a_dns_name_that_is_not_one_is_named_as_such(bad: str) -> None:
    # M3/W10 + review I1: an IP literal, a name with no dot, or anything the
    # asset form's normalise_dns_name refuses is not a name Kerberos can
    # request HTTP/<fqdn> for.
    assert _dns_name_refusal(bad) == (None, "invalid_dns_name")


def test_a_good_dns_name_is_returned_lower_cased() -> None:
    assert _dns_name_refusal("PC1.Corp.Example.com") == ("pc1.corp.example.com", None)


# --------------------------------------------------------------------------- #
# The credential secret: what reaches the wire for each type
# --------------------------------------------------------------------------- #


def test_a_windows_domain_secret_carries_the_kdc_and_ca_inside_the_wire_secret() -> None:
    username, wire_secret = _parse_credential_secret(WD_JSON, credential_type="windows_domain")
    assert username == "svc-push@corp.example.com"
    # The username travels in PushCredential.username and is NOT duplicated in
    # the blob; the agent's winrm path re-parses `secret` as this JSON.
    assert json.loads(wire_secret) == {
        "secret": "hunter2",
        "kdc": "dc1.corp.example.com",
        "ca_bundle": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    }


def test_a_windows_domain_secret_defaults_the_optional_parts_to_empty() -> None:
    raw = json.dumps({"username": "svc@corp.example.com", "secret": "p"})
    username, wire_secret = _parse_credential_secret(raw, credential_type="windows_domain")
    assert username == "svc@corp.example.com"
    assert json.loads(wire_secret) == {"secret": "p", "kdc": "", "ca_bundle": ""}


def test_a_windows_domain_secret_with_a_non_string_kdc_is_refused() -> None:
    raw = json.dumps({"username": "svc@corp.example.com", "secret": "p", "kdc": 88})
    with pytest.raises(PushValidationError, match="kdc"):
        _parse_credential_secret(raw, credential_type="windows_domain")


def test_a_push_install_secret_is_still_the_bare_password() -> None:
    assert _parse_credential_secret(SSH_JSON) == ("svc-push", "hunter2")
    assert _parse_credential_secret(SSH_JSON, credential_type="push_install") == (
        "svc-push",
        "hunter2",
    )


# --------------------------------------------------------------------------- #
# The batch: winrm installs, and every refusal lands BEFORE the decrypt
# --------------------------------------------------------------------------- #


async def test_a_windows_install_goes_over_winrm_with_no_confirmed_host_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, _device, ss_device = await _world(admin_engine, "wd-ok")
    vault = _FakeVault(WD_JSON)
    store = _FakeStore()

    # NO host key seeded: Kerberos authenticates the target (design §2.3, W4).
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path,
        store=store,
    )
    assert result.refused == {}
    assert len(result.dispatched) == 1
    assert vault.calls == 1

    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.type == INSTALL_JOB_TYPE
    assert job.signature
    assert str(job.target_id) == str(ss_device)
    p = job.params
    assert p["transport"] == "winrm"
    assert p["os"] == "windows"
    assert p["arch"] == "amd64"
    assert p["dns_name"] == FQDN
    assert p["host_key_fingerprint"] == "kerberos"
    assert p["credential"]["username"] == "svc-push@corp.example.com"
    assert json.loads(p["credential"]["secret"])["secret"] == "hunter2"
    assert p["enroll_url"] == CONSOLE
    # Values only -- nothing that could carry a command (2a clause (b)).
    assert set(p) == {
        "target_address", "expected_mac", "transport", "os", "arch", "dns_name",
        "release", "enroll_token", "credential", "host_key_fingerprint", "enroll_url",
    }
    assert store.calls[0][1:3] == ("windows", "amd64")
    assert "push.dispatched" in await _actions(admin_engine, org)


class _ExternalStore(_FakeStore):
    """External object storage (real S3, Wasabi): the presigned host is the
    storage provider's, never the console's."""

    def presign_agent_release_get(self, version, os_name, arch, *, expires_in) -> str:
        self.calls.append((version, os_name, arch, expires_in))
        return f"https://s3.eu-west-1.amazonaws.com/it-suite/{version}/{os_name}/{arch}?sig=x"


async def test_the_enroll_url_is_the_console_even_when_storage_is_elsewhere(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """W2 SUPERSEDED. A pushed computer learns where to enroll from
    ITS_PUBLIC_CONSOLE_URL, carried in the signed params -- never from the
    presigned release URL's origin, which on external storage is the storage
    provider, who would then receive the single-use enrollment token."""
    org, credential_id, *_ = await _world(admin_engine, "wd-s3")
    store = _ExternalStore()
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=_FakeVault(WD_JSON),
        tmp_path=tmp_path, store=store, public_console_url=CONSOLE + "/",
    )
    assert result.refused == {}
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.params["release"]["url"].startswith("https://s3.eu-west-1.amazonaws.com/")
    # The base the agent's ITS_AGENT_ENROLL_URL takes; its enrollment client
    # appends /enroll/v1 itself (agent/internal/enroll EnrollPath).
    assert job.params["enroll_url"] == CONSOLE


@pytest.mark.parametrize("configured", [None, "", "   "])
async def test_a_windows_install_with_no_console_url_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, configured
) -> None:
    tag = f"wd-nocon-{len(configured) if configured is not None else 'n'}"
    org, credential_id, *_ = await _world(admin_engine, tag)
    vault = _FakeVault(WD_JSON)
    store = _FakeStore()
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault,
        tmp_path=tmp_path, store=store, public_console_url=configured,
    )
    await _assert_refused(admin_engine, org, result, vault, "no_console_url")
    assert DISPATCH_REFUSAL_REASONS["no_console_url"].endswith(
        "Set ITS_PUBLIC_CONSOLE_URL so a pushed computer knows where to enroll."
    )
    assert vault.calls == 0
    assert store.calls == []
    assert "push.dispatched" not in await _actions(admin_engine, org)
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).first() is None


@pytest.mark.parametrize(
    ("tag", "configured"),
    [
        ("http", "http://itms.example.com"),
        ("path", "https://itms.example.com/console"),
        ("bare", "itms.example.com"),
        ("user", "https://user@itms.example.com"),
        ("query", "https://itms.example.com/?x=1"),
        # The agent's installers accept a DNS name or IPv4 literal and a port,
        # nothing else, so the server refuses what they would refuse.
        ("ipv6", "https://[::1]:8443"),
        ("port", "https://itms.example.com:99999"),
        ("space", "https://itms example.com"),
    ],
)
async def test_a_console_url_that_is_not_a_bare_https_origin_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, tag, configured
) -> None:
    """The agent writes this value into the service's ITS_AGENT_ENROLL_URL and
    accepts only a bare https origin; a value it would refuse on the target is
    refused here, before an administrative credential is decrypted."""
    org, credential_id, *_ = await _world(admin_engine, f"wd-badcon-{tag}")
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault,
        tmp_path=tmp_path, public_console_url=configured,
    )
    await _assert_refused(admin_engine, org, result, vault, "no_console_url")


async def test_a_windows_probe_needs_no_console_url(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A probe installs nothing and enrolls nothing, so it is not refused."""
    org, credential_id, *_ = await _world(admin_engine, "wd-probe-nocon")
    result = await _dispatch(
        app_engine, org, credential_id, kind="probe", vault=_FakeVault(WD_JSON),
        tmp_path=tmp_path, public_console_url=None,
    )
    assert result.refused == {}
    assert len(result.dispatched) == 1


async def test_a_windows_target_without_a_dns_name_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-nodns", fqdn=None)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    await _assert_refused(admin_engine, org, result, vault, "no_dns_name")
    assert "push.dispatched" not in await _actions(admin_engine, org)


async def test_a_windows_target_with_an_ip_literal_name_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-ipname", fqdn=TARGET)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    meta = await _assert_refused(admin_engine, org, result, vault, "invalid_dns_name")
    assert meta["dns_name"] == TARGET
    assert "push.dispatched" not in await _actions(admin_engine, org)


async def test_an_undeclared_arch_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Every refusal an install can raise about the target lands before the
    credential is decrypted, not only the DNS-name one."""
    org, credential_id, *_ = await _world(admin_engine, "wd-noarch", architecture="")
    vault = _FakeVault(WD_JSON)
    with pytest.raises(PushValidationError, match="arch"):
        await _dispatch(
            app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
        )
    assert vault.calls == 0


async def test_a_windows_target_with_an_ssh_credential_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The transport is the OS's, and the credential must be the transport's: a
    `push_install` (SSH) secret is never presented to a WinRM target."""
    org, credential_id, *_ = await _world(
        admin_engine, "wd-mismatch", credential_type="push_install"
    )
    vault = _FakeVault(SSH_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    await _assert_refused(admin_engine, org, result, vault, "wrong_credential_kind")


async def test_a_linux_target_with_a_windows_domain_credential_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-lin", os_family="linux")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint="SHA256:x")
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    await _assert_refused(admin_engine, org, result, vault, "wrong_credential_kind")


async def test_a_linux_install_carries_the_console_as_its_enroll_url(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """External storage, SSH: the enroll URL is still the console's."""
    org, credential_id, *_ = await _world(
        admin_engine, "wd-linux-s3", os_family="linux", credential_type="push_install"
    )
    await seed_confirmed_host_key(admin_engine, org, address=TARGET)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=_FakeVault(SSH_JSON),
        tmp_path=tmp_path, store=_ExternalStore(),
    )
    assert result.refused == {}
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.params["transport"] == "ssh"
    assert job.params["release"]["url"].startswith("https://s3.eu-west-1.amazonaws.com/")
    assert job.params["enroll_url"] == CONSOLE


async def test_a_linux_install_still_requires_a_confirmed_host_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The §7.3 bound is ssh-only, not gone."""
    org, credential_id, *_ = await _world(
        admin_engine, "wd-sshkey", os_family="linux", credential_type="push_install"
    )
    vault = _FakeVault(SSH_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    assert result.refused == {TARGET: ["host_key_unconfirmed"]}
    assert vault.calls == 0


async def test_a_windows_probe_is_a_winrm_probe_carrying_the_name_and_no_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-probe")
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="probe", vault=vault, tmp_path=tmp_path
    )
    assert result.refused == {}
    assert vault.calls == 0
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    assert job.type == PROBE_JOB_TYPE
    # C1: target_address, transport, dns_name -- and nothing that could hold a
    # credential.
    assert job.params == {"target_address": TARGET, "transport": "winrm", "dns_name": FQDN}


async def test_a_windows_probe_without_a_dns_name_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-probe-nodns", fqdn=None)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="probe", vault=vault, tmp_path=tmp_path,
    )
    await _assert_refused(admin_engine, org, result, vault, "no_dns_name")


# --------------------------------------------------------------------------- #
# Review I1: the realm is the CREDENTIAL's, and the target must be inside it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tag", "fqdn"),
    [
        ("elsewhere", "pc1.evil.example.net"),
        # A suffix match on a LABEL boundary: notcorp.example.com is not in
        # corp.example.com, whatever the string suffix says.
        ("label", "pc1.notcorp.example.com"),
        ("bare-realm", "corp.example.com"),
    ],
)
async def test_a_windows_target_outside_the_credentials_realm_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, tag, fqdn
) -> None:
    org, credential_id, *_ = await _world(admin_engine, f"wd-realm-{tag}", fqdn=fqdn)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    meta = await _assert_refused(admin_engine, org, result, vault, "outside_realm")
    assert meta["dns_name"] == fqdn and meta["realm"] == "CORP.EXAMPLE.COM"


async def test_a_target_deeper_inside_the_realm_is_accepted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(
        admin_engine, "wd-realm-deep", fqdn="pc1.branch.corp.example.com"
    )
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=_FakeVault(WD_JSON),
        tmp_path=tmp_path,
    )
    assert result.refused == {}


async def test_a_windows_credential_with_no_recorded_realm_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id, *_ = await _world(admin_engine, "wd-norealm", realm=None)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    await _assert_refused(admin_engine, org, result, vault, "realm_not_recorded")


async def test_a_decrypted_account_whose_domain_disagrees_with_the_recorded_realm_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Belt and braces: the recorded realm is what the target was checked
    against, so the account presented must be in that same realm."""
    org, credential_id, *_ = await _world(admin_engine, "wd-disagree")
    other = json.dumps({"username": "svc@other.example.org", "secret": "hunter2"})
    vault = _FakeVault(other)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path,
    )
    # This one is found only AFTER the decrypt -- the account is inside the
    # secret -- so the vault was called once; nothing was signed.
    assert result.refused == {TARGET: ["realm_changed"]} and result.dispatched == []
    assert vault.calls == 1
    assert "push.refused" in await _actions(admin_engine, org)
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).first() is None


@pytest.mark.parametrize(
    ("tag", "fqdn"),
    [("underscore", "pc_1.corp.example.com"), ("numeric-tld", "pc1.corp.example.123"),
     ("space", "pc 1.corp.example.com")],
)
async def test_a_windows_dns_name_that_is_not_a_dns_name_is_refused_before_the_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, tag, fqdn
) -> None:
    """`_require_hostname` applies the same check the asset form does
    (`normalise_dns_name`): inventory can write this column too."""
    org, credential_id, *_ = await _world(admin_engine, f"wd-dns-{tag}", fqdn=fqdn)
    vault = _FakeVault(WD_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path
    )
    await _assert_refused(admin_engine, org, result, vault, "invalid_dns_name")



# --------------------------------------------------------------------------- #
# Review Minor 2: the refusals speak plainly
# --------------------------------------------------------------------------- #

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_every_dispatch_refusal_is_plain_language() -> None:
    assert set(DISPATCH_REFUSAL_REASONS) == {
        "no_dns_name", "invalid_dns_name", "wrong_credential_kind", "no_console_url",
        "outside_realm", "realm_not_recorded", "realm_changed",
    }
    for reason, text in DISPATCH_REFUSAL_REASONS.items():
        assert len(text) > 60, reason
        for wire in ("windows_domain", "push_install", "winrm", "fqdn", "dns_name", "_"):
            assert wire not in text.replace("ITS_PUBLIC_CONSOLE_URL", ""), (reason, wire)
        assert not _UUID.search(text), reason


async def test_several_refusals_for_one_machine_are_all_named(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """All-or-nothing, and every failing bound named (spec §5.2): a Windows
    machine with no DNS name, pushed with an SSH account, with no console URL."""
    org, credential_id, *_ = await _world(
        admin_engine, "wd-several", fqdn=None, credential_type="push_install"
    )
    vault = _FakeVault(SSH_JSON)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=vault, tmp_path=tmp_path,
        public_console_url=None,
    )
    assert set(result.refused[TARGET]) == {"no_dns_name", "wrong_credential_kind", "no_console_url"}
    assert vault.calls == 0


async def test_the_job_is_built_from_the_target_snapshot_that_passed_validation(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex on #90 (dispatch.py:702). The install re-read the asset's OS and
    arch after validation, under READ COMMITTED, so an edit landing between
    the two reads built a job from two different rows: Windows -> Linux gave
    an SSH job carrying the `kerberos` sentinel and a Windows-domain
    credential. The target is read ONCE; a second read sees Linux here, and
    the job must still be the Windows one that was validated."""
    from app.modules.agent_push import dispatch as dispatch_module

    org, credential_id, _device, _ss = await _world(admin_engine, "wd-snapshot")
    real = dispatch_module._device_target
    reads = 0

    async def edited_after_the_first_read(session, org_id, device_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            return await real(session, org_id, device_id)
        return ("linux", "arm64", "ssh", None)

    monkeypatch.setattr(dispatch_module, "_device_target", edited_after_the_first_read)
    result = await _dispatch(
        app_engine, org, credential_id, kind="install", vault=_FakeVault(WD_JSON),
        tmp_path=tmp_path,
    )
    assert result.refused == {}
    assert reads == 1
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
    p = job.params
    assert (p["transport"], p["os"], p["arch"]) == ("winrm", "windows", "amd64")
    assert p["host_key_fingerprint"] == "kerberos"
    assert p["dns_name"] == FQDN

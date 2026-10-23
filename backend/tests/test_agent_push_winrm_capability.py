"""The site-server WinRM capability gate (design §4, plan §I6).

"WinRM push: the site server must report the `winrm` push capability; an older
site server is skipped with a reason, never used." An agent older than 1.6.x
advertises `agent.push_install` and cannot speak WinRM; handing it a `winrm`
job would be refused `unknown`-shaped at the far end at best. So for a Windows
target the originator must advertise `agent.push_install.winrm`, and when the
filter is what removed every candidate the refusal says so
(`no_winrm_site_server`), because the remedy -- upgrade that site server -- is
not the remedy for `no_site_server`.

**THE SSH-ONLY SERVER IS THE MORE SPECIFIC ONE, ON PURPOSE.** The ladder picks
the most specific segment first; if the WinRM-capable server were also the more
specific, a `_site_server_for` that ignored the capability would still return
it and every assertion here would pass against the unfiltered code.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import AuditEvent
from app.modules.agent_push.dispatch import dispatch_push_batch
from app.modules.agent_push.net import parse_target_address
from app.modules.agent_push.preflight import (
    ENROLL_URL_PUSH_CAPABILITY,
    REFUSAL_REASONS,
    WINRM_PUSH_CAPABILITY,
    _site_server_for,
    evaluate_targets,
)
from tests.agent_push_fixtures import (
    PushOrg,
    seed_agent_release,
    seed_asset,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
)
from tests.ipam_fixtures import seed_agent, seed_device
from tests.site_server_fixtures import seed_site_server

TARGET = "10.73.0.17"
NARROW = "10.73.0.0/24"
WIDE = "10.73.0.0/16"
FQDN = "pc1.corp.example.com"

OLD_AGENT = {"agent.push_probe": True, "agent.push_install": True}
WINRM_AGENT = {**OLD_AGENT, WINRM_PUSH_CAPABILITY: True}
#: An SSH-only site server of this release: it hands the placed binary its
#: enroll URL (review Minor 1), and does not speak WinRM.
SSH_AGENT = {**OLD_AGENT, ENROLL_URL_PUSH_CAPABILITY: True}


@pytest.fixture(autouse=True)
def _register_push_job_types():
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _grant_org_admin(engine: AsyncEngine, org: PushOrg) -> None:
    from app.models import Role, UserRole

    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.org_id, user_id=org.user_id, role_id=role.id))
        await session.commit()


async def _site_server(
    engine: AsyncEngine,
    org: PushOrg,
    *,
    segment: str,
    capabilities: dict[str, Any] | None,
) -> UUID:
    """A site server on `segment`. `capabilities=None` seeds NO agents row at
    all -- a site-server device whose agent has never reported anything."""
    device = await seed_device(engine, org.org_id)
    if capabilities is not None:
        await seed_agent(engine, org.org_id, device, capabilities=capabilities)
    return await seed_site_server(engine, org.org_id, device_id=device, segments=[segment])


async def _world(
    engine: AsyncEngine,
    tag: str,
    *,
    os_family: str = "windows",
    credential_type: str = "windows_domain",
) -> tuple[PushOrg, UUID]:
    org = await seed_org(engine, tag)
    await _grant_org_admin(engine, org)
    credential_id = await seed_credential(engine, org, credential_type=credential_type)
    await seed_asset(
        engine,
        org.org_id,
        primary_ip=TARGET,
        os_family=os_family,
        architecture="amd64",
        fqdn=FQDN,
    )
    await seed_push_authorization(engine, org, WIDE)
    await seed_push_credential_scope(engine, org, credential_id, WIDE)
    return org, credential_id


async def _pick(app_engine: AsyncEngine, org: PushOrg, transport: str | None):
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return await _site_server_for(
            session, org.org_id, parse_target_address(TARGET), transport=transport
        )


async def _decide(app_engine: AsyncEngine, org: PushOrg, credential_id: UUID):
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        decisions = await evaluate_targets(
            session,
            org.org_id,
            addresses=[parse_target_address(TARGET)],
            credential_id=credential_id,
        )
    return decisions[0]


# --------------------------------------------------------------------------- #
# _site_server_for(transport=...)
# --------------------------------------------------------------------------- #


async def test_winrm_skips_the_older_site_server_for_the_capable_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, _ = await _world(admin_engine, "wc-pick")
    capable = await _site_server(admin_engine, org, segment=WIDE, capabilities=WINRM_AGENT)
    older = await _site_server(admin_engine, org, segment=NARROW, capabilities=OLD_AGENT)

    chosen = await _pick(app_engine, org, "winrm")
    assert chosen is not None and chosen[0] == capable

    # SSH ignores the WinRM capability -- but since review Minor 1 it filters
    # on its own (`agent.push_install.enroll_url`), which the older agent
    # lacks too; with no filter the most specific server comes back, as before.
    assert await _pick(app_engine, org, "ssh") is None
    chosen = await _pick(app_engine, org, None)
    assert chosen is not None and chosen[0] == older


@pytest.mark.parametrize(
    "capabilities",
    [
        OLD_AGENT,
        {},  # an agent that has reported nothing is not taken to speak WinRM
        {WINRM_PUSH_CAPABILITY: "yes"},  # only a literal true counts
        None,  # no agents row at all
    ],
)
async def test_winrm_finds_no_originator_without_the_advertised_capability(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, capabilities: dict[str, Any] | None
) -> None:
    org, _ = await _world(admin_engine, "wc-none")
    await _site_server(admin_engine, org, segment=NARROW, capabilities=capabilities)
    assert await _pick(app_engine, org, "winrm") is None
    # A site server IS there; the filter is what refused it.
    assert await _pick(app_engine, org, None) is not None


# --------------------------------------------------------------------------- #
# evaluate_targets: the transport comes from the asset, the reason is named
# --------------------------------------------------------------------------- #


async def test_a_windows_target_served_only_by_an_older_site_server_is_no_winrm_site_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, credential_id = await _world(admin_engine, "wc-older")
    await _site_server(admin_engine, org, segment=NARROW, capabilities=OLD_AGENT)
    decision = await _decide(app_engine, org, credential_id)
    # Named for its remedy, and not double-counted as `no_site_server`.
    assert decision.refusals == ("no_winrm_site_server",)
    assert decision.site_server_id is None


async def test_a_windows_target_with_no_site_server_at_all_is_still_no_site_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, credential_id = await _world(admin_engine, "wc-nosite")
    await _site_server(admin_engine, org, segment="10.99.0.0/24", capabilities=WINRM_AGENT)
    decision = await _decide(app_engine, org, credential_id)
    assert decision.refusals == ("no_site_server",)


async def test_a_windows_target_with_a_capable_site_server_clears_the_ladder(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, credential_id = await _world(admin_engine, "wc-ok")
    await _site_server(admin_engine, org, segment=NARROW, capabilities=OLD_AGENT)
    capable = await _site_server(admin_engine, org, segment=WIDE, capabilities=WINRM_AGENT)
    decision = await _decide(app_engine, org, credential_id)
    assert decision.refusals == ()
    assert decision.site_server_id == capable


async def test_a_linux_target_ignores_the_winrm_capability(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, credential_id = await _world(
        admin_engine, "wc-linux", os_family="linux", credential_type="push_install"
    )
    ssh_only = await _site_server(admin_engine, org, segment=NARROW, capabilities=SSH_AGENT)
    decision = await _decide(app_engine, org, credential_id)
    assert decision.refusals == ()
    assert decision.site_server_id == ssh_only


# --------------------------------------------------------------------------- #
# Review Minor 1: an SSH push needs a site server that hands the enroll URL on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capabilities", [OLD_AGENT, None])
async def test_a_linux_target_whose_only_site_server_is_too_old_is_refused_as_such(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, capabilities
) -> None:
    """An agent older than this release runs `install --token-file` without
    `--enroll-url`: the placed agent would never enroll. It is skipped, and
    the refusal names the remedy (upgrade it), not `no_site_server`."""
    org, credential_id = await _world(
        admin_engine, f"wc-old-{capabilities is None}", os_family="linux",
        credential_type="push_install",
    )
    await _site_server(admin_engine, org, segment=NARROW, capabilities=capabilities)
    decision = await _decide(app_engine, org, credential_id)
    assert decision.refusals == ("site_server_too_old",)
    assert decision.site_server_id is None


async def test_a_linux_target_prefers_no_old_site_server_over_a_current_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, credential_id = await _world(
        admin_engine, "wc-mixed", os_family="linux", credential_type="push_install"
    )
    await _site_server(admin_engine, org, segment=NARROW, capabilities=OLD_AGENT)
    current = await _site_server(admin_engine, org, segment=WIDE, capabilities=SSH_AGENT)
    decision = await _decide(app_engine, org, credential_id)
    assert decision.refusals == ()
    assert decision.site_server_id == current


def test_site_server_too_old_carries_a_remedy() -> None:
    help_text = REFUSAL_REASONS["site_server_too_old"]
    assert "upgrade" in help_text.lower()


def test_no_winrm_site_server_carries_a_remedy() -> None:
    help_text = REFUSAL_REASONS["no_winrm_site_server"]
    assert len(help_text) > 80
    assert "upgrade" in help_text.lower()


# --------------------------------------------------------------------------- #
# Through dispatch: refused and recorded, nothing decrypted, nothing signed
# --------------------------------------------------------------------------- #


class _CountingVault:
    def __init__(self) -> None:
        self.calls = 0

    async def checkout(self, session, *, org_id, credential_id) -> str:
        self.calls += 1
        return json.dumps({"username": "CORP\\svc", "secret": "p"})


async def test_a_windows_push_with_only_an_older_site_server_is_refused_and_recorded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org, credential_id = await _world(admin_engine, "wc-dispatch")
    await _site_server(admin_engine, org, segment=NARROW, capabilities=OLD_AGENT)
    await seed_agent_release(admin_engine, os_name="windows", arch="amd64")
    vault = _CountingVault()

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = await dispatch_push_batch(
            session,
            JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key")),
            None,
            vault,
            org_id=org.org_id,
            actor_id=org.user_id,
            credential_id=credential_id,
            targets=[TARGET],
            kind="install",
        )
    assert result.refused == {TARGET: ["no_winrm_site_server"]}
    assert result.dispatched == []
    assert vault.calls == 0

    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id, AuditEvent.action == "push.refused"
                )
            )
        ).scalar_one()
    assert event.meta["reasons"] == ["no_winrm_site_server"]

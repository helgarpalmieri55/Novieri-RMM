"""The Group Policy MSI download (GPO MSI spec 2026-09-30 §2.1, §2.2, §2.6)."""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent
from app.modules.agent_distribution import msi as msi_svc
from app.services import msi_build
from tests.install_link_fixtures import switch_on
from tests.msi_fixtures import StoreWith, need_msitools, offer, signed_release

CONSOLE = "https://itms.example.com"
URL = "/api/v1/install-link/msi"


async def _setup(api_env, monkeypatch, *, version: str = "1.7.0", key: bool = True):
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    org_id = await api_env.create_org("msi")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    release = signed_release(version)
    offer(monkeypatch, release)
    monkeypatch.setattr(
        api_env.app.state.settings,
        "agent_release_public_key",
        release.public_key if key else None,
    )
    store = StoreWith({(release.version, "windows", "amd64"): release.data})
    api_env.app.state.object_store = store
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    return org_id, code, release, store, admin


async def _audits(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "install_link.msi_downloaded",
                    )
                )
            ).scalars()
        )


async def test_it_needs_enrollment_link_manage(api_env, monkeypatch) -> None:
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=tech)
    assert response.status_code == 403


async def test_the_msi_carries_the_agent_and_the_link_and_is_audited_without_the_code(
    api_env, monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    need_msitools()
    caplog.set_level(logging.DEBUG)
    org_id, code, release, store, admin = await _setup(api_env, monkeypatch)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/x-msi"
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="ITMS-Agent-itms.example.com-amd64.msi"'
    )
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-content-sha256"] == digest
    assert store.reads == [("1.7.0", "windows", "amd64")]

    msi = tmp_path / "agent.msi"
    msi.write_bytes(response.content)
    out = tmp_path / "x"
    subprocess.run(["msiextract", "-C", str(out), str(msi)], check=True, capture_output=True)
    assert (out / "ITSuite" / "its-agent.exe").read_bytes() == release.data
    assert json.loads((out / "ITSuite" / "msi-enroll.json").read_text()) == {
        "console": CONSOLE,
        "code": code,
    }

    [audit] = await _audits(api_env, org_id)
    assert audit.target_type == "install_link" and audit.target_id == str(org_id)
    assert audit.meta == {
        "arch": "amd64",
        "version": "1.7.0",
        "msi_sha256": digest,
        "agent_sha256": release.sha256,
    }
    assert code not in json.dumps(audit.meta)
    assert code not in caplog.text


async def test_it_is_off_when_the_link_is_off(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    off = await api_env.client.put(
        "/api/v1/install-link", json={"enabled": False, "networks": ["10.50.0.0/16"]}, headers=admin
    )
    assert off.status_code == 200
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "install link is off" in response.json()["detail"]
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_a_code_that_is_not_the_current_one_is_refused(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    regenerated = await api_env.client.post("/api/v1/install-link/regenerate", headers=admin)
    assert regenerated.status_code == 200
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "not this organisation's current install link" in response.json()["detail"]
    assert code not in response.text
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_an_agent_older_than_the_msi_mode_is_not_wrapped(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch, version="1.6.3")
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "1.7.0 or later" in response.json()["detail"]
    assert store.reads == []


async def test_without_the_release_public_key_nothing_is_built(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch, key=False)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 503
    assert "ITS_AGENT_RELEASE_PUBLIC_KEY" in response.json()["detail"]
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_bytes_that_are_not_the_published_release_are_refused(api_env, monkeypatch) -> None:
    org_id, code, release, store, admin = await _setup(api_env, monkeypatch)
    store.objects[(release.version, "windows", "amd64")] = release.data + b"tampered"
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "does not match its published SHA-256" in response.json()["detail"]
    assert await _audits(api_env, org_id) == []


async def test_a_signature_under_another_key_is_refused(api_env, monkeypatch) -> None:
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(
        api_env.app.state.settings, "agent_release_public_key", signed_release().public_key
    )
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "valid release signature" in response.json()["detail"]
    assert await _audits(api_env, org_id) == []


async def test_a_console_reached_by_ip_address_gets_no_msi(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", "https://10.0.0.5")
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "DNS name" in response.json()["detail"]
    assert store.reads == []


async def test_without_its_public_console_url_there_is_no_msi_whatever_the_host_header(
    api_env, monkeypatch
) -> None:
    """The MSI goes to every computer in the domain, so its console address
    comes from configuration and never from the request (CLAUDE.md 2a(b)'s
    spirit): a forged Host header must not be able to aim a fleet."""
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", None)
    response = await api_env.client.post(
        URL,
        json={"code": code, "arch": "amd64"},
        headers={**admin, "Host": "itms.example.com"},
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "ITS_PUBLIC_CONSOLE_URL is not set" in detail
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_a_console_url_with_a_path_says_so(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(
        api_env.app.state.settings, "public_console_url", "https://itms.example.com/console"
    )
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "ITS_PUBLIC_CONSOLE_URL" in detail and "path" in detail
    # It is a DNS name: the old sentence blamed an IP address.
    assert "IP address" not in detail
    assert store.reads == []


async def test_a_trailing_slash_on_the_console_url_is_fine(api_env, monkeypatch) -> None:
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE + "/")
    seen: list[str] = []

    def fake_build(release: Any, console: str, code: str, **_: Any) -> bytes:
        seen.append(console)
        return b"MSI"

    monkeypatch.setattr(msi_svc, "build_msi", fake_build)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 200, response.text
    assert seen == [CONSOLE]


async def test_the_build_runs_with_no_database_session_open(api_env, monkeypatch) -> None:
    """Up to a minute of wixl must not pin a pooled connection in an open
    transaction, nor a thread of the default executor, which ObjectStore uses."""
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    engines = [api_env.app.state.app_engine, api_env.app.state.admin_engine]
    during: list[tuple[list[int], str]] = []

    def fake_build(*_: Any, **__: Any) -> bytes:
        during.append(
            ([engine.pool.checkedout() for engine in engines], threading.current_thread().name)
        )
        return b"MSI-bytes"

    monkeypatch.setattr(msi_svc, "build_msi", fake_build)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 200, response.text
    [(checked_out, thread)] = during
    assert checked_out == [0, 0]
    assert thread.startswith("msi-build")
    # Delivered, so audited -- in its own short transaction after the build.
    [audit] = await _audits(api_env, org_id)
    assert audit.meta["msi_sha256"] == hashlib.sha256(b"MSI-bytes").hexdigest()


async def test_a_failed_build_is_not_audited(api_env, monkeypatch) -> None:
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)

    def failing_build(*_: Any, **__: Any) -> bytes:
        raise msi_svc.MsiRefused(503, "The MSI could not be built: wixl failed")

    monkeypatch.setattr(msi_svc, "build_msi", failing_build)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 503
    assert await _audits(api_env, org_id) == []


async def test_when_every_build_slot_is_busy_the_next_request_is_503(api_env, monkeypatch) -> None:
    """The next request is answered at once, never queued behind a build."""
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    slots = msi_build.MAX_CONCURRENT_BUILDS
    release = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def slow_build(*_: Any, **__: Any) -> bytes:
        with lock:
            started.append(threading.current_thread().name)
        assert release.wait(30), "the test never released the build"
        return b"MSI"

    monkeypatch.setattr(msi_svc, "build_msi", slow_build)
    body = {"code": code, "arch": "amd64"}
    busy = [
        asyncio.create_task(api_env.client.post(URL, json=body, headers=admin))
        for _ in range(slots)
    ]
    try:
        for _ in range(500):
            if len(started) == slots:
                break
            await asyncio.sleep(0.01)
        assert len(started) == slots

        refused = await asyncio.wait_for(api_env.client.post(URL, json=body, headers=admin), 5)
        assert refused.status_code == 503
        assert "try again" in refused.json()["detail"].lower()
        assert refused.headers["retry-after"] == "60"
    finally:
        release.set()
        done = await asyncio.gather(*busy)
    assert [r.status_code for r in done] == [200] * slots
    assert len(await _audits(api_env, org_id)) == slots

    # The slots are free again afterwards.
    again = await api_env.client.post(URL, json=body, headers=admin)
    assert again.status_code == 200


async def test_an_unknown_arch_is_a_422_and_the_code_is_never_in_the_url(
    api_env, monkeypatch
) -> None:
    _, code, _, _, admin = await _setup(api_env, monkeypatch)
    response = await api_env.client.post(URL, json={"code": code, "arch": "x86"}, headers=admin)
    assert response.status_code == 422
    # POST only: a GET would put the code in a URL, which access logs keep.
    assert (await api_env.client.get(URL, headers=admin)).status_code == 405


async def test_a_slot_whose_build_outlives_its_request_is_freed_when_the_build_ends() -> None:
    """A cancelled request must not free its slot while its build still runs
    on a thread, or the next request would queue behind a build nobody awaits."""
    free_before = msi_svc._BUILD_SLOTS._value
    build: concurrent.futures.Future[bytes] = concurrent.futures.Future()
    async with msi_svc._build_slot() as builds:
        builds.append(build)
    assert msi_svc._BUILD_SLOTS._value == free_before - 1
    build.set_result(b"MSI")
    await asyncio.sleep(0)
    assert msi_svc._BUILD_SLOTS._value == free_before


async def test_a_cancelled_request_keeps_its_slot_until_the_build_thread_ends() -> None:
    """Codex on #92 (msi.py:268). Awaiting `run_in_executor` hands back an
    asyncio wrapper: cancelling the request marks THAT done at once while the
    build keeps running on its thread, so the slot was freed early and the
    next requests queued behind builds nobody awaits. The slot must follow
    the build's own thread."""
    import threading

    gate = threading.Event()
    started = threading.Event()

    def slow_build() -> bytes:
        started.set()
        gate.wait(10)
        return b"MSI"

    free_before = msi_svc._BUILD_SLOTS._value

    async def request() -> bytes:
        async with msi_svc._build_slot() as builds:
            return await msi_svc._run_build(builds, slow_build)

    task = asyncio.create_task(request())
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert msi_svc._BUILD_SLOTS._value == free_before - 1, "freed while the build still runs"
    gate.set()
    for _ in range(100):
        if msi_svc._BUILD_SLOTS._value == free_before:
            break
        await asyncio.sleep(0.02)
    assert msi_svc._BUILD_SLOTS._value == free_before, "never freed after the build ended"

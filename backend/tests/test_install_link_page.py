"""The public install-link page, its Windows download and its install.sh
(spec 2026-09-28 §1.2, §5 Part 1)."""

import asyncio
import base64
import logging
import secrets
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import ObjectStore
from app.models import InstallLink
from app.models.core import AgentRelease
from app.modules.agent_distribution import link_page
from app.modules.agent_distribution.link_page import LinkRelease, pick_link_releases
from tests.install_link_fixtures import TRAEFIK, link_path, peer_client, switch_on

INSIDE = "203.0.113.5, 10.50.0.9"
SHA = "b" * 64
RELEASES = {
    ("windows", "amd64"): LinkRelease("1.6.2", "windows", "amd64", SHA, "sig-win-amd64"),
    ("windows", "arm64"): LinkRelease("1.6.2", "windows", "arm64", "c" * 64, "sig-win-arm64"),
    ("linux", "amd64"): LinkRelease("1.6.2", "linux", "amd64", SHA),
    ("darwin", "arm64"): LinkRelease("1.6.2", "darwin", "arm64", SHA),
}


class _StubStore(ObjectStore):
    def __init__(self) -> None:  # noqa: D107 - never reaches for a bucket
        self.calls: list[dict[str, Any]] = []

    def presign_agent_release_get(
        self,
        version: str,
        os_name: str,
        arch: str,
        *,
        expires_in: int,
        download_filename: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "version": version,
                "os": os_name,
                "arch": arch,
                "expires_in": expires_in,
                "download_filename": download_filename,
            }
        )
        return f"https://minio.test/agent-releases/{version}/its-agent-{os_name}-{arch}?sig=x"


@pytest.fixture()
def releases(monkeypatch: pytest.MonkeyPatch) -> dict:
    async def fake(_session: Any) -> dict:
        return dict(RELEASES)

    monkeypatch.setattr(link_page, "link_releases", fake)
    return RELEASES


async def _get(api_env, path: str, *, xff: str = INSIDE, headers: dict | None = None):
    async with peer_client(api_env, TRAEFIK) as client:
        return await client.get(
            path, headers={"X-Forwarded-For": xff} | (headers or {}), follow_redirects=False
        )


@pytest.mark.parametrize(
    "suffix", ["", "/windows?arch=amd64", "/windows.sig?arch=amd64", "/install.sh"]
)
@pytest.mark.parametrize(
    "case", ["off", "wrong_code", "outside", "switched_off_by_time", "malformed"]
)
async def test_every_route_answers_the_same_404(api_env, releases, suffix: str, case: str) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    xff = INSIDE
    if case == "off":
        admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
        await api_env.client.put(
            link_path(org_id),
            json={"enabled": False, "networks": ["10.50.0.0/16"]},
            headers=admin,
        )
    elif case == "wrong_code":
        code = code[:-1] + ("A" if code[-1] != "A" else "B")
    elif case == "outside":
        xff = "10.50.0.9, 192.0.2.44"
    elif case == "switched_off_by_time":
        async with AsyncSession(api_env.admin_engine) as session:
            await session.execute(
                update(InstallLink)
                .where(InstallLink.org_id == org_id)
                .values(disable_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
    else:  # malformed
        code = "not-a-code"
    response = await _get(api_env, f"/i/{code}{suffix}", xff=xff)
    unrouted = await _get(api_env, "/i-does-not-exist", xff=xff)
    assert response.status_code == unrouted.status_code == 404
    assert response.content == unrouted.content == b'{"detail":"Not Found"}'
    # Same headers too (names; x-request-id differs per request by design).
    names = {k.lower() for k in response.headers} - {"x-request-id"}
    assert names == {k.lower() for k in unrouted.headers} - {"x-request-id"}
    for name in names - {"content-length"}:
        assert response.headers[name] == unrouted.headers[name], name


async def test_the_page_offers_the_windows_download_and_the_command(api_env, releases) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "origin"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    page = response.text
    assert f"/i/{code}/windows?arch=amd64" in page
    assert f"curl -fsSL https://itms.example.test/i/{code}/install.sh | sudo sh" in page
    assert "<script" not in page


async def test_the_language_vary_is_added_to_the_pages_own_vary(api_env, releases) -> None:
    """The page varies on the client hints that pick its download; the language
    middleware must add `Accept-Language`, never replace those."""
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}")
    assert [t.strip() for t in response.headers["vary"].split(",")] == [
        "Sec-CH-UA-Arch",
        "Sec-CH-UA-Platform",
        "User-Agent",
        "Accept-Language",
    ]


async def test_an_arm_browser_is_offered_the_arm_download_first(api_env, releases) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    page = (await _get(api_env, f"/i/{code}", headers={"Sec-CH-UA-Arch": '"arm"'})).text
    assert page.index("arch=arm64") < page.index("arch=amd64")


async def test_with_no_eligible_release_the_page_offers_nothing(api_env, monkeypatch) -> None:
    async def none(_session: Any) -> dict:
        return {}

    monkeypatch.setattr(link_page, "link_releases", none)
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    page = (await _get(api_env, f"/i/{code}")).text
    assert "No Windows installer is available yet" in page
    assert "No macOS or Linux installer is available yet" in page
    assert "/windows?arch=" not in page and "install.sh" not in page


async def test_the_windows_download_is_a_15_minute_grant_named_for_the_code(
    api_env, releases
) -> None:
    store = _StubStore()
    api_env.app.state.object_store = store
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}/windows?arch=arm64")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://minio.test/agent-releases/1.6.2/")
    assert store.calls == [
        {
            "version": "1.6.2",
            "os": "windows",
            "arch": "arm64",
            "expires_in": 900,
            "download_filename": f"ITMS-Agent-Setup_itms.example.test_{code}.exe",
        }
    ]


@pytest.mark.parametrize(
    ("console", "host_part"),
    [
        ("https://itms.example.com", "itms.example.com"),
        ("https://itms.example.com:443/", "itms.example.com"),
        ("https://itms.example.com:8443", "itms.example.com@8443"),
    ],
)
async def test_the_saved_name_carries_the_console_host_and_any_port(
    api_env, releases, console: str, host_part: str
) -> None:
    """Operator decision D6a: the installer reads the console from its own
    file name, so a copy from a share or a USB stick installs too."""
    store = _StubStore()
    api_env.app.state.object_store = store
    api_env.settings.public_console_url = console
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}/windows?arch=amd64")
    assert response.status_code == 302, response.text
    assert store.calls[0]["download_filename"] == f"ITMS-Agent-Setup_{host_part}_{code}.exe"


@pytest.mark.parametrize("console", ["https://10.1.2.3", "https://[fd00::1]:8443"])
async def test_a_console_reached_by_ip_address_has_no_windows_download(
    api_env, releases, console: str
) -> None:
    store = _StubStore()
    api_env.app.state.object_store = store
    api_env.settings.public_console_url = console
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    download = await _get(api_env, f"/i/{code}/windows?arch=amd64")
    assert download.status_code == 404 and download.json() == {"detail": "Not Found"}
    assert store.calls == []
    page = (await _get(api_env, f"/i/{code}")).text
    assert "DNS name" in page
    assert "/windows?arch=" not in page


def test_only_a_dns_name_can_be_carried_in_the_file_name() -> None:
    from app.modules.agent_distribution.link_page import console_host_part

    assert console_host_part("https://itms.example.com") == "itms.example.com"
    assert console_host_part("https://ITMS.Example.com:9443") == "itms.example.com@9443"
    for bad in [
        "https://10.1.2.3",
        "https://[::1]",
        "https://under_score.example.com",
        "https://-dash.example.com",
        "https://10.1.2",
        "https://itms.example.com:0",
    ]:
        assert console_host_part(bad) is None, bad


async def test_an_unknown_arch_is_the_same_404(api_env, releases) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    for arch in ["x86", "a" * 40]:
        response = await _get(api_env, f"/i/{code}/windows?arch={arch}")
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}


async def test_install_sh_is_valid_sh_and_carries_the_code_only_as_data(api_env, releases) -> None:
    api_env.app.state.object_store = _StubStore()
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}/install.sh")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    script = response.text
    assert f"CODE='{code}'" in script
    assert script.count(code) == 1
    assert "--token-stdin" in script and "--token " not in script
    assert "CONSOLE='https://itms.example.test'" in script
    assert f"sha='{SHA}'" in script
    assert "linux-arm64)" not in script  # no release published for it
    sh = shutil.which("sh")
    assert sh is not None
    checked = subprocess.run([sh, "-n"], input=script, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    linux_b64 = script.split("printf %s '", 1)[1].split("'", 1)[0]
    shipped = Path(link_page.__file__).parent / "installers" / "linux" / "install.sh"
    assert base64.b64decode(linux_b64) == shipped.read_bytes()


class _Row:
    def __init__(
        self,
        version: str,
        os: str,
        arch: str,
        channel: str = "stable",
        sha: str = SHA,
        signature: str | None = "sig",
    ):
        self.version, self.os, self.arch, self.channel, self.sha256 = (
            version,
            os,
            arch,
            channel,
            sha,
        )
        self.release_signature = signature


def test_only_a_stable_release_of_1_6_0_or_later_with_bytes_is_offered() -> None:
    picked = pick_link_releases(
        [
            _Row("1.5.9", "windows", "amd64"),
            _Row("1.6.0-rc1", "windows", "amd64"),
            _Row("1.7.0", "windows", "amd64", channel="pilot"),
            _Row("1.6.3", "windows", "amd64", sha=""),
            _Row("1.6.1", "windows", "amd64"),
            _Row("1.6.0", "windows", "amd64"),
            _Row("not-semver", "linux", "amd64"),
        ]
    )
    assert picked == {("windows", "amd64"): LinkRelease("1.6.1", "windows", "amd64", SHA, "sig")}


def test_a_windows_release_without_its_release_signature_is_not_offered() -> None:
    """Final review M1. The self-installer refuses a release with no offline
    signature, so the page must not offer its download: the newest SIGNED
    stable release is offered instead, and with none the page says so. The
    macOS/Linux script checks only the digest and is unaffected."""
    picked = pick_link_releases(
        [
            _Row("1.6.9", "windows", "amd64", signature=None),
            _Row("1.6.8", "windows", "amd64", signature=""),
            _Row("1.6.1", "windows", "amd64"),
            _Row("1.6.9", "windows", "arm64", signature=None),
            _Row("1.6.9", "linux", "amd64", signature=None),
        ]
    )
    assert picked == {
        ("windows", "amd64"): LinkRelease("1.6.1", "windows", "amd64", SHA, "sig"),
        ("linux", "amd64"): LinkRelease("1.6.9", "linux", "amd64", SHA, None),
    }


async def test_the_page_offers_no_windows_download_for_an_unsigned_release(
    api_env, monkeypatch
) -> None:
    async def unsigned(_session: Any) -> dict:
        return pick_link_releases(
            [
                _Row("1.6.2", "windows", "amd64", signature=None),
                _Row("1.6.2", "windows", "arm64", signature=""),
                _Row("1.6.2", "linux", "amd64", signature=None),
            ]
        )

    monkeypatch.setattr(link_page, "link_releases", unsigned)
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    page = (await _get(api_env, f"/i/{code}")).text
    assert "No Windows installer is available yet" in page
    assert "/windows?arch=" not in page
    assert f"/i/{code}/install.sh" in page
    for arch in ("amd64", "arm64"):
        response = await _get(api_env, f"/i/{code}/windows?arch={arch}")
        assert response.status_code == 404


@pytest.fixture()
async def live_api_logged(api_env):
    """A real uvicorn with everything it logs captured -- the fixture shape
    `tests/test_ui_ws.py` explains line by line (log_config=None, loggers
    reset to what configure_logging leaves, ROOT captured)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # The TEST'S OWN http client logs every URL it fetches; that is not
            # the server's log and is not what this measures.
            if not record.name.startswith(("httpx", "httpcore")):
                lines.append(record.getMessage())

    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    uvicorn_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access", "uvicorn.error")]
    restore = [(lg, lg.level, lg.propagate, list(lg.handlers)) for lg in uvicorn_loggers]
    for lg in uvicorn_loggers:
        lg.handlers[:] = []
        lg.propagate = True
        lg.setLevel(logging.NOTSET)
    config = uvicorn.Config(
        api_env.app, host="127.0.0.1", port=port, log_config=None, access_log=True
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield port, lines
    finally:
        server.should_exit = True
        await task
        root.removeHandler(handler)
        root.setLevel(previous_level)
        for lg, level, propagate, handlers in restore:
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers[:] = handlers


async def test_the_code_never_reaches_the_access_log(api_env, releases, live_api_logged) -> None:
    """THE MEASUREMENT, with its control. uvicorn writes the full request line;
    `configure_logging`'s filter on `uvicorn.access` rewrites `/i/<code>` to
    `/i/[redacted]`. The control requests the same code under a path the
    filter does not own and must see it logged, or the capture proves nothing."""
    port, lines = live_api_logged
    api_env.app.state.object_store = _StubStore()
    org_id = await api_env.create_org("pg-log")
    code = await switch_on(api_env, org_id, ["127.0.0.0/8"])
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        for path in (
            f"/i/{code}",
            f"/i/{code}/install.sh",
            f"/i/{code}/windows?arch=amd64",
            f"/i/{code}/windows.sig?arch=amd64",
        ):
            await client.get(path, follow_redirects=False)
    await asyncio.sleep(0.2)
    clean = list(lines)
    assert sum("/i/[redacted]" in line for line in clean) == 4, clean
    assert [line for line in clean if code in line] == []

    lines.clear()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        await client.get(f"/not-the-link/{code}")
    await asyncio.sleep(0.2)
    assert any(code in line for line in lines), (
        f"the control did not reproduce the exposure; captured: {lines}"
    )


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
async def test_the_signature_document_describes_exactly_the_release_the_download_serves(
    api_env, releases, arch: str
) -> None:
    """The Windows self-installer fetches this and checks the bytes it copied
    against the release's offline signature before installing (the elevation
    TOCTOU fix). It must name the SAME release the download redirected to."""
    store = _StubStore()
    api_env.app.state.object_store = store
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    download = await _get(api_env, f"/i/{code}/windows?arch={arch}")
    assert download.status_code == 302
    response = await _get(api_env, f"/i/{code}/windows.sig?arch={arch}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    served = RELEASES[("windows", arch)]
    assert response.json() == {
        "version": served.version,
        "sha256": served.sha256,
        "signature": served.signature,
    }
    assert store.calls[0]["version"] == response.json()["version"]


async def test_the_signature_document_follows_the_download_s_refusals(api_env, releases) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    for arch in ["x86", "a" * 40]:
        response = await _get(api_env, f"/i/{code}/windows.sig?arch={arch}")
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    # A console reached by IP address has no Windows download, so no signature.
    api_env.settings.public_console_url = "https://10.1.2.3"
    response = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64")
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}


def test_the_release_signature_is_carried_through_the_pick() -> None:
    row = _Row("1.6.4", "windows", "amd64")
    row.release_signature = "offline-sig"  # type: ignore[attr-defined]
    picked = pick_link_releases([row])
    assert picked[("windows", "amd64")].signature == "offline-sig"


# --- The signature document by digest (final review I1) ---------------------
#
# A saved installer (a share, a USB stick) is an OLDER release once a newer one
# is promoted. The self-installer sends the digest of the bytes it staged, and
# the console answers for THAT release -- stable, 1.6.0 or later, Windows, this
# arch -- or with the same 404 as every other refusal. Releases are global, so
# every row here carries its own version and digest.


def _digest() -> str:
    return secrets.token_hex(32)


def _patch() -> int:
    return secrets.randbelow(10**9)


async def _seed_windows(
    api_env,
    version: str,
    arch: str,
    sha: str,
    *,
    channel: str = "stable",
    signature: str | None = "offline-sig",
) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        session.add(
            AgentRelease(
                version=version,
                os="windows",
                arch=arch,
                storage_ref=f"agent-releases/{version}/windows-{arch}",
                size_bytes=1234,
                sha256=sha,
                channel=channel,
                release_signature=signature,
                published_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def test_an_older_stable_release_still_answers_by_its_digest(api_env) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    patch = _patch()
    older, newer = _digest(), _digest()
    await _seed_windows(api_env, f"1.6.{patch}", "amd64", older, signature="sig-older")
    await _seed_windows(api_env, f"1.{10**9 + patch}.0", "amd64", newer, signature="sig-newer")

    response = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={older}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "version": f"1.6.{patch}",
        "sha256": older,
        "signature": "sig-older",
    }
    # An upper-case digest names the same release.
    upper = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={older.upper()}")
    assert upper.status_code == 200 and upper.json()["sha256"] == older


async def test_a_digest_for_the_other_arch_or_no_release_is_the_same_404(api_env) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    arm = _digest()
    await _seed_windows(api_env, f"1.6.{_patch()}", "arm64", arm)
    for query in [f"arch=amd64&sha256={arm}", f"arch=arm64&sha256={_digest()}"]:
        response = await _get(api_env, f"/i/{code}/windows.sig?{query}")
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, query


@pytest.mark.parametrize(
    ("version", "channel"),
    [("1.6.{p}", "pilot"), ("1.5.{p}", "stable"), ("1.6.0-rc{p}", "stable")],
)
async def test_a_digest_of_a_release_the_link_would_not_offer_is_the_same_404(
    api_env, version: str, channel: str
) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    sha = _digest()
    await _seed_windows(api_env, version.format(p=_patch()), "amd64", sha, channel=channel)
    response = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={sha}")
    unrouted = await _get(api_env, "/i-does-not-exist")
    assert response.status_code == 404
    assert response.content == unrouted.content


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "%00"])
async def test_a_malformed_digest_is_the_same_404(api_env, releases, digest: str) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={digest}")
    unrouted = await _get(api_env, "/i-does-not-exist")
    assert response.status_code == 404
    assert response.content == unrouted.content


async def test_a_digest_does_not_open_the_signature_document_for_a_dead_link(api_env) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    sha = _digest()
    await _seed_windows(api_env, f"1.6.{_patch()}", "amd64", sha)
    outside = await _get(
        api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={sha}", xff="10.50.0.9, 192.0.2.44"
    )
    assert outside.status_code == 404
    api_env.settings.public_console_url = "https://10.1.2.3"
    by_ip = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={sha}")
    assert by_ip.status_code == 404


async def test_a_digest_of_an_unsigned_windows_release_is_the_same_404(api_env) -> None:
    org_id = await api_env.create_org("pg")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    for signature in (None, ""):
        sha = _digest()
        await _seed_windows(api_env, f"1.6.{_patch()}", "amd64", sha, signature=signature)
        response = await _get(api_env, f"/i/{code}/windows.sig?arch=amd64&sha256={sha}")
        assert response.status_code == 404, signature


def test_every_value_install_sh_embeds_stays_data_whatever_it_contains(tmp_path: Path) -> None:
    """Codex on #89: ITS_PUBLIC_CONSOLE_URL skips the Host-header check (the
    operator set it), and was written between single quotes as it stood. A
    quote in it -- a typo, or a hostile value -- closed the assignment and
    what followed ran as root on every machine that used the link. Every
    embedded value is now POSIX-quoted, so each one reads back as the exact
    string it was, and nothing in it runs."""
    marker = tmp_path / "ran"
    hostile = f"https://host'; touch {marker}; #"
    release = LinkRelease(version="1.6.0", os="linux", arch="amd64", sha256="a" * 64)
    script = link_page.install_script(
        console=hostile,
        code="c0de",
        releases={("linux", "amd64"): release},
        presign=lambda _r: "https://store.example/x?a=1&b='2'",
    )
    sh = shutil.which("sh")
    assert sh is not None
    assert subprocess.run([sh, "-n"], input=script, text=True).returncode == 0
    console_line = next(ln for ln in script.splitlines() if ln.startswith("CONSOLE="))
    url_line = next(ln for ln in script.splitlines() if "linux-amd64) url=" in ln)
    url_assignment = url_line.split(")", 1)[1].split(";;", 1)[0]
    read_back = subprocess.run(
        [sh, "-c", f'{console_line}\n{url_assignment}\nprintf "%s\\n%s" "$CONSOLE" "$url"'],
        text=True,
        capture_output=True,
    )
    assert read_back.returncode == 0, read_back.stderr
    assert read_back.stdout == f"{hostile}\nhttps://store.example/x?a=1&b='2'"
    assert not marker.exists()

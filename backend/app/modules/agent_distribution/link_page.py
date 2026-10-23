"""The install link's public page, its Windows download and its install.sh.

Install-link spec 2026-09-28 §1.2. PUBLIC BY DESIGN -- no sign-in -- which is
the operator's recorded decision overturning this module's spec §7 ("no
unauthenticated download endpoint"); `docs/modules/agent-distribution/spec.md`
§7 carries the amendment. What bounds it lives in
`app/services/install_link.py`: the link must be on, the code must match and the
request must come from a listed network, or every route here answers exactly
the 404 FastAPI gives a path that does not exist.

The machine fetches from this server; this module opens no connection to it.

WHAT THE PAGE SENDS BACK AND WHY:
  * `Referrer-Policy: origin` -- the download redirect goes to the object store,
    and a full referrer would put the code in its request log.
  * The Windows download's saved-as name carries the console as well as the
    code, `ITMS-Agent-Setup_<host>[@<port>]_<code>.exe` (operator decision
    D6a, 2026-09-28, superseding the Mark-of-the-Web plan D6): the installer
    reads both from its own file name, so a copy from a share or a USB stick
    installs too. Only a DNS name can be carried; a console reached by an IP
    address gets no Windows download, and the page says why.
  * `Accept-CH` / `Critical-CH: Sec-CH-UA-Arch` -- Chromium browsers retry the
    request once with the machine's architecture, so the page can offer the ARM
    build first on an ARM PC without a line of script.
  * No script at all, and a CSP that says so.
"""

from __future__ import annotations

import base64
import html
import ipaddress
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.client_scope import ClientScope
from app.core.i18n import N_, locale_var, tr, tr_marked
from app.core.tenancy import org_session
from app.models.core import AgentRelease
from app.modules.agent_distribution.api import _object_store
from app.modules.agent_distribution.service import DOWNLOAD_TTL_S, _installer_bytes
from app.modules.agent_update.semver import Version, parse_version, try_parse_version
from app.services import install_link as link_svc

router = APIRouter(include_in_schema=False)

#: The first agent that understands a link code (spec §4).
LINK_MIN_AGENT_VERSION: Version = parse_version("1.6.0")
WINDOWS_ARCHES = ("amd64", "arm64")
UNIX_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
)
#: Marked rather than translated here: this is a table, and the label is worded
#: for a person wherever it is used (`tr_marked`), in the page and in the MSI's
#: refusals.
ARCH_LABEL = {"amd64": N_("64-bit Intel or AMD"), "arm64": N_("ARM")}


@dataclass(frozen=True)
class LinkRelease:
    version: str
    os: str
    arch: str
    sha256: str
    #: The release's OFFLINE signature, as stored (`agent_releases.
    #: release_signature`). Served by `/i/<code>/windows.sig` so the Windows
    #: self-installer can verify the bytes it copied before it installs them.
    signature: str | None = None


def pick_link_releases(rows: Iterable[Any]) -> dict[tuple[str, str], LinkRelease]:
    """Per (os, arch): the highest STABLE release, 1.6.0 or later, with bytes
    -- and, for Windows, its offline release signature (`_link_version`).

    `rows` are `AgentRelease` rows, or anything with the same attributes
    (`version, os, arch, channel, sha256, release_signature`) -- the unit test
    passes plain objects.
    """
    best: dict[tuple[str, str], tuple[Version, LinkRelease]] = {}
    for row in rows:
        version = _link_version(row)
        if version is None:
            continue
        key = (row.os, row.arch)
        current = best.get(key)
        if current is None or version > current[0]:
            best[key] = (
                version,
                LinkRelease(
                    row.version,
                    row.os,
                    row.arch,
                    row.sha256,
                    getattr(row, "release_signature", None),
                ),
            )
    return {key: release for key, (_, release) in best.items()}


async def link_releases(session: AsyncSession) -> dict[tuple[str, str], LinkRelease]:
    rows = (
        await session.execute(
            select(AgentRelease).where(AgentRelease.channel == "stable", AgentRelease.sha256 != "")
        )
    ).scalars()
    return pick_link_releases(rows)


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _link_version(row: Any) -> Version | None:
    """The row's version when the link may offer it -- STABLE, with bytes,
    1.6.0 or later -- else None. The one rule both the pick and the lookup by
    digest apply, so the two can never disagree about what the link serves.

    A WINDOWS release also needs its offline release signature (final review
    M1): the self-installer refuses one without, so offering its download
    would hand the technician a file that cannot install. The macOS/Linux
    script checks only the digest this server embeds."""
    if row.channel != "stable" or not row.sha256:
        return None
    if row.os == "windows" and not getattr(row, "release_signature", None):
        return None
    version = try_parse_version(row.version)
    if version is None or version < LINK_MIN_AGENT_VERSION:
        return None
    return version


def pick_by_digest(rows: Iterable[Any], os_name: str, arch: str, digest: str) -> LinkRelease | None:
    """The release the link may offer for (os_name, arch) whose bytes are
    `digest` (lower-case hex) -- the highest such version, should two ever
    share bytes -- or None."""
    found: tuple[Version, LinkRelease] | None = None
    for row in rows:
        if row.os != os_name or row.arch != arch or row.sha256 != digest:
            continue
        version = _link_version(row)
        if version is None or (found is not None and version <= found[0]):
            continue
        found = (
            version,
            LinkRelease(
                row.version, row.os, row.arch, row.sha256, getattr(row, "release_signature", None)
            ),
        )
    return None if found is None else found[1]


async def link_release_by_digest(
    session: AsyncSession, os_name: str, arch: str, digest: str
) -> LinkRelease | None:
    rows = (
        await session.execute(
            select(AgentRelease).where(
                AgentRelease.os == os_name,
                AgentRelease.arch == arch,
                AgentRelease.sha256 == digest,
                AgentRelease.channel == "stable",
            )
        )
    ).scalars()
    return pick_by_digest(rows, os_name, arch, digest)


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _is_dns_name(host: str) -> bool:
    """Labels of letters, digits and inner hyphens joined by dots; no
    underscore, no IP literal, and not all-numeric at the end (which would be
    an IP address to half the resolvers that saw it)."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    return all(_DNS_LABEL.match(label) for label in labels) and not labels[-1].isdigit()


def console_host_part(console: str) -> str | None:
    """`<host>` or `<host>@<port>` for the Windows file name, from the console
    base URL (`install_link.console_base`: `ITS_PUBLIC_CONSOLE_URL`, else the
    hardened Host header). `None` when the host is not a DNS name or the port
    is not a port -- the Windows download then answers 404."""
    parts = urlsplit(console)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if not _is_dns_name(host) or port == 0:
        return None
    return host if port in (None, 443) else f"{host}@{port}"


def windows_file_name(host_part: str, code: str) -> str:
    """Parsed from the RIGHT by the installer: the code is the last 32
    characters after a `_`, and everything between the prefix and that `_` is
    the console. A code may itself contain `_` and `-`."""
    return f"ITMS-Agent-Setup_{host_part}_{code}.exe"


def _not_found() -> JSONResponse:
    # Byte-for-byte FastAPI's own 404 for an unrouted path.
    return JSONResponse({"detail": "Not Found"}, status_code=404)


async def _live_org(request: Request, code: str) -> UUID | None:
    state = request.app.state
    return await link_svc.resolve_live(
        state.admin_engine,
        state.app_engine,
        code=code,
        source=client_address(request, state.trusted_proxies),
        now=datetime.now(UTC),
    )


async def _releases_for(request: Request, org_id: UUID) -> dict[tuple[str, str], LinkRelease]:
    async with org_session(
        org_id, engine=request.app.state.app_engine, clients=ClientScope.all()
    ) as session:
        return await link_releases(session)


def _console(request: Request) -> str:
    return link_svc.console_base(
        request.app.state.settings.public_console_url, request.headers.get("host")
    )


def _arch_hint(request: Request) -> str:
    hint = request.headers.get("sec-ch-ua-arch", "").strip().strip('"').lower()
    agent = request.headers.get("user-agent", "").lower()
    if hint == "arm" or "arm64" in agent or "aarch64" in agent:
        return "arm64"
    return "amd64"


def _on_windows(request: Request) -> bool:
    hint = request.headers.get("sec-ch-ua-platform", "").strip().strip('"').lower()
    return hint == "windows" or "windows" in request.headers.get("user-agent", "").lower()


_CSS = (
    "body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c2330}"
    "main{max-width:40rem;margin:3rem auto;padding:0 1rem}"
    "section{background:#fff;border:1px solid #d9dee6;border-radius:10px;"
    "padding:1rem 1.25rem;margin:1rem 0}"
    ".btn{display:inline-block;background:#1f5eff;color:#fff;padding:.6rem 1rem;border-radius:8px;"
    "text-decoration:none;font-weight:600}"
    "pre{background:#10141c;color:#e8edf5;padding:.75rem;border-radius:8px;overflow-x:auto;"
    "white-space:pre-wrap;word-break:break-all}"
)


def render_page(
    *,
    console: str,
    code: str,
    releases: Mapping[tuple[str, str], LinkRelease],
    windows_first: bool,
    arch: str,
    nonce: str,
    windows_named: bool = True,
) -> str:
    esc = html.escape

    def text(sentence: str) -> str:
        # Catalogue text is static, but it is concatenated into HTML: escape it
        # anyway, so a translation can never become markup. `quote=False` keeps
        # the English byte-identical (no catalogue sentence contains `&`, `<`
        # or `>`; apostrophes stay apostrophes).
        return html.escape(sentence, quote=False)

    windows = [a for a in WINDOWS_ARCHES if ("windows", a) in releases]
    if windows and not windows_named:
        win = (
            "<p>"
            + text(tr(
                "The Windows installer needs ITMS to be reached by a DNS name, such as "
                "itms.example.com, not an IP address: the installer learns where to join "
                "from the name of the file it downloads. Ask whoever runs ITMS to set one "
                "up."
            ))
            + "</p>"
        )
    elif windows:
        primary = arch if arch in windows else windows[0]
        ordered = [primary] + [a for a in windows if a != primary]
        links = [
            f'<a class="btn" href="/i/{esc(code)}/windows?arch={primary}">'
            + text(
                tr(
                    "Download for Windows ({arch_label})",
                    arch_label=tr_marked(ARCH_LABEL[primary]),
                )
            )
            + "</a>"
        ] + [
            f'<p><a href="/i/{esc(code)}/windows?arch={a}">'
            + text(tr(
                "Download for Windows ({arch_label}) instead",
                arch_label=tr_marked(ARCH_LABEL[a]),
            ))
            + "</a></p>"
            for a in ordered[1:]
        ]
        win = (
            "".join(links)
            + "<p>"
            + text(tr(
                "Open the downloaded file and approve the prompt. Keep the file's name "
                "as it is: it is how the installer knows where to join."
            ))
            + "</p>"
        )
    else:
        win = (
            "<p>"
            + text(tr(
                "No Windows installer is available yet. Ask whoever runs ITMS to mark "
                "a signed agent release 1.6.0 or later as stable."
            ))
            + "</p>"
        )
    if any(key in releases for key in UNIX_PLATFORMS):
        command = f"curl -fsSL {console.rstrip('/')}/i/{code}/install.sh | sudo sh"
        unix = (
            "<p>"
            + text(tr("Run this in a terminal on the computer:"))
            + f"</p><pre>{esc(command)}</pre>"
        )
    else:
        unix = (
            "<p>"
            + text(tr(
                "No macOS or Linux installer is available yet. Ask whoever runs ITMS to "
                "mark an agent release 1.6.0 or later as stable."
            ))
            + "</p>"
        )
    sections = [("Windows", win), (text(tr("macOS and Linux")), unix)]
    if not windows_first:
        sections.reverse()
    body = "".join(f"<section><h2>{title}</h2>{content}</section>" for title, content in sections)
    lang = "es" if locale_var.get() == "es" else "en"
    heading = text(tr("Install the ITMS agent"))
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="origin">'
        f"<title>{heading}</title>"
        f'<style nonce="{nonce}">{_CSS}</style></head><body><main>'
        f"<h1>{heading}</h1>"
        "<p>"
        + text(tr(
            "This installs the agent on the computer you are using now. It appears in "
            "ITMS within a minute and waits there until a technician approves it."
        ))
        + f"</p>{body}</main></body></html>"
    )


def _sh_quote(value: str) -> str:
    """`value` as ONE POSIX sh word that reads back exactly as itself.

    install.sh runs as root, so nothing it embeds may be shell text, whatever
    its source -- the configured console URL included (Codex on #89: it
    skips the Host-header check because the operator set it, and a quote in
    it closed the assignment). Single quotes, with each embedded quote
    written as '\\''.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _b64(rel: str) -> str:
    return base64.b64encode(_installer_bytes(rel)).decode("ascii")


def install_script(
    *,
    console: str,
    code: str,
    releases: Mapping[tuple[str, str], LinkRelease],
    presign: Callable[[LinkRelease], str],
) -> str:
    """A POSIX sh installer for this machine's platform.

    It downloads the published artefact, checks the SHA-256 this server
    embedded, and runs the SAME reviewed installer the install command runs
    (`_INSTALLERS_DIR`), handing it the code on stdin through the shell's own
    `printf` -- so the code is never an argument of any process.
    """
    cases = "\n".join(
        f"  {os_name}-{arch}) url={_sh_quote(presign(releases[(os_name, arch)]))}; "
        f"sha={_sh_quote(releases[(os_name, arch)].sha256)} ;;"
        for os_name, arch in UNIX_PLATFORMS
        if (os_name, arch) in releases
    )
    lines = [
        "#!/bin/sh",
        "# Installs the ITMS agent on this computer. Generated for one install link:",
        "# the code below is what lets this computer join, so do not pass this file on.",
        "set -eu",
        f"CONSOLE={_sh_quote(console.rstrip('/'))}",
        f"CODE={_sh_quote(code)}",
        "os=$(uname -s)",
        "arch=$(uname -m)",
        'case "$os/$arch" in',
        "  Linux/x86_64|Linux/amd64) key=linux-amd64 ;;",
        "  Linux/aarch64|Linux/arm64) key=linux-arm64 ;;",
        "  Darwin/x86_64) key=darwin-amd64 ;;",
        "  Darwin/arm64) key=darwin-arm64 ;;",
        '  *) echo "itms: $os $arch is not a platform the agent is published for" >&2; exit 1 ;;',
        "esac",
        "url=''",
        "sha=''",
        'case "$key" in',
        cases,
        "  *) : ;;",
        "esac",
        'if [ -z "$url" ]; then',
        '  echo "itms: no stable agent 1.6.0 or later is published for $key yet" >&2',
        "  exit 1",
        "fi",
        'if [ "$(id -u)" -ne 0 ]; then',
        '  echo "itms: run this with sudo" >&2',
        "  exit 1",
        "fi",
        "d=$(mktemp -d)",
        "trap 'rm -rf \"$d\"' EXIT",
        'curl -fsSL "$url" -o "$d/its-agent"',
        "if command -v sha256sum >/dev/null 2>&1; then",
        "  got=$(sha256sum \"$d/its-agent\" | cut -d ' ' -f 1)",
        "else",
        "  got=$(shasum -a 256 \"$d/its-agent\" | cut -d ' ' -f 1)",
        "fi",
        'if [ "$got" != "$sha" ]; then',
        '  echo "itms: the download does not match its published checksum;'
        ' nothing was installed" >&2',
        "  exit 1",
        "fi",
        'case "$key" in',
        "  linux-*)",
        f"    printf %s '{_b64('linux/install.sh')}' | base64 -d > \"$d/install.sh\"",
        f"    printf %s '{_b64('linux/its-agent.service')}' | base64 -d > \"$d/its-agent.service\"",
        "    ;;",
        "  darwin-*)",
        f"    printf %s '{_b64('darwin/install.sh')}' | base64 -D > \"$d/install.sh\"",
        f"    printf %s '{_b64('darwin/com.toeshee.itsagent.plist')}'"
        ' | base64 -D > "$d/com.toeshee.itsagent.plist"',
        "    ;;",
        "esac",
        'chmod +x "$d/install.sh"',
        "# The code reaches the installer on stdin, through the shell's own printf.",
        "printf '%s' \"$CODE\""
        ' | bash "$d/install.sh" --token-stdin --url "$CONSOLE" --binary "$d/its-agent"',
        'echo "itms: installed. This computer appears in ITMS within a minute,'
        ' waiting for review."',
        "",
    ]
    return "\n".join(lines)


_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "origin",
    "X-Frame-Options": "DENY",
    "Accept-CH": "Sec-CH-UA-Arch, Sec-CH-UA-Platform",
    "Critical-CH": "Sec-CH-UA-Arch",
    "Vary": "Sec-CH-UA-Arch, Sec-CH-UA-Platform, User-Agent",
}


@router.get("/i/{code}")
async def install_link_page(code: str, request: Request) -> Response:
    org_id = await _live_org(request, code)
    if org_id is None:
        return _not_found()
    nonce = secrets.token_urlsafe(16)
    console = _console(request)
    page = render_page(
        console=console,
        code=code,
        releases=await _releases_for(request, org_id),
        windows_first=_on_windows(request),
        arch=_arch_hint(request),
        nonce=nonce,
        windows_named=console_host_part(console) is not None,
    )
    headers = _PAGE_HEADERS | {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        )
    }
    return HTMLResponse(page, headers=headers)


@router.get("/i/{code}/windows")
async def install_link_windows(code: str, request: Request, arch: str = "amd64") -> Response:
    # `arch` is deliberately unbounded here: ANY value outside WINDOWS_ARCHES is
    # the same 404 as a wrong code, never a 422 that says the code was right.
    org_id = await _live_org(request, code)
    if org_id is None or arch not in WINDOWS_ARCHES:
        return _not_found()
    host_part = console_host_part(_console(request))
    if host_part is None:
        return _not_found()
    release = (await _releases_for(request, org_id)).get(("windows", arch))
    if release is None:
        return _not_found()
    url = _object_store(request).presign_agent_release_get(
        release.version,
        "windows",
        arch,
        expires_in=DOWNLOAD_TTL_S,
        download_filename=windows_file_name(host_part, code),
    )
    return RedirectResponse(
        url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "origin"}
    )


@router.get("/i/{code}/windows.sig")
async def install_link_windows_signature(
    code: str, request: Request, arch: str = "amd64", sha256: str | None = None
) -> Response:
    """What the Windows download for `arch` serves: its version, SHA-256 and
    offline release signature, for the self-installer to verify the copy it is
    about to run elevated against (the elevation TOCTOU fix).

    THE SAME REFUSALS AS THE DOWNLOAD, IN THE SAME ORDER, so the two can never
    describe different releases or answer differently for one code: the link
    must be live for this source, the arch known, the console a DNS name, and
    a stable release published for that arch. Anything else is the 404.

    `sha256` (final review I1; FROZEN from agent 1.6.0 on): the digest of the
    bytes the installer staged. A file saved from the link before a newer
    release was promoted -- on a share, on a USB stick -- is an older release,
    and still installs: the answer is the release the link WOULD offer (stable,
    1.6.0 or later, Windows, this arch) whose bytes are those, or the same 404.
    It selects; it does not vouch -- the installer still checks the staged
    bytes against the digest and the offline signature returned. Without it,
    the highest stable release, as the page's download serves."""
    org_id = await _live_org(request, code)
    if org_id is None or arch not in WINDOWS_ARCHES:
        return _not_found()
    if console_host_part(_console(request)) is None:
        return _not_found()
    release: LinkRelease | None
    if sha256 is None:
        release = (await _releases_for(request, org_id)).get(("windows", arch))
    else:
        digest = sha256.lower()
        if not _SHA256_HEX.fullmatch(digest):
            return _not_found()
        async with org_session(
            org_id, engine=request.app.state.app_engine, clients=ClientScope.all()
        ) as session:
            release = await link_release_by_digest(session, "windows", arch, digest)
    if release is None:
        return _not_found()
    return JSONResponse(
        {"version": release.version, "sha256": release.sha256, "signature": release.signature},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/i/{code}/install.sh")
async def install_link_script(code: str, request: Request) -> Response:
    org_id = await _live_org(request, code)
    if org_id is None:
        return _not_found()
    releases = await _releases_for(request, org_id)
    store = _object_store(request) if any(k in releases for k in UNIX_PLATFORMS) else None

    def presign(release: LinkRelease) -> str:
        assert store is not None
        return store.presign_agent_release_get(
            release.version, release.os, release.arch, expires_in=DOWNLOAD_TTL_S
        )

    script = install_script(
        console=_console(request), code=code, releases=releases, presign=presign
    )
    return PlainTextResponse(script, headers={"Cache-Control": "no-store"})

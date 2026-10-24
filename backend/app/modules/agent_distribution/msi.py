"""The Group Policy MSI, built for an administrator (GPO MSI spec 2026-09-30
§2.1, §2.2).

WHAT IS CHECKED, IN ORDER, BEFORE A BYTE IS PACKED:
  1. the link is on and the code the administrator supplied is its current
     code (only the code's hash is stored, so the console sends the code it
     was shown; `install_link.code_state`);
  2. `ITS_PUBLIC_CONSOLE_URL` is set, and is a bare https origin on a DNS
     name, because the MSI's file name and the agent's enrollment URL both
     carry it (`console_origin`). NEVER the request's Host header: the file
     goes to every computer in the domain, so an address a request could
     choose would aim a fleet (CLAUDE.md 2a(b)'s rule for the push's console
     URL, applied here in the same spirit);
  3. a stable, release-signed Windows agent of `MSI_MIN_AGENT_VERSION` or
     later is published for the architecture (`link_releases`);
  4. the platform knows the release-signing public key
     (`ITS_AGENT_RELEASE_PUBLIC_KEY`), and the bytes read back from object
     storage hash to `agent_releases.sha256` and carry a valid offline release
     signature under it (`verify_release_bytes`).
Anything else is a refusal with a sentence, and nothing is audited, because
nothing was handed out.

HOW LONG ANYTHING IS HELD. A build can take up to `msi_build.BUILD_TIMEOUT_S`,
so `msi_for_link` holds nothing expensive across it:
  * a build slot is taken in the event loop WITHOUT WAITING -- all slots busy
    is an immediate 503 with Retry-After, never a queue (`_BUILD_SLOTS`);
  * the checks run in one short transaction, which is closed before the
    object store is read;
  * the build runs on its own small thread pool (`_BUILD_POOL`) with no
    session open, so it never starves the default executor that
    `ObjectStore._run` uses;
  * the audit record is written in a second short transaction, after the
    build succeeded and before the bytes are returned: a build that failed
    was not delivered, and is not audited.

BUILT PER REQUEST AND NEVER STORED. The bytes go to the administrator and
nowhere else, so a regenerated code cannot survive in a cache. The audit
record carries the architecture, the release and the MSI's SHA-256 -- never
the code.

The build itself -- WiX source, wixl, the read-back -- is
`app/services/msi_build.py`, and why it lives there is said at its top.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import functools
import hashlib
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.storage import ObjectStore
from app.modules.agent_distribution.link_page import (
    ARCH_LABEL,
    LinkRelease,
    console_host_part,
    link_releases,
)
from app.modules.agent_update.semver import Version, parse_version, try_parse_version
from app.services import install_link as link_svc
from app.services import msi_build

#: The first agent that has `--msi-install` and `--msi-uninstall`. 1.6.0 was
#: tagged before them (`agent-v1.6.0`); an MSI wrapping it would fail its own
#: custom action on every computer.
MSI_MIN_AGENT_VERSION: Version = parse_version("1.7.0")
MSI_ARCHES = ("amd64", "arm64")
MSI_MEDIA_TYPE = "application/x-msi"
AUDIT_ACTION = "install_link.msi_downloaded"
#: Seconds a refused request is told to wait: about one build.
BUSY_RETRY_AFTER_S = 60

#: Builds in flight in this process. Taken WITHOUT WAITING (`_build_slot`), so
#: a request beyond the limit is refused at once rather than queued behind
#: a minute of wixl. Never awaited when locked, so it never binds to a loop.
_BUILD_SLOTS = asyncio.Semaphore(msi_build.MAX_CONCURRENT_BUILDS)
#: The builds' own threads, sized to the slots: a build never takes a thread
#: of the default executor, which `ObjectStore._run` and every
#: `asyncio.to_thread` in the process share.
_BUILD_POOL = ThreadPoolExecutor(
    max_workers=msi_build.MAX_CONCURRENT_BUILDS, thread_name_prefix="msi-build"
)


class MsiRefused(Exception):
    """A refusal an administrator can act on. `status` is the HTTP status the
    route answers with; `message` is one sentence, and never holds the code.
    `retry_after`, when set, is the Retry-After the route sends."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def release_message(version: str, os_name: str, arch: str, sha256_hex: str) -> bytes:
    """What the offline release key signs: `update.ReleaseMessage` in the
    agent, byte for byte (`docs/kernel/wire-protocol/testdata/
    release-signature.json` pins both sides)."""
    parsed = try_parse_version(version)
    canonical = str(parsed) if parsed is not None else version
    return (
        f"its-agent-release/v1\n{canonical}\n{os_name}\n{arch}\n{sha256_hex.lower()}\n"
    ).encode()


def release_public_key(value: str | None) -> Ed25519PublicKey:
    """`ITS_AGENT_RELEASE_PUBLIC_KEY`, parsed. Unset or malformed is a 503:
    the MSI is never built from bytes that could not be verified."""
    if value is None or not value.strip():
        raise MsiRefused(
            503,
            tr(
                "ITS_AGENT_RELEASE_PUBLIC_KEY is not set, so the agent the MSI would carry "
                "cannot be verified. Set it to the agent release's release-signing-key.pub."
            ),
        )
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise MsiRefused(
            503, tr("ITS_AGENT_RELEASE_PUBLIC_KEY is not a base64 Ed25519 public key.")
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_release_bytes(data: bytes, release: LinkRelease, key: Ed25519PublicKey) -> None:
    """The bytes are the release's: their SHA-256 is `agent_releases.sha256`,
    and the release carries a valid offline signature over (version, windows,
    arch, that digest) -- the check self-update and the self-installer make."""
    digest = hashlib.sha256(data).hexdigest()
    label = tr_marked(ARCH_LABEL.get(release.arch, release.arch))
    if digest != release.sha256.lower():
        raise MsiRefused(
            409,
            tr(
                "The stored agent {release_version} for Windows ({label}) does not match "
                "its published SHA-256, so no MSI was built. Publish the release again.",
                release_version=release.version,
                label=label,
            ),
        )
    try:
        signature = base64.b64decode(release.signature or "", validate=True)
        key.verify(signature, release_message(release.version, "windows", release.arch, digest))
    except (binascii.Error, ValueError, InvalidSignature):
        raise MsiRefused(
            409,
            tr(
                "The agent {release_version} for Windows ({label}) does not carry a valid "
                "release signature, so no MSI was built.",
                release_version=release.version,
                label=label,
            ),
        ) from None


def build_error_text(exc: msi_build.MsiBuildError) -> str:
    """The builder's refusal in the request's language.

    `services/msi_build.py` is stdlib-only (the Windows e2e runs it as a script),
    so it cannot call `tr()`. Each of its errors carries a machine `code` and the
    plain values the sentence mentions; the template for each code is chosen here,
    and under English it is the sentence the builder wrote. An unknown code falls
    back to that English sentence, never to a guess. No template takes the link
    code: the builder never puts it in an error."""
    p = exc.params
    match exc.code:
        case "bad_version":
            return tr("{version} is not a release version", version=p.get("version"))
        case "version_too_large":
            return tr(
                "{version} does not fit a Windows Installer product version (at most "
                "255.255.65535)",
                version=p.get("version"),
            )
        case "console_not_origin":
            return tr(
                "the console address must be a bare https origin: a host and an optional port"
            )
        case "code_malformed":
            return tr("the install link code must be 32 URL-safe characters")
        case "tool_missing":
            return tr(
                "{name} is not installed; building the MSI needs msitools (wixl, msibuild, "
                "msiinfo)",
                name=p.get("name"),
            )
        case "build_too_slow":
            return tr(
                "building the MSI took longer than {seconds} seconds", seconds=p.get("seconds")
            )
        case "tool_timeout":
            return tr(
                "{name} did not finish within {seconds} seconds",
                name=p.get("name"),
                seconds=p.get("seconds"),
            )
        case "tool_failed":
            return tr(
                "{name} failed with exit code {exit_code}: {stderr}",
                name=p.get("name"),
                exit_code=p.get("exit_code"),
                stderr=p.get("stderr"),
            )
        case "no_template":
            return tr("the built MSI has no Template in its summary information")
        case "custom_actions_differ":
            return tr(
                "the built MSI's custom actions are not the ones written: {actions}",
                actions=p.get("actions"),
            )
        case "schedule_differs":
            return tr("the built MSI schedules its custom actions differently from the source")
        case "missing_action":
            return tr("the built MSI has no {name} action", name=p.get("name"))
        case "actions_outside_script":
            return tr("the built MSI runs its custom actions outside the install script")
        case "wrong_files":
            return tr(
                "the built MSI holds {files}, not the agent and its settings file",
                files=p.get("files"),
            )
        case "no_service_stop":
            return tr("the built MSI does not stop the its-agent service on install and removal")
        case "wrong_template":
            return tr("the built MSI is not marked {template}", template=p.get("template"))
        case "no_arch":
            return tr("there is no MSI for the {arch} architecture", arch=p.get("arch"))
        case "busy":
            return tr("other MSIs are being built right now; try again in a minute")
    return str(exc)


def build_msi(
    release: LinkRelease,
    console: str,
    code: str,
    *,
    exe: bytes,
    org_id: UUID,
    key: Ed25519PublicKey,
) -> bytes:
    """Spec §2.2's builder. Verifies `exe` first, then packs it."""
    verify_release_bytes(exe, release, key)
    try:
        return msi_build.build_agent_msi(
            exe=exe,
            console=console,
            code=code,
            org_id=org_id,
            version=release.version,
            arch=release.arch,
        )
    except msi_build.MsiBuildError as exc:
        raise MsiRefused(
            503, tr("The MSI could not be built: {reason}", reason=build_error_text(exc))
        ) from None


@dataclass(frozen=True)
class BuiltMsi:
    filename: str
    data: bytes
    sha256: str
    version: str
    arch: str


def msi_file_name(host_part: str, arch: str) -> str:
    """`ITMS-Agent-<host>[@<port>]-<arch>.msi` (spec §2.1). The name is only a
    label here: the console the agent joins is inside the MSI."""
    return f"ITMS-Agent-{host_part}-{arch}.msi"


def console_origin(console: str | None) -> tuple[str, str]:
    """`(https://<host>[:<port>], <host>[@<port>])` from ITS_PUBLIC_CONSOLE_URL,
    or a refusal that says which rule it broke. It must be a bare https origin
    on a DNS name -- the same rule as the link's Windows download
    (`console_host_part`). Unset is a 503: the MSI's console address is never
    taken from the request."""
    configured = (console or "").strip().rstrip("/")
    if not configured:
        raise MsiRefused(
            503,
            tr(
                "ITS_PUBLIC_CONSOLE_URL is not set. The MSI takes the address computers "
                "join from it, never from the browser's request, because the file goes to "
                "every computer in the domain. Set it to the console's https address, such "
                "as https://itms.example.com."
            ),
        )
    parts = urlsplit(configured)
    if parts.scheme != "https":
        raise MsiRefused(
            409,
            tr(
                "ITS_PUBLIC_CONSOLE_URL must be an https address, such as https://itms.example.com."
            ),
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise MsiRefused(
            409,
            tr(
                "ITS_PUBLIC_CONSOLE_URL must be the console's bare https address, with no "
                "path, query or fragment, such as https://itms.example.com."
            ),
        )
    if parts.username is not None:
        raise MsiRefused(
            409,
            tr(
                "ITS_PUBLIC_CONSOLE_URL must not carry a user name, such as "
                "https://itms.example.com."
            ),
        )
    host_part = console_host_part(configured)
    if host_part is None:
        raise MsiRefused(
            409,
            tr(
                "The MSI needs ITMS to be reached by a DNS name, such as itms.example.com, "
                "not an IP address. Set ITS_PUBLIC_CONSOLE_URL to that name."
            ),
        )
    host, _, port = host_part.partition("@")
    return (f"https://{host}:{port}" if port else f"https://{host}", host_part)


@asynccontextmanager
async def _build_slot() -> AsyncIterator[list[Future[bytes]]]:
    """One of `_BUILD_SLOTS`, or an immediate 503. Nothing between `locked()`
    and `acquire()` yields, so the pair is atomic in the event loop.

    Yields a list the caller appends its build future to. If the request is
    cancelled while that build still runs on its thread, the slot is handed
    back when the THREAD finishes, not when the request goes: otherwise a new
    request could take the slot and queue behind a build nobody awaits."""
    if _BUILD_SLOTS.locked():
        raise MsiRefused(
            503,
            tr("Other MSIs are being built right now. Try again in a minute."),
            retry_after=BUSY_RETRY_AFTER_S,
        )
    await _BUILD_SLOTS.acquire()
    builds: list[Future[bytes]] = []
    try:
        yield builds
    finally:
        running = [build for build in builds if not build.done()]
        if running:
            # The callback runs on the build's thread; the semaphore belongs
            # to the event loop, so the release is handed back to it.
            loop = asyncio.get_running_loop()
            running[0].add_done_callback(
                lambda _build: loop.call_soon_threadsafe(_BUILD_SLOTS.release)
            )
        else:
            _BUILD_SLOTS.release()


async def _run_build(builds: list[Future[bytes]], build: Callable[[], bytes]) -> bytes:
    """Run `build` on `_BUILD_POOL` and record THE THREAD'S OWN future in the
    slot's `builds` (Codex on #92). `run_in_executor` would hand back an
    asyncio wrapper that a cancelled request marks done at once, while the
    thread goes on building; the pool's `concurrent.futures.Future` is done
    only when the thread is."""
    # The pool thread starts with an empty context. The build raises refusals
    # worded with `tr()`, which reads the request's language from a ContextVar,
    # so the thread runs inside a copy of the request's context.
    future = _BUILD_POOL.submit(contextvars.copy_context().run, build)
    builds.append(future)
    return await asyncio.wrap_future(future)


@dataclass(frozen=True)
class _Checked:
    release: LinkRelease
    origin: tuple[str, str]
    key: Ed25519PublicKey


async def _check(
    session: AsyncSession,
    *,
    site_id: UUID,
    code: str,
    arch: str,
    console: str | None,
    public_key: str | None,
    now: datetime,
) -> _Checked:
    """Everything the database answers, in one short transaction."""
    try:
        state = await link_svc.code_state(session, site_id, code=code, now=now)
    except link_svc.LinkSiteNotFound:
        raise MsiRefused(404, tr("no such site")) from None
    if state == "off":
        raise MsiRefused(
            409,
            tr(
                "The install link is off. Switch it on before downloading the MSI: "
                "computers join through it."
            ),
        )
    if state != "live":
        raise MsiRefused(
            409,
            tr(
                "That is not this organisation's current install link. Paste the link "
                "shown when it was last generated, or generate a new one."
            ),
        )
    origin = console_origin(console)
    release = (await link_releases(session)).get(("windows", arch))
    if release is None or parse_version(release.version) < MSI_MIN_AGENT_VERSION:
        raise MsiRefused(
            409,
            tr(
                "No stable, release-signed agent {msi_min_agent_version} or later is "
                "published for Windows ({arch_label}). Mark one stable first.",
                msi_min_agent_version=MSI_MIN_AGENT_VERSION,
                arch_label=tr_marked(ARCH_LABEL[arch]),
            ),
        )
    return _Checked(release=release, origin=origin, key=release_public_key(public_key))


async def msi_for_link(
    open_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    store: ObjectStore,
    *,
    org_id: UUID,
    site_id: UUID,
    actor_id: UUID,
    code: str,
    arch: str,
    console: str | None,
    public_key: str | None,
    now: datetime,
) -> BuiltMsi:
    """Check, build and audit one MSI. `open_session` opens a short,
    org-scoped transaction; it is called twice, and never held across the
    object-store read or the build (see "HOW LONG ANYTHING IS HELD")."""
    if arch not in MSI_ARCHES:
        raise MsiRefused(
            422, tr("The MSI is built for amd64 (64-bit Intel or AMD) or arm64 (ARM).")
        )
    async with _build_slot() as builds:
        async with open_session() as session:
            checked = await _check(
                session,
                site_id=site_id,
                code=code,
                arch=arch,
                console=console,
                public_key=public_key,
                now=now,
            )
        release = checked.release
        try:
            exe = await store.get_agent_release(release.version, "windows", arch)
        except Exception:  # noqa: BLE001 -- storage down or the object missing: one sentence
            raise MsiRefused(
                503,
                tr(
                    "The agent {release_version} for Windows could not be read from object "
                    "storage.",
                    release_version=release.version,
                ),
            ) from None
        data = await _run_build(
            builds,
            functools.partial(
                build_msi,
                release,
                checked.origin[0],
                code,
                exe=exe,
                org_id=org_id,
                key=checked.key,
            ),
        )
        digest = hashlib.sha256(data).hexdigest()
        # Delivered from here on, so audited -- in its own short transaction.
        async with open_session() as session:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="user",
                actor_id=actor_id,
                action=AUDIT_ACTION,
                target_type="install_link",
                # The site: one link per site (see `install_link._audit`).
                target_id=str(site_id),
                # NEVER the code. What was handed out, and how to recognise the file.
                metadata={
                    "arch": arch,
                    "version": release.version,
                    "msi_sha256": digest,
                    "agent_sha256": release.sha256,
                },
            )
    return BuiltMsi(
        filename=msi_file_name(checked.origin[1], arch),
        data=data,
        sha256=digest,
        version=release.version,
        arch=arch,
    )

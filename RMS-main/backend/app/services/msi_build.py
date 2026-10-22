"""Build the Group Policy MSI: the published agent, its settings file and two
custom actions (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md
§2.2).

STANDARD LIBRARY ONLY, AND RUNNABLE AS A SCRIPT. The Windows e2e
(`.github/workflows/agent-update-e2e.yml`, `TestMsiInstallWindows`) runs this
file directly -- `python backend/app/services/msi_build.py ...` -- with MSYS2's
wixl, so the MSI it installs comes from this template and nothing else.
`tests/test_msi_build.py` pins the rule.

WHY THIS IS NOT UNDER `app/modules/agent_distribution/`. Building runs three
local msitools programs, and that package's guard
(`tests/test_agent_distribution_opens_no_connection.py`) forbids the stdlib's
process module by name so that a push install cannot be written there by
accident. Running wixl opens no connection. The guard stays exact; the
process-running code lives here, and `agent_distribution/msi.py` calls it.

WHAT THE MSI HOLDS, AND NOTHING ELSE:
  * `its-agent.exe` -- bytes the caller verified against `agent_releases`;
  * `msi-enroll.json` -- `{"console": <https origin>, "code": <link code>}`;
  * `ItsMsiInstall` -- deferred, no impersonation (SYSTEM): the installed
    `its-agent.exe --msi-install`, after InstallFiles, on install;
  * `ItsMsiUninstall` -- the same with `--msi-uninstall`, before RemoveFiles,
    on a real removal only (never while an upgrade removes the older MSI),
    with its exit code ignored so a machine can always be removed;
  * a ServiceControl row that stops the `its-agent` service on install and on
    removal, so neither ever writes over a running image.

THE CODE NEVER REACHES A COMMAND LINE. It is written into `msi-enroll.json`
inside a private temporary directory; every argument a tool receives is a
constant or a file name chosen here. Each tool gets a minimal environment,
never this process's own, which holds database URLs and vault tokens. Errors
never carry the code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

__all__ = [
    "BUILD_TIMEOUT_S",
    "TEMPLATES",
    "UPGRADE_NAMESPACE",
    "MsiBuildError",
    "build_agent_msi",
    "product_version",
    "render_wxs",
    "settings_json",
    "upgrade_code",
    "verify_msi",
]

AGENT_FILE = "its-agent.exe"
SETTINGS_FILE = "msi-enroll.json"
PRODUCT_NAME = "ITMS Agent"
MANUFACTURER = "Toeshee"
#: The whole build -- every tool together -- must finish inside this (spec §2.2).
BUILD_TIMEOUT_S = 60.0
#: At most this many builds at once in one process; a third waits, then is refused.
MAX_CONCURRENT_BUILDS = 2
#: UpgradeCode is uuid5(UPGRADE_NAMESPACE, str(org_id)): constant for an
#: organisation, so a later MSI upgrades an earlier one instead of installing
#: beside it (spec §2.2 item 3). This value is fixed forever: changing it
#: would make every MSI already assigned in a domain a different product.
UPGRADE_NAMESPACE = uuid.UUID("22ad0fe1-20ac-49a3-938c-25347fa54ecb")
#: The Summary Information "Template" per architecture. wixl 0.103-0.106 knows
#: x86, x64 and ia64 only, so the ARM64 package is built as x64 and its
#: Template rewritten with msibuild; the tables are the same (one 64-bit
#: component under ProgramFiles64Folder).
TEMPLATES = {"amd64": "x64;1033", "arm64": "Arm64;1033"}
#: Explicit sequence numbers. wixl's Before=/After= scheduling is not reliable
#: (asked for Before="RemoveFiles", 0.103 placed the action at 1402, BEFORE
#: InstallInitialize, where a deferred action cannot run). verify_msi checks
#: the placement on every build.
INSTALL_SEQUENCE = 4010
UNINSTALL_SEQUENCE = 3490
INSTALL_CONDITION = 'NOT REMOVE~="ALL"'
UNINSTALL_CONDITION = 'REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE'
#: msidbCustomActionType: 18 (run an installed file) + 1024 (deferred) + 2048
#: (no impersonation: runs as SYSTEM). Removal adds 64 (exit code ignored).
EXPECTED_CUSTOM_ACTIONS = frozenset(
    {
        ("ItsMsiInstall", "3090", "its_agent_exe", "--msi-install"),
        ("ItsMsiUninstall", "3154", "its_agent_exe", "--msi-uninstall"),
    }
)

_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?")
_CODE = re.compile(r"[A-Za-z0-9_-]{32}")
_ORIGIN = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})(?::[1-9][0-9]{0,4})?")
_BUILDS = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)


# WHY THE MESSAGES RAISED AS `MsiBuildError` ARE NOT WRAPPED IN `tr()`. This module
# must stay stdlib-only (the Windows e2e runs it as a script), so it cannot import
# `app.core.i18n`. The text is NOT unseen: `agent_distribution/msi.py` wraps it as
# `MsiRefused(503, "The MSI could not be built: ...")` and `api/v1/install_link.py`
# answers with that 503, which an administrator reads. So every error carries a
# machine `code` and its plain `params`, and `msi.py::build_error_text` chooses a
# `tr()` template per code. `str(exc)` stays the English sentence: the log, the
# script's stderr and the tests read that.
class MsiBuildError(RuntimeError):
    """The MSI could not be built, or did not come out as written. The message
    is safe to show an administrator: it never carries the link code.

    `code` names which refusal this is and `params` holds the values the
    sentence mentions (never the link code), so a caller that can translate
    composes its own sentence; `str(exc)` is always the English one."""

    def __init__(self, code: str, message: str, **params: object) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


def upgrade_code(org_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(UPGRADE_NAMESPACE, str(org_id))


def product_version(version: str) -> str:
    """`MAJOR.MINOR.PATCH` for the MSI's ProductVersion. A pre-release suffix is
    dropped (Windows Installer versions are numeric; the stable channel carries
    none, the e2e build does); anything else is refused."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "bad_version", f"{version!r} is not a release version", version=repr(version)
        )
    major, minor, patch = (int(group) for group in match.groups())
    if major > 255 or minor > 255 or patch > 65535:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "version_too_large",
            f"{version} does not fit a Windows Installer product version (at most 255.255.65535)",
            version=version,
        )
    return f"{major}.{minor}.{patch}"


def settings_json(console: str, code: str) -> bytes:
    """`msi-enroll.json`. The agent applies the same two rules again
    (`internal/install/msi.go`) and refuses anything else."""
    if _ORIGIN.fullmatch(console) is None:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "console_not_origin",
            "the console address must be a bare https origin: a host and an optional port",
        )
    if _CODE.fullmatch(code) is None:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "code_malformed", "the install link code must be 32 URL-safe characters"
        )
    return json.dumps({"console": console, "code": code}, separators=(",", ":")).encode()


def render_wxs(*, upgrade: uuid.UUID, version: str) -> str:
    """The WiX source. FIXED TEXT: the only values in it are the product
    version (digits and dots, `product_version`) and the upgrade code (a UUID).
    The console and the code are in `msi-enroll.json`, never here."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="{PRODUCT_NAME}" Language="1033" Version="{product_version(version)}"
           Manufacturer="{MANUFACTURER}" UpgradeCode="{{{str(upgrade).upper()}}}">
    <Package InstallerVersion="500" Compressed="yes" InstallScope="perMachine"
             Description="{PRODUCT_NAME}" Manufacturer="{MANUFACTURER}"/>
    <Media Id="1" Cabinet="agent.cab" EmbedCab="yes"/>
    <MajorUpgrade AllowSameVersionUpgrades="yes"
                  DowngradeErrorMessage="A newer ITMS agent is already installed."/>
    <Property Id="ARPNOMODIFY" Value="1"/>
    <Property Id="ARPNOREPAIR" Value="1"/>
    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLFOLDER" Name="ITSuite">
          <Component Id="Agent" Guid="*">
            <File Id="its_agent_exe" Name="{AGENT_FILE}" Source="{AGENT_FILE}" KeyPath="yes"/>
            <File Id="msi_enroll_json" Name="{SETTINGS_FILE}" Source="{SETTINGS_FILE}"/>
            <ServiceControl Id="StopAgent" Name="its-agent" Stop="both" Wait="yes"/>
          </Component>
        </Directory>
      </Directory>
    </Directory>
    <Feature Id="Agent" Level="1">
      <ComponentRef Id="Agent"/>
    </Feature>
    <CustomAction Id="ItsMsiInstall" FileKey="its_agent_exe" ExeCommand="--msi-install"
                  Execute="deferred" Impersonate="no" Return="check"/>
    <CustomAction Id="ItsMsiUninstall" FileKey="its_agent_exe" ExeCommand="--msi-uninstall"
                  Execute="deferred" Impersonate="no" Return="ignore"/>
    <InstallExecuteSequence>
      <Custom Action="ItsMsiInstall"
              Sequence="{INSTALL_SEQUENCE}">{_xml(INSTALL_CONDITION)}</Custom>
      <Custom Action="ItsMsiUninstall"
              Sequence="{UNINSTALL_SEQUENCE}">{_xml(UNINSTALL_CONDITION)}</Custom>
    </InstallExecuteSequence>
  </Product>
</Wix>
"""


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "tool_missing",
            f"{name} is not installed; building the MSI needs msitools (wixl, msibuild, msiinfo)",
            name=name,
        )
    return path


def _env(work: Path) -> dict[str, str]:
    """What a tool may see of the environment: PATH, on Windows the two
    variables a process needs to start at all, and a temporary directory.

    TMPDIR, TEMP and TMP are all `work` -- during a build, the private 0700
    directory that holds the settings file -- so any scratch file a tool
    writes (a cabinet, a staged stream) is private and removed with it,
    never left in a shared /tmp or the runner's %TEMP%."""
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"}
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = str(work)
    return env


def _run(argv: list[str], *, cwd: Path, deadline: float) -> str:
    name = Path(argv[0]).name
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "build_too_slow",
            f"building the MSI took longer than {BUILD_TIMEOUT_S:.0f} seconds",
            seconds=f"{BUILD_TIMEOUT_S:.0f}",
        )
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            # The build's cwd is its private work directory (build_agent_msi).
            env=_env(cwd),
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "tool_timeout",
            f"{name} did not finish within {BUILD_TIMEOUT_S:.0f} seconds",
            name=name,
            seconds=f"{BUILD_TIMEOUT_S:.0f}",
        ) from None
    if done.returncode != 0:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "tool_failed",
            f"{name} failed with exit code {done.returncode}: {done.stderr.strip()[:500]}",
            name=name,
            exit_code=done.returncode,
            stderr=done.stderr.strip()[:500],
        )
    return done.stdout


def _table(msiinfo: str, msi: Path, table: str, deadline: float) -> list[list[str]]:
    out = _run([msiinfo, "export", msi.name, table], cwd=msi.parent, deadline=deadline)
    return [line.split("\t") for line in out.splitlines()[3:] if line]


def _template(msiinfo: str, msi: Path, deadline: float) -> str:
    for line in _run(
        [msiinfo, "suminfo", msi.name], cwd=msi.parent, deadline=deadline
    ).splitlines():
        if line.startswith("Template:"):
            return line.split(":", 1)[1].strip()
    raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
        "no_template", "the built MSI has no Template in its summary information"
    )


def verify_msi(msi: Path, *, arch: str, deadline: float | None = None) -> None:
    """Read the built file back and refuse it unless it is exactly what
    render_wxs wrote: the two custom actions, where they must run, the two
    files, the service stop, and the architecture. A future wixl that reads the
    source differently fails here, on the server, not on a domain's PCs."""
    msiinfo = _tool("msiinfo")
    deadline = time.monotonic() + BUILD_TIMEOUT_S if deadline is None else deadline
    actions = {tuple(row[:4]) for row in _table(msiinfo, msi, "CustomAction", deadline)}
    if actions != EXPECTED_CUSTOM_ACTIONS:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "custom_actions_differ",
            f"the built MSI's custom actions are not the ones written: {sorted(actions)}",
            actions=str(sorted(actions)),
        )
    seq = {
        row[0]: (row[1], int(row[2]))
        for row in _table(msiinfo, msi, "InstallExecuteSequence", deadline)
    }
    if seq.get("ItsMsiInstall") != (INSTALL_CONDITION, INSTALL_SEQUENCE) or seq.get(
        "ItsMsiUninstall"
    ) != (UNINSTALL_CONDITION, UNINSTALL_SEQUENCE):
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "schedule_differs",
            "the built MSI schedules its custom actions differently from the source",
        )
    try:
        ordered = (
            seq["InstallInitialize"][1]
            < UNINSTALL_SEQUENCE
            < seq["RemoveFiles"][1]
            < seq["InstallFiles"][1]
            < INSTALL_SEQUENCE
            < seq["InstallFinalize"][1]
        )
    except KeyError as exc:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "missing_action", f"the built MSI has no {exc.args[0]} action", name=exc.args[0]
        ) from None
    if not ordered:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "actions_outside_script",
            "the built MSI runs its custom actions outside the install script",
        )
    files = sorted(row[2] for row in _table(msiinfo, msi, "File", deadline))
    if files != [AGENT_FILE, SETTINGS_FILE]:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "wrong_files",
            f"the built MSI holds {files}, not the agent and its settings file",
            files=str(files),
        )
    if _table(msiinfo, msi, "ServiceControl", deadline) != [
        ["StopAgent", "its-agent", "34", "", "1", "Agent"]
    ]:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "no_service_stop",
            "the built MSI does not stop the its-agent service on install and removal",
        )
    if _template(msiinfo, msi, deadline) != TEMPLATES[arch]:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "wrong_template",
            f"the built MSI is not marked {TEMPLATES[arch]}",
            template=TEMPLATES[arch],
        )


def _write(path: Path, data: bytes) -> None:
    # O_BINARY on Windows: without it the C runtime would translate newlines
    # in the agent's bytes.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as out:
        out.write(data)


def build_agent_msi(
    *,
    exe: bytes,
    console: str,
    code: str,
    org_id: uuid.UUID,
    version: str,
    arch: str,
    timeout_s: float = BUILD_TIMEOUT_S,
) -> bytes:
    """The MSI's bytes. The caller has already verified `exe` (digest and
    release signature); this only packs it, and checks what it packed."""
    if arch not in TEMPLATES:
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "no_arch", f"there is no MSI for the {arch!r} architecture", arch=repr(arch)
        )
    settings = settings_json(console, code)
    wxs = render_wxs(upgrade=upgrade_code(org_id), version=version)
    wixl, msibuild = _tool("wixl"), _tool("msibuild")
    if not _BUILDS.acquire(timeout=timeout_s):
        raise MsiBuildError(  # i18n-exempt: translated in msi.py by code (build_error_text)
            "busy", "other MSIs are being built right now; try again in a minute"
        )
    try:
        deadline = time.monotonic() + timeout_s
        # 0700, and removed on the way out whatever happens: the settings
        # file holds the code, and the MSI is never stored (spec §2.2 item 4).
        with tempfile.TemporaryDirectory(prefix="itms-msi-") as tmp:
            work = Path(tmp)
            _write(work / AGENT_FILE, exe)
            _write(work / SETTINGS_FILE, settings)
            _write(work / "agent.wxs", wxs.encode())
            _run([wixl, "-a", "x64", "-o", "agent.msi", "agent.wxs"], cwd=work, deadline=deadline)
            if arch == "arm64":
                _run(
                    [msibuild, "agent.msi", "-s", PRODUCT_NAME, MANUFACTURER, TEMPLATES["arm64"]],
                    cwd=work,
                    deadline=deadline,
                )
            verify_msi(work / "agent.msi", arch=arch, deadline=deadline)
            return (work / "agent.msi").read_bytes()
    finally:
        _BUILDS.release()


def main(argv: list[str] | None = None) -> int:
    """`python backend/app/services/msi_build.py ...`, for the Windows e2e.
    The code comes from a FILE, like everywhere else: never an argument."""
    parser = argparse.ArgumentParser(description="Build the ITMS Group Policy MSI.")
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--console", required=True)
    parser.add_argument("--code-file", required=True, type=Path)
    parser.add_argument("--org", required=True, type=uuid.UUID)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True, choices=sorted(TEMPLATES))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        data = build_agent_msi(
            exe=args.exe.read_bytes(),
            console=args.console,
            code=args.code_file.read_text().strip(),
            org_id=args.org,
            version=args.version,
            arch=args.arch,
        )
    except MsiBuildError as exc:
        print(f"msi_build: {exc}", file=sys.stderr)
        return 1
    args.out.write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

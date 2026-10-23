"""The Group Policy MSI builder (GPO MSI spec 2026-09-30 §2.2) -- and the proof
the spec asked for first: wixl CAN express a deferred, no-impersonate
custom action, read back from the built file with msiinfo.

In CI, ITS_TEST_REQUIRE_MSITOOLS=1 turns a missing tool into a failure; on a
developer machine without msitools the tool-dependent tests skip."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services import msi_build
from app.services.msi_build import MsiBuildError, build_agent_msi, product_version, settings_json

ORG = uuid.UUID("7d0c4c2e-5b1f-4a8e-9f62-3c1d2e4b5a69")
CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"
CONSOLE = "https://itms.example.com"
EXE = b"MZ" + bytes(range(256)) * 64
REPO = Path(__file__).resolve().parents[2]


def _need_msitools() -> None:
    missing = [t for t in ("wixl", "msibuild", "msiinfo", "msiextract") if shutil.which(t) is None]
    if missing:
        if os.environ.get("ITS_TEST_REQUIRE_MSITOOLS") == "1":
            pytest.fail(f"ITS_TEST_REQUIRE_MSITOOLS=1 but {missing} are not on PATH")
        pytest.skip(f"msitools is not installed ({missing})")


def _build(
    tmp_path: Path, *, arch: str = "amd64", org: uuid.UUID = ORG, name: str = "agent"
) -> Path:
    _need_msitools()
    data = build_agent_msi(
        exe=EXE, console=CONSOLE, code=CODE, org_id=org, version="1.7.0", arch=arch
    )
    out = tmp_path / f"{name}-{arch}.msi"
    out.write_bytes(data)
    return out


def _table(msi: Path, table: str) -> list[list[str]]:
    out = subprocess.run(
        ["msiinfo", "export", str(msi), table], capture_output=True, text=True, check=True
    ).stdout
    return [line.split("\t") for line in out.splitlines()[3:] if line]


def _template(msi: Path) -> str:
    out = subprocess.run(
        ["msiinfo", "suminfo", str(msi)], capture_output=True, text=True, check=True
    ).stdout
    return next(
        line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("Template:")
    )


def test_wixl_expresses_a_deferred_no_impersonate_custom_action(tmp_path: Path) -> None:
    rows = {tuple(row[:4]) for row in _table(_build(tmp_path), "CustomAction")}
    assert rows == {
        ("ItsMsiInstall", "3090", "its_agent_exe", "--msi-install"),
        ("ItsMsiUninstall", "3154", "its_agent_exe", "--msi-uninstall"),
    }
    # 18: run an installed file. 1024: deferred. 2048: no impersonation (SYSTEM).
    # 64: exit code ignored -- on the removal action only.
    assert 3090 & 0x3F == 18 and 3090 & 1024 and 3090 & 2048 and not 3090 & 64
    assert 3154 & 0x3F == 18 and 3154 & 1024 and 3154 & 2048 and 3154 & 64


def test_both_actions_run_inside_the_install_script_where_they_must(tmp_path: Path) -> None:
    seq = {
        row[0]: (row[1], int(row[2])) for row in _table(_build(tmp_path), "InstallExecuteSequence")
    }
    assert seq["ItsMsiInstall"] == ('NOT REMOVE~="ALL"', 4010)
    assert seq["ItsMsiUninstall"] == ('REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE', 3490)
    assert (
        seq["InstallInitialize"][1]
        < 3490
        < seq["RemoveFiles"][1]
        < seq["InstallFiles"][1]
        < 4010
        < seq["InstallFinalize"][1]
    )
    assert seq["StopServices"][1] < 3490


def test_it_holds_exactly_the_agent_and_its_settings_file(tmp_path: Path) -> None:
    msi = _build(tmp_path)
    assert sorted(row[2] for row in _table(msi, "File")) == ["its-agent.exe", "msi-enroll.json"]
    out = tmp_path / "extracted"
    subprocess.run(["msiextract", "-C", str(out), str(msi)], check=True, capture_output=True)
    assert (out / "ITSuite" / "its-agent.exe").read_bytes() == EXE
    assert json.loads((out / "ITSuite" / "msi-enroll.json").read_text()) == {
        "console": CONSOLE,
        "code": CODE,
    }
    dirs = {row[0]: (row[1], row[2]) for row in _table(msi, "Directory")}
    assert dirs["INSTALLFOLDER"] == ("ProgramFiles64Folder", "ITSuite")
    assert _table(msi, "ServiceControl") == [["StopAgent", "its-agent", "34", "", "1", "Agent"]]


def test_the_upgrade_code_is_the_organisations_and_the_version_the_agents(tmp_path: Path) -> None:
    first = {row[0]: row[1] for row in _table(_build(tmp_path, name="a"), "Property")}
    again = {row[0]: row[1] for row in _table(_build(tmp_path, name="b"), "Property")}
    other = {
        row[0]: row[1] for row in _table(_build(tmp_path, name="c", org=uuid.uuid4()), "Property")
    }
    expected = "{" + str(uuid.uuid5(msi_build.UPGRADE_NAMESPACE, str(ORG))).upper() + "}"
    assert first["UpgradeCode"] == again["UpgradeCode"] == expected
    assert other["UpgradeCode"] != expected
    assert first["ProductCode"] != again["ProductCode"]
    assert first["ProductVersion"] == "1.7.0"


def test_the_arm64_msi_is_marked_arm64(tmp_path: Path) -> None:
    assert _template(_build(tmp_path, arch="amd64")) == "x64;1033"
    assert _template(_build(tmp_path, arch="arm64")) == "Arm64;1033"


def test_a_built_msi_whose_actions_were_changed_is_refused(tmp_path: Path) -> None:
    msi = _build(tmp_path)
    subprocess.run(
        [
            "msibuild",
            str(msi),
            "-q",
            "UPDATE `CustomAction` SET `Type` = 1042 WHERE `Action` = 'ItsMsiInstall'",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(MsiBuildError, match="custom actions"):
        msi_build.verify_msi(msi, arch="amd64")


def _tamper(msi: Path, *args: str) -> None:
    subprocess.run(["msibuild", str(msi), *args], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("query", "refusal"),
    [
        pytest.param(
            # Where wixl 0.103 put it when asked for Before="RemoveFiles":
            # before InstallInitialize, where a deferred action cannot run.
            "UPDATE `InstallExecuteSequence` SET `Sequence` = 1402 "
            "WHERE `Action` = 'ItsMsiInstall'",
            "schedules its custom actions differently",
            id="install-sequence-number",
        ),
        pytest.param(
            "UPDATE `InstallExecuteSequence` SET `Sequence` = 3491 "
            "WHERE `Action` = 'ItsMsiUninstall'",
            "schedules its custom actions differently",
            id="uninstall-sequence-number",
        ),
        pytest.param(
            # Would run the removal during a major upgrade too.
            "UPDATE `InstallExecuteSequence` SET `Condition` = 'REMOVE~=\"ALL\"' "
            "WHERE `Action` = 'ItsMsiUninstall'",
            "schedules its custom actions differently",
            id="uninstall-condition",
        ),
        pytest.param(
            "UPDATE `InstallExecuteSequence` SET `Condition` = '1' "
            "WHERE `Action` = 'ItsMsiInstall'",
            "schedules its custom actions differently",
            id="install-condition",
        ),
    ],
)
def test_a_built_msi_whose_schedule_was_changed_is_refused(
    tmp_path: Path, query: str, refusal: str
) -> None:
    msi = _build(tmp_path)
    msi_build.verify_msi(msi, arch="amd64")  # as built: accepted
    _tamper(msi, "-q", query)
    with pytest.raises(MsiBuildError, match=refusal):
        msi_build.verify_msi(msi, arch="amd64")


def test_a_built_msi_with_an_extra_file_is_refused(tmp_path: Path) -> None:
    msi = _build(tmp_path)
    [component] = {row[1] for row in _table(msi, "File")}
    _tamper(
        msi,
        "-q",
        "INSERT INTO `File` (`File`, `Component_`, `FileName`, `FileSize`, `Attributes`, "
        f"`Sequence`) VALUES ('extra_ps1', '{component}', 'run.ps1', 10, 512, 3)",
    )
    assert sorted(row[2] for row in _table(msi, "File")) == [
        "its-agent.exe",
        "msi-enroll.json",
        "run.ps1",
    ]
    with pytest.raises(MsiBuildError, match="holds"):
        msi_build.verify_msi(msi, arch="amd64")


@pytest.mark.parametrize(
    ("arch", "template"),
    [("amd64", "Intel;1033"), ("amd64", "Arm64;1033"), ("arm64", "x64;1033")],
)
def test_a_built_msi_whose_template_was_changed_is_refused(
    tmp_path: Path, arch: str, template: str
) -> None:
    msi = _build(tmp_path, arch=arch)
    msi_build.verify_msi(msi, arch=arch)  # as built: accepted
    _tamper(msi, "-s", "ITMS Agent", "Toeshee", template)
    assert _template(msi) == template
    with pytest.raises(MsiBuildError, match="is not marked"):
        msi_build.verify_msi(msi, arch=arch)


@pytest.mark.parametrize(
    "version", ["1.7", "256.0.0", "1.256.0", "1.0.65536", "latest", "1.7.0+build"]
)
def test_a_version_windows_installer_cannot_hold_is_refused(version: str) -> None:
    with pytest.raises(MsiBuildError):
        product_version(version)


def test_a_prerelease_suffix_is_dropped() -> None:
    assert product_version("0.0.1-e2e.1") == "0.0.1"
    assert product_version("1.7.0") == "1.7.0"


@pytest.mark.parametrize(
    "console",
    [
        "http://itms.example.com",
        "https://itms.example.com/",
        "https://itms.example.com/i/x",
        "https://user@itms.example.com",
        "https://itms.example.com:0",
        'https://itms"example.com',
        "https://",
    ],
)
def test_the_settings_refuse_anything_but_a_bare_https_origin(console: str) -> None:
    with pytest.raises(MsiBuildError, match="bare https origin"):
        settings_json(console, CODE)


@pytest.mark.parametrize("code", ["short", CODE + "x", CODE[:-1] + "!", CODE[:-1] + "\n"])
def test_the_settings_refuse_a_code_that_is_not_one_and_do_not_repeat_it(code: str) -> None:
    with pytest.raises(MsiBuildError) as refused:
        settings_json(CONSOLE, code)
    assert code not in str(refused.value)


def test_an_unknown_architecture_is_refused_before_any_tool_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(msi_build.shutil, "which", lambda name: pytest.fail(f"looked for {name}"))
    with pytest.raises(MsiBuildError, match="x86"):
        build_agent_msi(
            exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="x86"
        )


def test_no_tool_sees_the_code_or_this_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_msitools()
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql://its:secret@db/itsuite")
    seen: list[tuple[list[str], dict[str, str], Path, int]] = []
    real = subprocess.run

    def spy(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kw["cwd"])
        seen.append((list(argv), dict(kw["env"]), cwd, cwd.stat().st_mode & 0o777))
        return real(argv, **kw)

    monkeypatch.setattr(msi_build.subprocess, "run", spy)
    build_agent_msi(exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="arm64")
    assert [Path(argv[0]).name for argv, *_ in seen][:2] == ["wixl", "msibuild"]
    for argv, env, cwd, mode in seen:
        assert not any(CODE in arg for arg in argv)
        assert CODE not in "".join(env.values())
        assert "ITS_DATABASE_URL" not in env
        assert mode == 0o700
        assert not cwd.exists()
        # A tool's own scratch files land in the private directory too, never
        # in a shared /tmp where another local user could read them.
        assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(cwd)


def test_the_tools_temporary_directory_is_the_work_directory_even_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The e2e runs this on Windows, where TEMP and TMP would otherwise be
    copied from this process: the shared per-user temp directory."""
    monkeypatch.setenv("TEMP", r"C:\Users\runner\AppData\Local\Temp")
    monkeypatch.setenv("TMP", r"C:\Users\runner\AppData\Local\Temp")
    monkeypatch.setenv("TMPDIR", "/tmp")
    env = msi_build._env(tmp_path)
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(tmp_path)


def test_a_build_that_overruns_is_refused_and_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(msi_build.shutil, "which", lambda name: f"/usr/bin/{name}")
    dirs: list[Path] = []

    def slow(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        dirs.append(Path(kw["cwd"]))
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(msi_build.subprocess, "run", slow)
    with pytest.raises(MsiBuildError, match="did not finish"):
        build_agent_msi(
            exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="amd64"
        )
    assert dirs and not dirs[0].exists()


def test_the_builder_imports_only_the_standard_library() -> None:
    """The Windows e2e runs this file as a script, outside the backend's venv."""
    tree = ast.parse(Path(msi_build.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules - {"__future__"} <= set(sys.stdlib_module_names), modules


def test_the_builder_runs_as_a_script_with_the_code_in_a_file(tmp_path: Path) -> None:
    _need_msitools()
    exe = tmp_path / "its-agent.exe"
    exe.write_bytes(EXE)
    code_file = tmp_path / "code"
    code_file.write_text(CODE)
    out = tmp_path / "x.msi"
    done = subprocess.run(
        [
            sys.executable,
            msi_build.__file__,
            "--exe",
            str(exe),
            "--console",
            CONSOLE,
            "--code-file",
            str(code_file),
            "--org",
            str(ORG),
            "--version",
            "1.7.0",
            "--arch",
            "amd64",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == hashlib.sha256(out.read_bytes()).hexdigest()
    assert CODE not in done.stdout + done.stderr


def test_the_api_image_carries_msitools() -> None:
    """The MSI is built inside the api container (spec §2.2 item 4)."""
    dockerfile = (REPO / "deploy" / "backend.Dockerfile").read_text()
    assert "apt-get install -y --no-install-recommends wixl msitools" in dockerfile
    # Installed as root, before the image drops to the unprivileged user.
    assert dockerfile.index("wixl msitools") < dockerfile.index("USER its")
    # The tool check runs at build time, as root, after the install.
    assert "deploy/check-msitools.sh" in dockerfile
    assert dockerfile.index("check-msitools.sh") < dockerfile.index("USER its")


def _run_msitools_check(tmp_path: Path, wixl_says: str) -> subprocess.CompletedProcess[str]:
    """Run deploy/check-msitools.sh against stand-in tools on a private PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("msibuild", "msiinfo"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "wixl").write_text(f"#!/bin/sh\nprintf '%s\\n' '{wixl_says}'\n")
    (bin_dir / "wixl").chmod(0o755)
    return subprocess.run(
        ["/bin/sh", str(REPO / "deploy" / "check-msitools.sh")],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )


@pytest.mark.parametrize("version", ["0.102", "0.103", "0.106", "0.110", "1.0", "0.102.1"])
def test_the_image_check_accepts_wixl_0_102_and_later(tmp_path: Path, version: str) -> None:
    done = _run_msitools_check(tmp_path, version)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("version", ["0.101", "0.99", "0.9", "0.1", "", "wixl", "v0.106", "0.x"])
def test_the_image_check_refuses_wixl_older_than_0_102_or_unreadable(
    tmp_path: Path, version: str
) -> None:
    done = _run_msitools_check(tmp_path, version)
    assert done.returncode != 0
    assert "check-msitools" in done.stderr


def test_the_image_check_refuses_a_missing_tool(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "wixl").write_text("#!/bin/sh\necho 0.106\n")
    (bin_dir / "wixl").chmod(0o755)
    done = subprocess.run(
        ["/bin/sh", str(REPO / "deploy" / "check-msitools.sh")],
        capture_output=True,
        text=True,
        env={"PATH": str(bin_dir)},
        check=False,
    )
    assert done.returncode != 0
    assert "msibuild is not on PATH" in done.stderr

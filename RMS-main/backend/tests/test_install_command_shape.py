"""Unit tests over `install_command`'s pure string output.

The API tests in `test_agent_release_api.py` cover Windows and Linux against a
real token and a real presign; these exercise the darwin branch (which the API
suite does not mint) and the properties that hold for every platform, without a
database.
"""

import base64

from app.modules.agent_distribution.service import _installer_bytes, install_command

_URL = "https://minio.example/agent-releases/v1/its-agent-linux-amd64?sig=abc"
_SHA = "a" * 64
_TOKEN = "tok-DEADBEEF"
_ENROLL = "https://console.example"


def _cmd(os_name: str) -> str:
    return install_command(
        os_name=os_name,
        download_url=_URL,
        sha256=_SHA,
        token=_TOKEN,
        enroll_url=_ENROLL,
    )


def test_darwin_reuses_the_launchd_installer_and_keeps_the_token_off_argv() -> None:
    cmd = _cmd("darwin")
    # BSD decode + shasum, because macOS ships neither `base64 -d` nor sha256sum.
    assert "base64 -D" in cmd
    assert "shasum -a 256 -c -" in cmd
    # Runs the reviewed darwin/install.sh with the plist beside it.
    assert 'sudo bash "$d/install.sh"' in cmd
    assert '> "$d/com.toeshee.itsagent.plist"' in cmd
    assert "--token-stdin" in cmd
    assert f"--url '{_ENROLL}'" in cmd
    # Token on stdin, never an argument.
    assert f"printf '%s' '{_TOKEN}' | sudo bash" in cmd
    assert f"--token {_TOKEN}" not in cmd
    # Carries the exact reviewed script.
    sh_b64 = base64.b64encode(_installer_bytes("darwin/install.sh")).decode("ascii")
    assert sh_b64 in cmd


def test_every_platform_verifies_the_binary_before_running_the_installer() -> None:
    # Windows computes the digest with .NET (version-independent); unix with the
    # platform's checksum tool. In every case the check precedes the installer.
    win = _cmd("windows")
    assert "Security.Cryptography.SHA256" in win
    assert "Get-FileHash" not in win
    assert win.index("if ($h -ne") < win.index("-File $ps1")

    lin = _cmd("linux")
    assert "sha256sum -c -" in lin
    assert lin.index("sha256sum -c -") < lin.index('sudo bash "$d/install.sh"')


def test_the_enroll_url_reaches_every_platform() -> None:
    assert f"-Url '{_ENROLL}'" in _cmd("windows")
    assert f"--url '{_ENROLL}'" in _cmd("linux")
    assert f"--url '{_ENROLL}'" in _cmd("darwin")


def test_the_windows_command_balances_its_brackets() -> None:
    """The generated PowerShell is not a file packaging_test.go can check, so
    the balance property it guards for install.ps1 is asserted here for the
    wrapper. An unbalanced brace fails at parse time on the endpoint, after the
    paste -- the exact class of defect a command nobody ran on Windows hides.

    Base64 carries no bracket characters, and the token/urls are quoted
    literals, so counting over the whole string is safe here (unlike the .ps1
    files, which need the literal-stripper)."""
    cmd = _cmd("windows")
    depth = 0
    for ch in cmd:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        assert depth >= 0, "closed a bracket that was never opened"
    assert depth == 0, "unbalanced brackets in the generated PowerShell"

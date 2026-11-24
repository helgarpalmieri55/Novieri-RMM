"""The agent's names, the same on both sides (design 2026-11-23-vps-y-marca-
novieri §1.1: "the installer prefix and the service change at the same time on
both sides").

The control plane NAMES what the agent RECOGNISES: the file name the install
link hands a Windows machine is parsed by the agent (`install/mode.go`), the
MSI stops the service the agent registers, the release artefacts the console
stores are the files `release-sign` signs and the release workflow uploads,
and the install command carries byte copies of the packaging. A name changed
on one side only breaks an install with no error on either side's own tests.
These read the Go source and the workflow beside the Python they pair with.
And the names a client's computer shows in lower case (decided 2026-11-24):
the install command's messages, the snapshots, a restore's temporary file.
"""

import re
import uuid
from pathlib import Path

from app.core.storage import agent_release_suffix
from app.modules.agent_distribution import link_page, msi
from app.services import msi_build

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"


def _go_const(rel: str, name: str) -> str:
    source = (AGENT / rel).read_text()
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', source, re.MULTILINE)
    assert match is not None, f"{rel} has no {name}"
    return match.group(1)


def test_the_install_link_names_its_file_as_the_agent_parses_it() -> None:
    prefix = _go_const("internal/install/mode.go", "const FilePrefix")
    assert prefix == "NovieriRMM-Agent-Setup_"
    name = link_page.windows_file_name("rmm.novieri.com", "c" * 32)
    assert name == f"{prefix}rmm.novieri.com_{'c' * 32}.exe"
    assert (
        msi.msi_file_name("rmm.novieri.com", "amd64")
        == "NovieriRMM-Agent-rmm.novieri.com-amd64.msi"
    )


def test_the_service_has_one_name_in_the_agent_and_in_the_msi() -> None:
    service = _go_const("internal/service/service.go", "const Name")
    assert service == "novieri-agent"
    assert _go_const("internal/install/layout/layout.go", "serviceName") == service
    assert _go_const("internal/update/svcparse.go", "windowsService") == service
    assert _go_const("internal/update/svcparse.go", "systemdUnit") == service
    assert _go_const("internal/update/svcparse.go", "helperUnit") == "novieri-agent-update"
    assert _go_const("internal/capabilities/uninstall/system_linux.go", "const helperUnit") == (
        "novieri-agent-uninstall"
    )
    wxs = msi_build.render_wxs(upgrade=uuid.UUID(int=1), version="1.7.0")
    assert f'<ServiceControl Id="StopAgent" Name="{service}"' in wxs
    assert msi_build.AGENT_FILE == f"{service}.exe"
    assert msi_build.PRODUCT_NAME == "Novieri RMM Agent"
    assert msi_build.MANUFACTURER == "Novieri"
    assert _go_const("internal/service/service.go", "const DisplayName") == "Novieri RMM Agent"


def test_the_msi_is_a_new_product() -> None:
    """A new UpgradeCode namespace: the MSI is "Novieri RMM Agent", and no
    "ITMS Agent" MSI is assigned anywhere to upgrade (operator decision B1)."""
    assert msi_build.UPGRADE_NAMESPACE == uuid.UUID("8814cc17-6469-4df7-93f9-0f75ecdbb6a9")


def test_the_launchd_label_is_the_plists_file_name_everywhere() -> None:
    label = _go_const("internal/update/svcparse.go", "launchdLabel")
    assert label == "com.novieri.rmm.agent"
    assert (AGENT / "packaging/darwin" / f"{label}.plist").is_file()
    copies = REPO / "backend/app/modules/agent_distribution/installers"
    assert (copies / "darwin" / f"{label}.plist").is_file()
    assert (copies / "linux" / "novieri-agent.service").is_file()


def test_a_release_artefact_has_one_name_from_the_build_to_the_bucket() -> None:
    assert agent_release_suffix("1.7.0", "linux", "amd64").endswith("/novieri-agent-linux-amd64")
    sign = (AGENT / "cmd/release-sign/main.go").read_text()
    assert 'parts[0] != "novieri" || parts[1] != "agent"' in sign
    build = (AGENT / "packaging/build.sh").read_text()
    assert 'out="$dist/novieri-agent-${goos}-${goarch}"' in build
    release = (REPO / ".github/workflows/release-agent.yml").read_text()
    assert 'files+=("dist/novieri-agent-$t")' in release


def test_the_paths_on_an_endpoint_are_novieris() -> None:
    assert _go_const("internal/install/layout/push.go", "UnixPushStagingDir") == "/opt/novieri"
    config = (AGENT / "internal/config/config.go").read_text()
    assert 'filepath.Join(base, "NovieriRMM", "agent")' in config
    assert 'filepath.Join("/var", "lib", "novieri-agent")' in config
    backup = AGENT / "internal/capabilities/backup"
    snapshot = (backup / "snapshot.go").read_text()
    assert 'snapTagPrefix = "novieri-snap-"' in snapshot
    # A sibling of the data directory, as before: never inside it, where the
    # uninstaller's RemoveAll would walk into a stale read-only mount.
    assert 'snapMountRoot = "/var/lib/novieri-agent-snap"' in snapshot
    assert 'btrfsSnapDir = ".novieri-snap-"' in snapshot
    assert 'snapNamePrefix = "novieri-snap-"' in (backup / "snapshot_auto.go").read_text()
    assert '".novieri-restore-*"' in (backup / "restore.go").read_text()
    control = (AGENT / "packaging/linux/debian/control.in").read_text()
    assert control.startswith("Package: novieri-agent\n")
    assert "Maintainer: Novieri <it@novieri.example>" in control
    assert "Homepage:" not in control
    assert "Description: Novieri RMM endpoint agent" in control
    # The uninstaller removes the package by the name it was built with.
    linux = (AGENT / "internal/capabilities/uninstall/system_linux.go").read_text()
    assert '"dpkg", "--purge", "novieri-agent"' in linux
    assert '"/var/lib/dpkg/info/novieri-agent.list"' in linux


def test_the_install_command_speaks_as_the_agent() -> None:
    """What a person reads in the terminal of the computer being added."""
    script = link_page.install_script(
        console="https://rmm.novieri.com", code="c" * 32, releases={}, presign=lambda _r: ""
    )
    assert 'echo "novieri-agent: run this with sudo" >&2' in script
    assert "novieri-agent: installed. This computer appears in Novieri RMM" in script
    # The base64 blobs of the embedded packaging could contain the letters by
    # chance; every other line is strict.
    prose = [ln for ln in script.splitlines() if not re.search(r"printf %s '[A-Za-z0-9+/=]+'", ln)]
    assert not any("itms" in ln.lower() for ln in prose)

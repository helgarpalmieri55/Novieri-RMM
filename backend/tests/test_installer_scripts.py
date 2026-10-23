"""The backend ships byte-identical copies of the agent installer scripts, and
this is the guard that keeps them identical.

WHY A COPY EXISTS AT ALL. The one-machine install command has to reach a machine
that holds neither this repository nor a credential, so the installer travels
WITH the command (base64-encoded inside it). The backend cannot read the
originals at runtime: `deploy/backend.Dockerfile` copies `backend/app` and
nothing else, so `agent/packaging/` is simply not in the image. The copies under
`app/modules/agent_distribution/installers/` are what the command carries.

WHY THIS TEST IS LOAD-BEARING. A copy that drifts from its original is worse
than no copy: the console would hand out a command that installs a DIFFERENT
script from the one `agent/packaging/packaging_test.go` reviews for its brackets,
its ACL calls and its unenrolled-start guard. This test fails the moment they
diverge, so the two are edited together or not at all. It runs in CI, where the
full repository is checked out; in production only the copy is present, which is
the point.
"""

from pathlib import Path

import pytest

from app.modules.agent_distribution.service import _INSTALLERS_DIR

# backend/tests/ -> backend/ -> repo root -> agent/packaging
_PACKAGING = Path(__file__).resolve().parents[2] / "agent" / "packaging"

# (bundled copy relative to _INSTALLERS_DIR, original relative to agent/packaging)
_PAIRS = [
    ("windows/install.ps1", "windows/install.ps1"),
    ("linux/install.sh", "linux/install.sh"),
    ("linux/its-agent.service", "linux/its-agent.service"),
    ("darwin/install.sh", "darwin/install.sh"),
    ("darwin/com.toeshee.itsagent.plist", "darwin/com.toeshee.itsagent.plist"),
]


@pytest.mark.parametrize("bundled_rel,original_rel", _PAIRS)
def test_bundled_installer_matches_packaging(bundled_rel: str, original_rel: str) -> None:
    bundled = (_INSTALLERS_DIR / bundled_rel).read_bytes()
    original = (_PACKAGING / original_rel).read_bytes()
    assert bundled == original, (
        f"{bundled_rel} has drifted from agent/packaging/{original_rel}. "
        "The install command carries the bundled copy verbatim; edit both "
        "together, or the console hands out a script packaging_test.go never "
        "reviewed."
    )


def test_every_bundled_file_is_pinned() -> None:
    """Non-vacuity: a new file under installers/ that no pair covers would ship
    unchecked. Every bundled file must appear in _PAIRS."""
    bundled = {
        str(p.relative_to(_INSTALLERS_DIR))
        for p in _INSTALLERS_DIR.rglob("*")
        if p.is_file()
    }
    pinned = {b for b, _ in _PAIRS}
    assert bundled == pinned, (
        f"unpinned installer files: {bundled - pinned}; add each to _PAIRS so "
        "its drift from agent/packaging/ is caught"
    )

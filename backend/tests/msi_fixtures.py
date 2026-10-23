"""Shared helpers for the Group Policy MSI tests (not a test module)."""

import base64
import hashlib
import os
import shutil
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.storage import ObjectStore
from app.modules.agent_distribution import msi
from app.modules.agent_distribution.link_page import LinkRelease
from app.modules.agent_distribution.msi import release_message

MSITOOLS = ("wixl", "msibuild", "msiinfo", "msiextract")


def need_msitools() -> None:
    """Skip without msitools -- except in CI, where ITS_TEST_REQUIRE_MSITOOLS=1
    makes a missing tool a failure rather than a quiet skip."""
    missing = [tool for tool in MSITOOLS if shutil.which(tool) is None]
    if missing:
        if os.environ.get("ITS_TEST_REQUIRE_MSITOOLS") == "1":
            pytest.fail(f"ITS_TEST_REQUIRE_MSITOOLS=1 but {missing} are not on PATH")
        pytest.skip(f"msitools is not installed ({missing})")


@dataclass
class SignedRelease:
    version: str
    arch: str
    data: bytes
    sha256: str
    signature: str
    public_key: str


def signed_release(version: str = "1.7.0", arch: str = "amd64") -> SignedRelease:
    """A Windows agent build and its offline release signature, under a key
    made for this test."""
    key = Ed25519PrivateKey.generate()
    data = b"MZ" + os.urandom(4096)
    sha = hashlib.sha256(data).hexdigest()
    signature = key.sign(release_message(version, "windows", arch, sha))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return SignedRelease(
        version,
        arch,
        data,
        sha,
        base64.b64encode(signature).decode(),
        base64.b64encode(public).decode(),
    )


def offer(monkeypatch: pytest.MonkeyPatch, *releases: SignedRelease) -> None:
    """Make these the releases the link offers. The catalogue is GLOBAL and
    other tests publish into it, so the pick is replaced -- as
    `test_install_link_page.py`'s `releases` fixture does -- and the pick
    itself stays covered there (`pick_link_releases`)."""

    async def fake(_session: Any) -> dict[tuple[str, str], LinkRelease]:
        return {
            ("windows", r.arch): LinkRelease(r.version, "windows", r.arch, r.sha256, r.signature)
            for r in releases
        }

    monkeypatch.setattr(msi, "link_releases", fake)


class StoreWith(ObjectStore):
    """An object store holding exactly the given artefacts, by identity."""

    def __init__(self, objects: dict[tuple[str, str, str], bytes]) -> None:  # noqa: D107
        self.objects = objects
        self.reads: list[tuple[str, str, str]] = []

    async def get_agent_release(self, version: str, os_name: str, arch: str) -> bytes:
        self.reads.append((version, os_name, arch))
        return self.objects[(version, os_name, arch)]

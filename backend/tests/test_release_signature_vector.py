"""The server verifies a release signature exactly as the agent does
(`agent/internal/update/releasesig.go`). One vector, two verifiers: the Go
twin is `TestReleaseSignatureVector`. A drift in either message format fails
one of the two, instead of every MSI download failing closed in production."""

import base64
import json
from pathlib import Path

import pytest

from app.modules.agent_distribution.link_page import LinkRelease
from app.modules.agent_distribution.msi import (
    MsiRefused,
    release_message,
    release_public_key,
    verify_release_bytes,
)

REPO = Path(__file__).resolve().parents[2]
VECTOR = json.loads(
    (REPO / "docs/kernel/wire-protocol/testdata/release-signature.json").read_text()
)


def test_the_shared_vector_verifies() -> None:
    key = release_public_key(VECTOR["public_key"])
    message = release_message(VECTOR["version"], VECTOR["os"], VECTOR["arch"], VECTOR["sha256"])
    assert message == (
        b"its-agent-release/v1\n1.6.0\nwindows\namd64\n" + VECTOR["sha256"].encode() + b"\n"
    )
    key.verify(base64.b64decode(VECTOR["signature"]), message)


def test_every_further_vector_verifies_as_given() -> None:
    """`more_vectors` carries a leading v and upper-case hex, as given. The
    Go twin is `TestReleaseSignatureVectorWithVPrefixAndUpperCaseHex`."""
    more = VECTOR["more_vectors"]
    assert more, "release-signature.json has no more_vectors"
    assert any(v["version"].startswith("v") for v in more)
    assert any(v["sha256"] != v["sha256"].lower() for v in more)
    for v in more:
        key = release_public_key(v["public_key"])
        message = release_message(v["version"], v["os"], v["arch"], v["sha256"])
        key.verify(base64.b64decode(v["signature"]), message)
        # Through the MSI's own check too: the release row as the catalogue
        # would hold it, and bytes whose digest is the vector's.
        release = LinkRelease(v["version"], v["os"], v["arch"], v["sha256"], v["signature"])
        with pytest.raises(MsiRefused, match="does not match its published SHA-256"):
            verify_release_bytes(b"not the release", release, key)


def test_the_second_vector_is_signed_over_the_canonical_message() -> None:
    [v] = VECTOR["more_vectors"]
    assert release_message(v["version"], v["os"], v["arch"], v["sha256"]) == (
        b"its-agent-release/v1\n1.7.0\nwindows\narm64\n" + v["sha256"].lower().encode() + b"\n"
    )


def test_a_leading_v_and_upper_case_hex_sign_the_same_message() -> None:
    assert release_message("v1.6.0", "windows", "amd64", VECTOR["sha256"].upper()) == (
        release_message("1.6.0", "windows", "amd64", VECTOR["sha256"])
    )


def test_bytes_that_are_not_the_releases_are_refused_before_the_signature() -> None:
    key = release_public_key(VECTOR["public_key"])
    release = LinkRelease("1.6.0", "windows", "amd64", VECTOR["sha256"], VECTOR["signature"])
    with pytest.raises(MsiRefused, match="does not match its published SHA-256"):
        verify_release_bytes(b"not the release", release, key)


@pytest.mark.parametrize("value", [None, "", "   ", "not base64!", "AAAA"])
def test_a_missing_or_malformed_public_key_is_a_503(value: str | None) -> None:
    with pytest.raises(MsiRefused) as refused:
        release_public_key(value)
    assert refused.value.status == 503


def test_the_setting_is_documented_where_the_operator_edits() -> None:
    assert "\nITS_AGENT_RELEASE_PUBLIC_KEY=\n" in (REPO / "deploy" / ".env.example").read_text()

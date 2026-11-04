"""The shipped password blocklist (`app/core/auth/data/blocklist-12.txt.gz`;
design §2.1; plan decision D17).

The file is data nobody reads by eye, so what it is gets pinned here:

- its CONTENT is exactly what `blocklist-12.PROVENANCE` says the build made:
  515,286 entries and the SHA-256 of the decompressed bytes (not of the .gz,
  which another zlib may compress differently);
- every entry obeys the build's rules: normalised as a lookup is (NFC, lower
  case), 12 to 128 code points, unique, sorted by code point;
- the build script does what the PROVENANCE says, on a small source;
- its licence and its source travel with it, and agree with the script;
- `is_blocklisted` finds an entry whole, in any case, and nothing else.
"""

import gzip
import hashlib
import importlib.util
import secrets
from pathlib import Path
from types import ModuleType

from app.core.auth.blocklist import (
    LIST_PATH,
    MAX_ENTRY_LENGTH,
    MIN_ENTRY_LENGTH,
    is_blocklisted,
    normalise,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = LIST_PATH.parent
#: What `scripts/build_password_blocklist.py` printed (PROVENANCE, "Result").
CONTENT_SHA256 = "74234a19cf3c94a8a7b8a9d6f790f88ce8f3a55aebb1fc547bd174775c5bce5a"
ENTRIES = 515_286
CONTENT_BYTES = 7_637_957
#: SecLists' LICENSE at the pinned commit, copied verbatim.
LICENSE_SHA256 = "3dbdc93d5f8829de0941744841730a09c106d0732e5ae0e98ca1d77be7ded66c"


def _content() -> bytes:
    return gzip.decompress(LIST_PATH.read_bytes())


def _build_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "build_password_blocklist.py"
    spec = importlib.util.spec_from_file_location("build_password_blocklist", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_list_is_the_one_its_provenance_describes() -> None:
    content = _content()
    assert len(content) == CONTENT_BYTES
    assert hashlib.sha256(content).hexdigest() == CONTENT_SHA256
    assert content.count(b"\n") == ENTRIES


def test_every_entry_obeys_the_build_rules() -> None:
    lines = _content().decode("utf-8").split("\n")
    assert lines[-1] == ""  # one entry per line, the last one terminated
    entries = lines[:-1]
    assert entries == sorted(set(entries))  # unique, by code point
    for entry in entries:
        assert normalise(entry) == entry, entry
        assert MIN_ENTRY_LENGTH <= len(entry) <= MAX_ENTRY_LENGTH, entry


def test_the_provenance_and_the_licence_travel_with_it() -> None:
    script = _build_script()
    provenance = (DATA / "blocklist-12.PROVENANCE").read_text()
    assert script.SOURCE_URL in provenance.replace("\n  ", "")
    assert script.SOURCE_SHA256 in provenance
    assert CONTENT_SHA256 in provenance
    assert f"{ENTRIES:,}" in provenance
    assert "public domain" in provenance
    licence = (DATA / "blocklist-12.LICENSE").read_bytes()
    assert hashlib.sha256(licence).hexdigest() == LICENSE_SHA256
    assert b"MIT License" in licence


def test_the_build_does_what_the_provenance_says(tmp_path: Path) -> None:
    script = _build_script()
    source = [
        "Short1",  # under 12
        "PasswordPassword",  # lower-cased ...
        "passwordpassword",  # ... and then a duplicate
        "Cafe\u0301cafe\u0301cafe\u0301",  # decomposed: 15 code points, NFC makes it 12
        "x" * (MAX_ENTRY_LENGTH + 1),  # over 128
        "",
    ]
    words = script.entries(source)
    assert words == ["caf\u00e9caf\u00e9caf\u00e9", "passwordpassword"]

    first, second = tmp_path / "a.gz", tmp_path / "b.gz"
    script.write(first, words)
    script.write(second, words)
    assert first.read_bytes() == second.read_bytes()  # no name, no timestamp
    expected = "caf\u00e9caf\u00e9caf\u00e9\npasswordpassword\n"
    assert gzip.decompress(first.read_bytes()) == expected.encode()


def test_a_listed_password_is_found_whole_and_in_any_case() -> None:
    first = _content().split(b"\n", 1)[0].decode()
    assert is_blocklisted(first)
    assert is_blocklisted("password1234")
    assert is_blocklisted("PASSWORD1234")
    # The whole password, not a part of it (NIST §3.1.1.2).
    assert not is_blocklisted("password1234 and then some words")
    assert not is_blocklisted(secrets.token_urlsafe(24))

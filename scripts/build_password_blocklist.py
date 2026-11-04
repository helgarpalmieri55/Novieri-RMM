#!/usr/bin/env python3
"""Build `backend/app/core/auth/data/blocklist-12.txt.gz` from SecLists'
`xato-net-10-million-passwords.txt` (own authentication plan, decision D17).

Run from the repository root with the backend venv:

    backend/.venv/bin/python scripts/build_password_blocklist.py SOURCE OUTPUT

SOURCE must be the file pinned below, byte for byte (its SHA-256 is checked
first); OUTPUT is written as gzip with no name and no timestamp, so the same
source always gives the same content. Each line of the source is normalised
exactly as `app.core.auth.blocklist.normalise` looks a password up (NFC, then
lower case) and kept when it has 12 to 128 code points; the result is
de-duplicated, sorted by code point and written one entry per line.
`backend/app/core/auth/data/blocklist-12.PROVENANCE` records the run, and
`backend/tests/test_auth_blocklist.py` pins what it produced.
"""

import gzip
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.auth.blocklist import (  # noqa: E402
    MAX_ENTRY_LENGTH,
    MIN_ENTRY_LENGTH,
    normalise,
)

SOURCE_URL = (
    "https://raw.githubusercontent.com/danielmiessler/SecLists/"
    "4afe4d3de0a5d227c59ae2f162d22b93b4c27bfd/"
    "Passwords/Common-Credentials/xato-net-10-million-passwords.txt"
)
SOURCE_SHA256 = "c95989fecfae9c91aa9f5c730344d5cabd6335bb094c9c60aba16bf1742a72fe"


def entries(lines: list[str]) -> list[str]:
    """The list's entries from the source's lines, in order."""
    kept = {normalise(line) for line in lines}
    return sorted(e for e in kept if MIN_ENTRY_LENGTH <= len(e) <= MAX_ENTRY_LENGTH)


def write(path: Path, words: list[str]) -> None:
    data = "".join(f"{word}\n" for word in words).encode("utf-8")
    with path.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
    ) as out:
        out.write(data)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    source, output = Path(argv[1]), Path(argv[2])
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256:
        print(f"{source}: SHA-256 {digest}, expected {SOURCE_SHA256} ({SOURCE_URL})")
        return 1
    words = entries(raw.decode("utf-8").split("\n"))
    write(output, words)
    content = "".join(f"{word}\n" for word in words).encode("utf-8")
    print(f"{len(words)} entries, content SHA-256 {hashlib.sha256(content).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

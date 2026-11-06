"""The list of common and leaked passwords a new password is compared with
(design §2.1; NIST SP 800-63B-4 §3.1.1.2; plan decision D17).

THE LIST is SecLists' `xato-net-10-million-passwords.txt` (Mark Burnett's
2015 corpus, released to the public domain; SecLists is MIT), reduced to what
can matter here: NIST Appendix A, "since a minimum length requirement will
also govern the user's choice of passwords, this dictionary only needs to
include entries that meet that requirement". The staff minimum is 12, and the
contacts' 15 (phase 2) is a subset. `data/blocklist-12.PROVENANCE` says where
the file comes from and how `scripts/build_password_blocklist.py` makes it;
`tests/test_auth_blocklist.py` pins its content.

THE COMPARISON IS THE WHOLE PASSWORD ("The entire password SHALL be subject to
comparison, not substrings"), after the same normalisation the list was built
with: Unicode NFC (NIST: "the verifier SHOULD apply the normalization process
... NFC"), then lower case. Case-folded on purpose: `Password1234` is the same
guess as `password1234` to anyone who tries the list.

IN MEMORY, AN 8-BYTE DIGEST PER ENTRY, not the strings: 515,286 BLAKE2b-64
digests in one sorted `array` are 4 MB, where a `set` of the strings is about
45 MB per process (measured). A false match needs a 64-bit collision with one
of half a million entries -- about 3e-14 per password -- and its only effect is
asking for another password. Loaded on first use (~0.5 s), then kept.
"""

import gzip
import hashlib
import unicodedata
from array import array
from bisect import bisect_left
from functools import cache
from pathlib import Path

#: The shipped list (`scripts/build_password_blocklist.py` writes it).
LIST_PATH = Path(__file__).parent / "data" / "blocklist-12.txt.gz"
#: The entries' lengths, in code points: the staff minimum and the maximum a
#: password may have (`app.core.auth.passwords.MAX_LENGTH`).
MIN_ENTRY_LENGTH = 12
MAX_ENTRY_LENGTH = 128


def normalise(plain: str) -> str:
    """NFC, then lower case: how an entry is stored and how a password is looked up."""
    return unicodedata.normalize("NFC", plain).lower()


def _digest(entry: str) -> int:
    return int.from_bytes(hashlib.blake2b(entry.encode("utf-8"), digest_size=8).digest(), "big")


@cache
def _digests() -> array[int]:
    with gzip.open(LIST_PATH, "rt", encoding="utf-8", newline="\n") as lines:
        return array("Q", sorted(_digest(line.rstrip("\n")) for line in lines))


def is_blocklisted(plain: str) -> bool:
    """Whether the whole password, normalised, is on the list."""
    entry = normalise(plain)
    if not MIN_ENTRY_LENGTH <= len(entry) <= MAX_ENTRY_LENGTH:
        return False
    digests = _digests()
    wanted = _digest(entry)
    at = bisect_left(digests, wanted)
    return at < len(digests) and digests[at] == wanted

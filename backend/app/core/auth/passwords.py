"""Passwords: the rules for choosing one, and the verifier stored for it
(design §2.1; NIST SP 800-63B-4 §3.1.1.2; plan decision D6).

CHOOSING (`check_new_password`). At least `minimum` characters -- 12 for a
technician, who also has a second factor; 15 for a contact without one (phase
2) -- and at most `MAX_LENGTH`, counted in code points after NFC ("Each
Unicode code point SHALL be counted as a single character"). No composition
rule. Then the whole password against the blocklist (`app.core.auth.
blocklist`), and against the context: the person's address and the
platform's and organization's names, "and derivatives thereof" (NIST).
A derivative is a password that, once every context word in it is taken out,
is shorter than the minimum: the words add nothing an attacker would not try
first, so the rest has to stand on its own. A refusal says which rule
(`PasswordRejected.message`): "the CSP ... SHALL provide the reason for
rejection".

STORING (`hash_password`). argon2id with argon2-cffi's defaults -- RFC 9106's
second recommended option, 64 MiB, t=3, p=4, a 16-byte salt -- over
HMAC-SHA256(pepper, password): `app.core.auth.keys.pepper_hmac`, the OpenBao
transit key, bound to the organization. So the stored string is useless
without OpenBao, and a hash copied into another organization does not
verify. Before both, NFC: "This process is applied before hashing the byte
string that represents the password", so a password typed with a combining
accent on one keyboard verifies on another.

argon2 runs in a worker thread (`asyncio.to_thread`): about 50 ms of CPU and
64 MiB per call must not stall the event loop. A failed or corrupt
verification is `False`, never an exception a caller could turn into a
different answer (design §2.3: same answer whether the account exists).
`verify_against_nobody` does the same work for an address that names no
one, so the two take the same time.
"""

import asyncio
import re
import unicodedata
from collections.abc import Sequence
from functools import cache
from secrets import token_bytes
from typing import Literal
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.auth import keys
from app.core.auth.blocklist import is_blocklisted, normalise
from app.core.i18n import tr

#: The longest password accepted (design §2.1), in code points after NFC.
MAX_LENGTH = 128
#: A technician's minimum (design §2.1); a contact's is 15 (§3, phase 2).
STAFF_MINIMUM = 12
#: Context words shorter than this are not looked for: "ana" is inside too
#: many unrelated words to say anything.
CONTEXT_MIN_WORD = 4
#: The platform's own names, always context.
SERVICE_WORDS = ("itms", "novieri")
#: Domain separation inside the pepper's HMAC.
_DOMAIN = b"itms/password/v1\x00"

Reason = Literal["too_short", "too_long", "blocklisted", "context"]

#: argon2-cffi's defaults: `argon2.profiles.RFC_9106_LOW_MEMORY`.
_HASHER = PasswordHasher()


class PasswordRejected(Exception):
    """A new password breaks one of the rules; `reason` says which."""

    def __init__(self, *, reason: Reason, minimum: int) -> None:
        super().__init__(reason)
        self.reason: Reason = reason
        self.minimum = minimum

    @property
    def message(self) -> str:
        """What the person is told (NIST: the reason SHALL be given)."""
        if self.reason == "too_short":
            return tr("Use at least {minimum} characters.", minimum=self.minimum)
        if self.reason == "too_long":
            return tr("Use at most {maximum} characters.", maximum=MAX_LENGTH)
        if self.reason == "blocklisted":
            return tr(
                "This password is on a list of common or leaked passwords. Choose another one: "
                "several unrelated words together work well."
            )
        return tr(
            "This password is too close to your email address or to the platform's name. "
            "Choose one that does not build on them."
        )


def context_words(email: str, *names: str) -> tuple[str, ...]:
    """The context a new password is checked against: the address whole, its
    local part and domain and their pieces, `names` (the organization's,
    the person's) and the platform's own names -- lower case, at least
    `CONTEXT_MIN_WORD` characters."""
    address = normalise(email.strip())
    local, _, domain = address.partition("@")
    words = {address, local, domain, *re.split(r"[._+-]", local), *domain.split(".")}
    for name in names:
        folded = normalise(name.strip())
        words.add(folded)
        words.update(folded.split())
    words.update(SERVICE_WORDS)
    return tuple(sorted(w for w in words if len(w) >= CONTEXT_MIN_WORD))


def _builds_on(password: str, context: Sequence[str], minimum: int) -> bool:
    rest = normalise(password)
    for word in sorted({normalise(w) for w in context}, key=len, reverse=True):
        if len(word) >= CONTEXT_MIN_WORD:
            rest = rest.replace(word, "")
    return len(rest) < minimum


def check_new_password(plain: str, *, minimum: int, context: Sequence[str]) -> None:
    """Raise `PasswordRejected` unless `plain` may be chosen. Loads the
    blocklist on first use (~0.5 s): call it through `asyncio.to_thread`."""
    password = unicodedata.normalize("NFC", plain)
    if len(password) < minimum:
        raise PasswordRejected(reason="too_short", minimum=minimum)
    if len(password) > MAX_LENGTH:
        raise PasswordRejected(reason="too_long", minimum=minimum)
    if is_blocklisted(password):
        raise PasswordRejected(reason="blocklisted", minimum=minimum)
    if _builds_on(password, context, minimum):
        raise PasswordRejected(reason="context", minimum=minimum)


def _secret(plain: str) -> bytes:
    return _DOMAIN + unicodedata.normalize("NFC", plain).encode("utf-8")


async def hash_password(org_id: UUID, plain: str) -> tuple[str, int]:
    """The verifier to store for `plain`, and the pepper version it used."""
    mac, version = await keys.pepper_hmac(org_id, _secret(plain))
    stored = await asyncio.to_thread(_HASHER.hash, mac)
    return stored, version


def _matches(stored: str, mac: bytes) -> bool:
    try:
        return _HASHER.verify(stored, mac)
    except (InvalidHashError, VerificationError, ValueError):
        # VerifyMismatchError, the ordinary wrong password, is a
        # VerificationError; a corrupt verifier is no match either. So is a
        # non-ASCII one: argon2-cffi encodes the hash as ASCII before parsing
        # and raises UnicodeEncodeError, a ValueError.
        return False


async def verify_password(org_id: UUID, plain: str, stored: str, pepper_version: int) -> bool:
    """Whether `plain` is the password `stored` was made from (with the
    pepper of `pepper_version`). The comparison inside argon2 is constant
    time."""
    mac, _ = await keys.pepper_hmac(org_id, _secret(plain), version=pepper_version)
    return await asyncio.to_thread(_matches, stored, mac)


@cache
def _nobody() -> str:
    return _HASHER.hash(token_bytes(32))


async def verify_against_nobody(org_id: UUID, plain: str) -> Literal[False]:
    """The work of `verify_password` -- one pepper HMAC, one argon2 verify with
    the same parameters -- for an address that names no one. Always False."""
    mac, _ = await keys.pepper_hmac(org_id, _secret(plain))
    stored = await asyncio.to_thread(_nobody)
    await asyncio.to_thread(_matches, stored, mac)
    return False

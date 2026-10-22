"""Secret-name detection, shared by every gate that must not leak one.

PROMOTED OUT OF `app/core/ai/scrub.py` (2026-08-14) so the AI tool gate and the
LOG SINK use one predicate rather than two that drift. `scrub.py` re-exports it,
so every existing import path still resolves.

NOTHING HERE IMPORTS `app.core.ai`. That is the point of the move: a kernel
module the logger can depend on without dragging the AI layer into every
process that writes a log line.
"""

import re
import unicodedata

# Characters that are invisible inside a key name and change nothing about what
# it means to a reader: the zero-width space, the two joiners, the word joiner
# and the BOM. NFKC leaves them in place, so they are removed explicitly.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

# One key name -> its words. Splits on ANY non-alphanumeric separator, on
# camelCase humps, and on digit runs, so `api_key`, `apiKey`, `APIKey`, `APIKEY`
# and `token2` all yield the words the author was thinking in. This is what
# `\btoken\b` and the old `(?<![a-z0-9])token(?![a-z0-9])` were both
# approximating, and both missed every camelCase spelling -- which is how
# `apiKey` passed a gate whose whole job was `api_key`.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")

# The same key with the case thrown away first: separators only. See
# `_decompositions` -- this is what keeps `sEcReT` from splitting into four
# meaningless fragments and matching nothing.
_FLAT_WORD = re.compile(r"[a-z0-9]+")

# §7.3 clause 2's vocabulary, as WHOLE WORDS rather than substrings. Whole-word
# matching is what keeps `tokenizer`, `tokenised_body` and `detokenize` out of
# the blocked half without a lookaround: each tokenizes to one word, and that
# word is not `token`. `test_token_is_matched_as_a_whole_word` pins both sides,
# and it is the ONLY test that can tell this from a substring match.
#
# NAMES THAT ARE ONLY SOMETIMES SECRETS ARE IN, AND THE FALSE POSITIVE IS THE
# POINT: `signature`, `nonce`, `salt`, `seed`, `pin` and a bare `key` also name
# ordinary fields, and blocking a tool result is the cheap failure. The registry
# test `test_no_declared_response_field_is_secret_shaped` makes an over-block a
# red build rather than a silent degradation in production.
_SECRET_WORDS: frozenset[str] = frozenset(
    {
        # Passwords, by every spelling anyone uses.
        "password",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "passcode",
        # Secrets and credentials by name. `credential` is a whole word, so
        # `credential_id` blocks too -- §0.5 excludes even credential metadata
        # from the agent, so that is the intended answer and not a regression.
        "secret",
        "credential",
        # Bearer material.
        "token",
        "jwt",
        "bearer",
        "authorization",
        "cookie",
        # Key material. `key` as a whole word covers `api_key`, `apiKey`,
        # `private_key`, `session_key`, `signing_key`, `encryption_key`,
        # `shared_key`, `vault_key`, `cert_key`, `wpa_key` and `key_material`
        # in one rule; the entries after it are the spellings that run the two
        # words together, which the splitter cannot separate.
        "key",
        "keypair",
        "keyring",
        "keystore",
        "keyfile",
        "keymaterial",
        "apikey",
        "privatekey",
        "secretkey",
        "accesskey",
        "sharedkey",
        "psk",
        "dek",
        "kek",
        # Raw cryptographic material.
        "ciphertext",
        "cyphertext",
        "salt",
        "nonce",
        "signature",
        "seed",
        "entropy",
        # One-time and second-factor material.
        "otp",
        "totp",
        "hotp",
        "pin",
        # A DSN carries a password in its userinfo far more often than not.
        "dsn",
    }
)

# Adjacent word pairs that are credentials only together. An SNMP community
# string IS a credential and a connection string embeds one; neither has a word
# that could join the set above without blocking every `connection` and every
# `string` field in the estate.
_SECRET_WORD_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("community", "string"),
        ("connection", "string"),
        ("recovery", "code"),
        ("backup", "code"),
        ("activation", "code"),
    }
)


def _decompositions(key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A key name's words, TWO WAYS, because each way alone has a hole.

    NFKC folds the compatibility forms -- a fullwidth `password` becomes
    `password` -- and the invisible joiners are removed first, so a
    zero-width-joined `password` is one word rather than two. Neither trick
    survives into either decomposition.

      * CASE-SPLIT: `apiKey` -> `api`, `key`. This is what catches every
        camelCase spelling, which is how 35 of 48 measured secret names got past
        the pattern that required `_`. Its hole is ERRATIC case: `sEcReT` splits
        into `s`, `ec`, `re`, `t` and matches nothing.
      * FLATTENED: lowercase first, then split on separators only. `sEcReT` ->
        `secret`. Its hole is the case-split's whole job: `apiKey` stays
        `apikey`, one word.

    Neither is sound alone and the union has neither hole, which is why both are
    returned rather than one being picked. `test_matching_is_case_insensitive`
    is the test that found this -- with only the case-split, `sEcReT` passed.

    THIS DOES NOT FOLD HOMOGLYPHS. A Cyrillic spelling stays Cyrillic and is not
    matched here; it is blocked by the declared-contract check instead, which is
    the honest division of labour and the reason that check is the control.
    """
    normalized = unicodedata.normalize("NFKC", key).translate(_INVISIBLE)
    case_split = tuple(word.lower() for word in _WORD.findall(normalized))
    flattened = tuple(_FLAT_WORD.findall(normalized.lower()))
    return case_split, flattened


def looks_like_secret_key(key: str) -> bool:
    """§7.3 clause 2, as a predicate so its NEGATIVE half can be asserted
    directly rather than inferred from a whole payload's outcome."""
    for words in _decompositions(key):
        for word in words:
            # The plural of a secret is a secret: `tokens`, `keys`,
            # `credentials`. Only stripped when the singular is a known word, so
            # `pass` and `passes` stay distinct.
            if word in _SECRET_WORDS or (word.endswith("s") and word[:-1] in _SECRET_WORDS):
                return True
        if any(pair in _SECRET_WORD_PAIRS for pair in zip(words, words[1:], strict=False)):
            return True
    return False

"""Choosing and storing a password (`app.core.auth.passwords`; design §2.1;
NIST SP 800-63B-4 §3.1.1.2; plan Task 2).

- the verifier is argon2id with argon2-cffi's defaults over an HMAC with the
  pepper: useless without the pepper, bound to the organization, honouring
  the stored pepper version, and NFC-normalised;
- a wrong or corrupt verifier is `False`, never an exception;
- `verify_against_nobody` does the same work and is always `False`;
- the rules: length in code points, the blocklist (whole, any case), the
  context and its derivatives, and a reason the person can read, in both
  languages.
"""

import secrets
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.auth import keys, passwords
from app.core.auth.keys import LocalAuthKeys
from app.core.auth.passwords import (
    MAX_LENGTH,
    STAFF_MINIMUM,
    PasswordRejected,
    check_new_password,
    context_words,
    hash_password,
    verify_against_nobody,
    verify_password,
)
from app.core.i18n import use_locale

PEPPER = b"\x07" * 32
SIGNING = {1: ec.generate_private_key(ec.SECP256R1())}


@pytest.fixture()
def local() -> Iterator[LocalAuthKeys]:
    backend = LocalAuthKeys(pepper={1: PEPPER}, signing=SIGNING)
    keys.set_backend(backend)
    yield backend
    keys.set_backend(None)


async def test_a_password_verifies_and_another_does_not(local: Any) -> None:
    org = uuid4()
    stored, version = await hash_password(org, "una frase larga de prueba")

    assert version == 1
    assert await verify_password(org, "una frase larga de prueba", stored, version)
    assert not await verify_password(org, "una frase larga de prueba!", stored, version)


async def test_the_verifier_is_argon2id_with_the_library_defaults(local: Any) -> None:
    """RFC 9106's second recommended option: 64 MiB, t=3, p=4 (design §2.1)."""
    stored, _ = await hash_password(uuid4(), "una frase larga de prueba")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


async def test_the_verifier_is_useless_without_the_pepper(local: Any) -> None:
    """The stored string is not argon2(password): a stolen database alone
    cannot be attacked offline, and another pepper verifies nothing."""
    org = uuid4()
    stored, version = await hash_password(org, "una frase larga de prueba")

    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(stored, "una frase larga de prueba")
    keys.set_backend(LocalAuthKeys(pepper={1: b"\x08" * 32}, signing=SIGNING))
    assert not await verify_password(org, "una frase larga de prueba", stored, version)


async def test_a_verifier_does_not_verify_in_another_organization(local: Any) -> None:
    stored, version = await hash_password(uuid4(), "una frase larga de prueba")
    assert not await verify_password(uuid4(), "una frase larga de prueba", stored, version)


async def test_the_stored_pepper_version_is_honoured_after_a_rotation(local: Any) -> None:
    org = uuid4()
    stored, version = await hash_password(org, "una frase larga de prueba")
    keys.set_backend(
        LocalAuthKeys(pepper={1: PEPPER, 2: b"\x09" * 32}, signing=SIGNING)
    )

    assert await verify_password(org, "una frase larga de prueba", stored, version)
    assert not await verify_password(org, "una frase larga de prueba", stored, 2)
    assert (await hash_password(org, "otra frase larga"))[1] == 2


async def test_composed_and_decomposed_accents_are_one_password(local: Any) -> None:
    """NIST §3.1.1.2: NFC before hashing."""
    org = uuid4()
    # Escapes, so no tool can re-normalise what the test means: e + U+0301 is
    # the decomposed form, U+00E9 the composed one.
    decomposed = "Cafe\u0301 de la man\u0303ana"
    composed = "Caf\u00e9 de la ma\u00f1ana"
    assert decomposed != composed
    stored, version = await hash_password(org, decomposed)
    assert await verify_password(org, composed, stored, version)
    stored, version = await hash_password(org, composed)
    assert await verify_password(org, decomposed, stored, version)


@pytest.mark.parametrize(
    "stored",
    [
        "not-a-hash",
        "$argon2id$v=19$m=65536,t=3,p=4$garbage",
        "$argon2id$v=19$m=65536,t=3,p=4$garbag\u00e9",  # non-ASCII: argon2-cffi encodes as ASCII
        "",
    ],
)
async def test_a_corrupt_verifier_is_false_not_an_error(stored: str, local: Any) -> None:
    assert not await verify_password(uuid4(), "una frase larga de prueba", stored, 1)


async def test_verifying_against_nobody_does_the_work_and_is_false(
    local: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int | None] = []
    verifies: list[str] = []
    real = keys.pepper_hmac
    real_matches = passwords._matches

    def spy(stored: str, mac: bytes) -> bool:
        verifies.append(stored)
        return real_matches(stored, mac)

    async def counted(org_id: Any, data: bytes, *, version: int | None = None) -> Any:
        calls.append(version)
        return await real(org_id, data, version=version)

    monkeypatch.setattr(keys, "pepper_hmac", counted)
    monkeypatch.setattr(passwords, "_matches", spy)
    assert await verify_against_nobody(uuid4(), "una frase larga de prueba") is False
    assert calls == [None]
    # The ~50 ms argon2 verify is what makes an unknown address cost as much
    # as a real one (design §2.3): exactly one, against a real argon2id hash.
    assert len(verifies) == 1 and verifies[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def _reason(plain: str, *, minimum: int = STAFF_MINIMUM, context: tuple[str, ...] = ()) -> str:
    with pytest.raises(PasswordRejected) as refused:
        check_new_password(plain, minimum=minimum, context=context)
    return refused.value.reason


CONTEXT = context_words("Ana.Perez@Toeshee.com", "Toeshee S.A.S.")


@pytest.mark.parametrize(
    "plain",
    [
        "zq7-lamp-orbit",  # 14, nothing it builds on
        "\U0001f511" * STAFF_MINIMUM,  # 12 code points, 48 bytes
        "a" * MAX_LENGTH,
        "toeshee luna cometa verde",  # a context word, and 18 more of its own
    ],
)
def test_a_password_that_breaks_no_rule_is_accepted(plain: str) -> None:
    check_new_password(plain, minimum=STAFF_MINIMUM, context=CONTEXT)


def test_length_is_counted_in_code_points_after_nfc() -> None:
    assert _reason("\U0001f511" * (STAFF_MINIMUM - 1)) == "too_short"
    # Twelve code points decomposed, eleven composed: NFC decides.
    assert len("e\u0301" + "x" * 10) == STAFF_MINIMUM
    assert _reason("e\u0301" + "x" * 10) == "too_short"
    assert _reason("zq7-lamp-orbit", minimum=15) == "too_short"
    assert _reason("a" * (MAX_LENGTH + 1)) == "too_long"


@pytest.mark.parametrize("plain", ["password1234", "PassWord1234", "QWERTYUIOPASDFGH"])
def test_a_listed_password_is_refused_in_any_case(plain: str) -> None:
    assert _reason(plain) == "blocklisted"


@pytest.mark.parametrize(
    "plain",
    [
        "ana.perez@toeshee.com",
        "Novieri12345",
        "perez-toeshee-99",
        "ITMS-ITMS-2026",
    ],
)
def test_a_password_built_on_the_context_is_refused(plain: str) -> None:
    assert _reason(plain, context=CONTEXT) == "context"


def test_the_context_names_the_address_its_pieces_and_the_platform() -> None:
    assert {
        "ana.perez@toeshee.com", "ana.perez", "perez", "toeshee.com", "toeshee",
        "toeshee s.a.s.", "itms", "novieri",
    } <= set(CONTEXT)
    # Too short to say anything: "ana" is inside too many words.
    assert "ana" not in CONTEXT and "com" not in CONTEXT


@pytest.mark.parametrize(
    ("reason", "english", "spanish"),
    [
        ("too_short", "Use at least 12 characters.", "Usa al menos 12 caracteres."),
        ("too_long", "Use at most 128 characters.", "Usa como máximo 128 caracteres."),
        ("blocklisted", "list of common or leaked passwords", "contraseñas comunes o filtradas"),
        ("context", "too close to your email address", "se parece demasiado a tu correo"),
    ],
)
def test_the_reason_is_said_in_both_languages(reason: Any, english: str, spanish: str) -> None:
    """NIST §3.1.1.2: the CSP SHALL provide the reason for rejection."""
    refused = PasswordRejected(reason=reason, minimum=STAFF_MINIMUM)
    with use_locale("en"):
        assert english in refused.message
    with use_locale("es"):
        assert spanish in refused.message


def test_a_random_password_is_not_on_the_list() -> None:
    check_new_password(secrets.token_urlsafe(16), minimum=STAFF_MINIMUM, context=CONTEXT)

"""Passkey verification against FIXED vectors, with no database (P2 phase 1
Task 5; coordinator decision D22).

WHY FIXED VECTORS. The pairing of `@simplewebauthn/browser` 14.0.0 (what the
console sends) with py_webauthn 3.0.1 (what verifies it) has not been run end
to end in this repository; the CI's browser check with Chromium's virtual
authenticator is falsifier 4 of design §10, run in Task 13. These vectors pin
what CAN be pinned without a browser:

1. A response a REAL browser produced -- Firefox for Android, a `none`
   attestation -- copied from SimpleWebAuthn's own server tests
   (`packages/server/src/registration/verifyRegistrationResponse.test.ts`,
   `attestationNone`; MIT License, Copyright (c) 2020 Matthew Miller,
   https://github.com/MasterKale/SimpleWebAuthn). It verifies under
   `require_user_verification=True`, so a genuine browser's JSON is read by
   this library exactly as the server needs.
2. Responses recorded once from `tests/webauthn_authenticator.py`, in the shape
   `startRegistration` / `startAuthentication` return (the W3C
   `RegistrationResponseJSON` / `AuthenticationResponseJSON`, plus the members
   the browser library adds: `publicKey`, `publicKeyAlgorithm`,
   `authenticatorData`, `clientExtensionResults`, `authenticatorAttachment`).
   The library ignores what it does not read; these prove it.
3. D22 itself: the library's DEFAULT accepts a response whose authenticator did
   not verify the user. `app/core/auth/passkeys.py` therefore always passes
   `require_user_verification=True`; the test that shows the default is the
   reason the argument is never left out.
"""

from typing import Any

import pytest
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import CredentialDeviceType

# --- 1. A real browser (SimpleWebAuthn's test suite, MIT) ---------------------

_FIREFOX_ID = (
    "AdKXJEch1aV5Wo7bj7qLHskVY4OoNaj9qu8TPdJ7kSAgUeRxWNngXlcNIGt4gexZGKVGcqZpqqWordXb_he1izY"
)
FIREFOX_ANDROID_NONE: dict[str, Any] = {
    "id": _FIREFOX_ID,
    "rawId": _FIREFOX_ID,
    "response": {
        "attestationObject": (
            "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YVjFPdxHEOnAiLIp26idVjIguzn3I"
            "pr_RlsKZWsa-5qK-KBFAAAAAAAAAAAAAAAAAAAAAAAAAAAAQQHSlyRHIdWleVqO24-6ix7JFWODqDWo_arvEz3Se"
            "5EgIFHkcVjZ4F5XDSBreIHsWRilRnKmaaqlqK3V2_4XtYs2pQECAyYgASFYID5PQTZQQg6haZFQWFzqfAOyQ_ENs"
            "MH8xxQ4GRiNPsqrIlggU8IVUOV8qpgk_Jh-OTaLuZL52KdX1fTht07X4DiQPow"
        ),
        "clientDataJSON": (
            "eyJ0eXBlIjoid2ViYXV0aG4uY3JlYXRlIiwiY2hhbGxlbmdlIjoiYUVWalkxQlhkWHBw"
            "VURBd1NEQndOV2Q0YURKZmRUVmZVRU0wVG1WWloyUSIsIm9yaWdpbiI6Imh0dHBzOlwvXC9kZXYuZG9udG5lZWRh"
            "LnB3IiwiYW5kcm9pZFBhY2thZ2VOYW1lIjoib3JnLm1vemlsbGEuZmlyZWZveCJ9"
        ),
        "transports": [],
    },
    "type": "public-key",
    "clientExtensionResults": {},
}
FIREFOX_CHALLENGE = b"hEccPWuziP00H0p5gxh2_u5_PC4NeYgd"
FIREFOX_RP_ID = "dev.dontneeda.pw"
FIREFOX_ORIGIN = "https://dev.dontneeda.pw"
FIREFOX_PUBLIC_KEY = (
    "pQECAyYgASFYID5PQTZQQg6haZFQWFzqfAOyQ_ENsMH8xxQ4GRiNPsqrIlggU8IVUOV8qpgk_Jh-OTaLuZL52KdX1fTht07X4DiQPow"
)

# --- 2. Recorded from tests/webauthn_authenticator.py -------------------------
# Key: P-256, scalar 0x1F2E3D4C5B6A7988 repeated four times. Credential id: 16
# bytes of 0xA1. Registration challenge: bytes 0..31; assertion challenge:
# bytes 32..63. Flags: UP, UV, BE, BS (a synced passkey) -- and the second
# registration is the SAME response with UV cleared.

RP_ID = "itms.example.com"
ORIGIN = "https://itms.example.com"
REGISTRATION_CHALLENGE = bytes(range(32))
ASSERTION_CHALLENGE = bytes(range(32, 64))
CREDENTIAL_ID = bytes.fromhex("a1" * 16)
COSE_PUBLIC_KEY = bytes.fromhex(
    "a5010203262001215820bd7c73b88b2e9b4ceda62022b2da8be13193a5b56edc26e7df7842e24cd0b5eb"
    "2258200605ada7bda83ac6a2b80d7e314040fa47ff16b83bac85cedb014451bb7ce71a"
)
_CLIENT_DATA_CREATE = (
    "eyJ0eXBlIjoid2ViYXV0aG4uY3JlYXRlIiwiY2hhbGxlbmdlIjoiQUFFQ0F3UUZCZ2NJQ1FvTERBME9EeEFS"
    "RWhNVUZSWVhHQmthR3h3ZEhoOCIsIm9yaWdpbiI6Imh0dHBzOi8vaXRtcy5leGFtcGxlLmNvbSIsImNyb3Nz"
    "T3JpZ2luIjpmYWxzZX0"
)
_ATTESTED_TAIL = (
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAEKGhoaGhoaGhoaGhoaGhoaGlAQIDJiABIVggvXxzuIsum0ztpiAistqL4TGT"
    "pbVu3Cbn33hC4kzQtesiWCAGBa2nvag6xqK4DX4xQED6R_8WuDushc7bAURRu3znGg"
)
_PUBLIC_KEY_DER = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEvXxzuIsum0ztpiAistqL4TGTpbVu3Cbn33hC4kzQtesGBa2nva"
    "g6xqK4DX4xQED6R_8WuDushc7bAURRu3znGg"
)


def _registration(flags_and_tail: str) -> dict[str, Any]:
    """The recorded `RegistrationResponseJSON`; only the flags byte differs
    between the two recordings (0x5D = UP|UV|BE|BS|AT, 0x59 = the same without
    UV), and it sits inside both `attestationObject` and `authenticatorData`."""
    auth_data = "RVFWvDTxPt9J0_YPMVi_ZBcnbUEsML5rJxPcpXHQqC" + flags_and_tail
    return {
        "id": "oaGhoaGhoaGhoaGhoaGhoQ",
        "rawId": "oaGhoaGhoaGhoaGhoaGhoQ",
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": _CLIENT_DATA_CREATE,
            "attestationObject": "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YViU" + auth_data,
            "transports": ["hybrid", "internal"],
            "publicKeyAlgorithm": -7,
            "publicKey": _PUBLIC_KEY_DER,
            "authenticatorData": auth_data,
        },
    }


REGISTRATION_WITH_UV = _registration("pd" + _ATTESTED_TAIL)
REGISTRATION_WITHOUT_UV = _registration("pZ" + _ATTESTED_TAIL)

ASSERTION_WITH_UV: dict[str, Any] = {
    "id": "oaGhoaGhoaGhoaGhoaGhoQ",
    "rawId": "oaGhoaGhoaGhoaGhoaGhoQ",
    "type": "public-key",
    "authenticatorAttachment": "platform",
    "clientExtensionResults": {},
    "response": {
        "clientDataJSON": (
            "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0IiwiY2hhbGxlbmdlIjoiSUNFaUl5UWxKaWNvS1NvckxDMHVMekF4"
            "TWpNME5UWTNPRGs2T3p3OVBqOCIsIm9yaWdpbiI6Imh0dHBzOi8vaXRtcy5leGFtcGxlLmNvbSIsImNy"
            "b3NzT3JpZ2luIjpmYWxzZX0"
        ),
        "authenticatorData": "RVFWvDTxPt9J0_YPMVi_ZBcnbUEsML5rJxPcpXHQqCodAAAAAQ",
        "signature": (
            "MEQCIGdzOLrBwpeSoonVqUaSa6xRTcBjZh9MRn_Z7WNU_66jAiANaXumLLLgbEvYoSwHg-cdX5B40XLU"
            "RiA1ACVIEp1thQ"
        ),
        "userHandle": "xfp41Bp8Kky6sfYZdTO8TVev15Tft9Qi8CEN_FXfYJY",
    },
}


def _verify_registration(credential: dict[str, Any], **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "credential": credential,
        "expected_challenge": REGISTRATION_CHALLENGE,
        "expected_rp_id": RP_ID,
        "expected_origin": ORIGIN,
        "require_user_verification": True,
    }
    arguments.update(overrides)
    return verify_registration_response(**arguments)


def _verify_assertion(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "credential": ASSERTION_WITH_UV,
        "expected_challenge": ASSERTION_CHALLENGE,
        "expected_rp_id": RP_ID,
        "expected_origin": ORIGIN,
        "credential_public_key": COSE_PUBLIC_KEY,
        "credential_current_sign_count": 0,
        "require_user_verification": True,
    }
    arguments.update(overrides)
    return verify_authentication_response(**arguments)


def test_a_real_browsers_registration_verifies_with_user_verification_required() -> None:
    verified = verify_registration_response(
        credential=FIREFOX_ANDROID_NONE,
        expected_challenge=FIREFOX_CHALLENGE,
        expected_rp_id=FIREFOX_RP_ID,
        expected_origin=FIREFOX_ORIGIN,
        require_user_verification=True,
    )
    assert verified.fmt == "none"
    assert verified.user_verified is True
    assert verified.sign_count == 0
    assert bytes_to_base64url(verified.credential_public_key) == FIREFOX_PUBLIC_KEY
    assert bytes_to_base64url(verified.credential_id) == FIREFOX_ANDROID_NONE["id"]
    # A security key or a platform credential that is not synced.
    assert verified.credential_device_type == CredentialDeviceType.SINGLE_DEVICE
    assert verified.credential_backed_up is False


def test_the_recorded_registration_verifies_and_yields_the_recorded_key() -> None:
    verified = _verify_registration(REGISTRATION_WITH_UV)
    assert verified.credential_id == CREDENTIAL_ID
    assert verified.credential_public_key == COSE_PUBLIC_KEY
    assert verified.user_verified is True
    assert verified.sign_count == 0
    assert verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE
    assert verified.credential_backed_up is True


def test_the_library_default_accepts_an_unverified_user_which_is_why_it_is_never_used() -> None:
    """D22. The same response with UV cleared PASSES the library's default
    (`require_user_verification=False`) and fails the argument passkeys.py
    always passes. If this test's first half ever fails, the default changed
    upstream -- the second half is the one that protects the platform."""
    lenient = verify_registration_response(
        credential=REGISTRATION_WITHOUT_UV,
        expected_challenge=REGISTRATION_CHALLENGE,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
    )
    assert lenient.user_verified is False
    with pytest.raises(InvalidRegistrationResponse, match="User verification is required"):
        _verify_registration(REGISTRATION_WITHOUT_UV)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_rp_id": "itms.example.org"},
        {"expected_origin": "https://itms.example.org"},
        {"expected_challenge": bytes(32)},
    ],
    ids=["rp", "origin", "challenge"],
)
def test_the_recorded_registration_is_refused_for_anything_else(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidRegistrationResponse):
        _verify_registration(REGISTRATION_WITH_UV, **overrides)


def test_the_recorded_assertion_verifies_and_advances_the_counter_to_one() -> None:
    verified = _verify_assertion()
    assert verified.credential_id == CREDENTIAL_ID
    assert verified.new_sign_count == 1
    assert verified.user_verified is True
    assert verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_rp_id": "itms.example.org"},
        {"expected_origin": "https://itms.example.org"},
        {"expected_challenge": bytes(32)},
        # The counter already stands at 1: the same assertion again is a replay.
        {"credential_current_sign_count": 1},
    ],
    ids=["rp", "origin", "challenge", "replayed-counter"],
)
def test_the_recorded_assertion_is_refused_for_anything_else(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidAuthenticationResponse):
        _verify_assertion(**overrides)

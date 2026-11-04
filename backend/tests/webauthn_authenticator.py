"""A software WebAuthn authenticator for tests (P2 phase 1 Task 5).

It answers `navigator.credentials.create()` and `.get()` the way a browser
hands the result to `@simplewebauthn/browser` 14's `startRegistration` /
`startAuthentication`, which return the W3C `RegistrationResponseJSON` /
`AuthenticationResponseJSON` dictionaries
(https://www.w3.org/TR/webauthn-3/#dictdef-registrationresponsejson): bytes as
base64url without padding, plus the convenience members the browser library
adds (`publicKey`, `publicKeyAlgorithm`, `authenticatorData`,
`clientExtensionResults`). py_webauthn 3.0.1 reads the first and ignores the
second, which `test_auth_passkey_vectors.py` pins.

ES256 only (COSE -7): the algorithm every platform authenticator offers and the
one `cryptography` signs without extra dependencies. The FLAGS are the point:
`uv=False` is an authenticator that only checked presence -- the touch that
OWASP says "alone is not a second factor" -- and a test can also clear `up`,
set `be`/`bs`, or replay a counter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40

#: The AAGUID of an authenticator that does not name its model ("none"
#: attestation zeroes it in practice); 16 bytes.
ZERO_AAGUID = bytes(16)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "===")


def _flags(*, up: bool, uv: bool, be: bool, bs: bool, at: bool) -> int:
    return (
        (FLAG_UP if up else 0)
        | (FLAG_UV if uv else 0)
        | (FLAG_BE if be else 0)
        | (FLAG_BS if bs else 0)
        | (FLAG_AT if at else 0)
    )


@dataclass
class SoftPasskey:
    """One credential: its id, its P-256 key, its counter and its user handle."""

    rp_id: str
    user_handle: bytes
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    sign_count: int = 0
    backup_eligible: bool = True
    backed_up: bool = True

    def cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return encode_cbor(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, *, up: bool, uv: bool, attested: bool) -> bytes:
        rp_id_hash = hashlib.sha256(self.rp_id.encode("utf-8")).digest()
        flags = _flags(
            up=up, uv=uv, be=self.backup_eligible, bs=self.backed_up, at=attested
        )
        data = rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count)
        if attested:
            data += (
                ZERO_AAGUID
                + struct.pack(">H", len(self.credential_id))
                + self.credential_id
                + self.cose_public_key()
            )
        return data

    def create(
        self,
        options: dict[str, Any],
        *,
        origin: str,
        uv: bool = True,
        up: bool = True,
        challenge: str | None = None,
    ) -> dict[str, Any]:
        """The `RegistrationResponseJSON` for `options` (a creation-options
        JSON dict, as the server sent it). `challenge` overrides the one in
        `options`, to answer a challenge the server never issued."""
        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": challenge or options["challenge"],
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        auth_data = self._auth_data(up=up, uv=uv, attested=True)
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        public_der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["hybrid", "internal"],
                "publicKeyAlgorithm": -7,
                "publicKey": b64url(public_der),
                "authenticatorData": b64url(auth_data),
            },
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        origin: str,
        uv: bool = True,
        up: bool = True,
        advance: int = 1,
        challenge: str | None = None,
    ) -> dict[str, Any]:
        """The `AuthenticationResponseJSON` for request options `options`.
        `advance` is how far the counter moves (0 replays the last value)."""
        self.sign_count += advance
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": challenge or options["challenge"],
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        auth_data = self._auth_data(up=up, uv=uv, attested=False)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(self.user_handle),
            },
        }

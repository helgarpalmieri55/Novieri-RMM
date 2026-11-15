"""Tokens from an issuer the platform does not trust (own authentication phase 4).

Until phase 4 this was `tests/fake_idp.py`: a fake Keycloak whose JWKS the
test app was given, so `app.core.security.validate_token` could verify what
it minted. Keycloak and that validator are gone, and so is the JWKS: nothing
in the platform fetches a key for this issuer, so a token minted here is what
a token from Keycloak -- or from anyone else -- now is to every door, signed
by a key the platform has never seen.

THE TESTS THAT MINT ONE ARE THE REFUSALS. Every door that once read a
Keycloak token (`/api/v1`, the console socket, `/mcp`) still has a test
sending one, with every claim Keycloak put in it, and expecting the door's
refusal: those tests keep pinning that refusal now that nothing on the
platform's side can accept the token at all. (The remote tunnel reads no
bearer since own authentication phase 1; its tests send cookies.) Design §10,
falsifier 11 (`tests/test_auth_phase4_falsifiers.py`) walks every route with one.
"""

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

#: Where Keycloak's realm lived; the `iss` such a token carries.
ISSUER = "https://idp.test/realms/it-suite"
#: The client the console's SPA signed in with at Keycloak until own
#: authentication phase 1: the default `azp` below.
CONSOLE_CLIENT_ID = "it-suite-web"


class ForeignIssuer:
    """An RS256 key of its own, which no part of the platform knows."""

    def __init__(self, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def public_jwk(self) -> dict[str, Any]:
        """This key's public half as a JWK, for a test that plays a JWKS
        server (falsifier 11's positive control)."""
        jwk: dict[str, Any] = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}

    @property
    def private_pem(self) -> bytes:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def token(
        self,
        sub: str,
        org_slug: str | None = "toeshee",
        *,
        aud: str = "it-suite",
        iss: str = ISSUER,
        exp_in: int = 300,
        email: str = "user@example.com",
        name: str = "Test User",
        azp: str | None = CONSOLE_CLIENT_ID,
        typ: str | None = "Bearer",
        sid: str | None = "fake-session",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """An access token as Keycloak issued one: `typ` "Bearer", `azp` the
        client that obtained it (the console's, unless told otherwise), `sid`
        the person's sign-in session, `org` the organization's slug.
        `azp=None` / `typ=None` / `sid=None` omit the claim."""
        claims: dict[str, Any] = {
            "sub": sub,
            "aud": aud,
            "iss": iss,
            "iat": int(time.time()),
            "exp": int(time.time()) + exp_in,
            "email": email,
            "name": name,
        }
        if org_slug is not None:
            claims["org"] = org_slug
        if azp is not None:
            claims["azp"] = azp
        if typ is not None:
            claims["typ"] = typ
        if sid is not None:
            claims["sid"] = sid
        if extra:
            claims.update(extra)
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": self.kid})

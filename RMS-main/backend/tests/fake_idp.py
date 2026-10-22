"""A fake OIDC identity provider for tests.

Issues RS256 tokens from a locally generated RSA key and serves a JWKS
document. The application-side validation path is identical to production
(PyJWT + JWKS fetch) — only the HTTP transport is mocked.
"""

import json
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test/realms/it-suite"
JWKS_URL = "https://idp.test/realms/it-suite/protocol/openid-connect/certs"
# What Keycloak puts in `azp` for a token the console's own SPA obtained, and
# the default of `Settings.oidc_console_client_id`. The default `azp` of every
# token below, so a token minted with no `azp=` is a console token, as before.
CONSOLE_CLIENT_ID = "it-suite-web"


class FakeIdp:
    def __init__(self, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

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
        """An access token, as Keycloak issues one: `typ` "Bearer", `azp` the
        client that obtained it (the console's, unless told otherwise).
        `sid` names the person's sign-in session, as every token a person
        obtains carries. `azp=None` / `typ=None` / `sid=None` omit the claim."""
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

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return {"keys": [jwk]}


def jwks_client(*idps: FakeIdp) -> httpx.AsyncClient:
    """An httpx client whose transport serves the given IdPs' JWKS at JWKS_URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        keys = [k for idp in idps for k in idp.jwks()["keys"]]
        return httpx.Response(200, json={"keys": keys})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

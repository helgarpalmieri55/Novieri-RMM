"""A fake Microsoft for the tests of sign-in with Microsoft (plan 2026-11-06,
Tasks 5 and 9): one RSA signing key published with a templated issuer, as
the `organizations` key set publishes every key today ([M6], dossier), the
token endpoint -- which accepts only a client assertion signed by a
certificate it was given, as Entra does ([M9]) -- and ID tokens shaped as
the claims reference describes them ([M4]).

`install` replaces `app.core.auth.entra.http_client` -- the one seam every
call to Microsoft goes through -- and forgets the process's cached keys, so a
test signs with its own key."""

import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

#: Novieri's tenant and the application, in these tests.
TENANT = UUID("11111111-2222-4333-8444-555555555555")
APP = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
CONSUMERS = UUID("9188040d-6c67-4c5b-b112-36a304b66dad")
#: What a token endpoint's refusal may quote, which nothing may repeat.
QUOTED_REFUSAL = "AADSTS700027: Client assertion contains an invalid signature: quoted-detail"
TEMPLATED_ISSUER = "https://login.microsoftonline.com/{tenantid}/v2.0"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class FakeMicrosoft:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"kid-{secrets.token_hex(4)}"
        self.key_issuer = TEMPLATED_ISSUER
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.jwks_reads = 0
        self.jwks_down = False
        self.refuse_with: tuple[int, dict[str, Any]] | None = None
        #: The certificates "uploaded to Entra": an assertion must verify with one.
        self.trusted: list[x509.Certificate] = []

    def trust(self, certificate_pem: str) -> None:
        self.trusted.append(x509.load_pem_x509_certificate(certificate_pem.encode()))

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update(kid=self.kid, use="sig", issuer=self.key_issuer)
        return {"keys": [jwk]}

    def id_token(
        self,
        *,
        nonce: str | None,
        tid: UUID = TENANT,
        oid: UUID | None = None,
        aud: UUID = APP,
        headers: dict[str, Any] | None = None,
        key: Any = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": str(aud),
            "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
            "tid": str(tid),
            "oid": str(oid or uuid4()),
            "sub": secrets.token_urlsafe(16),
            "ver": "2.0",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "name": "Dana Okafor",
            "email": "dana@example.test",
        }
        if nonce is not None:
            claims["nonce"] = nonce
        for name, value in overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return jwt.encode(
            claims,
            key or self.private,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )

    def issue_code(self, **claims: Any) -> str:
        """A code the token endpoint redeems for an ID token with `claims`
        (`nonce` included: the test reads it from the authorize URL)."""
        code = secrets.token_urlsafe(24)
        self.codes[code] = claims
        return code

    def _assertion_ok(self, form: dict[str, str]) -> bool:
        if form.get("client_assertion_type") != ASSERTION_TYPE or "client_secret" in form:
            return False
        for certificate in self.trusted:
            try:
                jwt.decode(
                    form.get("client_assertion", ""),
                    certificate.public_key(),
                    algorithms=["PS256"],
                    audience="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
                    issuer=form.get("client_id"),
                )
            except jwt.PyJWTError:
                continue
            return True
        return False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discovery/v2.0/keys"):
            self.jwks_reads += 1
            if self.jwks_down:
                return httpx.Response(503)
            return httpx.Response(200, json=self.jwks())
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = dict(parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            if self.refuse_with is not None:
                status, body = self.refuse_with
                return httpx.Response(status, json=body)
            if not self._assertion_ok(form):
                return httpx.Response(
                    401, json={"error": "invalid_client", "error_description": QUOTED_REFUSAL}
                )
            claims = self.codes.pop(form.get("code", ""), None)
            if claims is None:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": QUOTED_REFUSAL}
                )
            body = {
                "token_type": "Bearer",
                "access_token": "opaque",
                "id_token": self.id_token(**claims),
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def install(self, monkeypatch: pytest.MonkeyPatch, app: Any | None = None) -> None:
        monkeypatch.setattr("app.core.auth.entra.http_client", self.client)
        if app is not None and hasattr(app.state, "entra_keys"):
            del app.state.entra_keys

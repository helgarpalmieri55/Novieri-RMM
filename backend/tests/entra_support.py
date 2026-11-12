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
from urllib.parse import parse_qs, parse_qsl, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import ENTRA_COOKIE
from app.models import Client
from app.models.auth import ENTRA, AuthExternalIdentity
from tests.auth_support import cookie, point_at

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


# --------------------------------------------------------------------------- #
# The flows' round trip, for the tests of `/auth/microsoft/*` (Task 9; moved
# here from `tests/test_auth_entra_flows.py` in Task 9b's fix round 1, so that
# no test module imports another's private helpers).
# --------------------------------------------------------------------------- #

#: A client's tenant, never Novieri's.
OTHER_TENANT = UUID("22222222-3333-4444-8555-666666666666")
SETTINGS = "/api/v1/settings/microsoft-sign-in"


async def entra_org(env: Any) -> tuple[UUID, dict[str, str]]:
    """An organization with sign-in with Microsoft switched on -- the
    platform's certificate made and "uploaded" (C4) -- and an admin's
    headers. `env.microsoft` is the `FakeMicrosoft` the fixture installed."""
    org = await env.create_org("entra-flow")
    await point_at(env, org)
    admin = await env.create_user(org, "org-admin")
    headers = await env.headers(org, admin)
    saved = await env.client.put(
        SETTINGS,
        headers=headers,
        json={
            "enabled": True,
            "app_client_id": str(APP),
            "staff_tenant_id": str(TENANT),
            "new_certificate": True,
        },
    )
    assert saved.status_code == 200, saved.text
    env.microsoft.trust(saved.json()["certificate"])
    return org, headers


def with_cookie(headers: dict[str, str], name: str, value: str) -> dict[str, str]:
    jar = [part for part in headers.get("Cookie", "").split("; ") if part]
    return {**headers, "Cookie": "; ".join([*jar, f"{name}={value}"])}


async def start_microsoft(
    env: Any, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    return await env.client.post("/api/v1/auth/microsoft/start", headers=headers, json=body)


async def finish_microsoft(
    env: Any,
    headers: dict[str, str],
    start: httpx.Response,
    *,
    oid: UUID,
    tid: UUID = TENANT,
    nonce: str | None = None,
) -> httpx.Response:
    """Microsoft's answer to `start`, brought back to `/complete`."""
    assert start.status_code == 200, start.text
    verifier = cookie(start, ENTRA_COOKIE)
    assert verifier is not None
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = env.microsoft.issue_code(nonce=nonce or query["nonce"][0], tid=tid, oid=oid)
    return await env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=with_cookie(headers, ENTRA_COOKIE, verifier),
        json={"code": code, "state": query["state"][0]},
    )


async def round_trip(
    env: Any,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    oid: UUID,
    tid: UUID = TENANT,
    nonce: str | None = None,
) -> httpx.Response:
    start = await start_microsoft(env, headers, body)
    return await finish_microsoft(env, headers, start, oid=oid, tid=tid, nonce=nonce)


async def list_other_tenant(
    env: Any, client_id: UUID, tenants: list[UUID] | None = None
) -> None:
    """The contact's client lists `OTHER_TENANT` (D35), written past the API."""
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(microsoft_tenants=[OTHER_TENANT] if tenants is None else tenants)
        )
        await s.commit()


async def seed_link(env: Any, who: Any, *, tenant: UUID, oid: UUID) -> None:
    """A link written past the API: one the flows would refuse to make today."""
    async with AsyncSession(env.admin_engine) as s:
        s.add(
            AuthExternalIdentity(
                org_id=who.org_id,
                user_id=who.user_id,
                provider=ENTRA,
                tenant_id=tenant,
                subject=oid,
            )
        )
        await s.commit()

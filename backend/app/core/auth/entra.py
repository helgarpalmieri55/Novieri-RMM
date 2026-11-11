"""Sign in with Microsoft (Entra ID), the protocol half (design §4; plan
2026-11-06, Task 5, decisions 1-5; operator decision C4).

ONE APPLICATION, REGISTERED BY NOVIERI, MULTI-TENANT, AT `organizations`
ALWAYS -- "Only users with work or school accounts from Microsoft Entra ID can
sign in" [M2]. Never a tenant's GUID: with one, Microsoft signs that tenant's
guests in, personal accounts included [M2], and a technician's rule is
`tid == staff_tenant_id` on a person signed in to their home tenant
(decision 1, B2-05). Nothing else is ever put in a URL: the host and the
authority are constants.

THE FLOW (plan decision D30; [M1], [R1]): the authorization code with PKCE
(`S256`), `state` and `nonce`; `response_mode=fragment`, "Also supported if
requesting only a code" [M1], to the console page `/sign-in/microsoft`, which
posts the code and the state back (Task 9). Microsoft recommends `form_post`
for web apps [M2]; this flow keeps `fragment` because a `form_post` is a
cross-site POST, which carries no `SameSite=Strict` cookie (nor a `Lax` one:
6265bis §5.6.7.1, Lax goes cross-site "if and only if they are top-level
navigations which use a 'safe' ... HTTP method"), so it would need
`SameSite=None` and an exception to the console's CSRF rule (design §6.4).
THE RESIDUALS OF `fragment`, accepted: until the page posts it, the code is
visible to the page's own scripts and sits in the tab's history entry (the
page replaces it at once); and a URL is "subject to a 2,048-character length
limit" [M2], which a code-only answer stays under (Task 13 confirms it).

THE PLATFORM PROVES ITSELF WITH A CERTIFICATE IT MADE (decision 5, C4):
`new_certificate` makes an RSA key and a self-signed certificate; a person
uploads the certificate to Entra; at the exchange `client_assertion` signs a
short JWT with the private key (PS256, `x5t#S256`, [M9]), which replaces the
client secret ("Client assertions can be used anywhere a client secret would
be used", [M9]). There is no shared secret to steal or to expire unseen.

THE ID TOKEN IS VALIDATED HERE (decision 2): "Web apps and web APIs that use
ID tokens for authorization must validate them" [M2] -- the signing key's
issuer included ([M6]). The person is then (`tid`, `oid`) -- never `email`,
which Microsoft says never to use for authorization ([S61], [M5]).

This module holds no session and opens no transaction: `entra_flows.py` (Task
9) composes it, and `entra_settings.py` supplies the application and its key.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings

logger = logging.getLogger("its.auth.entra")

MICROSOFT_HOST = "https://login.microsoftonline.com"
ORGANIZATIONS = "organizations"
AUTHORIZE_URL = f"{MICROSOFT_HOST}/{ORGANIZATIONS}/oauth2/v2.0/authorize"
#: The token endpoint, and the `aud` of every client assertion sent to it [M9].
TOKEN_URL = f"{MICROSOFT_HOST}/{ORGANIZATIONS}/oauth2/v2.0/token"
#: The `jwks_uri` of `organizations`' discovery document [M3].
JWKS_URL = f"{MICROSOFT_HOST}/{ORGANIZATIONS}/discovery/v2.0/keys"
#: The personal Microsoft account tenant: never a work or school account [M4].
CONSUMER_TENANT = UUID("9188040d-6c67-4c5b-b112-36a304b66dad")
SCOPE = "openid profile"
#: The console page Microsoft returns the browser to (Task 12), and the
#: redirect URI the application is registered with (`deploy/README.md`).
CALLBACK_PATH = "/sign-in/microsoft"
#: Seconds, per request: a wedged Microsoft must not hold a request.
HTTP_TIMEOUT_S = 15.0
#: How long the keys are reused, and how often an unknown `kid` may read
#: them again (decision 3).
JWKS_TTL_S = 3600.0
JWKS_MIN_REFETCH_S = 60.0
#: Clock skew allowed on `exp`, `nbf` and `iat` (decision 2).
LEEWAY_S = 60
#: The certificate's life, under a year: Microsoft's own self-signed example
#: lasts one ([M11]); its "less than 12 months" is for secrets ([M7], decision 5).
CERTIFICATE_DAYS = 360
#: A client assertion's life: "keep it short - 5-10 minutes after nbf at most" [M9].
ASSERTION_TTL_S = 300
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_ID_TOKEN_ALGORITHM = "RS256"
_ASSERTION_ALGORITHM = "PS256"
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Reason = Literal["exchange", "token", "signature", "claims", "nonce", "tenant"]


class EntraNotConfigured(Exception):
    """`ITS_PUBLIC_CONSOLE_URL` is unset or is not an https address."""


class EntraUnavailable(Exception):
    """Microsoft's keys could not be read at all (decision 3): an outage, not
    a bad token. The flows answer 503 `sign_in_unavailable`."""


class EntraRefused(Exception):
    """The sign-in cannot go on. `reason` is for the tests and the flows'
    choice of answer; the person is told one sentence whatever it is."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True)
class EntraIdentity:
    """Who Microsoft says signed in: `tid` and `oid`, and a name to show."""

    tenant_id: UUID
    object_id: UUID
    name: str


@dataclass(frozen=True)
class NewCertificate:
    """What `new_certificate` makes. `private_pem` goes to the vault and
    nowhere else; the rest is public (decision 5)."""

    private_pem: str
    certificate_pem: str
    #: `x5t#S256`: base64url SHA-256 of the certificate's DER [M9].
    thumbprint: str
    expires_at: datetime


@dataclass(frozen=True)
class SigningKey:
    """One of Microsoft's signing keys and the `issuer` it was published
    with: templated (`{tenantid}`) or one tenant's ([M6])."""

    key: Any
    issuer: str


def redirect_uri(settings: Settings) -> str:
    """From configuration, never from `Host` (host-header injection, the
    reasoning of `email_intake/microsoft.py::redirect_uri`)."""
    base = (settings.public_console_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise EntraNotConfigured()
    return f"{base}{CALLBACK_PATH}"


def challenge_of(verifier: str) -> str:
    """PKCE `S256`: BASE64URL(SHA256(verifier)), no padding (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(
    *, client_id: UUID, redirect_uri: str, state: str, nonce: str, code_challenge: str
) -> str:
    query = urlencode(
        {
            "client_id": str(client_id),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "fragment",
            "scope": SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Otherwise whoever is already signed in to Microsoft in this
            # browser is used without asking ([M1], `prompt`; B2-18): the
            # person chooses, knowingly, which account they link.
            "prompt": "select_account",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def new_certificate(*, app_client_id: UUID, now: datetime) -> NewCertificate:
    """An RSA key and a self-signed certificate for the application, valid
    `CERTIFICATE_DAYS` (decision 5). Self-signed: "if using a trusted CA
    isn't possible, self-signed certificates are still preferred over
    passwords" [M8]."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"itms-sign-in-{app_client_id}")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERTIFICATE_DAYS))
        .sign(private, hashes.SHA256())
    )
    der = certificate.public_bytes(Encoding.DER)
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    return NewCertificate(
        private_pem=private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(
            "ascii"
        ),
        certificate_pem=certificate.public_bytes(Encoding.PEM).decode("ascii"),
        thumbprint=thumbprint,
        expires_at=certificate.not_valid_after_utc,
    )


def client_assertion(*, client_id: UUID, private_pem: str, thumbprint: str) -> str:
    """The JWT that stands for the client secret at the token endpoint
    ([M9]): PS256, `x5t#S256`, `aud` the endpoint, `iss` = `sub` = the
    application, a unique `jti`, five minutes."""
    issued = int(time.time())
    return jwt.encode(
        {
            "aud": TOKEN_URL,
            "iss": str(client_id),
            "sub": str(client_id),
            "jti": str(uuid4()),
            "nbf": issued,
            "iat": issued,
            "exp": issued + ASSERTION_TTL_S,
        },
        private_pem,
        algorithm=_ASSERTION_ALGORITHM,
        headers={"typ": "JWT", "x5t#S256": thumbprint},
    )


def http_client() -> httpx.AsyncClient:
    """The client every call to Microsoft uses; the tests replace this seam."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)


def refusal_code(response: httpx.Response) -> str:
    """Microsoft's machine `error` code, and nothing else from the body: its
    `error_description` can quote what it refused (decision 4)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""


async def exchange(
    *,
    client: httpx.AsyncClient,
    client_id: UUID,
    client_assertion: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    """Redeem the code at the token endpoint, `private_key_jwt` [M3, M9]:
    the ID token. Any refusal is `EntraRefused("exchange")`, logged once with
    the error code alone (decision 4)."""
    error: str | None = None
    id_token: object = None
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": str(client_id),
                "scope": SCOPE,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": client_assertion,
            },
        )
        if response.status_code != 200:
            error = refusal_code(response) or f"http_{response.status_code}"
        else:
            body = response.json()
            id_token = body.get("id_token") if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError):
        error = "transport"
    if error is None and not isinstance(id_token, str):
        error = "no_id_token"
    if error is not None:
        logger.warning(
            "microsoft sign-in: the token endpoint refused the code", extra={"error": error}
        )
        raise EntraRefused("exchange")  # i18n-exempt: a reason code, never shown
    assert isinstance(id_token, str)
    return id_token


class EntraKeys:
    """Microsoft's signing keys (decision 3): read from `JWKS_URL` with each
    key's `issuer`, kept for `JWKS_TTL_S`, and read again for an unknown
    `kid` at most every `JWKS_MIN_REFETCH_S`, one read at a time. A failed
    read keeps the keys it had, however old: an outage at Microsoft must not
    stop a token signed with a key already known. With no key at all,
    `EntraUnavailable`."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Looked up at call time, so the tests' replacement of `http_client`
        # reaches a cache made before it.
        self._client_factory = client_factory
        self._clock = clock
        self._keys: dict[str, SigningKey] = {}
        self._read_at: float | None = None
        self._last_try = float("-inf")
        self._lock = asyncio.Lock()

    async def _read(self) -> None:
        factory = self._client_factory or http_client
        try:
            async with factory() as client:
                response = await client.get(JWKS_URL)
                response.raise_for_status()
                entries = response.json()["keys"]
            keys = {
                entry["kid"]: SigningKey(
                    key=RSAAlgorithm.from_jwk(json.dumps(entry)), issuer=entry["issuer"]
                )
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("kty") == "RSA"
                and isinstance(entry.get("kid"), str)
                # A key with no issuer cannot be checked against the token's
                # (decision 2, [M6]): it is not used.
                and isinstance(entry.get("issuer"), str)
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
            return
        if keys:
            self._keys, self._read_at = keys, self._clock()

    async def key(self, kid: str) -> SigningKey:
        async with self._lock:
            now = self._clock()
            stale = self._read_at is None or now - self._read_at >= JWKS_TTL_S
            if (kid not in self._keys or stale) and now - self._last_try >= JWKS_MIN_REFETCH_S:
                self._last_try = now
                await self._read()
        if not self._keys:
            raise EntraUnavailable()
        found = self._keys.get(kid)
        if found is None:
            raise EntraRefused("signature")  # i18n-exempt: a reason code, never shown
        return found


def keys_of(app: Any) -> EntraKeys:
    """The process's one key cache, on `app.state` (made on first use)."""
    keys: EntraKeys | None = getattr(app.state, "entra_keys", None)
    if keys is None:
        keys = EntraKeys()
        app.state.entra_keys = keys
    return keys


def _idp_is_a_member(idp: object, tenant: UUID) -> bool:
    """Decision 2's `idp` defence: absent, or naming the token's own tenant.
    `live.com`, the personal-accounts tenant or another tenant is a guest."""
    if idp is None:
        return True
    if not isinstance(idp, str):
        return False
    named = set(_GUID.findall(idp.lower()))
    return named == {str(tenant)}


async def validate_id_token(
    id_token: str, *, client_id: UUID, nonce: str, keys: EntraKeys
) -> EntraIdentity:
    """Decision 2, every rule, in this order; the first that fails refuses."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise EntraRefused("token") from exc  # i18n-exempt: a reason code, never shown
    if header.get("alg") != _ID_TOKEN_ALGORITHM or not isinstance(header.get("kid"), str):
        raise EntraRefused("token")  # i18n-exempt: a reason code, never shown
    signing = await keys.key(header["kid"])
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            signing.key,
            algorithms=[_ID_TOKEN_ALGORITHM],
            audience=str(client_id),
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "nbf", "aud", "iss"]},
        )
    except jwt.InvalidSignatureError as exc:
        raise EntraRefused("signature") from exc  # i18n-exempt: a reason code, never shown
    except jwt.PyJWTError as exc:
        raise EntraRefused("claims") from exc  # i18n-exempt: a reason code, never shown
    try:
        tenant = UUID(str(claims["tid"]))
        account = UUID(str(claims["oid"]))
    except (KeyError, ValueError) as exc:
        raise EntraRefused("claims") from exc  # i18n-exempt: a reason code, never shown
    issuer = str(claims.get("iss", "")).lower()
    if claims.get("ver") != "2.0" or issuer != f"{MICROSOFT_HOST}/{tenant}/v2.0":
        raise EntraRefused("claims")  # i18n-exempt: a reason code, never shown
    # [M6]: the key signs only for the issuer it was published with.
    if signing.issuer.replace("{tenantid}", str(tenant)).lower() != issuer:
        raise EntraRefused("signature")  # i18n-exempt: a reason code, never shown
    if tenant == CONSUMER_TENANT or not _idp_is_a_member(claims.get("idp"), tenant):
        raise EntraRefused("tenant")  # i18n-exempt: a reason code, never shown
    sent = claims.get("nonce")
    if not isinstance(sent, str) or not hmac.compare_digest(
        sent.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise EntraRefused("nonce")  # i18n-exempt: a reason code, never shown
    name = claims.get("name")
    return EntraIdentity(
        tenant_id=tenant, object_id=account, name=name if isinstance(name, str) else ""
    )

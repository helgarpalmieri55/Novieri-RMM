"""The MCP access token (autenticación propia design §5.3, §6.3; plan D5, D6, D14).

A JWT, ES256, signed by the platform's key in OpenBao through
`app.core.auth.keys.sign_jwt` (the assistant's internal token's signer), ten
minutes:

    header  {"alg": "ES256", "kid": <RFC 7638 thumbprint>, "typ": "at+jwt"}
    payload {"iss": <issuer>, "aud": <the MCP resource>, "sub": <users.id>,
             "client_id": <the AI app>, "org": <organizations.id>,
             "fam": <oauth_refresh_families.id>, "scope": "mcp",
             "jti": <random>, "iat": <now>, "exp": <now + 600>}

The shape of RFC 9068 §2.2's claims, plus `org` (the door pins the tenant from it, as it
does from the internal token's `org`) and `fam` (the grant the token came
from: revoking it stops the token at the next call, plan D6). No address and
no name: the door reads them from the person's row (Task 9).

VERIFIED WITH EVERYTHING FIXED (RFC 8725 [S38]): `alg` ES256 and nothing else
(§3.1); `typ` exactly `at+jwt` (§3.11; RFC 9068 §4 also allows
`application/at+jwt`, and this check is deliberately stricter: the platform is
the only issuer and emits only `at+jwt`), so the internal token (`loopback+jwt`) never passes for
one, nor one for it (§3.12); `kid` naming a published key; `iss` this
platform (§3.8); `aud` EXACTLY the MCP resource, a string and not a list
holding it with others (§3.9; 1c(a): "carrying the MCP audience and no other
platform audience"; MCP: servers "MUST validate that access tokens were
issued specifically for them" [S27]); no longer than ten minutes; every claim
present and of its type. Any failure is None, and the door answers one 401.

AN UNKNOWN `kid` IS NOT A FREE READ OF OPENBAO (plan D14). `/mcp` is public:
a token with an invented `kid` would force `keys.jwks(refresh=True)` per
request. The keys are read again at most once per `REFRESH_EVERY_S` per
process -- `JwksCache`'s defence for Keycloak's keys (`app/core/security.py`)
-- so a token signed just after a rotation is still admitted at once, unless
a stranger's token with an invented `kid` armed the interval first: then for
up to `REFRESH_EVERY_S` seconds. Acceptable, and said so (plan D14).

NOT A CLAIM OF RFC 9068 CONFORMANCE (plan B3-06). The token follows RFC
9068's `typ` and claims; RFC 9068 §2.1 also says "MUST include RS256" of
servers "conforming to this specification", and this one signs ES256 only
(design §5.3, §6.3). Its only resource server is this platform.
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import jwt

from app.core.auth import keys
from app.core.auth.oauth.policy import ACCESS_TOKEN_TTL_S, ACCESS_TYP, SCOPE, issuer
from app.core.mcp_channel import mcp_resource

_REQUIRED = ["iss", "aud", "sub", "client_id", "org", "fam", "scope", "jti", "iat", "exp"]
#: Seconds between two forced re-reads of the public keys (plan D14).
REFRESH_EVERY_S = 5.0
_next_refresh_at = float("-inf")


@dataclass(frozen=True)
class McpGrant:
    """An MCP access token, as the door verified it."""

    user_id: UUID
    org_id: UUID
    client_id: str
    family_id: UUID
    iat: int
    exp: int
    #: The person's address, from their `users` row once the door has read it
    #: (Task 9). Never from the token, which carries none.
    email: str = field(default="", compare=False)

    @property
    def azp(self) -> str:
        """The AI app, by the name the rest of the MCP code uses for it (the
        channel marker's `azp`, the audit rows' `app`)."""
        return self.client_id


def _now() -> int:
    """A named seam, so a test can mint a token in the past."""
    return int(time.time())


def _monotonic() -> float:
    """A named seam, so a test can move the refresh throttle's clock."""
    return time.monotonic()


async def mint_access_token(
    *,
    settings: object,
    user_id: UUID,
    org_id: UUID,
    client_id: str,
    family_id: UUID,
    now: int | None = None,
) -> tuple[str, int]:
    """The token and its life in seconds (`expires_in`, RFC 6749 §5.1)."""
    iss, aud = issuer(settings), mcp_resource(settings)
    if iss is None or aud is None:
        raise ValueError(  # i18n-exempt: a wiring defect; the routes answer 503 first
            "no issuer: ITS_PUBLIC_CONSOLE_URL is not an https address without a path"
        )
    issued = _now() if now is None else now
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": str(user_id),
        "client_id": client_id,
        "org": str(org_id),
        "fam": str(family_id),
        "scope": SCOPE,
        "jti": secrets.token_urlsafe(16),
        "iat": issued,
        "exp": issued + ACCESS_TOKEN_TTL_S,
    }
    return await keys.sign_jwt(claims, typ=ACCESS_TYP), ACCESS_TOKEN_TTL_S


def unverified_client_id(token: str) -> str:
    """The AI app a token claims to be for, WITHOUT verifying it: only the
    label `/mcp` puts on its scope marker before the door verifies the same
    token (`app/mcp/server.py`), and compared with the verified one after."""
    try:
        value = jwt.decode(token, options={"verify_signature": False}).get("client_id")
    except Exception:  # noqa: BLE001 - a label, never a decision; validation follows
        return ""
    return value if isinstance(value, str) else ""


def _canonical_uuid(value: object) -> UUID:
    """A UUID in its one canonical spelling (lower-case, hyphenated): `urn:uuid:`,
    braces and unhyphenated forms are refused. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("not a string")  # i18n-exempt: caught by the caller
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("not canonical")  # i18n-exempt: caught by the caller
    return parsed


def _find(document: dict[str, Any], kid: str) -> jwt.PyJWK | None:
    try:
        return jwt.PyJWKSet.from_dict(document)[kid]
    except (KeyError, jwt.PyJWKSetError):
        return None


async def _key_for(kid: str) -> jwt.PyJWK | None:
    global _next_refresh_at
    key = _find(await keys.jwks(), kid)
    if key is not None:
        return key
    now = _monotonic()
    if now < _next_refresh_at:
        return None
    _next_refresh_at = now + REFRESH_EVERY_S
    return _find(await keys.jwks(refresh=True), kid)


async def verify_access_token(token: str, *, settings: object) -> McpGrant | None:
    """The grant a genuine MCP access token names, or None. Raises only
    `keys.AuthKeysUnavailable` (OpenBao down: the app's 503)."""
    iss, aud = issuer(settings), mcp_resource(settings)
    if iss is None or aud is None:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return None
    kid = header.get("kid")
    if header.get("alg") != "ES256" or header.get("typ") != ACCESS_TYP:
        return None
    if not isinstance(kid, str) or not kid:
        return None
    key = await _key_for(kid)
    if key is None:
        return None
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            audience=aud,
            issuer=iss,
            options={"require": _REQUIRED},
        )
    except jwt.InvalidTokenError:
        return None
    try:
        iat, exp = int(claims["iat"]), int(claims["exp"])
        client_id = claims["client_id"]
        if claims["aud"] != aud or claims["scope"] != SCOPE or exp - iat > ACCESS_TOKEN_TTL_S:
            return None
        if not isinstance(client_id, str) or not client_id:
            return None
        return McpGrant(
            user_id=_canonical_uuid(claims["sub"]),
            org_id=_canonical_uuid(claims["org"]),
            client_id=client_id,
            family_id=_canonical_uuid(claims["fam"]),
            iat=iat,
            exp=exp,
        )
    except (KeyError, TypeError, ValueError):
        return None

"""Operator token validation. The API is an OIDC resource server: Keycloak (or
any OIDC IdP) issues RS256 access tokens; we validate signature, issuer,
audience, and expiry against the IdP's JWKS. MFA and federation live in the
IdP, not here."""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from app.core.config import Settings
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource


class TokenValidationError(Exception):
    pass


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    email: str
    name: str
    org_slug: str
    # Provenance fields for the MCP channel. `compare=False`: they describe the token
    # (which app, when issued, for whom), not the person, so two claims for the same
    # person are equal however the token was minted -- and the existing equality
    # contract of this class is unchanged.
    azp: str = field(default="", compare=False)
    iat: int = field(default=0, compare=False)
    aud: tuple[str, ...] = field(default=(), compare=False)
    # Who obtained the token, for the MCP door's "a person signed in" rule
    # (`require_person_mcp_token`). Keycloak names a service account
    # `service-account-<client id>`, and its `service_account` client scope
    # adds a `client_id` claim that a person's token never carries -- spelled
    # `clientId` by the hardcoded mapper of releases before 25, so either
    # spelling sets `client_id_claim`.
    preferred_username: str = field(default="", compare=False)
    client_id_claim: bool = field(default=False, compare=False)
    # The sign-in session the token belongs to. Keycloak sets `sid` from a
    # person's own session; a `client_credentials` token has none (recorded
    # live by deploy/keycloak/ci_mcp_flow.py, which fails CI if that changes).
    sid: str = field(default="", compare=False)
    # When the token stops being valid (epoch seconds; `validate_token`
    # requires the claim). A door that outlives its handshake -- the console
    # WebSocket -- closes itself when this passes (MSP clients, final review).
    exp: int = field(default=0, compare=False)

    @property
    def is_service_account(self) -> bool:
        """Either marker suffices: refusing on one we did not need is the safe
        direction, and depending on a single marker is not."""
        return self.client_id_claim or self.preferred_username.startswith("service-account-")


class JwksCache:
    """Fetches and caches JWKS keys by kid; refetches on an unknown kid so IdP
    key rotation does not require a restart.

    Hardened against unauthenticated refetch-amplification: an attacker can send
    a flood of well-formed tokens carrying random `kid`s (validation of a token
    can't begin until its signing key is fetched). Three guards:
    - a single-flight lock coalesces concurrent refetches into one HTTP call;
    - a minimum interval between refetches caps the network rate regardless of
      how many unknown kids arrive;
    - a failed refetch never clears the existing key set.
    """

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        min_refetch_interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=10)
        self._keys: dict[str, Any] = {}
        self._min_interval = min_refetch_interval_s
        self._clock = clock
        self._last_refetch = float("-inf")
        self._lock = asyncio.Lock()

    async def _refetch(self) -> None:
        response = await self._client.get(self._url)
        response.raise_for_status()
        fetched: dict[str, Any] = {}
        for jwk in response.json().get("keys", []):
            kid = jwk.get("kid")
            if kid and jwk.get("kty") == "RSA":
                fetched[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        # Swap in only on success — a failed/partial fetch must not blank the cache.
        self._keys = fetched

    async def get_key(self, kid: str) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            # Another waiter may have refetched while we queued on the lock.
            if kid in self._keys:
                return self._keys[kid]
            if self._clock() - self._last_refetch >= self._min_interval:
                self._last_refetch = self._clock()
                await self._refetch()
        if kid not in self._keys:
            raise TokenValidationError(tr("no JWKS key for kid {kid}", kid=repr(kid)))
        return self._keys[kid]


async def validate_token(
    token: str,
    *,
    settings: Settings,
    jwks: JwksCache,
    audience: str | None = None,
) -> TokenClaims:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise TokenValidationError(str(exc)) from exc
    kid = header.get("kid")
    if not kid:
        raise TokenValidationError(tr("token has no kid header"))
    key = await jwks.get_key(kid)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=audience or settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenValidationError(str(exc)) from exc
    # ACCESS TOKENS ONLY, on every door. The audience says which resource a
    # token is for, not what kind of token it is: Keycloak signs ID, refresh,
    # offline and logout tokens with the same realm key, and an ID token's
    # `aud` is the client it was issued to -- so the ID token of a client
    # registered (open registration) under the id `it-suite` passes the
    # audience check above as a console token, with the person's `sub` and
    # `org`. Keycloak issues every access token with `typ` "Bearer", exactly
    # that spelling; anything else, or no `typ` at all, is refused.
    if claims.get("typ") != "Bearer":
        raise TokenValidationError(tr("this token is not an access token"))

    claim_org = claims.get(settings.oidc_org_claim)
    # Multi-tenant: the org claim is mandatory — never fall back to a default
    # org, or a token issued for one tenant could be admitted into another.
    if settings.multi_tenant:
        if not claim_org:
            raise TokenValidationError(
                tr(
                    "token missing required {oidc_org_claim} claim in multi-tenant mode",
                    oidc_org_claim=repr(settings.oidc_org_claim),
                )
            )
        org_slug = claim_org
    else:
        org_slug = claim_org or settings.default_org_slug
        if not org_slug:
            raise TokenValidationError(
                tr(
                    "token missing {oidc_org_claim} claim and no default org configured",
                    oidc_org_claim=repr(settings.oidc_org_claim),
                )
            )
    raw_aud = claims.get("aud")
    aud = (raw_aud,) if isinstance(raw_aud, str) else tuple(str(a) for a in (raw_aud or ()))
    return TokenClaims(
        sub=claims["sub"],
        email=claims.get("email", ""),
        name=claims.get("name", ""),
        org_slug=org_slug,
        azp=str(claims.get("azp") or ""),
        iat=int(claims["iat"]),
        aud=aud,
        preferred_username=str(claims.get("preferred_username") or ""),
        client_id_claim="client_id" in claims or "clientId" in claims,
        sid=str(claims.get("sid") or ""),
        exp=int(claims["exp"]),
    )


def require_console_token(claims: TokenClaims, settings: Settings) -> None:
    """THE CONSOLE ACCEPTS ONLY ITS OWN TOKENS. Every door that is not `/mcp`
    (`auth_context` unmarked, the UI WebSocket, the remote tunnel) calls this
    after `validate_token`, so the three cannot drift apart.

    The audience alone does not prove the console's client obtained a token:
    any client that can put `it-suite` in `aud` -- one an attacker registered
    through open client registration, say -- would otherwise be the console,
    with the person's full RBAC and none of the MCP controls. `azp` is the
    client the token was issued to, and the console's is exactly one.

    And a token that carries the MCP audience is refused even if it also
    carries the console's: an MCP grant is consented to for an AI app, and must
    never be replayable against the console directly -- not the API, not the
    UI WebSocket, not a remote session."""
    mcp_audience = mcp_resource(settings)
    if mcp_audience is not None and mcp_audience in claims.aud:
        raise TokenValidationError(
            tr("this token is for the MCP server and is accepted only through it")
        )
    if claims.azp != settings.oidc_console_client_id:
        raise TokenValidationError(tr("this token was not issued to the console"))


def require_person_mcp_token(claims: TokenClaims, settings: Settings) -> None:
    """AN MCP SESSION IS A PERSON'S (invariant 1c; MCP server design §9): "a
    token issued by the platform's identity provider to that person after
    their own sign-in". So not a token the console's own client obtained --
    that grant was consented to for the console, not for an AI app -- and not
    a service account's `client_credentials` token, which no person signed in
    for: a self-registered app could enable one on itself."""
    if claims.azp == settings.oidc_console_client_id:
        raise TokenValidationError(
            tr("this token was issued to the console and is not accepted over MCP")
        )
    # A POSITIVE marker as well as the two negative ones: the negatives come from
    # client scopes, `sid` from the person's own sign-in session.
    if claims.is_service_account or not claims.sid:
        raise TokenValidationError(tr("MCP access must be granted by a person signing in"))

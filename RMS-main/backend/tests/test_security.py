import pytest

from app.core.config import Settings
from app.core.security import JwksCache, TokenClaims, TokenValidationError, validate_token
from tests.fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/x",
        database_admin_url="postgresql+asyncpg://x:x@localhost/x",
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir="/tmp/ca",
    )


async def test_valid_token_yields_claims() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    claims = await validate_token(idp.token("subject-1", "acme"), settings=_settings(), jwks=cache)
    assert claims == TokenClaims(
        sub="subject-1", email="user@example.com", name="Test User", org_slug="acme"
    )


async def test_expired_token_rejected() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(idp.token("s", exp_in=-60), settings=_settings(), jwks=cache)


async def test_wrong_audience_rejected() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(idp.token("s", aud="other-app"), settings=_settings(), jwks=cache)


async def test_wrong_issuer_rejected() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(
            idp.token("s", iss="https://evil.test"), settings=_settings(), jwks=cache
        )


async def test_garbage_token_rejected() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token("not.a.jwt", settings=_settings(), jwks=cache)


async def test_signature_from_unknown_key_rejected() -> None:
    served, rogue = FakeIdp("kid-a"), FakeIdp("kid-a")  # same kid, different key
    cache = JwksCache(JWKS_URL, client=jwks_client(served))
    with pytest.raises(TokenValidationError):
        await validate_token(rogue.token("s"), settings=_settings(), jwks=cache)


async def test_kid_rotation_refetches_jwks() -> None:
    old, new = FakeIdp("kid-old"), FakeIdp("kid-new")
    now = [1000.0]
    cache = JwksCache(JWKS_URL, client=jwks_client(old), clock=lambda: now[0])
    await validate_token(old.token("s"), settings=_settings(), jwks=cache)

    # rotate: the endpoint now serves both keys; after the refetch cooldown a
    # token signed with the new kid triggers a refetch rather than failing stale
    cache._client = jwks_client(old, new)
    now[0] += 10  # advance past the min refetch interval
    claims = await validate_token(new.token("s2"), settings=_settings(), jwks=cache)
    assert claims.sub == "s2"


async def test_unknown_kid_flood_is_rate_limited() -> None:
    """Repeated unknown-kid lookups within the cooldown must not each hit the
    network (unauthenticated refetch-amplification defense)."""
    served = FakeIdp("real")
    fetches = [0]

    def handler(request):  # type: ignore[no-untyped-def]
        import httpx

        fetches[0] += 1
        return httpx.Response(200, json=served.jwks())

    import httpx

    now = [500.0]
    cache = JwksCache(
        JWKS_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    # first unknown kid -> one fetch; twenty more within the cooldown -> no fetch
    for _ in range(20):
        with pytest.raises(TokenValidationError):
            await cache.get_key("attacker-kid")
    assert fetches[0] == 1


async def test_failed_refetch_keeps_existing_keys() -> None:
    served = FakeIdp("real")
    mode = {"fail": False}

    def handler(request):  # type: ignore[no-untyped-def]
        import httpx

        if mode["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=served.jwks())

    import httpx

    now = [0.0]
    cache = JwksCache(
        JWKS_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    assert await cache.get_key("real") is not None  # populate
    mode["fail"] = True
    now[0] += 100  # past cooldown so a refetch is attempted
    with pytest.raises(Exception):  # noqa: B017 - the 500 propagates
        await cache.get_key("unknown-kid")
    # the good key survived the failed refetch
    assert await cache.get_key("real") is not None


async def test_missing_org_claim_uses_default_slug() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    settings = _settings().model_copy(update={"default_org_slug": "toeshee"})
    claims = await validate_token(idp.token("s", org_slug=None), settings=settings, jwks=cache)
    assert claims.org_slug == "toeshee"


async def test_missing_org_claim_without_default_rejected() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(idp.token("s", org_slug=None), settings=_settings(), jwks=cache)


async def test_multi_tenant_rejects_missing_org_claim_even_with_default() -> None:
    # Deferred finding D: in multi-tenant mode the default-org fallback must not
    # apply — a claimless token must be rejected, never placed in the default org.
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    settings = _settings().model_copy(
        update={"multi_tenant": True, "default_org_slug": "toeshee"}
    )
    with pytest.raises(TokenValidationError):
        await validate_token(idp.token("s", org_slug=None), settings=settings, jwks=cache)


async def test_multi_tenant_accepts_token_with_org_claim() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    settings = _settings().model_copy(update={"multi_tenant": True})
    claims = await validate_token(idp.token("s", org_slug="acme"), settings=settings, jwks=cache)
    assert claims.org_slug == "acme"


_MCP_AUD = "https://itms.example.com/mcp"


async def test_an_mcp_audience_token_is_refused_on_the_default_audience() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(idp.token("s", aud=_MCP_AUD), settings=_settings(), jwks=cache)


async def test_an_mcp_audience_token_passes_with_the_audience_override() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    claims = await validate_token(
        idp.token("s", aud=_MCP_AUD, azp="claude"),
        settings=_settings(),
        jwks=cache,
        audience=_MCP_AUD,
    )
    assert claims.azp == "claude"
    assert isinstance(claims.iat, int)
    assert claims.iat > 0
    assert claims.aud == (_MCP_AUD,)


async def test_a_list_audience_is_kept_as_a_tuple() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    token = idp.token("s", aud="it-suite", azp=None, extra={"aud": ["it-suite", _MCP_AUD]})
    claims = await validate_token(token, settings=_settings(), jwks=cache, audience=_MCP_AUD)
    assert claims.aud == ("it-suite", _MCP_AUD)
    assert claims.azp == ""


async def test_a_console_token_is_refused_on_the_mcp_audience() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(
            idp.token("s", aud="it-suite"), settings=_settings(), jwks=cache, audience=_MCP_AUD
        )

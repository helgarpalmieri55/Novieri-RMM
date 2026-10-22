"""Turning the stored refresh token into an access token, at point of use.

Microsoft may rotate the refresh token on every exchange. The new one is
stored BEFORE the access token is handed back, so a crash between the two costs
one poll rather than the mailbox (design §4.2). And a dead grant and a network
blip are different exceptions, because they need different people.
"""

from typing import Any

import pytest

from app.core.vault import get_vault
from app.modules.email_intake import tokens
from app.modules.email_intake.tokens import TokenRevoked, TokenTransportError
from tests.email_oauth_env import (
    ACCESS,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH,
    OAuthEnv,
    build_oauth_env,
    token_error,
    token_ok,
    token_unreachable,
)


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> OAuthEnv:
    env = await build_oauth_env(api_env, monkeypatch)
    assert (await env.connect()).status_code == 200
    env.requests.clear()
    return env


async def _token(env: OAuthEnv) -> str:
    return await tokens.access_token_for(
        env.org_id,
        env.mailbox_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
    )


async def test_the_refresh_request_goes_to_microsoft_with_the_vaulted_secrets(
    env: OAuthEnv,
) -> None:
    env.use(token_ok(refresh=None))

    assert await _token(env) == ACCESS

    (request,) = env.requests
    assert request.url.host == "login.microsoftonline.com"
    (form,) = env.token_forms()
    assert form == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    }


async def test_a_rotated_refresh_token_is_stored(env: OAuthEnv) -> None:
    env.use(token_ok(refresh="ROTATED-REFRESH"))

    await _token(env)

    assert await env.stored_secret() == "ROTATED-REFRESH"


async def test_the_rotation_is_stored_before_the_access_token_is_returned(
    env: OAuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If storing the new token fails, the access token must not be used: the
    old refresh token may already be retired at Microsoft's end."""
    env.use(token_ok(refresh="ROTATED-REFRESH"))

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vault down")

    monkeypatch.setattr(tokens, "_store_secret", refuse)
    with pytest.raises(RuntimeError):
        await _token(env)


async def test_no_new_refresh_token_keeps_the_old_one(env: OAuthEnv) -> None:
    env.use(token_ok(refresh=None))

    await _token(env)

    assert await env.stored_secret() == REFRESH


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required", "consent_required"])
async def test_a_dead_grant_is_revoked_for_consent(env: OAuthEnv, error: str) -> None:
    env.use(token_error(400, error, f"token {REFRESH} is no good"))

    with pytest.raises(TokenRevoked) as caught:
        await _token(env)

    assert caught.value.cause == "consent"
    assert REFRESH not in str(caught.value)


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
async def test_a_refused_application_is_revoked_for_the_application(
    env: OAuthEnv, error: str
) -> None:
    env.use(token_error(401, error, f"secret {CLIENT_SECRET}"))

    with pytest.raises(TokenRevoked) as caught:
        await _token(env)

    assert caught.value.cause == "application"
    assert CLIENT_SECRET not in str(caught.value)


async def test_a_network_failure_is_transient(env: OAuthEnv) -> None:
    env.use(token_unreachable)

    with pytest.raises(TokenTransportError) as caught:
        await _token(env)

    assert caught.value.reason == "microsoft-unreachable"


async def test_a_5xx_is_transient(env: OAuthEnv) -> None:
    env.use(token_error(503, "temporarily_unavailable"))

    with pytest.raises(TokenTransportError) as caught:
        await _token(env)

    assert caught.value.reason == "microsoft-http-503"


async def test_every_decrypt_is_audited(env: OAuthEnv) -> None:
    env.use(token_ok(refresh=None))
    before = (await env.audit_actions()).count("email_intake.credential_used")

    await _token(env)

    after = (await env.audit_actions()).count("email_intake.credential_used")
    # The refresh token and the client secret: two decrypts, two events.
    assert after - before == 2
    assert REFRESH not in await env.audit_dump()

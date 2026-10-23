"""Connect with Microsoft: start, complete, abandon, disconnect.

The callback lands on a CONSOLE route; the console then makes an ordinary
authenticated call with the operator's own token. So RBAC and tenancy come from
that token and RLS, and `state` does only CSRF and single-use replay defence.
Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.3-§4.5.
"""

import base64
import hashlib
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.email_oauth_env import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH,
    OAuthEnv,
    build_oauth_env,
    token_error,
    token_ok,
    token_unreachable,
)
from tests.test_email_intake_api import _reader_headers

COMPLETE = "/api/v1/email-intake/oauth/complete"
ABANDON = "/api/v1/email-intake/oauth/abandon"


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> OAuthEnv:
    return await build_oauth_env(api_env, monkeypatch)


# --------------------------------------------------------------------------- #
# creating a Microsoft mailbox
# --------------------------------------------------------------------------- #


async def test_a_microsoft_mailbox_is_created_unconnected_and_inactive(env: OAuthEnv) -> None:
    response = await env.client.get(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}", headers=env.headers
    )

    body = response.json()
    assert body["auth_kind"] == "microsoft_oauth"
    assert body["has_credential"] is False
    assert body["active"] is False
    assert body["credential_state"] == "ok"


async def test_the_api_creates_a_microsoft_mailbox_without_a_password(env: OAuthEnv) -> None:
    response = await env.client.post(
        "/api/v1/email-intake/mailboxes",
        headers=env.headers,
        json={
            "address": "desk@toeshee.com",
            "host": "outlook.office365.com",
            "port": 993,
            "username": "desk@toeshee.com",
            "auth_kind": "microsoft_oauth",
            "default_category_id": str(env.category_id),
        },
    )

    assert response.status_code == 201, response.text
    # Created switched off, whatever `active` defaulted to: nothing to poll with yet.
    assert response.json()["active"] is False


async def test_a_microsoft_mailbox_refuses_a_password(env: OAuthEnv) -> None:
    response = await env.client.post(
        "/api/v1/email-intake/mailboxes",
        headers=env.headers,
        json={
            "address": "desk2@toeshee.com",
            "host": "outlook.office365.com",
            "port": 993,
            "username": "desk2@toeshee.com",
            "auth_kind": "microsoft_oauth",
            "password": "typed-by-mistake",
            "default_category_id": str(env.category_id),
        },
    )

    assert response.status_code == 400
    assert "typed-by-mistake" not in response.text


async def test_a_password_mailbox_still_requires_a_password(env: OAuthEnv) -> None:
    response = await env.client.post(
        "/api/v1/email-intake/mailboxes",
        headers=env.headers,
        json={
            "address": "desk3@example.com",
            "host": "imap.example.com",
            "port": 993,
            "username": "desk3@example.com",
            "default_category_id": str(env.category_id),
        },
    )

    assert response.status_code == 400
    assert "password" in response.json()["detail"]


async def test_an_unconnected_mailbox_cannot_be_switched_on(env: OAuthEnv) -> None:
    response = await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        headers=env.headers,
        json={"active": True},
    )

    assert response.status_code == 400
    assert "connect" in response.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


async def test_start_returns_an_authorize_url_with_pkce_and_the_configured_return(
    env: OAuthEnv,
) -> None:
    response = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/start", headers=env.headers
    )

    assert response.status_code == 200
    url = urlparse(response.json()["authorize_url"])
    assert url.netloc == "login.microsoftonline.com"
    query = await env.start()
    assert query["client_id"] == CLIENT_ID
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 43
    assert query["redirect_uri"] == "https://itms.example/settings/email-intake/oauth/callback"


async def test_start_ignores_a_forged_host_header(env: OAuthEnv) -> None:
    headers = {**env.headers, "Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    response = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/start", headers=headers
    )

    assert "evil.example" not in response.text


async def test_start_refuses_a_password_mailbox(env: OAuthEnv) -> None:
    response = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.password_mailbox_id}/oauth/start",
        headers=env.headers,
    )

    assert response.status_code == 409


async def test_start_without_a_console_address_is_503(
    env: OAuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.api_env.app.state.settings, "public_console_url", None)

    response = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/start", headers=env.headers
    )

    assert response.status_code == 503
    assert "ITS_PUBLIC_CONSOLE_URL" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# complete
# --------------------------------------------------------------------------- #


async def test_complete_stores_the_refresh_token_and_never_returns_it(env: OAuthEnv) -> None:
    done = await env.connect()

    assert done.status_code == 200, done.text
    assert REFRESH not in done.text
    assert CLIENT_SECRET not in done.text
    assert done.json()["has_credential"] is True
    assert done.json()["credential_state"] == "ok"
    # The operator switches polling on; connecting does not do it for them.
    assert done.json()["active"] is False
    assert await env.stored_secret() == REFRESH


async def test_the_token_request_goes_to_microsoft_with_pkce_and_the_vaulted_secret(
    env: OAuthEnv,
) -> None:
    query = await env.start()
    await env.client.post(
        COMPLETE, headers=env.headers, json={"state": query["state"], "code": "auth-code"}
    )

    (request,) = env.requests
    assert request.url.host == "login.microsoftonline.com"
    assert request.url.path == "/contoso.onmicrosoft.com/oauth2/v2.0/token"
    (form,) = env.token_forms()
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code"
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert form["redirect_uri"] == "https://itms.example/settings/email-intake/oauth/callback"
    # PKCE is worth nothing unless the verifier sent is the challenge's pre-image.
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == query["code_challenge"]


async def test_a_state_is_single_use(env: OAuthEnv) -> None:
    state = (await env.start())["state"]
    body = {"state": state, "code": "auth-code"}

    first = await env.client.post(COMPLETE, headers=env.headers, json=body)
    second = await env.client.post(COMPLETE, headers=env.headers, json=body)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(env.requests) == 1


async def test_a_failed_exchange_still_uses_up_the_state(env: OAuthEnv) -> None:
    env.use(token_unreachable)
    state = (await env.start())["state"]
    body = {"state": state, "code": "auth-code"}

    first = await env.client.post(COMPLETE, headers=env.headers, json=body)
    env.use(token_ok())
    second = await env.client.post(COMPLETE, headers=env.headers, json=body)

    assert first.status_code == 502
    assert second.status_code == 409


async def test_a_state_from_another_organization_is_invisible(
    env: OAuthEnv, api_env: Any
) -> None:
    state = (await env.start())["state"]
    other_org = await api_env.create_org()
    other = await api_env.headers(other_org, await api_env.create_user(other_org, "org-admin"))

    response = await env.client.post(
        COMPLETE, headers=other, json={"state": state, "code": "auth-code"}
    )

    assert response.status_code == 409
    assert env.requests == []
    # And the rightful owner can still finish.
    owner = await env.client.post(
        COMPLETE, headers=env.headers, json={"state": state, "code": "auth-code"}
    )
    assert owner.status_code == 200


async def test_a_state_started_by_another_admin_is_refused_and_not_burned(
    env: OAuthEnv, api_env: Any
) -> None:
    state = (await env.start())["state"]
    colleague = await api_env.headers(
        env.org_id, await api_env.create_user(env.org_id, "org-admin")
    )

    response = await env.client.post(
        COMPLETE, headers=colleague, json={"state": state, "code": "auth-code"}
    )

    assert response.status_code == 403
    assert env.requests == []
    owner = await env.client.post(
        COMPLETE, headers=env.headers, json={"state": state, "code": "auth-code"}
    )
    assert owner.status_code == 200


async def test_an_expired_authorization_is_refused(env: OAuthEnv) -> None:
    state = (await env.start())["state"]
    await env.expire_authorizations()

    response = await env.client.post(
        COMPLETE, headers=env.headers, json={"state": state, "code": "auth-code"}
    )

    assert response.status_code == 410
    assert env.requests == []


async def test_no_refresh_token_is_refused_with_an_explanation(env: OAuthEnv) -> None:
    env.use(token_ok(refresh=None))

    response = await env.connect()

    assert response.status_code == 400
    assert "offline_access" in response.json()["detail"]
    assert (await env.mailbox()).credential_id is None


async def test_a_refused_application_names_the_microsoft_connection_and_echoes_nothing(
    env: OAuthEnv,
) -> None:
    env.use(token_error(401, "invalid_client", f"secret {CLIENT_SECRET} was wrong"))

    response = await env.connect()

    assert response.status_code == 400
    assert CLIENT_SECRET not in response.text
    assert "Microsoft connection" in response.json()["detail"]


async def test_microsoft_unreachable_is_a_502_that_says_so(env: OAuthEnv) -> None:
    env.use(token_unreachable)

    response = await env.connect()

    assert response.status_code == 502
    assert "reach Microsoft" in response.json()["detail"]


async def test_reconnecting_rotates_the_token_on_the_same_credential(env: OAuthEnv) -> None:
    await env.connect()
    first = (await env.mailbox()).credential_id

    env.use(token_ok(refresh="SECOND-REFRESH"))
    await env.connect()

    row = await env.mailbox()
    assert row.credential_id == first
    assert await env.stored_secret() == "SECOND-REFRESH"


# --------------------------------------------------------------------------- #
# abandon and disconnect
# --------------------------------------------------------------------------- #


async def test_abandon_consumes_the_state_and_leaves_the_mailbox_alone(env: OAuthEnv) -> None:
    state = (await env.start())["state"]

    abandoned = await env.client.post(ABANDON, headers=env.headers, json={"state": state})
    completed = await env.client.post(
        COMPLETE, headers=env.headers, json={"state": state, "code": "auth-code"}
    )

    assert abandoned.status_code == 204
    assert completed.status_code == 409
    assert (await env.mailbox()).credential_id is None
    assert "email_intake.oauth_abandoned" in await env.audit_actions()


async def test_disconnect_destroys_the_credential_and_switches_polling_off(
    env: OAuthEnv,
) -> None:
    await env.connect()
    await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        headers=env.headers,
        json={"active": True},
    )

    response = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/disconnect", headers=env.headers
    )

    assert response.status_code == 200
    assert response.json()["has_credential"] is False
    assert response.json()["active"] is False
    async with AsyncSession(env.api_env.admin_engine) as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM credentials "
                    "WHERE type = 'email_mailbox_oauth' AND org_id = :org"
                ),
                {"org": env.org_id},
            )
        ).scalar_one()
    assert count == 0
    assert "email_intake.oauth_disconnected" in await env.audit_actions()


# --------------------------------------------------------------------------- #
# switching a mailbox between password and Microsoft
# --------------------------------------------------------------------------- #


async def test_switching_a_password_mailbox_to_microsoft_drops_the_password(
    env: OAuthEnv,
) -> None:
    response = await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.password_mailbox_id}",
        headers=env.headers,
        json={"auth_kind": "microsoft_oauth"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_kind"] == "microsoft_oauth"
    assert body["has_credential"] is False
    assert body["active"] is False


async def test_switching_back_to_a_password_needs_one(env: OAuthEnv) -> None:
    await env.connect()

    refused = await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        headers=env.headers,
        json={"auth_kind": "password"},
    )
    accepted = await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        headers=env.headers,
        json={"auth_kind": "password", "password": "back-to-imap"},
    )

    assert refused.status_code == 400
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["auth_kind"] == "password"
    assert await env.stored_secret() == "back-to-imap"


async def test_a_password_cannot_be_set_on_a_microsoft_mailbox(env: OAuthEnv) -> None:
    response = await env.client.patch(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        headers=env.headers,
        json={"password": "stray"},
    )

    assert response.status_code == 400
    assert "stray" not in response.text


# --------------------------------------------------------------------------- #
# audit and RBAC
# --------------------------------------------------------------------------- #


async def test_the_audit_chain_records_the_handshake_and_no_secret(env: OAuthEnv) -> None:
    await env.connect()

    actions = await env.audit_actions()
    assert "email_intake.oauth_started" in actions
    assert "email_intake.oauth_connected" in actions
    # The client secret was decrypted to redeem the code: that is on record too.
    assert "email_intake.credential_used" in actions
    dump = await env.audit_dump()
    assert REFRESH not in dump and CLIENT_SECRET not in dump


async def test_every_oauth_route_needs_admin(env: OAuthEnv, api_env: Any) -> None:
    reader = await _reader_headers(api_env, env.org_id, "email_intake.read")
    for path, body in [
        (f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/start", None),
        (f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/disconnect", None),
        (COMPLETE, {"state": "s", "code": "c"}),
        (ABANDON, {"state": "s"}),
    ]:
        response = await env.client.post(path, headers=reader, json=body)
        assert response.status_code == 403, path

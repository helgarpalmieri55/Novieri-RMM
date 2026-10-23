"""The poller with a Microsoft mailbox: token, not password; revocation is a
state, not an error to retry. Design: mail-oauth design §4.5.
"""

from typing import Any

import pytest
from moto import mock_aws

from app.core.storage import ObjectStore
from app.core.vault import get_vault
from app.modules.email_intake import poller
from tests.email_oauth_env import (
    ACCESS,
    CLIENT_ID,
    REFRESH,
    TENANT,
    OAuthEnv,
    build_oauth_env,
    token_error,
    token_ok,
    token_unreachable,
)


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def fetch_unseen(self, limit: int) -> list[tuple[str, bytes]]:
        return []

    def mark_seen(self, uid: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Client:
        self.calls.append(kwargs)
        return _Client()


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with mock_aws():
        env = await build_oauth_env(api_env, monkeypatch)
        assert (await env.connect()).status_code == 200
        switched_on = await env.client.patch(
            f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
            headers=env.headers,
            json={"active": True},
        )
        assert switched_on.status_code == 200, switched_on.text
        env.requests.clear()
        env.use(token_ok(refresh=None))
        env.store = ObjectStore(api_env.settings)  # type: ignore[attr-defined]
        yield env


async def _poll(env: OAuthEnv, factory: _Factory, mailbox_id: Any = None) -> dict[str, int]:
    return await poller.poll_mailbox(
        env.org_id,
        mailbox_id or env.mailbox_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        store=env.store,  # type: ignore[attr-defined]
        client_factory=factory,
    )


async def test_a_microsoft_mailbox_signs_in_with_a_token_not_a_password(env: OAuthEnv) -> None:
    factory = _Factory()

    result = await _poll(env, factory)

    assert result["failed"] == 0
    (call,) = factory.calls
    assert call["access_token"] == ACCESS
    assert call["password"] == ""
    assert call["username"] == "support@toeshee.com"
    row = await env.mailbox()
    assert row.last_error is None and row.claimed_at is None


async def test_a_password_mailbox_is_unchanged(env: OAuthEnv) -> None:
    factory = _Factory()

    await _poll(env, factory, env.password_mailbox_id)

    (call,) = factory.calls
    assert call.get("access_token") is None
    assert call["password"] == "legacy-imap-password"
    assert env.requests == []


async def test_a_revoked_sign_in_becomes_reconnect_and_stops(env: OAuthEnv) -> None:
    env.use(token_error(400, "invalid_grant", f"refresh token {REFRESH} expired"))
    factory = _Factory()

    result = await _poll(env, factory)

    assert result["failed"] == 1
    assert factory.calls == []  # nothing worth presenting to the server
    row = await env.mailbox()
    assert row.credential_state == "needs_reconsent"
    assert "reconnect" in (row.last_error or "").lower()
    assert REFRESH not in (row.last_error or "")
    assert row.claimed_at is None
    assert "email_intake.oauth_revoked" in await env.audit_actions()

    # And the next scheduled poll does not even ask Microsoft.
    env.requests.clear()
    again = await _poll(env, factory)
    assert again["skipped_claimed"] == 1
    assert env.requests == []


async def test_a_refused_application_points_at_the_microsoft_connection(env: OAuthEnv) -> None:
    env.use(token_error(401, "invalid_client"))

    await _poll(env, _Factory())

    row = await env.mailbox()
    assert row.credential_state == "needs_reconsent"
    assert "Microsoft connection" in (row.last_error or "")
    assert "reconnect this mailbox" not in (row.last_error or "").lower()

    # Fixing the application gives the mailbox another try.
    fixed = await env.client.put(
        "/api/v1/email-intake/oauth-app",
        headers=env.headers,
        json={"tenant_id": TENANT, "client_id": CLIENT_ID, "client_secret": "a-new-secret"},
    )
    assert fixed.status_code == 200
    assert (await env.mailbox()).credential_state == "ok"


async def test_a_network_blip_is_not_a_revocation(env: OAuthEnv) -> None:
    env.use(token_unreachable)

    result = await _poll(env, _Factory())

    assert result["failed"] == 1
    row = await env.mailbox()
    assert row.credential_state == "ok"
    assert row.last_error == "sign-in failed: microsoft-unreachable"
    assert "email_intake.oauth_revoked" not in await env.audit_actions()


async def test_every_poll_records_the_credential_use(env: OAuthEnv) -> None:
    before = (await env.audit_actions()).count("email_intake.credential_used")

    await _poll(env, _Factory())

    assert (await env.audit_actions()).count("email_intake.credential_used") == before + 2


async def test_the_manual_test_uses_the_token_and_names_the_operator(env: OAuthEnv) -> None:
    factory = _Factory()

    result = await poller.test_mailbox(
        env.org_id,
        env.mailbox_id,
        actor_id=env.user_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        client_factory=factory,
    )

    assert result.ok
    assert factory.calls[0]["access_token"] == ACCESS
    events = [e for e in await env.audit() if e.action == "email_intake.credential_used"]
    assert events[-1].actor_id == env.user_id


async def test_the_manual_test_reports_a_revoked_sign_in_without_changing_state(
    env: OAuthEnv,
) -> None:
    env.use(token_error(400, "invalid_grant"))

    result = await poller.test_mailbox(
        env.org_id,
        env.mailbox_id,
        actor_id=env.user_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        client_factory=_Factory(),
    )

    assert not result.ok
    assert "reconnect" in result.detail.lower()
    assert (await env.mailbox()).credential_state == "ok"


async def test_an_unconnected_mailbox_test_says_so(env: OAuthEnv) -> None:
    await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/disconnect", headers=env.headers
    )

    result = await poller.test_mailbox(
        env.org_id,
        env.mailbox_id,
        actor_id=env.user_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        client_factory=_Factory(),
    )

    assert not result.ok
    assert "not connected" in result.detail

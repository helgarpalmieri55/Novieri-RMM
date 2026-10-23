"""One sweep for the two Microsoft secrets, everywhere they could surface.

Each module asserts its own surface; this is the backstop for the one nobody
thought of. Raw bytes, not key checks: a renamed field passes a key check while
still shipping the value. A failure here is a leak to fix, not a test to bend.
"""

import json
from typing import Any

import pytest
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import ObjectStore
from app.core.vault import get_vault
from app.modules.email_intake import poller
from app.modules.email_intake.imap import ImapError
from tests.email_oauth_env import (
    ACCESS,
    CLIENT_SECRET,
    REFRESH,
    OAuthEnv,
    build_oauth_env,
    token_error,
    token_ok,
)


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with mock_aws():
        env = await build_oauth_env(api_env, monkeypatch)
        assert (await env.connect()).status_code == 200
        await env.client.patch(
            f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
            headers=env.headers,
            json={"active": True},
        )
        env.use(token_ok(refresh=None))
        env.store = ObjectStore(api_env.settings)  # type: ignore[attr-defined]
        yield env


async def test_no_route_on_this_module_returns_a_secret(env: OAuthEnv) -> None:
    for path in (
        "/api/v1/email-intake/mailboxes",
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}",
        "/api/v1/email-intake/oauth-app",
        "/api/v1/email-intake/messages",
    ):
        response = await env.client.get(path, headers=env.headers)
        assert response.status_code == 200, path
        for secret in (REFRESH, CLIENT_SECRET, ACCESS):
            assert secret not in response.text, (path, secret)


async def test_no_secret_is_in_any_column_in_plaintext(env: OAuthEnv) -> None:
    async with AsyncSession(env.api_env.admin_engine) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT ciphertext FROM credentials "
                        "UNION ALL SELECT last_error FROM email_mailbox "
                        "UNION ALL SELECT sealed_verifier FROM email_oauth_authorization "
                        "UNION ALL SELECT tenant_id || client_id FROM email_oauth_app"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows
    for value in rows:
        for secret in (REFRESH, CLIENT_SECRET, ACCESS):
            assert secret not in (value or "")


async def test_no_secret_reaches_the_audit_chain(env: OAuthEnv) -> None:
    await poller.poll_mailbox(
        env.org_id,
        env.mailbox_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        store=env.store,  # type: ignore[attr-defined]
        client_factory=lambda **_: _Empty(),
    )

    dump = await env.audit_dump()
    for secret in (REFRESH, CLIENT_SECRET, ACCESS):
        assert secret not in dump


async def test_a_server_that_echoes_the_token_never_puts_it_in_last_error(
    env: OAuthEnv,
) -> None:
    """An IMAP refusal line routinely quotes what it refused."""

    def echoing(**kwargs: Any) -> Any:
        raise ImapError(
            f"AUTHENTICATE failed for {kwargs['access_token']}", stage="login", reason="rejected"
        )

    await poller.poll_mailbox(
        env.org_id,
        env.mailbox_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        store=env.store,  # type: ignore[attr-defined]
        client_factory=echoing,
    )

    row = await env.mailbox()
    assert row.last_error == "login failed: rejected"
    assert ACCESS not in json.dumps(await env.audit_dump())


async def test_a_provider_error_that_quotes_the_token_goes_nowhere(env: OAuthEnv) -> None:
    env.use(token_error(400, "invalid_grant", f"refresh {REFRESH} secret {CLIENT_SECRET}"))

    await poller.poll_mailbox(
        env.org_id,
        env.mailbox_id,
        engine=env.api_env.app_engine,
        vault=get_vault(env.api_env.settings),
        store=env.store,  # type: ignore[attr-defined]
        client_factory=lambda **_: _Empty(),
    )

    row = await env.mailbox()
    for secret in (REFRESH, CLIENT_SECRET):
        assert secret not in (row.last_error or "")
        assert secret not in await env.audit_dump()


class _Empty:
    def fetch_unseen(self, limit: int) -> list[tuple[str, bytes]]:
        return []

    def mark_seen(self, uid: str) -> None:
        pass

    def close(self) -> None:
        pass

"""Shared seeding for the Microsoft 365 sign-in tests (not a test module).

No real Microsoft call is ever made: `microsoft.http_client` is replaced with
an `httpx.MockTransport` whose handler each test chooses, and every request
it sees is recorded so a test can assert what this platform SENT.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import get_vault
from app.models import AuditEvent
from app.modules.email_intake import microsoft
from app.modules.email_intake.mailboxes import create_mailbox
from app.modules.email_intake.models import EmailMailbox, EmailOAuthAuthorization
from app.modules.email_intake.oauth_app import set_app
from app.modules.email_intake.schema import MailboxCreate, OAuthAppUpdate
from tests.test_email_intake_migration import _seed_category, _seed_user

CLIENT_SECRET = "CLIENT-SECRET-SENTINEL"
REFRESH = "REFRESH-TOKEN-SENTINEL"
ACCESS = "ACCESS-TOKEN-SENTINEL"
CONSOLE = "https://itms.example"
TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"

_REAL_CLIENT = httpx.AsyncClient


def token_ok(
    refresh: str | None = REFRESH, access: str = ACCESS
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {"access_token": access, "expires_in": 3599, "token_type": "Bearer"}
        if refresh is not None:
            body["refresh_token"] = refresh
        return httpx.Response(200, json=body)

    return handler


def token_error(status: int, error: str, description: str = "") -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": error, "error_description": description})

    return handler


def token_unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route to host", request=request)


@dataclass
class OAuthEnv:
    api_env: Any
    org_id: UUID
    category_id: UUID
    user_id: UUID
    admin_sub: str
    headers: dict[str, str]
    mailbox_id: UUID
    password_mailbox_id: UUID
    requests: list[httpx.Request] = field(default_factory=list)
    handler: Callable[[httpx.Request], httpx.Response] = field(default=token_ok())

    @property
    def client(self) -> httpx.AsyncClient:
        return self.api_env.client  # type: ignore[no-any-return]

    def use(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler

    def token_forms(self) -> list[dict[str, str]]:
        return [
            {k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in self.requests
        ]

    async def start(self, mailbox_id: UUID | None = None) -> dict[str, str]:
        response = await self.client.post(
            f"/api/v1/email-intake/mailboxes/{mailbox_id or self.mailbox_id}/oauth/start",
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        query = parse_qs(urlparse(response.json()["authorize_url"]).query)
        return {k: v[0] for k, v in query.items()}

    async def connect(self) -> httpx.Response:
        state = (await self.start())["state"]
        return await self.client.post(
            "/api/v1/email-intake/oauth/complete",
            headers=self.headers,
            json={"state": state, "code": "auth-code"},
        )

    async def expire_authorizations(self) -> None:
        async with AsyncSession(self.api_env.admin_engine) as session:
            await session.execute(
                update(EmailOAuthAuthorization).values(
                    expires_at=datetime.now(UTC) - timedelta(hours=1)
                )
            )
            await session.commit()

    async def mailbox(self, mailbox_id: UUID | None = None) -> EmailMailbox:
        async with AsyncSession(self.api_env.admin_engine) as session:
            return (
                await session.execute(
                    select(EmailMailbox).where(EmailMailbox.id == (mailbox_id or self.mailbox_id))
                )
            ).scalar_one()

    async def stored_secret(self, mailbox_id: UUID | None = None) -> str:
        row = await self.mailbox(mailbox_id)
        assert row.credential_id is not None
        vault = get_vault(self.api_env.settings)
        async with org_session(
            self.org_id, engine=self.api_env.app_engine, clients=ClientScope.all()
        ) as session:
            return await vault.checkout(
                session, org_id=self.org_id, credential_id=row.credential_id
            )

    async def audit(self) -> list[AuditEvent]:
        async with AsyncSession(self.api_env.admin_engine) as session:
            return list(
                (
                    await session.execute(
                        select(AuditEvent)
                        .where(AuditEvent.org_id == self.org_id)
                        .order_by(AuditEvent.id)
                    )
                )
                .scalars()
                .all()
            )

    async def audit_actions(self) -> list[str]:
        return [e.action for e in await self.audit()]

    async def audit_dump(self) -> str:
        return json.dumps([e.meta for e in await self.audit()], default=str)


async def build_oauth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> OAuthEnv:
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    org_id = await api_env.create_org()
    category_id = await _seed_category(api_env, org_id)
    admin_sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine) as session:
        from app.models import User

        user_id = (
            await session.execute(select(User.id).where(User.external_subject == admin_sub))
        ).scalar_one()
    actor = await _seed_user(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await set_app(
            session,
            org_id=org_id,
            actor_id=actor,
            body=OAuthAppUpdate(tenant_id=TENANT, client_id=CLIENT_ID, client_secret=CLIENT_SECRET),
            vault=vault,
        )
        oauth_box = await create_mailbox(
            session,
            org_id,
            actor,
            MailboxCreate(
                address="support@toeshee.com",
                host="outlook.office365.com",
                port=993,
                username="support@toeshee.com",
                auth_kind="microsoft_oauth",
                default_category_id=category_id,
                active=False,
            ),
            vault=vault,
        )
        password_box = await create_mailbox(
            session,
            org_id,
            actor,
            MailboxCreate(
                address="legacy@toeshee.com",
                host="imap.toeshee.com",
                port=993,
                username="legacy@toeshee.com",
                password="legacy-imap-password",
                default_category_id=category_id,
            ),
            vault=vault,
        )
        oauth_id, password_id = oauth_box.id, password_box.id

    env = OAuthEnv(
        api_env=api_env,
        org_id=org_id,
        category_id=category_id,
        user_id=user_id,
        admin_sub=admin_sub,
        headers=await api_env.headers(org_id, admin_sub),
        mailbox_id=oauth_id,
        password_mailbox_id=password_id,
    )

    def factory() -> httpx.AsyncClient:
        def record(request: httpx.Request) -> httpx.Response:
            env.requests.append(request)
            return env.handler(request)

        return _REAL_CLIENT(transport=httpx.MockTransport(record), timeout=5.0)

    monkeypatch.setattr(microsoft, "http_client", factory)
    return env

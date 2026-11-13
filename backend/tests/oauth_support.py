"""Shared helpers for the OAuth server's tests (autenticación propia plan
2026-11-06). Tasks 6 to 10 add their own helpers at the end of this file.

`oauth_org` makes an organization the deployment signs in to (`ITS_DEFAULT_
ORG_SLUG`, plan D3) at the console address the MCP tests already use, with
MCP switched on; `seed_client` writes an AI app's registration directly;
`oauth_http` is a network-shaped client: no cookie, no console headers.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth.store import oauth_session
from app.models import Organization
from app.models.oauth import OAuthClient

#: `tests.mcp_support.MCP_BASE`, the console the MCP gate answers at.
BASE = "https://itms.example.com"
RESOURCE = f"{BASE}/mcp"
#: Anthropic: the hosted Claude apps "register exactly this redirect URI" [S33].
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_CALLBACK = "http://127.0.0.1:33418/callback"


async def oauth_org(
    env: Any, prefix: str = "oauth", *, mcp_enabled: bool = True
) -> tuple[UUID, str]:
    org_id = await env.create_org(prefix)
    if mcp_enabled:
        async with AsyncSession(env.admin_engine) as session:
            await session.execute(
                update(Organization)
                .where(Organization.id == org_id)
                .values(settings={"mcp_enabled": True})
            )
            await session.commit()
    slug = await env.org_slug(org_id)
    env.settings.public_console_url = BASE
    env.settings.default_org_slug = slug
    return org_id, slug


async def seed_client(
    env: Any,
    org_id: UUID,
    *,
    client_id: str | None = None,
    redirect_uris: tuple[str, ...] = (CLAUDE_CALLBACK,),
    name: str | None = "Claude",
    kind: str = "registered",
    last_used_at: datetime | None = None,
    metadata_expires_at: datetime | None = None,
) -> OAuthClient:
    now = datetime.now(UTC)
    if kind == "metadata_document" and metadata_expires_at is None:
        metadata_expires_at = now + timedelta(hours=1)
    async with oauth_session(org_id, env.app_engine) as session:
        row = OAuthClient(
            org_id=org_id,
            client_id=client_id or f"itms_{uuid4().hex}",
            kind=kind,
            client_name=name,
            redirect_uris=list(redirect_uris),
            created_at=now,
            last_used_at=last_used_at or now,
            metadata_expires_at=metadata_expires_at if kind == "metadata_document" else None,
        )
        session.add(row)
        await session.flush()
    return row


def oauth_http(env: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE)

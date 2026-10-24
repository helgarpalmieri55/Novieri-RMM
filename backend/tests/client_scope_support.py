"""Clients, sites and per-client technicians for the client-scope tests (not a
test module)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Permission, Role, RolePermission, Site, User, UserRole
from tests.conftest import ApiEnv

#: Task 4's refusal for a write that needs a scope of every client.
EVERY_CLIENT = "only a technician with access to every client can do this"


async def client_with_site(
    env: ApiEnv,
    org: UUID,
    name: str | None = None,
    *,
    client_archived: bool = False,
    site_archived: bool = False,
) -> tuple[UUID, UUID]:
    """A customer client of `org` and its one site, as `(client_id, site_id)`."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        client = Client(
            org_id=org,
            name=name or f"Cliente {uuid4().hex[:6]}",
            kind="customer",
            status="archived" if client_archived else "active",
        )
        s.add(client)
        await s.flush()
        site = Site(
            org_id=org,
            client_id=client.id,
            name="Principal",
            archived_at=datetime.now(UTC) if site_archived else None,
        )
        s.add(site)
        await s.commit()
        return client.id, site.id


async def everything_on(env: ApiEnv, org: UUID, client_id: UUID) -> dict[str, str]:
    """Headers of a person holding EVERY permission code, on one client only."""
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org, name=f"everything-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (await s.execute(select(Permission.id))).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=client_id))
        await s.commit()
    return await env.headers(org, sub)

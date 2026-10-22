"""Task 2 of the MCP server: the `mcp.use` permission, the `mcp_revocation`
table and the `mcp_enabled` org switch."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import PERMISSIONS
from app.models import McpRevocation, Permission


async def test_mcp_use_is_catalogued_and_seeded(api_env) -> None:
    assert "mcp.use" in PERMISSIONS
    async with AsyncSession(api_env.admin_engine) as s:
        found = await s.execute(select(Permission).where(Permission.code == "mcp.use"))
        assert found.scalar_one()


async def test_mcp_revocation_model_maps_the_table(api_env) -> None:
    org = await api_env.create_org("mcp")
    async with AsyncSession(api_env.admin_engine) as s:
        row = McpRevocation(org_id=org, client_id="app-1")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        assert row.user_id is None and row.revoked_by is None and row.revoked_at is not None


async def test_org_admin_holds_mcp_use_and_a_technician_does_not(api_env) -> None:
    org = await api_env.create_org("mcp")
    admin = await api_env.create_user(org, "org-admin")
    tech = await api_env.create_user(org, "technician")
    me_admin = (
        await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, admin))
    ).json()
    me_tech = (
        await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, tech))
    ).json()
    assert "mcp.use" in me_admin["permissions"] and "mcp.use" not in me_tech["permissions"]


async def test_mcp_enabled_is_an_org_setting_that_defaults_off(api_env) -> None:
    org = await api_env.create_org("mcp")
    admin = await api_env.create_user(org, "org-admin")
    h = await api_env.headers(org, admin)
    got = await api_env.client.get("/api/v1/orgs/current", headers=h)
    assert got.json()["settings"].get("mcp_enabled") in (None, False)
    r = await api_env.client.patch(
        "/api/v1/orgs/current", headers=h, json={"settings": {"mcp_enabled": True}}
    )
    assert r.status_code == 200 and r.json()["settings"]["mcp_enabled"] is True
    bad = await api_env.client.patch(
        "/api/v1/orgs/current", headers=h, json={"settings": {"mcp_enabled": "yes"}}
    )
    assert bad.status_code == 422

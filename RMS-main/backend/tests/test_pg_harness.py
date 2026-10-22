from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_app_user_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False

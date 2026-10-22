"""portal_session: the ONLY way a portal request reaches the database.

The fail-closed test is the point of this file. If portal_session sets the org
GUC but not the requester GUC, a portal user sees every ticket in the org --
and every other test here would still pass.
"""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session, portal_session


async def test_portal_session_sets_both_gucs(api_env, portal_engine) -> None:
    org_id = await api_env.create_org("portal-guc")
    requester_id = uuid4()
    async with portal_session(org_id, requester_id, engine=portal_engine) as s:
        got = await s.execute(
            text(
                "SELECT current_setting('app.current_org', true), "
                "       current_setting('app.current_requester', true)"
            )
        )
        org_guc, req_guc = got.one()
    assert org_guc == str(org_id)
    assert req_guc == str(requester_id)


async def test_portal_session_sees_only_the_requesters_own_tickets(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Spec §9.2 -- cross-requester isolation. Must fail if the policy's
    requester predicate is removed."""
    org_id = await api_env.create_org("portal-iso")
    mine = await a_ticket_for(org_id, "me")
    theirs = await a_ticket_for(org_id, "them")

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    ids = {str(r) for r in rows}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids, "saw another requester's ticket"


async def test_a_portal_session_with_no_requester_sees_nothing(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Spec §9.1 -- FAIL-CLOSED, the claim the whole design rests on.

    Connect as portal_user and set ONLY the org GUC, simulating a bug that
    forgot the requester context. The result must be zero rows, not the org's
    tickets.
    """
    org_id = await api_env.create_org("portal-failclosed")
    await a_ticket_for(org_id, "me")

    async with AsyncSession(portal_engine, expire_on_commit=False) as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)}
            )
            rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    assert rows == [], "portal_user saw rows with no requester context set"


async def test_staff_sessions_are_unaffected(api_env, app_engine, a_ticket_for) -> None:
    """Spec §9.10 -- the new policies are TO portal_user, so app_user still
    sees every ticket in its org."""
    org_id = await api_env.create_org("portal-staff")
    await a_ticket_for(org_id, "me")
    await a_ticket_for(org_id, "them")
    async with org_session(org_id, engine=app_engine) as s:
        rows = (await s.execute(text("SELECT id FROM ticket"))).scalars().all()
    assert len(rows) == 2


def test_portal_engine_fails_loudly_when_its_url_is_unset() -> None:
    """`database_portal_url` defaults to "" (a required field would break ~20
    pre-existing test modules that build Settings directly), so a deployment
    that forgets ITS_DATABASE_PORTAL_URL cannot fail at startup. It must
    therefore fail on first portal use with a message naming the variable --
    not with create_async_engine("")'s opaque parse error.
    """
    from pathlib import Path

    import pytest

    from app.core import db
    from app.core.config import Settings, override_settings

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        database_admin_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        database_portal_url="",
        oidc_issuer="https://issuer.example.com",
        oidc_jwks_url="https://issuer.example.com/jwks",
        ca_dir=Path("/tmp/its-portal-test-ca"),
    )
    previous, db._portal_engine = db._portal_engine, None
    try:
        with override_settings(settings):
            with pytest.raises(RuntimeError, match="ITS_DATABASE_PORTAL_URL"):
                db.get_portal_engine()
    finally:
        db._portal_engine = previous

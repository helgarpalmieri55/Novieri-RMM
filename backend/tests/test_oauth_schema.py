"""The OAuth server's tables (migration 0247; autenticación propia design
§6.1; plan 2026-11-06, Task 5).

- all four carry `org_id` under forced RLS: one organization's grants do not
  exist for another (invariant 5, falsifier 1);
- a technician's grant rows are global rows (O under `users`): written only
  under an every-client scope;
- a code and a refresh token are stored as 32-byte digests, never as the
  value;
- a grant names an AI app of its own organization: a foreign key RLS cannot
  see is held by a composite key;
- the deployment's organization is `ITS_DEFAULT_ORG_SLUG`'s, read through
  0246's SECURITY DEFINER lookup, and nothing in the OAuth server opens the
  admin engine (design §6.2; B3-13);
- a registration unused for 30 days is not found.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import clients
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.client_scope import ClientScope
from app.core.global_rows import GlobalRowNotWritable
from app.core.tenancy import org_session
from app.models import Client
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import CLAUDE_CALLBACK, RESOURCE, oauth_org, seed_client
from tests.session_support import user_id_of

TABLES = ("oauth_clients", "oauth_codes", "oauth_refresh_families", "oauth_refresh_tokens")


async def test_the_four_tables_are_tenant_scoped_and_forced(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": list(TABLES)},
            )
        ).all()
    assert {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows} == {
        t: (True, True) for t in TABLES
    }


async def _grant(env, org_id, user_id, app: OAuthClient) -> None:
    """One family, one refresh token and one code of `user_id` for `app`."""
    now = datetime.now(UTC)
    async with oauth_session(org_id, env.app_engine) as session:
        family = OAuthRefreshFamily(
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app.id,
            resource=RESOURCE,
            created_at=now,
            last_used_at=now,
            expires_at=now + timedelta(days=30),
        )
        session.add(family)
        await session.flush()
        session.add(
            OAuthRefreshToken(
                org_id=org_id,
                user_id=user_id,
                family_id=family.id,
                token_hash=sha256(uuid4().bytes).digest(),
                created_at=now,
            )
        )
        session.add(
            OAuthCode(
                org_id=org_id,
                user_id=user_id,
                oauth_client_id=app.id,
                code_hash=sha256(uuid4().bytes).digest(),
                redirect_uri=CLAUDE_CALLBACK,
                code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                resource=RESOURCE,
                created_at=now,
                expires_at=now + timedelta(seconds=60),
            )
        )


async def test_one_organizations_grants_do_not_exist_for_another(api_env) -> None:
    a_id, _ = await oauth_org(api_env, "oauth-a")
    b_id = await api_env.create_org("oauth-b")
    sub = await api_env.create_user(a_id, "org-admin")
    app = await seed_client(api_env, a_id)
    await _grant(api_env, a_id, await user_id_of(api_env, sub), app)
    models = (OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken)
    async with oauth_session(a_id, api_env.app_engine) as session:
        seen = [len((await session.execute(select(m))).scalars().all()) for m in models]
    assert seen == [1, 1, 1, 1]
    async with oauth_session(b_id, api_env.app_engine) as session:
        seen = [len((await session.execute(select(m))).scalars().all()) for m in models]
    assert seen == [0, 0, 0, 0]


async def test_a_technicians_grant_is_a_global_row(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    user_id = await user_id_of(api_env, sub)
    app = await seed_client(api_env, org_id)
    await _grant(api_env, org_id, user_id, app)
    async with oauth_session(org_id, api_env.app_engine) as session:
        family = (await session.execute(select(OAuthRefreshFamily))).scalar_one()
    assert family.client_id is None  # its_client_from_parent copied the technician's NULL
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        await session.commit()
    now = datetime.now(UTC)
    with pytest.raises(GlobalRowNotWritable):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.only([client.id])
        ) as session:
            session.add(
                OAuthRefreshFamily(
                    org_id=org_id,
                    user_id=user_id,
                    oauth_client_id=app.id,
                    resource=RESOURCE,
                    created_at=now,
                    last_used_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )


async def test_a_code_is_stored_as_a_32_byte_digest(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(
                OAuthCode(
                    org_id=org_id,
                    user_id=await user_id_of(api_env, sub),
                    oauth_client_id=app.id,
                    code_hash=b"the-code-itself",
                    redirect_uri=CLAUDE_CALLBACK,
                    code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                    resource=RESOURCE,
                    created_at=now,
                    expires_at=now + timedelta(seconds=60),
                )
            )


async def test_a_grant_names_an_app_of_its_own_organization(api_env) -> None:
    a_id, _ = await oauth_org(api_env, "oauth-a")
    b_id = await api_env.create_org("oauth-b")
    sub = await api_env.create_user(a_id, "org-admin")
    other_app = await seed_client(api_env, b_id)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        # The admin engine skips RLS, as a foreign key check does: only the
        # composite key stands between org A's grant and org B's app.
        async with AsyncSession(api_env.admin_engine) as session:
            session.add(
                OAuthRefreshFamily(
                    org_id=a_id,
                    user_id=await user_id_of(api_env, sub),
                    oauth_client_id=other_app.id,
                    resource=RESOURCE,
                    created_at=now,
                    last_used_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            await session.commit()


async def test_a_registration_unused_for_30_days_is_not_found(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    fresh = await seed_client(api_env, org_id)
    idle = await seed_client(api_env, org_id, last_used_at=now - timedelta(days=30, minutes=1))
    async with oauth_session(org_id, api_env.app_engine) as session:
        found = await clients.find_client(
            session, org_id=org_id, client_id=fresh.client_id, now=now
        )
        gone = await clients.find_client(
            session, org_id=org_id, client_id=idle.client_id, now=now
        )
        assert found is not None and gone is None
        clients.touch(found, now + timedelta(days=29))
    async with oauth_session(org_id, api_env.app_engine) as session:
        again = await clients.find_client(
            session, org_id=org_id, client_id=fresh.client_id, now=now + timedelta(days=58)
        )
    assert again is not None
    view = clients.view(again)
    assert (view.id, view.client_id, view.kind) == (fresh.id, fresh.client_id, "registered")
    assert view.redirect_uris == (CLAUDE_CALLBACK,)


async def test_a_use_is_written_at_most_once_an_hour(api_env) -> None:
    """A3-37: one app row is shared by everyone who connects it; a token
    request that wrote it every time would queue them all on its lock."""
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    row = await seed_client(api_env, org_id, last_used_at=now - timedelta(minutes=59))
    clients.touch(row, now)
    assert row.last_used_at == now - timedelta(minutes=59)
    clients.touch(row, now + timedelta(minutes=1))
    assert row.last_used_at == now + timedelta(minutes=1)


async def test_the_deployment_org_is_the_default_slugs(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    assert await deployment_org(api_env.app) == org_id
    api_env.settings.default_org_slug = None
    assert await deployment_org(api_env.app) is None
    api_env.settings.default_org_slug = f"no-such-{uuid4().hex[:6]}"
    assert await deployment_org(api_env.app) is None


def test_the_oauth_server_never_opens_the_admin_engine() -> None:
    """Design §6.2: before the organization is known, only a SECURITY DEFINER
    function reads; the admin engine bypasses RLS altogether."""
    package = Path(__file__).resolve().parents[1] / "app" / "core" / "auth" / "oauth"
    offenders = [p.name for p in sorted(package.glob("*.py")) if "admin_engine" in p.read_text()]
    assert offenders == []
    assert (package / "store.py").is_file()  # non-vacuity: the scan sees the package

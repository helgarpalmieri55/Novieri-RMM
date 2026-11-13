"""The OAuth server's tables (migration 0247; autenticación propia design
§6.1; plan 2026-11-06, Task 5).

- all four carry `org_id` under forced RLS: one organization's grants do not
  exist for another (invariant 5, falsifier 1);
- a technician's grant rows are global rows (O under `users`): written only
  under an every-client scope;
- a code and a refresh token are stored as 32-byte digests, never as the
  value;
- a grant names an AI app of its own organization, and a token or a code a
  family of its own person: foreign keys RLS cannot see are held by
  composite keys;
- an app's grants go with it (plan D10), and every foreign key that deletes
  or nulls rows here has an index to find them by;
- the closed lists in the CHECKs are the models';
- the deployment's organization is `ITS_DEFAULT_ORG_SLUG`'s, read through
  0246's SECURITY DEFINER lookup, and nothing in the OAuth server opens the
  admin engine, its session or its URL (design §6.2; B3-13);
- a registration unused for 30 days is not found;
- THE APP ROW'S PLACE IN THE LOCK ORDER (`store.py`; fix round 1): a writer of
  grants holds the app row before any grant row, so an idle app's purge waits
  for it instead of meeting it in the cascade, and a use that commits first
  spares the app; the app's other users do not wait (A3-37).
"""

import ast
import asyncio
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import clients
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.client_scope import ClientScope
from app.core.global_rows import GlobalRowNotWritable
from app.core.tenancy import org_session
from app.models import Client
from app.models.oauth import (
    CLIENT_KINDS,
    REVOKE_REASONS,
    OAuthClient,
    OAuthCode,
    OAuthRefreshFamily,
    OAuthRefreshToken,
)
from tests.oauth_support import CLAUDE_CALLBACK, RESOURCE, oauth_org, seed_client
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import user_id_of

TABLES = ("oauth_clients", "oauth_codes", "oauth_refresh_families", "oauth_refresh_tokens")
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
#: A statement that would wait on a row lock fails at once instead.
NO_WAITING = "SET LOCAL lock_timeout = '500ms'"


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


def _code(org_id: UUID, user_id: UUID, app: OAuthClient, **kw: object) -> OAuthCode:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "org_id": org_id,
        "user_id": user_id,
        "oauth_client_id": app.id,
        "code_hash": sha256(uuid4().bytes).digest(),
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": CHALLENGE,
        "resource": RESOURCE,
        "created_at": now,
        "expires_at": now + timedelta(seconds=60),
    }
    return OAuthCode(**(fields | kw))


async def _grant(env, org_id, user_id, app: OAuthClient) -> UUID:
    """One family, one refresh token and one code of `user_id` for `app`; the
    family's id."""
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
        session.add(_code(org_id, user_id, app))
    return family.id


async def _two_people(env, org_id) -> tuple[UUID, UUID]:
    first = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    second = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    return first, second


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
    with pytest.raises(IntegrityError, match="ck_oauth_codes_code_hash"):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(
                _code(
                    org_id,
                    await user_id_of(api_env, sub),
                    app,
                    code_hash=b"the-code-itself",
                )
            )


async def test_a_refresh_token_is_stored_as_a_32_byte_digest(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    user_id = await user_id_of(api_env, await api_env.create_user(org_id, "org-admin"))
    family_id = await _grant(api_env, org_id, user_id, await seed_client(api_env, org_id))
    with pytest.raises(IntegrityError, match="ck_oauth_refresh_tokens_token_hash"):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(
                OAuthRefreshToken(
                    org_id=org_id,
                    user_id=user_id,
                    family_id=family_id,
                    token_hash=b"the-refresh-token-itself",
                    created_at=datetime.now(UTC),
                )
            )


async def test_a_grant_names_an_app_of_its_own_organization(api_env) -> None:
    a_id, _ = await oauth_org(api_env, "oauth-a")
    b_id = await api_env.create_org("oauth-b")
    sub = await api_env.create_user(a_id, "org-admin")
    other_app = await seed_client(api_env, b_id)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError, match="fk_oauth_refresh_families_app_org"):
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


async def test_a_refresh_token_names_a_family_of_its_own_person(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    ana, bob = await _two_people(api_env, org_id)
    anas = await _grant(api_env, org_id, ana, await seed_client(api_env, org_id))
    with pytest.raises(IntegrityError, match="fk_oauth_refresh_tokens_family_user"):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(
                OAuthRefreshToken(
                    org_id=org_id,
                    user_id=bob,
                    family_id=anas,
                    token_hash=sha256(uuid4().bytes).digest(),
                    created_at=datetime.now(UTC),
                )
            )


async def test_a_code_names_a_family_of_its_own_person(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    ana, bob = await _two_people(api_env, org_id)
    app = await seed_client(api_env, org_id)
    anas = await _grant(api_env, org_id, ana, app)
    with pytest.raises(IntegrityError, match="fk_oauth_codes_family_user"):
        async with oauth_session(org_id, api_env.app_engine) as session:
            session.add(_code(org_id, bob, app, family_id=anas))


async def test_a_code_outlives_its_family_and_keeps_its_person(api_env) -> None:
    """`ON DELETE SET NULL (family_id)`: the family's end empties the code's
    `family_id` and leaves its `user_id`, which a bare SET NULL on the
    composite key would empty too (and NOT NULL would refuse the delete)."""
    org_id, _ = await oauth_org(api_env)
    user_id = await user_id_of(api_env, await api_env.create_user(org_id, "org-admin"))
    app = await seed_client(api_env, org_id)
    family_id = await _grant(api_env, org_id, user_id, app)
    async with oauth_session(org_id, api_env.app_engine) as session:
        code = _code(org_id, user_id, app, family_id=family_id)
        session.add(code)
    async with oauth_session(org_id, api_env.app_engine) as session:
        await session.execute(delete(OAuthRefreshFamily).where(OAuthRefreshFamily.id == family_id))
    async with oauth_session(org_id, api_env.app_engine) as session:
        kept = (
            await session.execute(
                select(OAuthCode.user_id, OAuthCode.family_id).where(OAuthCode.id == code.id)
            )
        ).one()
    assert (kept.user_id, kept.family_id) == (user_id, None)


async def test_an_apps_grants_go_with_it(api_env) -> None:
    """Plan D10: an expired registration takes its families, their refresh
    tokens and its codes along; another app's stay."""
    org_id, _ = await oauth_org(api_env)
    user_id = await user_id_of(api_env, await api_env.create_user(org_id, "org-admin"))
    going, staying = await seed_client(api_env, org_id), await seed_client(api_env, org_id)
    await _grant(api_env, org_id, user_id, going)
    kept = await _grant(api_env, org_id, user_id, staying)
    async with oauth_session(org_id, api_env.app_engine) as session:
        await session.execute(delete(OAuthClient).where(OAuthClient.id == going.id))
    async with oauth_session(org_id, api_env.app_engine) as session:
        families = (await session.execute(select(OAuthRefreshFamily.id))).scalars().all()
        tokens = (await session.execute(select(OAuthRefreshToken.family_id))).scalars().all()
        codes = (await session.execute(select(OAuthCode.oauth_client_id))).scalars().all()
    assert (families, tokens, codes) == ([kept], [kept], [staying.id])


async def test_every_foreign_key_that_acts_on_delete_has_an_index(admin_engine) -> None:
    """A delete of a person, an app or a family reaches these rows by the
    foreign key's columns (CASCADE, SET NULL): without an index led by them,
    each such delete -- an idle app's purge among them -- scans the table."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conname AS name, EXISTS (SELECT 1 FROM pg_index i "
                    "  WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]) AS indexed "
                    "FROM pg_constraint c "
                    "WHERE c.contype = 'f' AND c.confdeltype IN ('c', 'n', 'd') "
                    "AND c.conrelid::regclass::text = ANY(:names)"
                ),
                {"names": list(TABLES)},
            )
        ).all()
    found = {r.name: r.indexed for r in rows}
    assert sorted(name for name, indexed in found.items() if not indexed) == []
    assert set(found) == {
        "oauth_refresh_families_user_id_fkey",
        "fk_oauth_refresh_families_app_org",
        "oauth_refresh_tokens_user_id_fkey",
        "fk_oauth_refresh_tokens_family_user",
        "oauth_codes_user_id_fkey",
        "fk_oauth_codes_app_org",
        "fk_oauth_codes_family_user",
    }


async def test_the_closed_lists_are_the_models(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        checks = dict(
            (
                await conn.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('ck_oauth_clients_kind', "
                        "'ck_oauth_refresh_families_reason')"
                    )
                )
            )
            .tuples()
            .all()
        )
    assert set(re.findall(r"'(\w+)'", checks["ck_oauth_clients_kind"])) == set(CLIENT_KINDS)
    reasons = set(re.findall(r"'(\w+)'", checks["ck_oauth_refresh_families_reason"]))
    assert reasons == set(REVOKE_REASONS)


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


# --------------------------------------------------------------------------- #
# The app row's place in the lock order (`store.py`, step 3; fix round 1).
# --------------------------------------------------------------------------- #


async def test_a_held_app_keeps_its_purge_waiting_and_none_of_its_users(api_env) -> None:
    """`clients.hold` takes the app row FOR KEY SHARE: the lock an idle app's
    purge (a DELETE, FOR UPDATE) must wait for, and one that neither another
    person's token request (`hold` again) nor a use being written waits for
    (A3-37). Here no use is due, so nothing is written."""
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    app = await seed_client(api_env, org_id, last_used_at=now - timedelta(minutes=5))
    async with oauth_session(org_id, api_env.app_engine) as session:
        held = await clients.hold(session, org_id=org_id, app_id=app.id, now=now)
        assert held is not None and held.last_used_at == now - timedelta(minutes=5)
        with pytest.raises(DBAPIError, match="lock timeout"):
            async with oauth_session(org_id, api_env.app_engine) as purge:
                await purge.execute(text(NO_WAITING))
                await purge.execute(delete(OAuthClient).where(OAuthClient.id == app.id))
        async with oauth_session(org_id, api_env.app_engine) as other:
            await other.execute(text(NO_WAITING))
            assert await clients.hold(other, org_id=org_id, app_id=app.id, now=now) is not None
        async with oauth_session(org_id, api_env.app_engine) as use:
            await use.execute(text(NO_WAITING))
            await use.execute(
                text("UPDATE oauth_clients SET last_used_at = :t WHERE id = :id"),
                {"t": now, "id": app.id},
            )


async def test_a_held_app_due_a_use_has_it_written_at_once(api_env) -> None:
    """When a use is due, `hold` writes it and flushes: the UPDATE is sent
    then, before the caller writes any grant row, not at the commit."""
    org_id, _ = await oauth_org(api_env)
    now = datetime.now(UTC)
    app = await seed_client(api_env, org_id, last_used_at=now - timedelta(hours=2))
    async with oauth_session(org_id, api_env.app_engine) as session:
        held = await clients.hold(session, org_id=org_id, app_id=app.id, now=now)
        assert held is not None and held.last_used_at == now
        with pytest.raises(DBAPIError, match="lock timeout"):
            async with oauth_session(org_id, api_env.app_engine) as rival:
                await rival.execute(text(NO_WAITING))
                await rival.execute(
                    text("UPDATE oauth_clients SET last_used_at = :t WHERE id = :id"),
                    {"t": now, "id": app.id},
                )
    async with oauth_session(org_id, api_env.app_engine) as session:
        stored = (
            await session.execute(
                select(OAuthClient.last_used_at).where(OAuthClient.id == app.id)
            )
        ).scalar_one()
    assert stored == now


async def test_an_app_gone_idle_or_of_another_org_is_not_held(api_env) -> None:
    """A purge that committed after the app was looked up leaves nothing to
    hold -- None, not a stale-row 500 at the flush."""
    org_id, _ = await oauth_org(api_env)
    other_org = await api_env.create_org("oauth-b")
    now = datetime.now(UTC)
    purged = await seed_client(api_env, org_id)
    idle = await seed_client(api_env, org_id, last_used_at=now - timedelta(days=30, minutes=1))
    foreign = await seed_client(api_env, other_org)
    async with oauth_session(org_id, api_env.app_engine) as session:
        assert await clients.find_client(
            session, org_id=org_id, client_id=purged.client_id, now=now
        )
        async with oauth_session(org_id, api_env.app_engine) as purge:
            await purge.execute(delete(OAuthClient).where(OAuthClient.id == purged.id))
        assert await clients.hold(session, org_id=org_id, app_id=purged.id, now=now) is None
        assert await clients.hold(session, org_id=org_id, app_id=idle.id, now=now) is None
        assert await clients.hold(session, org_id=org_id, app_id=foreign.id, now=now) is None
    async with oauth_session(org_id, api_env.app_engine) as session:
        untouched = (
            await session.execute(
                select(OAuthClient.last_used_at).where(OAuthClient.id == idle.id)
            )
        ).scalar_one()
    assert untouched == now - timedelta(days=30, minutes=1)


async def test_a_purge_that_waited_on_a_use_spares_the_app(api_env) -> None:
    """The purge in `guards.lock_user_row`'s lock map: one app per statement,
    the idle condition inside it. It meets a holder of the app row, waits on
    the row and not in the cascade, and once the holder's use commits,
    Postgres re-checks the condition on the row as it now is: the app and its
    grants survive."""
    org_id, _ = await oauth_org(api_env)
    user_id = await user_id_of(api_env, await api_env.create_user(org_id, "org-admin"))
    then = datetime.now(UTC) - timedelta(days=30)
    app = await seed_client(api_env, org_id, last_used_at=then)
    family_id = await _grant(api_env, org_id, user_id, app)

    async def purge() -> int:
        async with oauth_session(org_id, api_env.app_engine) as session:
            result = await session.execute(
                delete(OAuthClient).where(
                    OAuthClient.id == app.id, OAuthClient.last_used_at <= then
                )
            )
            return int(result.rowcount)

    async with oauth_session(org_id, api_env.app_engine) as session:
        held = await clients.hold(
            session, org_id=org_id, app_id=app.id, now=then + timedelta(days=29)
        )
        assert held is not None
        waiting = asyncio.create_task(purge())
        blocked = await until_blocked_by(api_env.app_engine, await backend_pid(session))
        assert [normalised(q).split(" ")[0] for q in blocked] == ["DELETE"]
    assert await waiting == 0
    async with oauth_session(org_id, api_env.app_engine) as session:
        survivors = (
            await session.execute(select(OAuthRefreshFamily.id).where(
                OAuthRefreshFamily.oauth_client_id == app.id
            ))
        ).scalars().all()
    assert survivors == [family_id]


# --------------------------------------------------------------------------- #
# The deployment's organization, and no admin engine (design §6.2; B3-13).
# --------------------------------------------------------------------------- #


async def test_the_deployment_org_is_the_default_slugs(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    assert await deployment_org(api_env.app) == org_id
    api_env.settings.default_org_slug = None
    assert await deployment_org(api_env.app) is None
    api_env.settings.default_org_slug = f"no-such-{uuid4().hex[:6]}"
    assert await deployment_org(api_env.app) is None


PACKAGE = Path(__file__).resolve().parents[1] / "app" / "core" / "auth" / "oauth"
#: The RLS-bypassing role's doors: its engine (`app.state.admin_engine`,
#: `db.get_admin_engine()`), its session (`tenancy.admin_session()`, which
#: defaults to that engine) and its URL (`settings.database_admin_url`).
ADMIN_DOORS = frozenset({"admin_engine", "get_admin_engine", "admin_session", "database_admin_url"})


def _admin_doors(sources: list[tuple[str, ast.Module]]) -> list[str]:
    """`path:line:name` of every import, name or attribute that opens one."""
    found: list[str] = []
    for path, tree in sources:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [part for alias in node.names for part in alias.name.split(".")]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            found.extend(f"{path}:{node.lineno}:{name}" for name in names if name in ADMIN_DOORS)
    return sorted(found)


def test_the_oauth_server_never_opens_the_admin_engine() -> None:
    """Design §6.2: before the organization is known, only a SECURITY DEFINER
    function reads; the admin engine bypasses RLS altogether."""
    files = sorted(PACKAGE.glob("*.py"))
    assert _admin_doors([(p.name, ast.parse(p.read_text())) for p in files]) == []
    # And the text, for a name the syntax does not show: `getattr(x, "...")`.
    assert [p.name for p in files if any(door in p.read_text() for door in ADMIN_DOORS)] == []
    assert {"store.py", "clients.py"} <= {p.name for p in files}  # the scan sees the package


def test_the_admin_engine_guard_is_not_vacuous() -> None:
    bad = ast.parse(
        "from app.core.tenancy import admin_session\n"
        "from app.core import db, tenancy\n"
        "a = db.get_admin_engine()\n"
        "b = request.app.state.admin_engine\n"
        "c = create_async_engine(settings.database_admin_url)\n"
        "d = tenancy.admin_session()\n"
        "import app.core.db.get_admin_engine\n"
    )
    assert _admin_doors([("x.py", bad)]) == [
        "x.py:1:admin_session",
        "x.py:3:get_admin_engine",
        "x.py:4:admin_engine",
        "x.py:5:database_admin_url",
        "x.py:6:admin_session",
        "x.py:7:get_admin_engine",
    ]

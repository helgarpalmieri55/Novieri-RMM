"""Test harness: throwaway PostgreSQL 16 database with a non-BYPASSRLS app role.

Tests run against real Postgres because RLS is the security backstop under test.
The harness connects as the `postgres` superuser (peer auth via unix socket is not
available to the test uid, so it uses TCP with trust/local config) to create a
throwaway database and an `app_user` role, runs Alembic migrations as admin, and
hands tests two DSNs: the app (RLS-constrained) one and the admin one.
"""

import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent

APP_ROLE = "app_user"
APP_PASSWORD = "app"

PORTAL_ROLE = "portal_user"
PORTAL_PASSWORD = "portal"


# --------------------------------------------------------------------------- #
# The deployment's AI provider (spec `docs/modules/ai-admin/spec.md` §0.14).
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def ai_provider_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as a deployment that HAS a model provider configured.

    **THIS FIXTURE EXISTS BECAUSE THE SWITCH MOVED.** Before §0.14 (the
    operator's decision of 2026-08-25) an AI test seeded `ai_settings.enabled =
    true` and `provider_key = 'local'` and that was the whole of "this org can
    use the assistant". Both columns are gone: whether the assistant answers is
    now a property of the deployment's `ITS_AI_*` environment and nothing else.
    So the suite has to say which deployment it is, and this is where it says
    it -- one self-hosted `openai_compatible` provider, which §0.10 documents as
    the on-prem default and under which nothing leaves the network.

    It is autouse and suite-wide DELIBERATELY: the switch is global now, and a
    per-module fixture would let a module quietly run as a differently
    configured deployment. A test that needs the UNCONFIGURED deployment -- and
    several do, because 503-when-nothing-is-configured is the one remaining
    unavailability path -- overrides `_CONFIG_CACHE` with `{}` itself, which
    still works because this sets the same attribute.

    THE BASE URL IS `.invalid` ON PURPOSE (RFC 2606): nothing here should ever
    reach a network, and a hostname that cannot resolve makes an accidental real
    request fail loudly rather than hit somebody's machine.
    """
    from app.core.ai.providers import ProviderConfig

    monkeypatch.setattr(
        "app.core.ai.providers._CONFIG_CACHE",
        {
            "local": ProviderConfig(
                key="local",
                kind="openai_compatible",
                base_url="http://vllm.test.invalid/v1",
                model="a-model-name",
            )
        },
    )


def _psql(sql: str, dbname: str = "postgres") -> str:
    """Run SQL as the postgres superuser via sudo -u postgres psql."""
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-A", "-t", "-d", dbname, "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class PgCluster:
    dbname: str
    app_url: str
    admin_url: str
    portal_url: str
    app_url_sync: str
    admin_url_sync: str


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[PgCluster]:
    dbname = f"its_test_{secrets.token_hex(4)}"
    _psql(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
        f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS; "
        f"END IF; END $$;"
    )
    # portal_user: end-user-portal role. Migration 0028 creates it as NOLOGIN
    # (idempotent, `IF NOT EXISTS`) and grants its table privileges; here we
    # only need it to be able to LOG IN for the test cluster, so add that
    # afterward rather than duplicating the migration's grants.
    _psql(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{PORTAL_ROLE}') THEN "
        f"CREATE ROLE {PORTAL_ROLE} LOGIN PASSWORD '{PORTAL_PASSWORD}' NOSUPERUSER NOBYPASSRLS; "
        f"ELSE ALTER ROLE {PORTAL_ROLE} LOGIN PASSWORD '{PORTAL_PASSWORD}'; END IF; END $$;"
    )
    admin_password = "its_admin_test"
    _psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'its_admin') THEN "
        f"CREATE ROLE its_admin LOGIN PASSWORD '{admin_password}' NOSUPERUSER "
        "CREATEDB CREATEROLE; END IF; END $$;"
    )
    # The admin role is the explicit cross-org path (migrations, org bootstrap,
    # audited super-admin operations). All request traffic uses app_user, which
    # is RLS-constrained; FORCE RLS on tenant tables applies even to the owner,
    # so the admin role carries BYPASSRLS deliberately.
    _psql("ALTER ROLE its_admin BYPASSRLS")
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO {APP_ROLE}', dbname)
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO {PORTAL_ROLE}', dbname)

    host = os.environ.get("PGHOST", "127.0.0.1")
    admin_dsn = f"its_admin:{admin_password}@{host}:5432/{dbname}"
    app_dsn = f"{APP_ROLE}:{APP_PASSWORD}@{host}:5432/{dbname}"
    portal_dsn = f"{PORTAL_ROLE}:{PORTAL_PASSWORD}@{host}:5432/{dbname}"
    cluster = PgCluster(
        dbname=dbname,
        app_url=f"postgresql+asyncpg://{app_dsn}",
        admin_url=f"postgresql+asyncpg://{admin_dsn}",
        portal_url=f"postgresql+asyncpg://{portal_dsn}",
        app_url_sync=f"postgresql://{app_dsn}",
        admin_url_sync=f"postgresql://{admin_dsn}",
    )

    _run_migrations(cluster)

    yield cluster

    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _run_migrations(cluster: PgCluster) -> None:
    """Upgrade the throwaway DB to head as the admin role, then grant app privileges."""
    versions = BACKEND_DIR / "migrations" / "versions"
    if not versions.exists() or not any(versions.glob("*.py")):
        return  # no migrations yet (early tasks)
    env = os.environ | {"ITS_ALEMBIC_URL": cluster.admin_url}
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    grants = (
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};"
        f"REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE};"
        f"REVOKE INSERT, UPDATE, DELETE ON permissions, role_permissions FROM {APP_ROLE};"
        f"REVOKE UPDATE, DELETE ON security_events FROM {APP_ROLE};"
    )
    _psql(grants, cluster.dbname)


@pytest.fixture()
async def admin_engine(pg_cluster: PgCluster) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_cluster.admin_url)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def app_engine(pg_cluster: PgCluster) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_cluster.app_url)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def bypassrls_session(admin_engine: AsyncEngine):
    """A session on the BYPASSRLS `its_admin` role, for the ONE test per file
    that has to observe the application's OWN `org_id` predicate.

    WHY THIS EXISTS. Everything a request touches runs as `app_user`, which is
    NOBYPASSRLS, and `0002_rls.py` puts a forced `tenant_isolation` policy on
    every tenant table. Inside `org_session` a `SELECT` carrying no tenant
    predicate at all therefore returns exactly the rows a correct one would --
    so deleting `WHERE org_id` from a service turns no route test and no
    `org_session` test red. `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`
    counted 219 tests in that position, several of them named as though they
    pinned the filter.

    On this session Postgres stops filtering and the application's predicate is
    the only thing scoping anything, which makes the belt observable
    independently of the braces. That is the whole point: a test written here
    goes RED when the predicate is deleted, and a test written on `org_session`
    does not.

    HOW TO USE IT. Seed both orgs' rows through this same session (or through
    `admin_engine` directly), then call the service under test with it and
    assert that org A's call does not answer with org B's row. Assert counts
    and totals as well as page contents -- an org-wide count is where a leak
    hides behind a correctly-filtered page.

    WHAT IT IS NOT FOR. It is not a faster `org_session`. A test that merely
    wants rows in the database should seed with `admin_engine` and exercise the
    application through `org_session` or the route, the way the rest of the
    suite does; RLS is the security backstop and most tests should be running
    with it on. Reach for this only when the assertion IS the application
    filter.

    NO TRANSACTION IS OPENED HERE, deliberately: services under test call
    `session.commit()` themselves, and wrapping this in `session.begin()` (the
    shape `admin_session()` uses) makes those commits close the block's
    transaction out from under a test that then wants to read the rows back.
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture()
async def portal_engine(pg_cluster: PgCluster) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_cluster.portal_url)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# API app environment: real DB + fake IdP + local CA/signer, in-process ASGI.
# ---------------------------------------------------------------------------

from uuid import UUID, uuid4  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402


class ApiEnv:
    def __init__(self, app, client, idp, settings, admin_engine, app_engine):
        self.app = app
        self.client = client
        self.idp = idp
        self.settings = settings
        self.admin_engine = admin_engine
        self.app_engine = app_engine

    async def create_org(self, slug_prefix: str = "org") -> UUID:
        from app.models import Organization

        slug = f"{slug_prefix}-{uuid4().hex[:8]}"
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            org = Organization(name=slug, slug=slug)
            session.add(org)
            await session.commit()
            return org.id

    async def org_slug(self, org_id: UUID) -> str:
        from app.models import Organization

        async with AsyncSession(self.admin_engine) as session:
            return (
                await session.execute(select(Organization.slug).where(Organization.id == org_id))
            ).scalar_one()

    async def create_user(self, org_id: UUID, *role_names: str, sub: str | None = None) -> str:
        """Create a user projection row with global roles; returns the OIDC sub."""
        from app.models import Role, User, UserRole

        sub = sub or f"sub-{uuid4().hex[:10]}"
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
            session.add(user)
            await session.flush()
            for name in role_names:
                role = (
                    await session.execute(
                        select(Role).where(Role.name == name, Role.org_id.is_(None))
                    )
                ).scalar_one()
                session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
            await session.commit()
        return sub

    async def headers(self, org_id: UUID, sub: str) -> dict[str, str]:
        """Auth headers for a user `create_user` seeded.

        THE EMAIL CLAIM MATCHES THE SEEDED ROW, and it has to. `create_user`
        writes `{sub}@test.local`; the fake IdP's default claim is
        `user@example.com`, so every token minted here used to assert an
        address the row did not hold. That was invisible until design §7.2
        (`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md`)
        made `app/api/deps.py` refresh `users.email` from the claim on
        admission -- at which point the harness's own inconsistency became a
        genuine identity-change event, and an `auth.email_changed` row landed
        at the head of the audit chain in every test that asserts the complete
        list of actions its module wrote.

        Fixing it here rather than in those tests: the mismatch was a fixture
        telling a small lie, and pinning `auth.email_changed` into a reporting
        or SLA test would have taught two unrelated suites to expect it. Tests
        for which the divergence IS the subject mint their own token with an
        explicit `email=` (see `tests/test_me_profile.py`).
        """
        slug = await self.org_slug(org_id)
        token = self.idp.token(sub, slug, email=f"{sub}@test.local")
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
async def api_env(pg_cluster: PgCluster, tmp_path, admin_engine, app_engine):
    from app.core.config import Settings
    from app.main import create_app
    from tests.fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client

    idp = FakeIdp()
    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        database_portal_url=pg_cluster.portal_url,
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9"),
    )
    app = create_app(settings, jwks_http_client=jwks_client(idp))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            yield ApiEnv(app, client, idp, settings, admin_engine, app_engine)
    finally:
        # DISPOSE WHAT `create_app` BUILT, not only what this fixture built.
        # `create_app` constructs its own `app.state.app_engine` and
        # `app.state.admin_engine`; every request served through the transport
        # above checks a connection out of them and checks it back IN -- held,
        # not closed, which is the entire point of a pool. Nothing here used to
        # dispose them, so each of the ~137 files that take `api_env` left live
        # Postgres backends behind at a rate bounded only by when CPython got
        # around to finalising the engine and asyncpg's `__del__` terminated
        # the socket. That is why the exhaustion was intermittent, and why it
        # accused whichever test happened to be running when the last of
        # Postgres's 100 slots went (`tests/test_realm_settings_api.py`, twice,
        # on two different tests). The `finally` matters as much as the calls:
        # a test that raises must not skip the release.
        owned = [app.state.app_engine, app.state.admin_engine]
        # And the third one, which only exists after a portal request has been
        # served: `app.modules.portal.api._portal_engine` builds it on first
        # use and caches it on `app.state`. Guarded with `getattr` because most
        # tests never touch the portal, and an unconditional attribute access
        # here would fail every one of them.
        portal = getattr(app.state, "portal_engine", None)
        if portal is not None:
            owned.append(portal)
        for engine in owned:
            await engine.dispose()
        # Redis is the same shape -- a lazily-built pool the app owns and
        # nothing else closes. Not the cause of the Postgres exhaustion, but
        # leaking it from every API test is the same defect with a different
        # server on the other end.
        #
        # RESOLVED, NOT CALLED BLIND. Several tests swap `app.state.redis` for
        # a double that implements the two or three commands they exercise and
        # nothing else (`_BrokenRedis` in tests/test_email_intake_api.py,
        # `_NoHeartbeats` in tests/test_platform_health_probes.py). A double
        # holds no connection pool, so there is nothing to release; calling
        # `aclose()` on it unconditionally turned that email-intake test into a
        # teardown ERROR. Deleting this guard reproduces it.
        aclose = getattr(app.state.redis, "aclose", None)
        if aclose is not None:
            await aclose()


# ---------------------------------------------------------------------------
# Portal test helper: a minimal ticket owned by a given requester.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ATicket:
    id: UUID
    requester_id: UUID


@pytest.fixture()
def a_ticket_for(admin_engine: AsyncEngine):
    """Returns an async factory `a_ticket_for(org_id, who) -> ATicket` that
    creates a fresh user and a ticket they requested, via the admin (RLS-
    bypassing) engine -- so portal-session tests can set up fixtures without
    themselves depending on portal_session.

    `who` only distinguishes call sites in test failure output; a fresh user
    is created every call.
    """

    async def _make(org_id: UUID, who: str) -> ATicket:
        from app.models import Ticket, TicketCategory, TicketQueue, User

        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            async with session.begin():
                user = User(
                    org_id=org_id,
                    external_subject=f"portal-{who}-{uuid4().hex[:10]}",
                    email=f"{who}-{uuid4().hex[:6]}@test.local",
                )
                session.add(user)
                await session.flush()

                queue = TicketQueue(
                    org_id=org_id,
                    key=f"q-{uuid4().hex[:10]}",
                    name=f"Queue for {who}",
                    created_by=user.id,
                )
                session.add(queue)
                await session.flush()

                category = TicketCategory(
                    org_id=org_id,
                    key=f"c-{uuid4().hex[:10]}",
                    name=f"Category for {who}",
                    type="incident",
                    default_queue_id=queue.id,
                    created_by=user.id,
                )
                session.add(category)
                await session.flush()

                ticket = Ticket(
                    org_id=org_id,
                    client_id=org_id,
                    number=f"T-{uuid4().hex[:10]}",
                    type="incident",
                    category_id=category.id,
                    queue_id=queue.id,
                    status="new",
                    subject=f"ticket for {who}",
                    requester_id=user.id,
                    origin="portal",
                    created_by=user.id,
                )
                session.add(ticket)
                await session.flush()

                return ATicket(id=ticket.id, requester_id=user.id)

    return _make


@pytest.fixture()
def add_comment_as_staff(admin_engine: AsyncEngine):
    """Returns an async factory `add_comment_as_staff(ticket, *, body,
    internal) -> None` that appends a comment to `ticket` (an `ATicket` from
    `a_ticket_for`) as a freshly-created staff member, via the admin
    (RLS-bypassing) engine -- so portal-service tests can seed both a public
    comment and an internal note without themselves depending on
    `portal_session` or on `service_desk.threads.add_comment`'s own
    audit/notification side effects.

    `internal=True` writes `visibility="internal"` -- the row a requester
    must NEVER see (spec §2.3, §9.3); `internal=False` writes
    `visibility="public"`.
    """

    async def _make(ticket: ATicket, *, body: str, internal: bool) -> None:
        from app.models import Ticket, TicketComment, User

        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            async with session.begin():
                ticket_row = await session.get(Ticket, ticket.id)
                assert ticket_row is not None
                staff = User(
                    org_id=ticket_row.org_id,
                    external_subject=f"staff-{uuid4().hex[:10]}",
                    email=f"staff-{uuid4().hex[:6]}@test.local",
                )
                session.add(staff)
                await session.flush()

                session.add(
                    TicketComment(
                        org_id=ticket_row.org_id,
                        ticket_id=ticket.id,
                        author_id=staff.id,
                        author_type="user",
                        body=body,
                        visibility="internal" if internal else "public",
                    )
                )

    return _make

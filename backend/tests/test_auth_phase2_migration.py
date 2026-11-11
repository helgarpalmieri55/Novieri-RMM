"""Migration 0246 (own authentication, phase 2; plan 2026-11-06, Task 1).

- upgrade 0245 -> 0246 -> downgrade 0245 -> upgrade 0246 leaves nothing
  behind: the two tables, the column, the function and the
  `its_client_to_auth_external_identities` trigger that lives on `users`.
- `its_auth_org` answers the id a slug names and nothing else, to `app_user`
  only (design §6.2, plan decision D36).
- A Microsoft identity follows its person (invariant 5a): a contact's link is
  inside the contact's client; a technician limited to another client does
  not see it.
- One Microsoft account is one person per organization, and a person has one
  link per provider (plan decision 1); the settings cannot be switched on half
  configured, and a certificate is all there or not at all (decision 3), the
  next one too, and only beside a current one (decision 5); a client lists at
  most twenty tenants, and the personal-accounts tenant is stored nowhere
  (decision 4). Every statement a CHECK refuses runs INSIDE `pytest.raises`:
  PostgreSQL checks a CHECK at the statement, not at the commit ("NOT NULL
  and CHECK constraints are not deferrable", CREATE TABLE), so an UPDATE
  sent by `session.execute` raises there.
"""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.auth.lookup import org_of_slug
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Client, Credential, Organization, User
from app.models.auth import ENTRA, AuthEntraSettings, AuthExternalIdentity
from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLES = ("auth_external_identities", "auth_entra_settings")
FUNCTION = "its_auth_org(text)"
#: Microsoft's personal-accounts tenant ([M4]).
CONSUMERS = UUID("9188040d-6c67-4c5b-b112-36a304b66dad")


@pytest.fixture()
def db_at_0245(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_auth_p2_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0245")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _schema(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        tables = {
            t
            for t in TABLES
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar_one()
        }
        function = (
            await conn.execute(text("SELECT to_regprocedure(:f)"), {"f": FUNCTION})
        ).scalar_one()
        column = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'clients' AND column_name = 'microsoft_tenants'"
                )
            )
        ).scalar_one()
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'users'::regclass "
                        "AND tgname = 'its_client_to_auth_external_identities'"
                    )
                )
            ).scalars()
        )
    return {
        "tables": tables,
        "function": None if function is None else str(function),
        "column": column,
        "triggers": triggers,
    }


async def test_0246_round_trips(db_at_0245: str) -> None:
    engine = create_async_engine(db_at_0245)
    present = {
        "tables": set(TABLES),
        "function": "its_auth_org(text)",
        "column": 1,
        "triggers": {"its_client_to_auth_external_identities"},
    }
    absent = {"tables": set(), "function": None, "column": 0, "triggers": set()}
    try:
        _alembic(db_at_0245, "upgrade", "0246")
        assert await _schema(engine) == present
        _alembic(db_at_0245, "downgrade", "0245")
        assert await _schema(engine) == absent
        _alembic(db_at_0245, "upgrade", "0246")
        assert await _schema(engine) == present
    finally:
        await engine.dispose()


async def _org(admin_engine: AsyncEngine) -> tuple[UUID, str]:
    slug = f"p2-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id, slug


async def _contact(admin_engine: AsyncEngine, org_id: UUID, name: str) -> tuple[UUID, UUID]:
    """(client id, contact user id)."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name=f"{name}-{uuid4().hex[:6]}", kind="customer")
        session.add(client)
        await session.flush()
        user = User(
            org_id=org_id,
            kind="contact",
            client_id=client.id,
            email=f"{uuid4().hex[:8]}@{name}.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        session.add(user)
        await session.commit()
        return client.id, user.id


async def test_its_auth_org_names_an_organization_and_nothing_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, slug = await _org(admin_engine)
    assert await org_of_slug(app_engine, slug) == org_id
    assert await org_of_slug(app_engine, f"{slug}-nope") is None
    async with admin_engine.connect() as conn:
        allowed = {
            role: (
                await conn.execute(
                    text("SELECT has_function_privilege(:role, :fn, 'EXECUTE')"),
                    {"role": role, "fn": FUNCTION},
                )
            ).scalar_one()
            for role in ("app_user", "portal_user")
        }
        definer = (
            await conn.execute(
                text("SELECT prosecdef FROM pg_proc WHERE oid = to_regprocedure(:fn)"),
                {"fn": FUNCTION},
            )
        ).scalar_one()
    assert allowed == {"app_user": True, "portal_user": False}
    assert definer is True


async def test_a_contacts_microsoft_link_is_inside_the_contacts_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _slug = await _org(admin_engine)
    acme, ana = await _contact(admin_engine, org_id, "acme")
    other, _bob = await _contact(admin_engine, org_id, "other")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.only([acme])) as s:
        link = AuthExternalIdentity(
            org_id=org_id, user_id=ana, provider=ENTRA, tenant_id=uuid4(), subject=uuid4()
        )
        s.add(link)
        await s.flush()
        await s.refresh(link)
        assert link.client_id == acme
        link_id = link.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.only([other])) as s:
        seen = await s.scalar(
            select(AuthExternalIdentity.id).where(AuthExternalIdentity.id == link_id)
        )
    assert seen is None


async def test_one_microsoft_account_is_one_person_and_one_link_per_person(
    admin_engine: AsyncEngine,
) -> None:
    org_id, _slug = await _org(admin_engine)
    _acme, ana = await _contact(admin_engine, org_id, "acme")
    _beta, bob = await _contact(admin_engine, org_id, "beta")
    tenant, subject = uuid4(), uuid4()
    async with AsyncSession(admin_engine) as session:
        session.add(
            AuthExternalIdentity(
                org_id=org_id, user_id=ana, provider=ENTRA, tenant_id=tenant, subject=subject
            )
        )
        await session.commit()
    for user_id, tid, oid in ((bob, tenant, subject), (ana, uuid4(), uuid4())):
        async with AsyncSession(admin_engine) as session:
            session.add(
                AuthExternalIdentity(
                    org_id=org_id, user_id=user_id, provider=ENTRA, tenant_id=tid, subject=oid
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


async def test_the_settings_cannot_be_switched_on_half_configured(
    admin_engine: AsyncEngine,
) -> None:
    org_id, _slug = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(AuthEntraSettings(org_id=org_id, enabled=True, app_client_id=uuid4()))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_certificate_is_all_there_or_not_at_all(admin_engine: AsyncEngine) -> None:
    org_id, _slug = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(
            AuthEntraSettings(org_id=org_id, certificate_pem="-----BEGIN CERTIFICATE-----")
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_next_certificate_is_whole_and_only_beside_a_current_one(
    admin_engine: AsyncEngine,
) -> None:
    """Decision 5: half a next slot, or a next slot with no current
    certificate, is refused."""
    org_id, _slug = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        key = Credential(org_id=org_id, name="entra-sign-in", type="entra_sign_in", vault_ref="")
        session.add(key)
        await session.commit()
    half = {"next_credential_id": key.id, "next_certificate_pem": "-----BEGIN CERTIFICATE-----"}
    alone = {
        "next_credential_id": key.id,
        "next_certificate_pem": "-----BEGIN CERTIFICATE-----",
        "next_certificate_thumbprint": "x",
        "next_certificate_expires_at": datetime.now(UTC),
    }
    for values in (half, alone):
        async with AsyncSession(admin_engine) as session:
            session.add(AuthEntraSettings(org_id=org_id, **values))
            with pytest.raises(IntegrityError):
                await session.commit()


async def test_the_personal_accounts_tenant_is_stored_nowhere(admin_engine: AsyncEngine) -> None:
    org_id, _slug = await _org(admin_engine)
    acme, _ana = await _contact(admin_engine, org_id, "acme")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Client).where(Client.id == acme).values(microsoft_tenants=[CONSUMERS])
            )
    async with AsyncSession(admin_engine) as session:
        session.add(AuthEntraSettings(org_id=org_id, staff_tenant_id=CONSUMERS))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_client_lists_at_most_twenty_tenants(admin_engine: AsyncEngine) -> None:
    org_id, _slug = await _org(admin_engine)
    acme, _ana = await _contact(admin_engine, org_id, "acme")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Client).where(Client.id == acme).values(microsoft_tenants=[uuid4()] * 20)
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Client).where(Client.id == acme).values(microsoft_tenants=[uuid4()] * 21)
            )

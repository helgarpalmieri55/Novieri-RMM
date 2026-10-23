"""Migration 0231 at work (MSP clients spec §2.2-2.4), through the real RLS.

Seeding is through the ADMIN engine (no RLS); every assertion about what a
session sees or may write is through `app_user`, where the restrictive
`client_isolation` policies apply. Task 8 sweeps every phase-1 table; this file
pins what 0231 adds beyond the catalog's literal copy:

- a record's versions take the RECORD's client (the second parent of
  `doc_versions`), so a list scope does not see them as "every client";
- a session with no scope GUC sees zero rows and cannot write (spec §2.2);
- `clients` itself shows a list scope its own clients and never `unassigned`;
- a client id of ANOTHER organization is refused by a foreign key, which RLS
  cannot be relied on for: a foreign key check ignores RLS;
- an inherited `client_id` cannot be rewritten by an UPDATE, and a job takes
  its target's client.

The fixture makes the client the ONLY discriminator: X and Y share the org,
each has one device and one record with one version (HANDOFF.md §4).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Client,
    Device,
    DeviceGroup,
    DeviceTag,
    DocArticle,
    DocRecord,
    DocVersion,
    Job,
    Organization,
    Role,
    Site,
    User,
    UserRole,
)


@dataclass(frozen=True)
class TwoClients:
    org: UUID
    x: UUID  # the internal client: id = org id (plan Global Constraints)
    y: UUID  # a customer of the same org
    unassigned: UUID
    device_x: UUID
    device_y: UUID
    version_x: UUID  # the version of a record owned by X
    version_y: UUID  # the version of a record owned by Y


async def _two_clients(admin_engine: AsyncEngine) -> TwoClients:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="Acme", slug=f"cs-{uuid4().hex[:8]}")
        s.add(org)
        await s.flush()
        y = Client(org_id=org.id, name="Cliente Y", kind="customer")
        s.add(y)
        await s.flush()
        site_y = Site(org_id=org.id, client_id=y.id, name="Sede Y")
        s.add(site_y)
        await s.flush()
        device_x = Device(
            org_id=org.id, client_id=org.id, site_id=org.id, hostname="hx", os_family="linux"
        )
        device_y = Device(
            org_id=org.id, client_id=y.id, site_id=site_y.id, hostname="hy", os_family="linux"
        )
        record_x = DocRecord(org_id=org.id, client_id=org.id, record_type="service", name="rx")
        record_y = DocRecord(org_id=org.id, client_id=y.id, record_type="service", name="ry")
        s.add_all([device_x, device_y, record_x, record_y])
        await s.flush()
        now = datetime.now(UTC)
        # No client_id: the version takes its record's (its_client_from_record).
        version_x = DocVersion(org_id=org.id, record_id=record_x.id, version=1, ts=now)
        version_y = DocVersion(org_id=org.id, record_id=record_y.id, version=1, ts=now)
        s.add_all([version_x, version_y])
        await s.flush()
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org.id, Client.kind == "unassigned")
            )
        ).scalar_one()
        await s.commit()
    return TwoClients(
        org=org.id,
        x=org.id,
        y=y.id,
        unassigned=unassigned,
        device_x=device_x.id,
        device_y=device_y.id,
        version_x=version_x.id,
        version_y=version_y.id,
    )


async def _visible(s: AsyncSession, c: TwoClients) -> tuple[set[UUID], set[UUID]]:
    devices = (
        await s.execute(
            text("SELECT id FROM devices WHERE id IN (:a, :b)"),
            {"a": c.device_x, "b": c.device_y},
        )
    ).scalars()
    versions = (
        await s.execute(
            text("SELECT id FROM doc_versions WHERE id IN (:a, :b)"),
            {"a": c.version_x, "b": c.version_y},
        )
    ).scalars()
    return set(devices), set(versions)


async def test_a_record_version_takes_its_records_client(admin_engine: AsyncEngine) -> None:
    """The second parent: without `its_client_from_record` these would be
    NULL, which an O table reads as "every client"."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine) as s:
        got = dict(
            (
                await s.execute(
                    text("SELECT id, client_id FROM doc_versions WHERE id IN (:a, :b)"),
                    {"a": c.version_x, "b": c.version_y},
                )
            ).all()
        )
    assert got == {c.version_x: c.x, c.version_y: c.y}


async def test_a_list_scope_does_not_see_another_clients_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        devices, versions = await _visible(s, c)
    assert devices == {c.device_x}
    assert versions == {c.version_x}


async def test_the_all_scope_sees_every_clients_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        devices, versions = await _visible(s, c)
    assert devices == {c.device_x, c.device_y}
    assert versions == {c.version_x, c.version_y}


async def _org_only(s: AsyncSession, org: UUID) -> None:
    """The tenant GUC and NOT the scope: the state a path that pinned only
    `app.current_org` would leave a session in."""
    await s.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})


async def test_no_scope_guc_sees_zero_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with AsyncSession(app_engine) as s, s.begin():
        await _org_only(s, c.org)
        devices, versions = await _visible(s, c)
        clients = set((await s.execute(text("SELECT id FROM clients"))).scalars())
    assert devices == set()
    assert versions == set()
    assert clients == set()


async def test_no_scope_guc_cannot_write(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine) as s:
        record_x = (
            await s.execute(text("SELECT record_id FROM doc_versions WHERE id = :v"),
                            {"v": c.version_x})
        ).scalar_one()
    with pytest.raises(DBAPIError, match="row-level security"):
        async with AsyncSession(app_engine) as s, s.begin():
            await _org_only(s, c.org)
            s.add(
                DocVersion(
                    org_id=c.org, record_id=record_x, client_id=c.x, version=2,
                    ts=datetime.now(UTC),
                )
            )
            await s.flush()


async def test_the_clients_table_shows_a_list_scope_its_own_clients_only(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        narrow = set((await s.execute(text("SELECT id FROM clients"))).scalars())
    # Even naming `unassigned` in the list does not show it (spec §1.1).
    async with org_session(
        c.org, clients=ClientScope.only([c.x, c.unassigned]), engine=app_engine
    ) as s:
        with_unassigned = set((await s.execute(text("SELECT id FROM clients"))).scalars())
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        wide = set((await s.execute(text("SELECT id FROM clients"))).scalars())
    assert narrow == {c.x}
    assert with_unassigned == {c.x}
    assert wide == {c.x, c.y, c.unassigned}


def _device_group(org: UUID, client: UUID) -> Any:
    return DeviceGroup(org_id=org, client_id=client, name=f"g-{uuid4().hex[:6]}")


def _doc_article(org: UUID, client: UUID) -> Any:
    return DocArticle(org_id=org, client_id=client, title="t")


def _contact(org: UUID, client: UUID) -> Any:
    return User(
        org_id=org, external_subject=uuid4().hex, email="c@test.local",
        kind="contact", client_id=client,
    )


@pytest.mark.parametrize(
    ("table", "make"),
    [
        ("device_groups", _device_group),  # C
        ("doc_articles", _doc_article),  # O
        ("users", _contact),  # O, its own policy
    ],
)
async def test_a_client_of_another_organization_is_refused(
    table: str, make: Any, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Under scope `all` the client policy passes any client id and the
    tenant policy only looks at `org_id`; a plain FK to `clients(id)` ignores
    RLS. Only `(client_id, org_id) REFERENCES clients (id, org_id)` stops org
    A's row from naming org B's client."""
    c = await _two_clients(admin_engine)
    other = await _two_clients(admin_engine)
    with pytest.raises(DBAPIError, match=f"fk_{table}_client_org"):
        async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
            s.add(make(c.org, other.y))
            await s.flush()


async def test_a_grant_on_a_client_of_another_organization_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """`user_roles` is the source of the scope. Through the admin engine, which
    bypasses RLS altogether: the foreign key is the only thing left."""
    c = await _two_clients(admin_engine)
    other = await _two_clients(admin_engine)
    with pytest.raises(DBAPIError, match="fk_user_roles_client_org"):
        async with AsyncSession(admin_engine) as s, s.begin():
            user = User(org_id=c.org, external_subject=uuid4().hex, email="u@test.local")
            s.add(user)
            await s.flush()
            role = (
                await s.execute(select(Role.id).where(Role.org_id.is_(None)).limit(1))
            ).scalar_one()
            s.add(UserRole(org_id=c.org, user_id=user.id, role_id=role, client_id=other.y))
            await s.flush()


async def test_an_inherited_client_cannot_be_rewritten(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §2.4: a client_id different from the parent's raises -- on an
    UPDATE of client_id alone as well as on the INSERT."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        tag = DeviceTag(org_id=c.org, device_id=c.device_x, key="k")
        s.add(tag)
        await s.commit()
    assert tag.client_id == c.x  # read back from the trigger, not sent
    with pytest.raises(DBAPIError, match="differs"):
        async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
            await s.execute(
                text("UPDATE device_tags SET client_id = :y WHERE id = :t"),
                {"y": c.y, "t": tag.id},
            )


async def test_a_narrowed_scope_writes_a_staff_row_but_not_another_clients_contact(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`users` has its own WITH CHECK (0231): `PATCH /me` and the login
    bookkeeping write a STAFF row (client_id NULL) under the person's own,
    possibly narrowed, scope -- which the generic O check would refuse. A
    CONTACT row still needs its client in the scope."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        staff = User(org_id=c.org, external_subject=uuid4().hex, email="s@test.local")
        contact_y = User(
            org_id=c.org, external_subject=uuid4().hex, email="y@test.local",
            kind="contact", client_id=c.y,
        )
        s.add_all([staff, contact_y])
        await s.commit()
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        touched = await s.execute(
            text("UPDATE users SET display_name = 'me' WHERE id IN (:s, :y)"),
            {"s": staff.id, "y": contact_y.id},
        )
    # The staff row only: Y's contact is not even visible to the UPDATE.
    assert touched.rowcount == 1  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
            s.add(_contact(c.org, c.y))
            await s.flush()


def _job(org: UUID, target: UUID, target_type: str = "device") -> Job:
    now = datetime.now(UTC)
    return Job(
        org_id=org, type="inventory.collect", target_type=target_type, target_id=target,
        params={}, nonce=uuid4().hex, issued_at=now, expires_at=now + timedelta(hours=1),
    )


async def test_a_job_takes_its_targets_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        job = _job(c.org, c.device_y)
        s.add(job)
        await s.flush()
        assert job.client_id == c.y
    # A list scope cannot reach a device outside it, so the job has no client
    # and NOT NULL refuses it (spec §4: never written into another client).
    with pytest.raises(DBAPIError, match="client_id"):
        async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
            s.add(_job(c.org, c.device_y))
            await s.flush()


async def test_a_job_with_an_unknown_target_type_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`device` and `group` are the only targets (the job engine writes
    `device`); anything else must not fall through to a devices lookup."""
    c = await _two_clients(admin_engine)
    with pytest.raises(DBAPIError, match="target_type"):
        async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
            s.add(_job(c.org, c.device_y, target_type="site"))
            await s.flush()

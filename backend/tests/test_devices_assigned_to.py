"""The fleet list's "Assigned to" column (people spec, operator decision 2026-09-23).

The operator folded People into Devices: the People page left the menu and each
device carries who holds it. Underneath, the holder is still the `person` the
`device_person` link points at -- set by hand (`operator`) or resolved from who
signs in at the machine (`inventory`) -- and `GET /devices` now serves it as
`assigned_to: {id, display_name} | null`, resolved in the same SQL page query.

What these tests pin, each with a fixture in which the guarded and unguarded
path produce DIFFERENT observable values:

* **Both sources resolve, and "nothing known" is null.** A device with no link
  row, and one whose link names nobody (an unresolved sign-in), both read null.
* **Tenancy on the BYPASSRLS engine.** A link row in MY org that points at a
  person in ANOTHER org (the FK is to `person.id` alone, so Postgres permits
  it) must not surface that person's name -- not as the holder, not through the
  search. Inside `org_session` RLS would hide the row and the test would pass
  with the join's own `org_id` predicate deleted; here it cannot.
* **The search reaches the holder's name** and the tab counts obey it, while
  the fleet tiles do not.
* **The name is only disclosed to a caller holding `people.read`.** The list is
  gated `devices.read`; a caller without `people.read` gets null and a search
  by a holder's name finds nothing, because a search that matched would disclose
  the same association the null withholds.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import (
    Device,
    DevicePerson,
    Organization,
    Permission,
    Person,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.services.device_list import DevicePage, device_page
from tests.conftest import ApiEnv

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


async def _org(admin_engine: AsyncEngine, prefix: str) -> UUID:
    slug = f"{prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _person(admin_engine: AsyncEngine, org_id: UUID, name: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # The org's internal client, whose id is the org's: every device here is too.
        person = Person(org_id=org_id, client_id=org_id, display_name=name, source="operator")
        session.add(person)
        await session.commit()
        return person.id


async def _reader(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    """A user holding `people.read` on every client of `org_id`: whom the
    holders are read FOR. `device_page` reads no holder without one."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=uuid4().hex, email="reader@test.local")
        role = Role(org_id=org_id, name=f"holders-{uuid4().hex[:6]}")
        session.add_all([user, role])
        await session.flush()
        permission = (
            await session.execute(select(Permission.id).where(Permission.code == "people.read"))
        ).scalar_one()
        session.add(RolePermission(role_id=role.id, permission_id=permission))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
        return user.id


async def _device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    hostname: str,
    *,
    person_id: UUID | None = None,
    source: str | None = None,
    observed_login: str | None = None,
) -> UUID:
    """A device, and -- when `source` is given -- its `device_person` row."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, client_id=org_id, site_id=org_id)
        session.add(device)
        await session.flush()
        if source is not None:
            session.add(
                DevicePerson(
                    org_id=org_id,
                    device_id=device.id,
                    person_id=person_id,
                    source=source,
                    observed_login=observed_login,
                )
            )
        await session.commit()
        return device.id


def _holders(page: DevicePage) -> dict[str | None, tuple[UUID, str] | None]:
    return {
        device.hostname: (holder.id, holder.display_name) if holder is not None else None
        for device, *_, holder in page.rows
    }


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


async def test_the_holder_resolves_from_either_source_and_nothing_known_is_null(
    admin_engine: AsyncEngine,
) -> None:
    org_id = await _org(admin_engine, "at-src")
    ana = await _person(admin_engine, org_id, "Ana Ruiz")
    ben = await _person(admin_engine, org_id, "Ben Okafor")
    await _device(admin_engine, org_id, "a-by-hand", person_id=ana, source="operator")
    await _device(
        admin_engine, org_id, "b-by-signin",
        person_id=ben, source="inventory", observed_login="CORP\\bokafor",
    )
    # Somebody signed in, and nobody here matched the name.
    await _device(
        admin_engine, org_id, "c-unresolved",
        source="inventory", observed_login="CORP\\stranger",
    )
    # No link row at all: a switch, or a laptop in its box.
    await _device(admin_engine, org_id, "d-no-link")
    reader = await _reader(admin_engine, org_id)

    async with AsyncSession(admin_engine) as session:
        page = await device_page(
            session, org_id, with_holder=True, holder_reader=reader, now=NOW
        )

    assert _holders(page) == {
        "a-by-hand": (ana, "Ana Ruiz"),
        "b-by-signin": (ben, "Ben Okafor"),
        "c-unresolved": None,
        "d-no-link": None,
    }
    # The join is one-to-one: no row is repeated and the counts are untouched.
    assert page.total == page.state_counts.all == page.fleet.total == 4


async def test_without_the_holder_the_page_is_what_it_was(admin_engine: AsyncEngine) -> None:
    """`with_holder=False` (the default) resolves nothing and searches nothing
    extra -- the shape a caller without `people.read` is served."""
    org_id = await _org(admin_engine, "at-off")
    ana = await _person(admin_engine, org_id, "Ana Ruiz")
    await _device(admin_engine, org_id, "a-by-hand", person_id=ana, source="operator")

    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, now=NOW)
        searched = await device_page(session, org_id, q="ruiz", now=NOW)

    assert _holders(page) == {"a-by-hand": None}
    assert searched.rows == [] and searched.total == 0


async def test_the_search_finds_devices_by_who_holds_them(admin_engine: AsyncEngine) -> None:
    """Searching a holder's name finds their machines; the strip obeys the
    search and the tiles do not. Two of Ana's machines and one of Ben's, so a
    search that ignored the holder (0 rows) or ignored the term (3 rows) both
    differ from the right answer (2)."""
    org_id = await _org(admin_engine, "at-q")
    ana = await _person(admin_engine, org_id, "Ana Ruiz")
    ben = await _person(admin_engine, org_id, "Ben Okafor")
    await _device(admin_engine, org_id, "fin-lt-01", person_id=ana, source="operator")
    await _device(admin_engine, org_id, "fin-lt-02", person_id=ana, source="inventory")
    await _device(admin_engine, org_id, "ops-lt-07", person_id=ben, source="operator")
    reader = await _reader(admin_engine, org_id)

    async with AsyncSession(admin_engine) as session:
        page = await device_page(
            session, org_id, q="RUIZ", with_holder=True, holder_reader=reader, now=NOW
        )
        by_host = await device_page(
            session, org_id, q="ops-", with_holder=True, holder_reader=reader, now=NOW
        )

    assert [d.hostname for d, *_ in page.rows] == ["fin-lt-01", "fin-lt-02"]
    assert page.total == page.state_counts.all == 2
    assert page.fleet.total == 3
    # The existing columns still search as before.
    assert [d.hostname for d, *_ in by_host.rows] == ["ops-lt-07"]


async def test_another_orgs_person_never_appears(admin_engine: AsyncEngine) -> None:
    """**A MISSING TENANT PREDICATE FAILS HERE** (the person half of the join).

    The link row is in MY org and names THEIR person. `device_person.person_id`
    references `person.id` alone, so Postgres accepted it, and on this BYPASSRLS
    engine nothing but the join's `Person.org_id == org_id` stands between
    their person's name and my screen. Deleting that predicate reports
    `('Eve Stone')` as my device's holder and makes `q="stone"` find it.

    Since migration 0235 the database refuses that row as well: a link names a
    person of the DEVICE'S CLIENT (`fk_device_person_person_client`), and a
    client is one organisation's. The join's predicate is still the
    application's own belt, so the key is lifted for the rest of the test
    inside a transaction that is rolled back -- the way
    `test_client_catalog.py` lifts a key to show what it guards.
    """
    mine = await _org(admin_engine, "at-mine")
    theirs = await _org(admin_engine, "at-theirs")
    eve = await _person(admin_engine, theirs, "Eve Stone")
    # Their own, correctly linked device: never on my page at all.
    await _device(admin_engine, theirs, "theirs-1", person_id=eve, source="operator")
    with pytest.raises(IntegrityError, match="fk_device_person_person_client"):
        await _device(admin_engine, mine, "ours-1", person_id=eve, source="operator")
    reader = await _reader(admin_engine, mine)

    async with admin_engine.connect() as conn:
        await conn.execute(
            text("ALTER TABLE device_person DROP CONSTRAINT fk_device_person_person_client")
        )
        session = AsyncSession(bind=conn)
        device = Device(org_id=mine, hostname="ours-1", client_id=mine, site_id=mine)
        session.add(device)
        await session.flush()
        session.add(
            DevicePerson(org_id=mine, device_id=device.id, person_id=eve, source="operator")
        )
        await session.flush()
        page = await device_page(
            session, mine, with_holder=True, holder_reader=reader, now=NOW
        )
        searched = await device_page(
            session, mine, q="stone", with_holder=True, holder_reader=reader, now=NOW
        )
        await conn.rollback()

    assert _holders(page) == {"ours-1": None}, "another org's person leaked as a holder"
    assert searched.rows == [] and searched.total == 0, "the search reached another org"
    assert page.fleet.total == 1


async def test_a_link_row_from_another_org_is_not_joined(admin_engine: AsyncEngine) -> None:
    """The other half of the join: a `device_person` row carrying ANOTHER org's
    `org_id` against my device is not my org's statement about who holds it,
    even when the person it names is mine. Only `DevicePerson.org_id == org_id`
    keeps it out -- the person join is satisfied -- so deleting that predicate
    reports `Ana Ruiz` here, which this test was run against to confirm."""
    mine = await _org(admin_engine, "at-mine2")
    theirs = await _org(admin_engine, "at-theirs2")
    ana = await _person(admin_engine, mine, "Ana Ruiz")
    device_id = await _device(admin_engine, mine, "ours-2")
    async with AsyncSession(admin_engine) as session:
        session.add(
            DevicePerson(org_id=theirs, device_id=device_id, person_id=ana, source="operator")
        )
        await session.commit()
    reader = await _reader(admin_engine, mine)

    async with AsyncSession(admin_engine) as session:
        page = await device_page(
            session, mine, with_holder=True, holder_reader=reader, now=NOW
        )
    assert _holders(page) == {"ours-2": None}


async def test_no_holder_is_read_for_nobody(admin_engine: AsyncEngine) -> None:
    """FAILS CLOSED (Task 5 review). Whether a holder may be shown depends on
    who reads it -- `people.read` in the holder's client -- so a call that
    asks for holders and names no reader is refused, never answered with every
    client's holders."""
    org_id = await _org(admin_engine, "at-noreader")
    ana = await _person(admin_engine, org_id, "Ana Ruiz")
    await _device(admin_engine, org_id, "a-by-hand", person_id=ana, source="operator")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(ValueError, match="holder_reader"):
            await device_page(session, org_id, with_holder=True, now=NOW)
        with pytest.raises(ValueError, match="holder_reader"):
            await device_page(session, org_id, q="ruiz", with_holder=True, now=NOW)


# --------------------------------------------------------------------------- #
# The route, and who may see a name
# --------------------------------------------------------------------------- #


async def _only(api_env: ApiEnv, org_id: UUID, *codes: str) -> dict[str, str]:
    """A user holding exactly `codes`, through a role of this org's own. No
    seeded role holds `devices.read` without `people.read`."""
    subject = await api_env.create_user(org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org_id, name=f"only-{uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        for code in codes:
            perm = (await s.execute(select(Permission).where(Permission.code == code))).scalar_one()
            s.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = (
            await s.execute(select(User).where(User.external_subject == subject))
        ).scalar_one()
        s.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await s.commit()
    return await api_env.headers(org_id, subject)


async def test_the_route_serves_the_holder_only_to_people_readers(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("at-route")
    ana = await _person(api_env.admin_engine, org_id, "Ana Ruiz")
    await _device(api_env.admin_engine, org_id, "fin-lt-01", person_id=ana, source="operator")
    await _device(api_env.admin_engine, org_id, "spare-01")

    reader = await _only(api_env, org_id, "devices.read", "people.read")
    body = (await api_env.client.get("/api/v1/devices", headers=reader)).json()
    assert {item["hostname"]: item["assigned_to"] for item in body["items"]} == {
        "fin-lt-01": {"id": str(ana), "display_name": "Ana Ruiz"},
        "spare-01": None,
    }
    found = (await api_env.client.get("/api/v1/devices?q=ruiz", headers=reader)).json()
    assert [item["hostname"] for item in found["items"]] == ["fin-lt-01"]

    # `devices.read` alone: the list, and no names.
    devices_only = await _only(api_env, org_id, "devices.read")
    response = await api_env.client.get("/api/v1/devices", headers=devices_only)
    assert response.status_code == 200, response.text
    assert [item["assigned_to"] for item in response.json()["items"]] == [None, None]
    # And the search cannot be used to ask the question the null declined.
    hidden = (await api_env.client.get("/api/v1/devices?q=ruiz", headers=devices_only)).json()
    assert hidden["items"] == [] and hidden["total"] == 0

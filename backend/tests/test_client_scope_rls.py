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
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.events import publish
from app.core.tenancy import org_session
from app.models import (
    Client,
    Credential,
    Device,
    DeviceGroup,
    DeviceTag,
    DocArticle,
    DocRecord,
    DocVersion,
    EnrollmentToken,
    HealthAlert,
    HealthAlertAction,
    InstallLink,
    Job,
    JobLog,
    Organization,
    Permission,
    Role,
    RolePermission,
    Site,
    User,
    UserRole,
)
from tests.health_fixtures import seed_check, seed_profile


@dataclass(frozen=True)
class TwoClients:
    org: UUID
    x: UUID  # the internal client: id = org id (plan Global Constraints)
    y: UUID  # a customer of the same org
    site_y: UUID  # Y's one site, holding device_y
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
        site_y=site_y.id,
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


async def test_no_scope_guc_hides_global_rows_and_staff_too(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Strict fail-closed (spec §2.2): a NULL client means "every client", and
    a session with no scope is not a client at all -- so it sees no NULL row
    either, and cannot touch a staff row. The positive control is a list
    scope, which must still read both."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        article = DocArticle(org_id=c.org, title="global runbook")  # client_id NULL
        staff = User(org_id=c.org, external_subject=uuid4().hex, email="s@test.local")
        s.add_all([article, staff])
        await s.commit()
    probe = (
        "SELECT (SELECT count(*) FROM doc_articles WHERE id = :a) "
        "+ (SELECT count(*) FROM users WHERE id = :u)"
    )
    ids = {"a": article.id, "u": staff.id}
    async with AsyncSession(app_engine) as s, s.begin():
        await _org_only(s, c.org)
        unscoped = (await s.execute(text(probe), ids)).scalar_one()
        touched = await s.execute(
            text("UPDATE users SET display_name = 'x' WHERE id = :u"), {"u": staff.id}
        )
    assert unscoped == 0
    assert touched.rowcount == 0  # type: ignore[attr-defined]
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        assert (await s.execute(text(probe), ids)).scalar_one() == 2


async def test_a_narrowed_scope_publishes_a_domain_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`events.publish` cannot name a client yet (phase 2 derives it from the
    subject), so in phase 1 an event is written with NULL client from ANY
    pinned scope -- or every route that publishes fails under a narrowed
    one."""
    c = await _two_clients(admin_engine)
    subject = uuid4()
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        await publish(
            s, org_id=c.org, event_type="ticket.assigned", subject_type="ticket",
            subject_id=subject, payload={}, actor_user_id=None,
        )
        await s.flush()
    async with AsyncSession(admin_engine) as s:
        stored = (
            await s.execute(
                text("SELECT client_id FROM domain_events WHERE subject_id = :s"),
                {"s": subject},
            )
        ).scalars().all()
    assert stored == [None]


async def test_a_site_with_a_device_cannot_change_client(admin_engine: AsyncEngine) -> None:
    """`fk_devices_site_client`: the device names (site, client), so the site
    cannot leave the client while it holds the device. Through the admin
    engine: the foreign key, not RLS, is what refuses."""
    c = await _two_clients(admin_engine)
    with pytest.raises(DBAPIError, match="fk_devices_site_client"):
        async with AsyncSession(admin_engine) as s, s.begin():
            await s.execute(
                text("UPDATE sites SET client_id = :x WHERE id = :s"),
                {"x": c.x, "s": c.site_y},
            )


def _token(org: UUID, client: UUID, site: UUID) -> Any:
    return EnrollmentToken(
        org_id=org, client_id=client, site_id=site, token_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _link(org: UUID, client: UUID, site: UUID) -> Any:
    return InstallLink(org_id=org, client_id=client, site_id=site)


@pytest.mark.parametrize(
    ("table", "make"), [("enrollment_tokens", _token), ("install_links", _link)]
)
async def test_a_site_of_another_client_is_refused(
    table: str, make: Any, admin_engine: AsyncEngine
) -> None:
    """Spec §1.5: an enrollment token or install link names a site of its own
    client -- otherwise it would enroll a device into one client's site under
    another client."""
    c = await _two_clients(admin_engine)
    with pytest.raises(DBAPIError, match=f"fk_{table}_site_client"):
        async with AsyncSession(admin_engine) as s, s.begin():
            s.add(make(c.org, c.x, c.site_y))
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


# --------------------------------------------------------------------------- #
# Review round 1 (Task 6): global rows, propagation, identity, empty scopes.
# --------------------------------------------------------------------------- #


async def _ids(s: AsyncSession, sql: str, **params: Any) -> set[UUID]:
    return set((await s.execute(text(sql), params)).scalars())


async def _rowcount(s: AsyncSession, sql: str, **params: Any) -> int:
    result = await s.execute(text(sql), params)
    return int(result.rowcount)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class OptionalRows:
    article_global: UUID
    article_x: UUID
    credential_global: UUID
    credential_x: UUID


async def _optional_rows(admin_engine: AsyncEngine, c: TwoClients) -> OptionalRows:
    """A global row and one of X's in two O tables: an article (no parent) and
    a credential (parent device_groups, unset)."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        rows = [
            DocArticle(org_id=c.org, title="global"),
            DocArticle(org_id=c.org, client_id=c.x, title="x"),
            Credential(org_id=c.org, name="global", type="ssh_key", vault_ref="v"),
            Credential(org_id=c.org, client_id=c.x, name="x", type="ssh_key", vault_ref="v"),
        ]
        s.add_all(rows)
        await s.commit()
    return OptionalRows(*(r.id for r in rows))


async def test_a_narrowed_scope_cannot_change_or_delete_global_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §2.2 and §5 test 3: a limited technician reads global
    configuration but cannot change it -- not by UPDATE, not by claiming it
    for their client, and not by DELETE, which checks no WITH CHECK at all."""
    c = await _two_clients(admin_engine)
    r = await _optional_rows(admin_engine, c)
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        global_deleted = [
            await _rowcount(s, "DELETE FROM doc_articles WHERE id = :i", i=r.article_global),
            await _rowcount(s, "DELETE FROM credentials WHERE id = :i", i=r.credential_global),
        ]
        claimed = await _rowcount(
            s, "UPDATE doc_articles SET client_id = :x WHERE id = :i", x=c.x, i=r.article_global
        )
        edited = await _rowcount(
            s, "UPDATE credentials SET name = 'n' WHERE id = :i", i=r.credential_global
        )
        # X's own rows are X's to change.
        own = [
            await _rowcount(s, "UPDATE doc_articles SET title = 't' WHERE id = :i", i=r.article_x),
            await _rowcount(s, "DELETE FROM doc_articles WHERE id = :i", i=r.article_x),
            await _rowcount(s, "DELETE FROM credentials WHERE id = :i", i=r.credential_x),
        ]
    assert global_deleted == [0, 0]
    assert claimed == 0
    assert edited == 0
    assert own == [1, 1, 1]
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        assert await _rowcount(
            s, "UPDATE doc_articles SET client_id = :x WHERE id = :i", x=c.x, i=r.article_global
        ) == 1
        assert await _rowcount(
            s, "DELETE FROM credentials WHERE id = :i", i=r.credential_global
        ) == 1


async def test_a_narrowed_scope_cannot_change_or_delete_a_global_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`domain_events` takes a NULL row from any pinned scope (phase 1), but
    changing or deleting one is a change to every client's outbox."""
    c = await _two_clients(admin_engine)
    subject = uuid4()
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        await publish(
            s, org_id=c.org, event_type="ticket.assigned", subject_type="ticket",
            subject_id=subject, payload={}, actor_user_id=None,
        )
    by_subject = "WHERE subject_id = :subj"
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        assert len(await _ids(s, f"SELECT id FROM domain_events {by_subject}", subj=subject)) == 1
        updated = await _rowcount(
            s, f"UPDATE domain_events SET processed_at = now() {by_subject}", subj=subject
        )
        deleted = await _rowcount(s, f"DELETE FROM domain_events {by_subject}", subj=subject)
    assert (updated, deleted) == (0, 0)
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        assert await _rowcount(
            s, f"UPDATE domain_events SET processed_at = now() {by_subject}", subj=subject
        ) == 1
        assert await _rowcount(s, f"DELETE FROM domain_events {by_subject}", subj=subject) == 1


async def test_a_narrowed_scope_cannot_rewrite_who_a_user_is(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`users` lets a narrowed scope write a staff row -- its own person's
    profile and login bookkeeping. It does not let it change what the row IS:
    `kind` and `external_subject` (and with `kind`, via `ck_users_kind_client`,
    the client) change only under scope all. Nor may it delete a staff row."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        staff = User(org_id=c.org, external_subject=uuid4().hex, email="s@test.local")
        contact = User(
            org_id=c.org, external_subject=uuid4().hex, email="c@test.local",
            kind="contact", client_id=c.x,
        )
        s.add_all([staff, contact])
        await s.commit()
    probes = [
        # The reviewer's probe: an admin's staff row turned into X's contact,
        # its login subject replaced, in one statement.
        (
            "UPDATE users SET kind = 'contact', client_id = :x, external_subject = 'taken' "
            "WHERE id = :u",
            {"x": c.x, "u": staff.id},
        ),
        ("UPDATE users SET kind = 'staff', client_id = NULL WHERE id = :u", {"u": contact.id}),
        ("UPDATE users SET external_subject = 'taken' WHERE id = :u", {"u": staff.id}),
    ]
    for sql, params in probes:
        with pytest.raises(DBAPIError, match="only under the every-client scope"):
            async with org_session(
                c.org, clients=ClientScope.only([c.x]), engine=app_engine
            ) as s:
                await s.execute(text(sql), params)
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        profile = await _rowcount(
            s,
            "UPDATE users SET display_name = 'Me', phone = '+57', timezone = 'America/Bogota', "
            "locale = 'es', last_login_at = now() WHERE id = :u",
            u=staff.id,
        )
        deleted = await _rowcount(s, "DELETE FROM users WHERE id = :u", u=staff.id)
    assert (profile, deleted) == (1, 0)
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        assert await _rowcount(
            s, "UPDATE users SET external_subject = :n WHERE id = :u", n=uuid4().hex, u=staff.id
        ) == 1


async def test_an_empty_scope_sees_no_global_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §2.2: an `X-Client-Id` the person may not use narrows to the EMPTY
    list, which sees zero rows -- global ones and staff included."""
    c = await _two_clients(admin_engine)
    r = await _optional_rows(admin_engine, c)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        staff = User(org_id=c.org, external_subject=uuid4().hex, email="s@test.local")
        s.add(staff)
        await s.commit()
    async with org_session(c.org, clients=ClientScope.only([]), engine=app_engine) as s:
        seen = await _ids(
            s,
            "SELECT id FROM doc_articles WHERE id = :a UNION ALL "
            "SELECT id FROM users WHERE id = :u",
            a=r.article_global, u=staff.id,
        )
    assert seen == set()


async def test_a_device_cannot_join_another_clients_group(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        group_y = DeviceGroup(org_id=c.org, client_id=c.y, name="gy")
        s.add(group_y)
        await s.commit()
    with pytest.raises(DBAPIError, match="device group belongs to another client"):
        async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
            await s.execute(
                text("UPDATE devices SET device_group_id = :g WHERE id = :d"),
                {"g": group_y.id, "d": c.device_x},
            )


async def test_a_group_cannot_hang_from_another_clients_group(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        group_y = DeviceGroup(org_id=c.org, client_id=c.y, name="gy")
        s.add(group_y)
        await s.commit()
    with pytest.raises(DBAPIError, match="parent group belongs to another client"):
        async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
            s.add(
                DeviceGroup(org_id=c.org, client_id=c.x, name="gx", parent_group_id=group_y.id)
            )
            await s.flush()


async def _client_of(admin_engine: AsyncEngine, table: str, row: UUID) -> UUID | None:
    async with AsyncSession(admin_engine) as s:
        got: UUID | None = (
            await s.execute(text(f"SELECT client_id FROM {table} WHERE id = :i"), {"i": row})
        ).scalar_one()
        return got


async def test_an_articles_versions_follow_it_to_another_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The reviewer's probe: a global article's version stayed global -- and
    readable by every scope -- after the article became Y's."""
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        article = DocArticle(org_id=c.org, title="runbook")
        s.add(article)
        await s.flush()
        version = DocVersion(
            org_id=c.org, article_id=article.id, version=1,
            snapshot={"body": "secret-of-Y"}, ts=datetime.now(UTC),
        )
        s.add(version)
        await s.commit()
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        await s.execute(
            text("UPDATE doc_articles SET client_id = :y WHERE id = :a"),
            {"y": c.y, "a": article.id},
        )
    assert await _client_of(admin_engine, "doc_versions", version.id) == c.y
    async with org_session(c.org, clients=ClientScope.only([c.x]), engine=app_engine) as s:
        assert await _ids(s, "SELECT id FROM doc_versions WHERE id = :v", v=version.id) == set()


async def test_a_retargeted_jobs_logs_follow_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    c = await _two_clients(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        job = _job(c.org, c.device_x)
        s.add(job)
        await s.flush()
        log = JobLog(org_id=c.org, job_id=job.id, ts=datetime.now(UTC), message="m")
        s.add(log)
        await s.commit()
    assert log.client_id == c.x
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        await s.execute(
            text("UPDATE jobs SET target_id = :d WHERE id = :j"), {"d": c.device_y, "j": job.id}
        )
    assert await _client_of(admin_engine, "jobs", job.id) == c.y
    assert await _client_of(admin_engine, "job_logs", log.id) == c.y


async def test_an_alerts_actions_follow_its_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Two levels: the alert takes the device's client in its BEFORE trigger,
    and its action follows from the alert's AFTER trigger -- the action's own
    BEFORE trigger then sees a consistent parent and does not raise."""
    c = await _two_clients(admin_engine)
    profile = await seed_profile(admin_engine, c.org)
    check = await seed_check(admin_engine, c.org, profile)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        alert = HealthAlert(
            org_id=c.org, device_id=c.device_x, check_id=check, profile_id=profile,
            check_type="cpu.busy_pct", severity="warning",
        )
        s.add(alert)
        await s.flush()
        action = HealthAlertAction(org_id=c.org, alert_id=alert.id, kind="notify")
        s.add(action)
        await s.commit()
    assert action.client_id == c.x
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        await s.execute(
            text("UPDATE health_alert SET device_id = :d WHERE id = :a"),
            {"d": c.device_y, "a": alert.id},
        )
    assert await _client_of(admin_engine, "health_alert", alert.id) == c.y
    assert await _client_of(admin_engine, "health_alert_action", action.id) == c.y


async def test_an_inherited_client_is_not_left_stale_in_memory(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """After an ORM UPDATE of the fk the trigger has changed `client_id`;
    the ORM must not keep answering the old one. It expires the attribute
    (`server_onupdate`), and a refresh reads the new client."""
    c = await _two_clients(admin_engine)
    async with org_session(c.org, clients=ClientScope.all(), engine=app_engine) as s:
        tag = DeviceTag(org_id=c.org, device_id=c.device_x, key="k")
        s.add(tag)
        await s.flush()
        assert tag.client_id == c.x
        tag.device_id = c.device_y
        await s.flush()
        assert "client_id" in sa_inspect(tag).unloaded
        await s.refresh(tag, ["client_id"])
        assert tag.client_id == c.y


async def test_the_credential_delete_route_cannot_delete_a_global_credential(
    api_env: Any,
) -> None:
    """The reviewer's ⚠️, through the API: a technician holding
    `credentials.manage` on client X only. `DELETE /credentials/{id}` loads
    the row in the person's scope (a global credential is readable) and
    deletes it -- the database refuses the global one and allows X's."""
    env = api_env
    org = await env.create_org("cred")
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org, name=f"everything-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (await s.execute(select(Permission.id))).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=org))
        rows = [
            Credential(org_id=org, name="global", type="ssh_key", vault_ref="v"),
            Credential(org_id=org, client_id=org, name="x", type="ssh_key", vault_ref="v"),
        ]
        s.add_all(rows)
        await s.commit()
    headers = await env.headers(org, sub)
    for row in rows:
        await env.client.delete(f"/api/v1/credentials/{row.id}", headers=headers)
    async with AsyncSession(env.admin_engine) as s:
        left = set(
            (
                await s.execute(
                    select(Credential.id).where(Credential.id.in_([r.id for r in rows]))
                )
            ).scalars()
        )
    assert left == {rows[0].id}

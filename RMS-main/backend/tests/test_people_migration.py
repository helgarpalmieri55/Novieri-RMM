"""Migration 0115 (people): RLS, permissions, and the §1.1 backfill.

The backfill test runs on its own throwaway database: it migrates to the
revision before people, seeds free-text rows, then upgrades and checks what
was linked and what was left alone.
"""

import os
import re
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import BACKEND_DIR, _psql

PEOPLE_TABLES = {
    "person",
    "person_login",
    "device_person",
    "person_checklist",
    "person_checklist_step",
}
_MIGRATION = BACKEND_DIR / "migrations" / "versions" / "0115_people.py"


async def test_people_tables_have_forced_rls_and_a_tenant_policy(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_engine.connect() as conn:
        flags = {
            r.relname: (r.relrowsecurity, r.relforcerowsecurity)
            for r in await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname='public' AND c.relkind='r'"
                )
            )
        }
        policies = {
            (r.tablename, r.policyname)
            for r in await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'")
            )
        }
    for table in PEOPLE_TABLES:
        assert flags.get(table) == (True, True), table
        assert (table, "tenant_isolation") in policies, table


async def test_people_role_grants(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name AS role_name, p.code AS code FROM role_permissions rp"
                " JOIN roles r ON r.id = rp.role_id"
                " JOIN permissions p ON p.id = rp.permission_id"
                " WHERE p.module = 'people' AND r.org_id IS NULL"
            )
        )
        grants: dict[str, set[str]] = {}
        for r in rows:
            grants.setdefault(r.role_name, set()).add(r.code)
    every = {"people.read", "people.manage", "people.offboard"}
    assert grants["super-admin"] == every
    assert grants["org-admin"] == every
    assert grants["technician"] == {"people.read", "people.manage"}
    assert grants["auditor"] == {"people.read"}
    assert grants["read-only"] == {"people.read"}
    assert "requester" not in grants


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_DIR,
        env=os.environ | {"ITS_ALEMBIC_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )


def _previous_revision() -> str:
    match = re.search(r'down_revision: str \| None = "([^"]+)"', _MIGRATION.read_text())
    assert match is not None
    return match.group(1)


@pytest.fixture()
def fresh_db_url(pg_cluster) -> object:  # type: ignore[no-untyped-def]
    dbname = f"its_people_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    admin_url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    yield admin_url
    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def test_backfill_links_on_email_only_and_leaves_the_rest(fresh_db_url: str) -> None:
    _alembic(fresh_db_url, "upgrade", _previous_revision())

    org, other_org = uuid4(), uuid4()
    maria, maria_dup, bob, blank = uuid4(), uuid4(), uuid4(), uuid4()
    sub, lic, prod = uuid4(), uuid4(), uuid4()
    seat_match, seat_miss, alloc_user, alloc_dev = uuid4(), uuid4(), uuid4(), uuid4()
    dev_owned, dev_other = uuid4(), uuid4()

    engine = create_async_engine(fresh_db_url)
    try:
        async with engine.begin() as conn:
            for org_id, slug in ((org, "a"), (other_org, "b")):
                await conn.execute(
                    text("INSERT INTO organizations (id, name, slug, status, settings)"
                         " VALUES (:id, :s, :s, 'active', '{}')"),
                    {"id": org_id, "s": f"people-bf-{slug}-{org_id.hex[:6]}"},
                )
            users = [
                (maria, org, "sub-maria", "Maria.Garcia@corp.example", "Maria Garcia", "active"),
                # The same address, differently cased, on a later account.
                (maria_dup, org, "sub-maria-2", "maria.garcia@corp.example", "", "active"),
                (bob, org, "sub-bob", "bob@corp.example", "", "deleted"),
                (blank, org, "sub-blank", "", "Service", "active"),
            ]
            for uid, org_id, subj, email, name, status in users:
                await conn.execute(
                    text("INSERT INTO users (id, org_id, external_subject, email, display_name,"
                         " status, created_at, updated_at) VALUES (:id, :o, :s, :e, :n, :st,"
                         " now() + make_interval(secs => :ord), now())"),
                    {"id": uid, "o": org_id, "s": subj, "e": email, "n": name, "st": status,
                     "ord": users.index((uid, org_id, subj, email, name, status))},
                )
            await conn.execute(
                text("INSERT INTO subscription (id, org_id, service_name, billing_cycle,"
                     " cost_per_cycle, currency, seats, created_by, auto_renew, notice_days,"
                     " status, plan, category) VALUES (:id, :o, 'Slack', 'monthly', 1, 'USD', 5,"
                     " :u, false, 0, 'active', '', '')"),
                {"id": sub, "o": org, "u": maria},
            )
            for seat_id, email in ((seat_match, " MARIA.garcia@corp.example"),
                                   (seat_miss, "stranger@elsewhere.example")):
                await conn.execute(
                    text("INSERT INTO seat_assignment (id, org_id, subscription_id,"
                         " assignee_name, assignee_email, created_by)"
                         " VALUES (:id, :o, :s, 'x', :e, :u)"),
                    {"id": seat_id, "o": org, "s": sub, "e": email, "u": maria},
                )
            await conn.execute(
                text("INSERT INTO software_product (id, org_id, name, publisher, match_rules,"
                     " created_by) VALUES (:id, :o, 'Office', '', '[]', :u)"),
                {"id": prod, "o": org, "u": maria},
            )
            await conn.execute(
                text("INSERT INTO license (id, org_id, product_id, type, entitlements, notes,"
                     " created_by) VALUES (:id, :o, :p, 'perpetual', 5, '', :u)"),
                {"id": lic, "o": org, "p": prod, "u": maria},
            )
            for dev_id, owner in ((dev_owned, "maria.GARCIA@corp.example"),
                                  (dev_other, "Maria from accounts")):
                await conn.execute(
                    text("INSERT INTO devices (id, org_id, hostname, os_version, architecture,"
                         " cpu, owner, status) VALUES (:id, :o, :h, '', '', '', :ow, 'active')"),
                    {"id": dev_id, "o": org, "h": f"pc-{dev_id.hex[:6]}", "ow": owner},
                )
            for alloc_id, ttype, target in ((alloc_user, "user", maria_dup),
                                            (alloc_dev, "device", dev_owned)):
                await conn.execute(
                    text("INSERT INTO license_allocation (id, org_id, license_id, target_type,"
                         " target_id, created_by) VALUES (:id, :o, :l, :t, :tid, :u)"),
                    {"id": alloc_id, "o": org, "l": lic, "t": ttype, "tid": target, "u": maria},
                )

        _alembic(fresh_db_url, "upgrade", "0115")

        async with engine.connect() as conn:
            people = (
                await conn.execute(
                    text("SELECT id, email, user_id, status, source, display_name FROM person"
                         " WHERE org_id = :o"),
                    {"o": org},
                )
            ).all()
            by_user = {p.user_id: p for p in people}
            # One person for the two Maria accounts, linked to the older one.
            assert maria in by_user and maria_dup not in by_user
            assert by_user[maria].display_name == "Maria Garcia"
            assert by_user[maria].source == "account"
            assert by_user[bob].status == "left"
            assert by_user[blank].email is None
            assert len(people) == 3
            maria_person = by_user[maria].id

            seats = dict(
                (await conn.execute(text("SELECT id, person_id FROM seat_assignment"))).all()
            )
            assert seats[seat_match] == maria_person
            assert seats[seat_miss] is None

            allocs = dict(
                (await conn.execute(text("SELECT id, person_id FROM license_allocation"))).all()
            )
            # The duplicate account shares Maria's address, so its allocation is hers.
            assert allocs[alloc_user] == maria_person
            assert allocs[alloc_dev] is None

            links = (
                await conn.execute(text("SELECT device_id, person_id, source FROM device_person"))
            ).all()
            assert [(r.device_id, r.person_id, r.source) for r in links] == [
                (dev_owned, maria_person, "operator")
            ]
            # Free text is never rewritten.
            owners = dict((await conn.execute(text("SELECT id, owner FROM devices"))).all())
            assert owners[dev_other] == "Maria from accounts"

        _alembic(fresh_db_url, "downgrade", _previous_revision())
        _alembic(fresh_db_url, "upgrade", "0115")
    finally:
        await engine.dispose()


async def test_backfill_carries_allocations_to_the_linked_account(fresh_db_url: str) -> None:
    _alembic(fresh_db_url, "upgrade", _previous_revision())
    org, user, prod, lic, alloc = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    engine = create_async_engine(fresh_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organizations (id, name, slug, status, settings)"
                     " VALUES (:id, 'x', :s, 'active', '{}')"),
                {"id": org, "s": f"people-bf2-{org.hex[:6]}"},
            )
            await conn.execute(
                text("INSERT INTO users (id, org_id, external_subject, email, display_name,"
                     " status) VALUES (:id, :o, 's', 'ann@corp.example', 'Ann', 'active')"),
                {"id": user, "o": org},
            )
            await conn.execute(
                text("INSERT INTO software_product (id, org_id, name, publisher, match_rules,"
                     " created_by) VALUES (:id, :o, 'Office', '', '[]', :u)"),
                {"id": prod, "o": org, "u": user},
            )
            await conn.execute(
                text("INSERT INTO license (id, org_id, product_id, type, entitlements, notes,"
                     " created_by) VALUES (:id, :o, :p, 'perpetual', 5, '', :u)"),
                {"id": lic, "o": org, "p": prod, "u": user},
            )
            await conn.execute(
                text("INSERT INTO license_allocation (id, org_id, license_id, target_type,"
                     " target_id, created_by) VALUES (:id, :o, :l, 'user', :u, :u)"),
                {"id": alloc, "o": org, "l": lic, "u": user},
            )
        _alembic(fresh_db_url, "upgrade", "0115")
        async with engine.connect() as conn:
            linked = (
                await conn.execute(
                    text("SELECT p.user_id FROM license_allocation a JOIN person p"
                         " ON p.id = a.person_id WHERE a.id = :a"),
                    {"a": alloc},
                )
            ).scalar_one()
        assert linked == user
    finally:
        await engine.dispose()


def test_migration_file_is_where_the_test_expects_it() -> None:
    assert Path(_MIGRATION).exists()

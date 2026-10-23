"""Migration 0200: `push_run` and `push_run_target` (subnet push design §1.3).

Tenancy is asserted on both tables. So is the one property that makes a run's
list the authority: once a run is sealed, the database refuses a new target and
refuses any change to which machine an existing target names. That holds
whatever code runs above it.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from tests.agent_push_fixtures import PushOrg, seed_asset, seed_credential, seed_org

TABLES = ("push_run", "push_run_target")
DIGEST = "a" * 64


async def _insert_run(
    admin_engine: AsyncEngine,
    org: PushOrg,
    *,
    state: str = "running",
    sealed: bool = True,
    credential_id: UUID | None = None,
    device_id: UUID | None = None,
) -> UUID:
    run_id = uuid4()
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_run (id, org_id, created_by, credential_id, prefix,"
                " list_digest, state, target_count, created_at, sealed_at, updated_at,"
                " auth_window_started_at)"
                " VALUES (:id, :org, :user, :cred, CAST('10.61.0.0/24' AS cidr), :digest,"
                " :state, 1, now(), NULL, now(), now())"
            ),
            {
                "id": run_id, "org": org.org_id, "user": org.user_id,
                "digest": DIGEST, "state": state, "cred": credential_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO push_run_target (id, org_id, run_id, device_id, address,"
                " position, state, updated_at)"
                " VALUES (:id, :org, :run, :dev, CAST('10.61.0.5' AS inet), 0, 'waiting', now())"
            ),
            {"id": uuid4(), "org": org.org_id, "run": run_id, "dev": device_id},
        )
        if sealed:
            await session.execute(
                text("UPDATE push_run SET sealed_at = now() WHERE id = :id"), {"id": run_id}
            )
        await session.commit()
    return run_id


async def _add_target(admin_engine: AsyncEngine, org: PushOrg, run_id: UUID, address: str) -> None:
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_run_target (id, org_id, run_id, device_id, address,"
                " position, state, updated_at)"
                " VALUES (:id, :org, :run, NULL, CAST(:addr AS inet), 7, 'waiting', now())"
            ),
            {"id": uuid4(), "org": org.org_id, "run": run_id, "addr": address},
        )
        await session.commit()


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity and row.relforcerowsecurity


async def test_another_org_cannot_read_a_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner = await seed_org(admin_engine, "pr-own")
    other = await seed_org(admin_engine, "pr-oth")
    await _insert_run(admin_engine, owner)
    async with org_session(other.org_id, engine=app_engine) as session:
        runs = (await session.execute(text("SELECT id FROM push_run"))).all()
        targets = (await session.execute(text("SELECT id FROM push_run_target"))).all()
    assert runs == [] and targets == []


async def test_a_run_cannot_be_written_into_another_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    mine = await seed_org(admin_engine, "pr-ins")
    theirs = await seed_org(admin_engine, "pr-ins2")
    with pytest.raises(DBAPIError):
        async with org_session(mine.org_id, engine=app_engine) as session:
            await session.execute(
                text(
                    "INSERT INTO push_run (id, org_id, created_by, prefix, list_digest,"
                    " state, target_count, created_at, updated_at, auth_window_started_at)"
                    " VALUES (:id, :org, :user, CAST('10.61.0.0/24' AS cidr), :d,"
                    " 'running', 1, now(), now(), now())"
                ),
                {"id": uuid4(), "org": theirs.org_id, "user": theirs.user_id, "d": DIGEST},
            )


async def test_one_active_run_per_org_is_a_database_rule(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-one")
    await _insert_run(admin_engine, org, state="running")
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="paused")
    # A run the lockout guard stopped is still active (spec §1.4a): a person
    # must resume or cancel it before another run may start.
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="stopped_credential")
    # Finished runs are history, not active: any number may exist.
    await _insert_run(admin_engine, org, state="completed")
    await _insert_run(admin_engine, org, state="cancelled")


async def test_a_sealed_run_takes_no_new_target(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-seal")
    run_id = await _insert_run(admin_engine, org, sealed=True)
    with pytest.raises(DBAPIError) as exc:
        await _add_target(admin_engine, org, run_id, "10.61.0.9")
    assert "sealed" in str(exc.value)


async def test_an_unsealed_run_is_still_being_created_and_takes_targets(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "pr-open")
    run_id = await _insert_run(admin_engine, org, sealed=False)
    await _add_target(admin_engine, org, run_id, "10.61.0.9")


async def test_a_target_never_changes_which_machine_it_names(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-fixed")
    run_id = await _insert_run(admin_engine, org)
    async with AsyncSession(admin_engine) as session:
        # Its state may move ...
        await session.execute(
            text("UPDATE push_run_target SET state = 'refused' WHERE run_id = :r"), {"r": run_id}
        )
        await session.commit()
    with pytest.raises(DBAPIError) as exc:
        async with AsyncSession(admin_engine) as session:
            # ... but never its address.
            await session.execute(
                text(
                    "UPDATE push_run_target SET address = CAST('10.61.0.200' AS inet)"
                    " WHERE run_id = :r"
                ),
                {"r": run_id},
            )
            await session.commit()
    assert "never changes" in str(exc.value)


# --------------------------------------------------------------------------- #
# R14, as the docstring claims it: the seal and what a person confirmed
# --------------------------------------------------------------------------- #


async def _execute(admin_engine: AsyncEngine, sql: str, **params: object) -> None:
    async with AsyncSession(admin_engine) as session:
        await session.execute(text(sql), params)
        await session.commit()


async def _target_device(admin_engine: AsyncEngine, run_id: UUID) -> UUID | None:
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(
                text("SELECT device_id FROM push_run_target WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()


async def _run_row(admin_engine: AsyncEngine, run_id: UUID):
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(text("SELECT * FROM push_run WHERE id = :r"), {"r": run_id})
        ).one()


async def test_a_target_never_moves_to_another_machine_but_may_lose_it(
    admin_engine: AsyncEngine,
) -> None:
    """The confirmed machine is the unit of authority: `device_id` may only
    become NULL (the machine record was deleted), never another machine."""
    org = await seed_org(admin_engine, "pr-dev")
    confirmed = await seed_asset(admin_engine, org.org_id, primary_ip="10.61.0.5")
    other = await seed_asset(admin_engine, org.org_id, primary_ip="10.61.0.6")
    run_id = await _insert_run(admin_engine, org, device_id=confirmed)
    with pytest.raises(DBAPIError) as exc:
        await _execute(
            admin_engine, "UPDATE push_run_target SET device_id = :d WHERE run_id = :r",
            d=other, r=run_id,
        )
    assert "never changes which" in str(exc.value)
    assert await _target_device(admin_engine, run_id) == confirmed

    # ON DELETE SET NULL still works: the machine record goes, the target stays.
    await _execute(admin_engine, "DELETE FROM devices WHERE id = :d", d=confirmed)
    assert await _target_device(admin_engine, run_id) is None
    # ... and a target that lost its machine is never given another.
    with pytest.raises(DBAPIError):
        await _execute(
            admin_engine, "UPDATE push_run_target SET device_id = :d WHERE run_id = :r",
            d=other, r=run_id,
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sealed_at", "NULL"),
        ("sealed_at", "now() + interval '1 day'"),
        ("prefix", "CAST('10.62.0.0/24' AS cidr)"),
        ("list_digest", "'" + "b" * 64 + "'"),
        ("target_count", "2"),
    ],
)
async def test_a_sealed_run_never_changes_what_was_confirmed(
    admin_engine: AsyncEngine, column: str, value: str
) -> None:
    org = await seed_org(admin_engine, "pr-rfix")
    run_id = await _insert_run(admin_engine, org)
    before = await _run_row(admin_engine, run_id)
    with pytest.raises(DBAPIError) as exc:
        await _execute(
            admin_engine, f"UPDATE push_run SET {column} = {value} WHERE id = :r", r=run_id
        )
    assert "push_run" in str(exc.value)
    assert getattr(await _run_row(admin_engine, run_id), column) == getattr(before, column)


async def test_a_run_never_changes_its_person_or_its_org(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-who")
    elsewhere = await seed_org(admin_engine, "pr-who2")
    run_id = await _insert_run(admin_engine, org)
    for column, value in (("created_by", elsewhere.user_id), ("org_id", elsewhere.org_id)):
        with pytest.raises(DBAPIError) as exc:
            await _execute(
                admin_engine, f"UPDATE push_run SET {column} = :v WHERE id = :r",
                v=value, r=run_id,
            )
        assert "push_run" in str(exc.value)
    row = await _run_row(admin_engine, run_id)
    assert (row.created_by, row.org_id) == (org.user_id, org.org_id)


async def test_a_run_never_changes_its_credential_but_loses_a_deleted_one(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "pr-cred")
    first = await seed_credential(admin_engine, org)
    second = await seed_credential(admin_engine, org)
    run_id = await _insert_run(admin_engine, org, credential_id=first)
    with pytest.raises(DBAPIError) as exc:
        await _execute(admin_engine, "UPDATE push_run SET credential_id = :c WHERE id = :r",
                       c=second, r=run_id)
    assert "credential" in str(exc.value)
    assert (await _run_row(admin_engine, run_id)).credential_id == first

    # `ON DELETE SET NULL` is the one change allowed: the pacer then refuses
    # every remaining target as `credential_missing`.
    await _execute(admin_engine, "DELETE FROM credentials WHERE id = :c", c=first)
    assert (await _run_row(admin_engine, run_id)).credential_id is None
    # ... and a run that lost its credential is never handed another.
    with pytest.raises(DBAPIError):
        await _execute(admin_engine, "UPDATE push_run SET credential_id = :c WHERE id = :r",
                       c=second, r=run_id)


async def test_the_run_may_still_move_through_its_states(admin_engine: AsyncEngine) -> None:
    """What the seal does NOT freeze: the lifecycle columns."""
    org = await seed_org(admin_engine, "pr-life")
    run_id = await _insert_run(admin_engine, org)
    await _execute(
        admin_engine,
        "UPDATE push_run SET state = 'paused', updated_at = now(), stopped_at = now(),"
        " auth_window_started_at = now(), finished_at = now() WHERE id = :r",
        r=run_id,
    )


async def test_a_target_cannot_be_written_into_another_orgs_run(
    admin_engine: AsyncEngine,
) -> None:
    """Review item 7: the trigger fails closed. A run it cannot find in the
    target's own organization is refused like a sealed one, whatever role
    writes and whatever RLS lets that role see."""
    owner = await seed_org(admin_engine, "pr-xorg")
    other = await seed_org(admin_engine, "pr-xorg2")
    run_id = await _insert_run(admin_engine, owner, sealed=False)
    with pytest.raises(DBAPIError):
        await _execute(
            admin_engine,
            "INSERT INTO push_run_target (id, org_id, run_id, device_id, address,"
            " position, state, updated_at)"
            " VALUES (:id, :org, :run, NULL, CAST('10.61.0.9' AS inet), 7, 'waiting', now())",
            id=uuid4(), org=other.org_id, run=run_id,
        )


async def test_the_state_columns_are_constrained(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-ck")
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="resumed-by-pacer")


def test_the_migration_revises_0190() -> None:
    from pathlib import Path

    import migrations.versions as versions_pkg  # type: ignore[import-untyped]

    source = (Path(next(iter(versions_pkg.__path__))) / "0200_push_runs.py").read_text()
    assert 'revision: str = "0200"' in source
    assert 'down_revision: str | None = "0190"' in source

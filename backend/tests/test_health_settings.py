"""Per-org health tunables (docs/modules/endpoint-health-alerting/spec.md §2.8,
§0.9, §10).

READ THIS BEFORE ADDING A TEST HERE.

`get_settings` RETURNS EFFECTIVE VALUES AND WRITES NOTHING. That is the
property most of this file exists to hold: a `GET` that lazily INSERTs makes a
`health.read` request a write, and populates the table for every org that ever
opened the settings screen while changing nothing about behaviour. The test
that holds it counts rows AFTER the read -- asserting the returned values alone
would pass against a lazy insert.

The range tests come in pairs. Each out-of-range value is asserted to raise a
typed `HealthValidationError` at the SERVICE, and the same value is asserted to
be refused by the DATABASE in `test_health_migration.py`. Neither is redundant:
without the service check the API returns 500 for a typo, and without the CHECK
the bound survives no second caller and no SQL fixup.

The `org_id` predicate test runs on the ADMIN engine. Driven through
`org_session` it would pass with the predicate deleted, because RLS would have
removed the other org's row before the query ran.
"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, HealthOrgSettings
from app.modules.health.errors import HealthValidationError
from app.modules.health.settings import (
    SETTING_BOUNDS,
    SETTING_DEFAULTS,
    get_settings,
    get_settings_row,
    update_settings,
)
from tests.health_fixtures import seed_org


async def _audit(admin_engine: AsyncEngine, org_id: Any) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _settings_row_count(admin_engine: AsyncEngine, org_id: Any) -> int:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                sqltext("SELECT count(*) FROM health_org_settings WHERE org_id = :o"),
                {"o": org_id},
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# The read writes nothing
# ---------------------------------------------------------------------------


async def test_get_settings_returns_the_documented_defaults_for_an_unconfigured_org(
    api_env,
) -> None:
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        effective = await get_settings(session, org_id)

    assert effective.flap_window_s == 3600
    assert effective.flap_threshold == 4
    assert effective.flap_cooldown_s == 3600
    assert effective.unavailable_clear_after_s == 86400
    assert effective.alert_retention_days == 400
    assert effective.escalate_after_s == 3600
    assert effective.offline_after_s == 900
    assert effective.approval_ttl_s == 86400
    assert effective.auto_resolve_alert_tickets is True
    assert effective.default_alert_queue_id is None
    assert effective.monitoring_deep_link_template is None


async def test_get_settings_writes_nothing(api_env) -> None:
    """THE ONE THAT MATTERS. A lazily-inserting `get_settings` returns exactly
    the same values as this one, so asserting on the return value cannot see
    the difference -- only the row count can."""
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await get_settings(session, org_id)
        await get_settings(session, org_id)
    assert await _settings_row_count(api_env.admin_engine, org_id) == 0
    assert await _audit(api_env.admin_engine, org_id) == []


async def test_the_service_defaults_match_the_database_defaults(admin_engine) -> None:
    """`SETTING_DEFAULTS` is what an unconfigured org behaves as; the column
    `server_default` is what a configured org gets for a field it did not send.
    If those two disagree, writing ONE setting silently changes the other
    seven, and no other test in this file would see it."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT column_name, column_default FROM information_schema.columns "
                    "WHERE table_name = 'health_org_settings' AND column_default IS NOT NULL"
                )
            )
        ).all()
    db_defaults = {r.column_name: r.column_default for r in rows}
    for field, expected in SETTING_DEFAULTS.items():
        if field not in db_defaults:
            continue
        raw = db_defaults[field].split("::")[0].strip("'")
        parsed: Any = raw
        if isinstance(expected, bool):
            parsed = raw == "true"
        elif isinstance(expected, int):
            parsed = int(raw)
        assert parsed == expected, f"{field}: service default {expected!r} vs db {raw!r}"


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


async def test_update_settings_creates_the_row_and_audits_old_and_new(api_env) -> None:
    """"Who set this org's flap threshold to 50" is only answerable if the
    PREVIOUS value is in the audit row -- the new one is already in the
    table."""
    org = await seed_org(api_env, "set-w")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        row = await update_settings(
            session, org.org_id, org.user_id, {"flap_threshold": 10, "offline_after_s": 1800}
        )

    assert row.flap_threshold == 10
    assert row.offline_after_s == 1800
    # Untouched fields keep the documented defaults.
    assert row.flap_window_s == 3600
    assert row.alert_retention_days == 400

    events = await _audit(api_env.admin_engine, org.org_id)
    assert [e.action for e in events] == ["health.settings_updated"]
    meta = events[0].meta
    assert meta["changed"] == {"flap_threshold": 10, "offline_after_s": 1800}
    assert meta["previous"] == {"flap_threshold": 4, "offline_after_s": 900}


async def test_update_settings_is_an_upsert_not_a_check_then_insert(api_env) -> None:
    """Second write updates rather than raising -- and the row count stays 1,
    which is what `ON CONFLICT DO UPDATE` on the `org_id` PK buys."""
    org = await seed_org(api_env, "set-u")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await update_settings(session, org.org_id, org.user_id, {"flap_threshold": 10})
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        row = await update_settings(session, org.org_id, org.user_id, {"flap_threshold": 20})
    assert row.flap_threshold == 20
    assert await _settings_row_count(api_env.admin_engine, org.org_id) == 1

    events = await _audit(api_env.admin_engine, org.org_id)
    assert [e.meta["previous"]["flap_threshold"] for e in events] == [4, 10]


async def test_update_settings_reflects_in_the_next_get(api_env) -> None:
    org = await seed_org(api_env, "set-r")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await update_settings(session, org.org_id, org.user_id, {"unavailable_clear_after_s": 7200})
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        effective = await get_settings(session, org.org_id)
    assert effective.unavailable_clear_after_s == 7200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("flap_window_s", 299),
        ("flap_window_s", 86401),
        ("flap_threshold", 1),
        ("flap_threshold", 51),
        ("flap_cooldown_s", 299),
        ("flap_cooldown_s", 86401),
        ("unavailable_clear_after_s", 3599),
        ("unavailable_clear_after_s", 2592001),
        ("alert_retention_days", 89),
        ("alert_retention_days", 3651),
        ("escalate_after_s", 299),
        ("escalate_after_s", 604801),
        ("offline_after_s", 299),
        ("offline_after_s", 86401),
        ("approval_ttl_s", 299),
        ("approval_ttl_s", 604801),
    ],
)
async def test_an_out_of_range_setting_is_a_typed_error_not_a_500(
    api_env, field: str, value: int
) -> None:
    """The service refuses it before Postgres does. Without this the API's
    answer to a typo is a 500 from an `IntegrityError`, and the operator
    learns nothing about which bound they crossed."""
    org = await seed_org(api_env, "set-rng")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(HealthValidationError) as exc:
            await update_settings(session, org.org_id, org.user_id, {field: value})
    assert field in str(exc.value)
    assert await _settings_row_count(api_env.admin_engine, org.org_id) == 0


async def test_every_bounded_setting_accepts_both_ends_of_its_range(api_env) -> None:
    """The positive control for the sixteen rejections above: a validator that
    refused EVERYTHING would pass all of them."""
    org = await seed_org(api_env, "set-ends")
    for field, (low, high) in SETTING_BOUNDS.items():
        for value in (low, high):
            async with org_session(
                org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
            ) as session:
                row = await update_settings(session, org.org_id, org.user_id, {field: value})
            assert getattr(row, field) == value, field


async def test_an_unknown_settings_field_is_refused_not_ignored(api_env) -> None:
    """Silently dropping it returns 200 for a typo'd body having changed
    nothing, and audits a no-op edit."""
    org = await seed_org(api_env, "set-unk")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(HealthValidationError):
            await update_settings(session, org.org_id, org.user_id, {"flap_windows": 3600})


async def test_an_empty_settings_update_is_refused(api_env) -> None:
    org = await seed_org(api_env, "set-empty")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(HealthValidationError):
            await update_settings(session, org.org_id, org.user_id, {})


async def test_a_queue_from_another_org_is_refused(api_env) -> None:
    """`default_alert_queue_id` is a plain FK: Postgres alone would happily let
    this org route its alert tickets into another tenant's queue."""
    from app.models import TicketQueue

    a = await seed_org(api_env, "set-qa")
    b = await seed_org(api_env, "set-qb")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = TicketQueue(
            org_id=b.org_id, key=f"q-{uuid4().hex[:8]}", name="B's queue", created_by=b.user_id
        )
        session.add(queue)
        await session.commit()
        foreign_queue_id = queue.id

    async with org_session(
        a.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(HealthValidationError):
            await update_settings(
                session, a.org_id, a.user_id, {"default_alert_queue_id": foreign_queue_id}
            )


# ---------------------------------------------------------------------------
# Tenancy -- load-bearing, and only visible on the admin engine
# ---------------------------------------------------------------------------


async def test_the_settings_read_carries_its_own_org_predicate(admin_engine) -> None:
    """THE ONE RLS CANNOT MASK. Through `org_session` this passes with the
    `org_id` predicate deleted from `get_settings_row`, because RLS removes the
    other org's row first. On the admin engine the predicate is the only thing
    separating two orgs' rows: delete it and this reads whichever row the
    planner returns, so org A observes org B's `flap_threshold`."""
    a = await seed_org(admin_engine, "set-pa")
    b = await seed_org(admin_engine, "set-pb")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(HealthOrgSettings(org_id=a.org_id, flap_threshold=7, updated_by=a.user_id))
        session.add(HealthOrgSettings(org_id=b.org_id, flap_threshold=33, updated_by=b.user_id))
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row_a = await get_settings_row(session, a.org_id)
        row_b = await get_settings_row(session, b.org_id)
        effective_a = await get_settings(session, a.org_id)
    assert row_a is not None and row_a.flap_threshold == 7
    assert row_b is not None and row_b.flap_threshold == 33
    assert effective_a.flap_threshold == 7

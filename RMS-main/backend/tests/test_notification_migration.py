"""Schema-shape tests for migration 0025 (notification core).

Note: the task brief for this test sketched a `db_session` fixture, but no
such fixture exists in this test harness (see tests/conftest.py) -- the
schema-shape convention used across this suite (test_rls.py,
test_schema_drift.py) is a raw `admin_engine.connect()`, since these checks
introspect `pg_class` / `information_schema` / `pg_indexes` directly rather
than exercising tenant-scoped row access. That is what these tests use.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

NEW_TABLES = [
    "domain_events",
    "notification_channels",
    "notification_rules",
    "queue_members",
    "notification_deliveries",
    "webhook_allowlist_entries",
]


async def test_notification_tables_exist(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        for table in NEW_TABLES:
            got = await conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            )
            assert got.scalar() is not None, f"{table} missing"


async def test_notification_tables_have_forced_rls(admin_engine: AsyncEngine) -> None:
    # Spec §8: RLS ENABLE + FORCE, not merely ENABLE. Without FORCE the table
    # owner bypasses the policy, and the app connects as the owner.
    async with admin_engine.connect() as conn:
        for table in NEW_TABLES:
            got = await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :t"
                ),
                {"t": table},
            )
            enabled, forced = got.one()
            assert enabled is True, f"{table}: RLS not enabled"
            assert forced is True, f"{table}: RLS not FORCED"


async def test_actor_user_id_is_nullable(admin_engine: AsyncEngine) -> None:
    # Spec §2.1: machine-origin events (Phase 16 alerts) have no human actor.
    # A NOT NULL here would force a synthetic user into the record.
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'domain_events' AND column_name = 'actor_user_id'"
            )
        )
        assert got.scalar() == "YES"


async def test_allowlist_host_unique_per_org(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_indexes "
                "WHERE tablename = 'webhook_allowlist_entries' AND indexdef LIKE '%UNIQUE%'"
            )
        )
        assert got.scalar() >= 1


async def test_drain_query_has_a_usable_index(admin_engine: AsyncEngine) -> None:
    """Migration 0027 (whole-branch review finding M11): the drain query
    (`app.worker.drain_outbox`) filters `(org_id, processed_at IS NULL)`.
    `ix_domain_events_drain` (migration 0025, on `claimed_at`/`occurred_at`)
    cannot serve that predicate at all -- this asserts the index that
    actually matches it exists, as a partial index on `org_id` restricted to
    `processed_at IS NULL`."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'domain_events' "
                "AND indexname = 'ix_domain_events_org_unprocessed'"
            )
        )
        indexdef = got.scalar()
        assert indexdef is not None, "ix_domain_events_org_unprocessed is missing"
        assert "org_id" in indexdef
        assert "processed_at IS NULL" in indexdef

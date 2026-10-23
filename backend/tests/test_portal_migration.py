"""Migration 0028: the portal_user role, its policies, and the requester tier.

The privilege tests here are the point of this file. A policy cannot restrict a
table the role has no grant on, and a grant with no policy exposes the whole
org -- both layers are required and both are asserted.

Note: the task brief for this file sketched a `db_session` fixture, but no
such fixture exists in this test harness (see tests/conftest.py) -- the
schema-shape convention used across this suite (test_notification_migration.py,
test_rls.py, test_rbac.py) is a raw `admin_engine.connect()`, since these
checks introspect `pg_roles` / `pg_policies` / `has_table_privilege` /
`information_schema` directly rather than exercising tenant-scoped row
access. That is what these tests use.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Spec §2.1. Note what is ABSENT: ticket_queue. A requester never chooses a
# queue, so portal_user gets no access to it at all.
PORTAL_READABLE = [
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_status_history",
    "ticket_approval",
    "ticket_category",
    "ticket_number_counter",
]


async def test_portal_user_role_exists(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'portal_user'")
        )
        assert got.scalar() == 1


async def test_portal_user_is_not_bypassrls(admin_engine: AsyncEngine) -> None:
    """The whole design rests on RLS applying to this role."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'portal_user'")
        )
        assert got.scalar() is False


@pytest.mark.parametrize("table", PORTAL_READABLE)
async def test_portal_user_can_select_the_tables_it_needs(
    admin_engine: AsyncEngine, table: str
) -> None:
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT has_table_privilege('portal_user', :t, 'SELECT')"), {"t": table}
        )
        assert got.scalar() is True, f"portal_user cannot SELECT {table}"


async def test_portal_user_can_insert_where_it_must(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        for table in ("ticket", "ticket_comment", "ticket_attachment", "ticket_number_counter"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', :t, 'INSERT')"), {"t": table}
            )
            assert got.scalar() is True, f"portal_user cannot INSERT {table}"


async def test_portal_user_can_update_the_number_counter_only(admin_engine: AsyncEngine) -> None:
    """numbering.py does INSERT ... ON CONFLICT DO NOTHING then UPDATE ...
    RETURNING. Both are required, and UPDATE is required NOWHERE else -- a
    requester never edits a ticket row after creating it."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT has_table_privilege('portal_user', 'ticket_number_counter', 'UPDATE')")
        )
        assert got.scalar() is True
        got = await conn.execute(
            text("SELECT has_table_privilege('portal_user', 'ticket', 'UPDATE')")
        )
        assert got.scalar() is False, "a requester must not be able to edit a ticket row"


async def test_portal_user_cannot_delete_anything(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        for table in PORTAL_READABLE:
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', :t, 'DELETE')"), {"t": table}
            )
            assert got.scalar() is False, f"portal_user can DELETE {table}"


# Only these six carry a portal policy. ticket_category and
# ticket_number_counter deliberately do not -- see the migration's comment.
# `ticket_sla` (migration 0038) joins the list: `create_my_ticket` reaches
# `sla.tracking.start_tracking` and therefore INSERTs one, and without a
# restrictive policy the SELECT half of that grant would expose every SLA row
# in the org. The four `sla_calendar*`/`sla_policy` tables 0038 also grants are
# NOT here, on the same grounds as `ticket_category`: org configuration a
# delegated write must read, already org-scoped by tenant_isolation.
PORTAL_POLICIED = [
    "ticket",
    "ticket_comment",
    "ticket_attachment",
    "ticket_status_history",
    "ticket_approval",
    "ticket_sla",
]


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_every_portal_table_has_a_portal_policy(
    admin_engine: AsyncEngine, table: str
) -> None:
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_policies "
                "WHERE tablename = :t AND policyname = 'portal_isolation'"
            ),
            {"t": table},
        )
        assert got.scalar() == 1, f"{table} has no portal_isolation policy"


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_portal_policies_are_restrictive(admin_engine: AsyncEngine, table: str) -> None:
    """THE MOST IMPORTANT ASSERTION IN THIS FILE.

    tenant_isolation is PERMISSIVE with roles={public}, so it also covers
    portal_user, and Postgres OR's permissive policies. A PERMISSIVE portal
    policy would be OR'd with the org-only predicate and portal_user would see
    EVERY ticket in the org -- while every other assertion in this file still
    passed. Verified empirically: permissive -> 2 of 2 rows; restrictive -> 1.
    """
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT permissive FROM pg_policies "
                "WHERE tablename = :t AND policyname = 'portal_isolation'"
            ),
            {"t": table},
        )
        assert got.scalar() == "RESTRICTIVE", f"{table}'s portal policy is not RESTRICTIVE"


@pytest.mark.parametrize("table", PORTAL_POLICIED)
async def test_portal_policies_apply_only_to_portal_user(
    admin_engine: AsyncEngine, table: str
) -> None:
    """A restrictive policy with no TO clause would apply to app_user too and
    break every staff route on these tables."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT roles FROM pg_policies "
                "WHERE tablename = :t AND policyname = 'portal_isolation'"
            ),
            {"t": table},
        )
        assert got.scalar() == ["portal_user"]


async def test_ticket_portal_policy_scopes_by_requester(admin_engine: AsyncEngine) -> None:
    """The restrictive policy supplies the REQUESTER half; org scope comes from
    the permissive tenant_isolation policy it is AND'd with."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT qual FROM pg_policies "
                "WHERE tablename = 'ticket' AND policyname = 'portal_isolation'"
            )
        )
        assert "app.current_requester" in (got.scalar() or "")


async def test_comment_portal_policy_filters_visibility(admin_engine: AsyncEngine) -> None:
    """Spec §2.3 -- the internal-note guard lives in the POLICY, so no future
    portal query can expose one."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT qual FROM pg_policies "
                "WHERE tablename = 'ticket_comment' AND policyname = 'portal_isolation'"
            )
        )
        assert "public" in (got.scalar() or "")


async def test_comment_insert_is_constrained_to_public(admin_engine: AsyncEngine) -> None:
    """Spec §2.2 -- WITH CHECK, so a requester cannot WRITE an internal note
    even if application code were wrong."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT with_check FROM pg_policies "
                "WHERE tablename = 'ticket_comment' AND policyname = 'portal_isolation'"
            )
        )
        assert "public" in (got.scalar() or "")


async def test_ticket_insert_forces_the_requester(admin_engine: AsyncEngine) -> None:
    """Spec §2.2 -- a requester cannot file a ticket naming somebody else."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT with_check FROM pg_policies "
                "WHERE tablename = 'ticket' AND policyname = 'portal_isolation'"
            )
        )
        assert "app.current_requester" in (got.scalar() or "")


async def test_requester_role_exists_and_holds_only_portal_permissions(
    admin_engine: AsyncEngine,
) -> None:
    """Spec §4 -- ticket.read is ORG-WIDE. A requester holding it would defeat
    the module regardless of RLS, because it also gates staff routes."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text(
                "SELECT p.code FROM roles r "
                "JOIN role_permissions rp ON rp.role_id = r.id "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE r.name = 'requester' AND r.org_id IS NULL"
            )
        )
        codes = sorted(row[0] for row in got.all())
        assert codes == ["portal.ticket.read", "portal.ticket.write"]

async def test_portal_user_can_read_ticket_queue(admin_engine) -> None:
    """Migration 0029. `ticket.queue_id` is NOT NULL and
    `tickets.create_ticket` resolves it through `refdata.resolve_queue`, which
    issues `select(TicketQueue)`. Without this grant that SELECT raises
    `permission denied for table ticket_queue` and EVERY portal ticket creation
    fails with a 500.

    A requester still never sees a queue -- the portal's response models carry
    no queue field. This grant is for the service's internal resolution only,
    and org scope comes from the existing tenant_isolation policy.
    """
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT has_table_privilege('portal_user', 'ticket_queue', 'SELECT')")
        )
        assert got.scalar() is True


async def test_portal_user_still_cannot_write_ticket_queue(admin_engine) -> None:
    """Read-only: a requester must not be able to create or rename a queue."""
    async with admin_engine.connect() as conn:
        for priv in ("INSERT", "UPDATE", "DELETE"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', 'ticket_queue', :p)"),
                {"p": priv},
            )
            assert got.scalar() is False, f"portal_user can {priv} ticket_queue"


# ---------------------------------------------------------------------------
# Migration 0030 -- the write-path grants that let the ticket-core write
# functions (create_ticket / add_comment / add_attachment) run natively under
# portal_session, with no second, elevated connection.
# ---------------------------------------------------------------------------


async def test_portal_user_has_the_new_write_path_grants(admin_engine: AsyncEngine) -> None:
    """Empirically-determined minimum (see migration 0030's docstring and
    `.git/sdd/portal-fix-db-enforced-writes-report.md`): `users` SELECT,
    `audit_events` SELECT+INSERT (the hash-chain lookup needs SELECT, not
    just the append), `domain_events` INSERT, and INSERT added to
    `ticket_status_history`/`ticket_approval`'s existing SELECT."""
    expected = {
        "users": ["SELECT"],
        "audit_events": ["SELECT", "INSERT"],
        "domain_events": ["INSERT"],
        "ticket_status_history": ["INSERT"],
        "ticket_approval": ["INSERT"],
    }
    async with admin_engine.connect() as conn:
        for table, privs in expected.items():
            for priv in privs:
                got = await conn.execute(
                    text("SELECT has_table_privilege('portal_user', :t, :p)"),
                    {"t": table, "p": priv},
                )
                assert got.scalar() is True, f"portal_user cannot {priv} {table}"


async def test_portal_user_cannot_update_or_delete_audit_events(
    admin_engine: AsyncEngine,
) -> None:
    """Invariant #4: the audit log is append-only. Migration 0030 adds
    SELECT+INSERT to portal_user, never UPDATE or DELETE."""
    async with admin_engine.connect() as conn:
        for priv in ("UPDATE", "DELETE"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', 'audit_events', :p)"),
                {"p": priv},
            )
            assert got.scalar() is False, f"portal_user can {priv} audit_events"


async def test_portal_user_cannot_select_domain_events(admin_engine: AsyncEngine) -> None:
    """The notification outbox is write-only from the portal's perspective --
    nothing in the portal's call paths reads it back, so no SELECT is
    granted (least privilege)."""
    async with admin_engine.connect() as conn:
        got = await conn.execute(
            text("SELECT has_table_privilege('portal_user', 'domain_events', 'SELECT')")
        )
        assert got.scalar() is False


async def test_portal_user_cannot_write_users(admin_engine: AsyncEngine) -> None:
    """SELECT only -- a requester never creates or edits a user row."""
    async with admin_engine.connect() as conn:
        for priv in ("INSERT", "UPDATE", "DELETE"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', 'users', :p)"), {"p": priv}
            )
            assert got.scalar() is False, f"portal_user can {priv} users"


async def test_users_has_a_restrictive_self_row_policy_for_portal_user(
    admin_engine: AsyncEngine,
) -> None:
    """The load-bearing policy: without it, a plain SELECT grant on `users`
    would let a requester read every colleague's name and email in the org
    via the pre-existing org-scoped tenant_isolation policy alone."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT permissive, roles, qual FROM pg_policies "
                    "WHERE tablename = 'users' AND policyname = 'portal_isolation'"
                )
            )
        ).one_or_none()
        assert row is not None, "users has no portal_isolation policy"
        permissive, roles, qual = row
        assert permissive == "RESTRICTIVE"
        assert roles == ["portal_user"]
        assert "app.current_requester" in (qual or "")


# ---------------------------------------------------------------------------
# I4 -- nothing else asserted the ABSENCE of grants, so a later blanket
# `GRANT ... ON ALL TABLES IN SCHEMA public TO portal_user` (the exact shape
# `tests/conftest.py::_run_migrations` already issues for `app_user`) would
# pass every other test in this file while handing the portal role the whole
# control plane. This pins the EXACT expected {table: {privileges}} map and
# fails on anything added, not just on anything missing.
# ---------------------------------------------------------------------------

# The full, closed universe of what portal_user may touch, across migrations
# 0028/0029/0030/0038 -- 16 tables, no more. Anything not listed here must have
# NO privilege at all.
#
# Migration 0038 added the last five, and the shape of that addition is the
# point: `ticket_sla` gets SELECT + INSERT *and a restrictive policy* (a
# requester's own rows only), while the four SLA configuration tables get
# SELECT alone. **No UPDATE and no DELETE anywhere in the group.** Every SLA
# write a portal request can reach is the creation-time INSERT; `pause`,
# `resume`, every met stamp, every breach stamp and `voided_at` are UPDATEs
# driven by staff-side transitions on `app_user`, so a requester cannot move
# their own deadline, mark their own promise met, or void it.
EXPECTED_PORTAL_GRANTS: dict[str, frozenset[str]] = {
    "ticket": frozenset({"SELECT", "INSERT"}),
    "ticket_comment": frozenset({"SELECT", "INSERT"}),
    "ticket_attachment": frozenset({"SELECT", "INSERT"}),
    "ticket_status_history": frozenset({"SELECT", "INSERT"}),
    "ticket_approval": frozenset({"SELECT", "INSERT"}),
    "ticket_category": frozenset({"SELECT"}),
    "ticket_number_counter": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "ticket_queue": frozenset({"SELECT"}),
    "users": frozenset({"SELECT"}),
    "audit_events": frozenset({"SELECT", "INSERT"}),
    "domain_events": frozenset({"INSERT"}),
    # Migration 0038 -- creation-time SLA materialisation.
    "ticket_sla": frozenset({"SELECT", "INSERT"}),
    "sla_policy": frozenset({"SELECT"}),
    "sla_calendar": frozenset({"SELECT"}),
    "sla_calendar_window": frozenset({"SELECT"}),
    "sla_calendar_holiday": frozenset({"SELECT"}),
}


async def test_portal_user_grant_surface_is_exactly_the_expected_set(
    admin_engine: AsyncEngine,
) -> None:
    """THE point of this test (I4): asserts both directions. A later migration
    adding ANY grant beyond `EXPECTED_PORTAL_GRANTS` -- e.g. the blanket
    `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO
    portal_user` shape `tests/conftest.py::_run_migrations` already issues
    for `app_user` -- fails here even though every other, per-table test in
    this file would still pass."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema."
                    "role_table_grants WHERE grantee = 'portal_user' "
                    "AND table_schema = 'public'"
                )
            )
        ).all()

    actual: dict[str, set[str]] = {}
    for table_name, privilege_type in rows:
        actual.setdefault(table_name, set()).add(privilege_type)

    assert set(actual) == set(EXPECTED_PORTAL_GRANTS), (
        f"portal_user's granted TABLES changed: {set(actual) - set(EXPECTED_PORTAL_GRANTS)} "
        f"added, {set(EXPECTED_PORTAL_GRANTS) - set(actual)} missing"
    )
    for table, expected_privs in EXPECTED_PORTAL_GRANTS.items():
        assert actual[table] == set(expected_privs), (
            f"portal_user's privileges on {table} changed: "
            f"has {actual[table]}, expected {set(expected_privs)}"
        )


# M8 fix: the existing `test_portal_user_cannot_delete_anything` above only
# iterates `PORTAL_READABLE` (7 tables) -- `ticket_queue`, `users`,
# `audit_events`, `domain_events` had no DELETE assertion anywhere in this
# file. Cover every granted table explicitly (subsumed in spirit by the
# exact-grants test above, which would also catch a DELETE grant appearing,
# but this pins it per-table with a readable failure message). Iterating
# `EXPECTED_PORTAL_GRANTS` rather than a second hand-written list is what kept
# it correct when migration 0038 added five more tables.
async def test_portal_user_cannot_delete_any_of_its_granted_tables(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_engine.connect() as conn:
        for table in EXPECTED_PORTAL_GRANTS:
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', :t, 'DELETE')"), {"t": table}
            )
            assert got.scalar() is False, f"portal_user can DELETE {table}"


async def test_portal_user_cannot_update_any_sla_table(admin_engine: AsyncEngine) -> None:
    """Migration 0038's load-bearing omission, asserted by name rather than
    left to the exact-grants map alone.

    A requester's connection may INSERT the `ticket_sla` row that
    `create_ticket` materialises for their own new ticket, and nothing else.
    UPDATE is what every subsequent SLA write uses -- `pause`/`resume`,
    `response_met_at`, `resolution_met_at`, both breach stamps, `escalated_at`
    and `voided_at` -- so withholding it is what stops a requester from
    editing what their organisation promised them after the fact. Granting it
    "just for the pause" (the shape considered when `pending_approval` tickets
    needed to be born paused, and rejected -- `_materialise` sets `paused_at`
    in the INSERT instead) would have opened all of it at once."""
    async with admin_engine.connect() as conn:
        for table in (
            "ticket_sla",
            "sla_policy",
            "sla_calendar",
            "sla_calendar_window",
            "sla_calendar_holiday",
        ):
            for priv in ("UPDATE", "DELETE"):
                got = await conn.execute(
                    text("SELECT has_table_privilege('portal_user', :t, :p)"),
                    {"t": table, "p": priv},
                )
                assert got.scalar() is False, f"portal_user can {priv} {table}"


# ---------------------------------------------------------------------------
# The reviewer's cheap extra (Open Question 1): migration 0031's restrictive
# INSERT policy on audit_events stops a portal principal from attributing a
# forged event to another actor. Does not touch the chain-head SELECT.
# ---------------------------------------------------------------------------


async def test_audit_events_has_a_restrictive_actor_policy_for_portal_user(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT permissive, roles, qual, with_check FROM pg_policies "
                    "WHERE tablename = 'audit_events' AND policyname = 'portal_isolation'"
                )
            )
        ).one_or_none()
        assert row is not None, "audit_events has no portal_isolation policy"
        permissive, roles, qual, with_check = row
        assert permissive == "RESTRICTIVE"
        assert roles == ["portal_user"]
        # No USING clause (or an unconditional one) -- the hash-chain's own
        # prev_hash lookup must keep seeing every actor's events in the org;
        # only the WRITE side is constrained.
        assert qual is None or qual == "true"
        assert "app.current_requester" in (with_check or "")


async def test_cross_actor_audit_insert_is_refused_by_the_database(
    admin_engine: AsyncEngine, portal_engine: AsyncEngine
) -> None:
    """The concrete forgery migration 0031 closes: a portal connection pinned
    to ONE requester cannot INSERT an audit_events row attributing the event
    to a DIFFERENT actor id."""
    from uuid import uuid4

    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.tenancy import portal_session
    from app.models import Organization

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            org = Organization(
                name="portal-audit-forge", slug=f"portal-audit-forge-{uuid4().hex[:8]}"
            )
            session.add(org)
            await session.flush()
            org_id = org.id

    me = uuid4()
    someone_else = uuid4()

    with pytest.raises(ProgrammingError, match="row-level security policy"):
        async with portal_session(org_id, me, engine=portal_engine) as s:
            await s.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, org_id, ts, actor_type, actor_id, action, target_type, "
                    "target_id, metadata, prev_hash, hash) VALUES "
                    "(gen_random_uuid(), :org_id, now(), 'user', :actor_id, "
                    "'ticket.created', 'ticket', '', '{}'::jsonb, "
                    "repeat('0', 64), repeat('f', 64))"
                ),
                {"org_id": str(org_id), "actor_id": str(someone_else)},
            )


async def test_ticket_status_history_and_approval_have_no_with_check_but_still_refuse_inserts(
    admin_engine: AsyncEngine,
) -> None:
    """Documents the empirical finding migration 0030's docstring relies on:
    these two policies were created (migration 0028) with only a USING
    clause and no explicit WITH CHECK. Postgres itself is why that still
    constrains INSERT (see test_portal_tickets.py's cross-requester proof
    test for the runtime behaviour); at the catalog level this just confirms
    with_check is unset, so nothing here is accidentally relying on a WITH
    CHECK clause that was never written."""
    async with admin_engine.connect() as conn:
        for table in ("ticket_status_history", "ticket_approval"):
            got = await conn.execute(
                text(
                    "SELECT with_check FROM pg_policies "
                    "WHERE tablename = :t AND policyname = 'portal_isolation'"
                ),
                {"t": table},
            )
            assert got.scalar() is None, f"{table}'s portal_isolation unexpectedly has WITH CHECK"

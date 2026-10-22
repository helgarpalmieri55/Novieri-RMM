import re
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.tenancy import org_session
from app.models import Device, Organization


async def _make_org_with_device(admin_engine: AsyncEngine, slug: str) -> tuple[UUID, UUID]:
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname=f"host-{slug}", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def test_org_session_sees_only_own_rows_even_raw_sql(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, dev_a = await _make_org_with_device(admin_engine, "rls-a")
    org_b, _ = await _make_org_with_device(admin_engine, "rls-b")

    async with org_session(org_a, engine=app_engine) as session:
        rows = (await session.execute(text("SELECT id, org_id FROM devices"))).all()
    assert {r.org_id for r in rows} == {org_a}
    assert dev_a in {r.id for r in rows}


async def test_insert_with_foreign_org_id_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, _ = await _make_org_with_device(admin_engine, "rls-c")
    org_b, _ = await _make_org_with_device(admin_engine, "rls-d")

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with org_session(org_a, engine=app_engine) as session:
            session.add(Device(org_id=org_b, hostname="evil", os_family="linux"))
            await session.flush()


async def test_no_tenant_context_sees_nothing_and_cannot_insert(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, _ = await _make_org_with_device(admin_engine, "rls-e")

    async with app_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM devices"))).all()
        assert rows == []
        with pytest.raises((DBAPIError, ProgrammingError)):
            await conn.execute(
                text(
                    "INSERT INTO devices (id, org_id, hostname, os_family, os_version,"
                    " architecture, cpu, status, created_at, updated_at)"
                    f" VALUES (gen_random_uuid(), '{org_a}', 'h', 'linux', '', '', '',"
                    " 'active', now(), now())"
                )
            )


async def test_every_org_scoped_table_has_forced_rls_policy(admin_engine: AsyncEngine) -> None:
    """Adding a tenant-scoped table without a forced RLS policy is a security bug."""
    async with admin_engine.connect() as conn:
        org_tables = {
            r.table_name
            for r in await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns"
                    " WHERE table_schema='public' AND column_name='org_id'"
                )
            )
        } | {"organizations"}
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
        policied = {
            r.tablename
            for r in await conn.execute(text("SELECT DISTINCT tablename FROM pg_policies"))
        }

    problems = []
    for table in sorted(org_tables):
        if table.startswith("alembic"):
            continue
        if flags.get(table) != (True, True):
            problems.append(f"{table}: RLS not enabled+forced {flags.get(table)}")
        if table not in policied:
            problems.append(f"{table}: no RLS policy")
    assert not problems, problems


# --------------------------------------------------------------------------- #
# R2: "has a policy" is not "is protected by a policy".
# --------------------------------------------------------------------------- #

# Tables carrying `org_id` whose RLS policies deliberately do NOT scope by it.
# Each entry is a decision somebody made and can be argued with; the point of
# naming them is that the next one has to be argued for rather than arriving
# unnoticed, and that the entry goes red when it stops being true.
#
# This list is duplicated, deliberately, in `deploy/postgres/assert-rls.sql`'s
# block 3 -- that file runs in CI against the deploy stack's own database and
# cannot import Python. `test_the_sql_assertion_names_the_same_exemptions`
# below holds the two copies equal.
POLICY_WITHOUT_ORG_ID_BY_DESIGN: dict[str, str] = {
    # Platform-scoped by design (error-monitoring spec §4.1, and
    # `tests/test_tenant_column_sweep.py`'s `TENANTLESS_BY_DESIGN`). `org_id`
    # here records which org was in scope when an exception was captured -- a
    # nullable CORRELATION column, not a tenancy key, because a failure at
    # process startup belongs to no org and the grouping the module exists for
    # ("this line has failed 4,000 times") is cross-org by construction.
    #
    # WHAT THIS EXEMPTION COSTS, STATED RATHER THAN GLOSSED: RLS is not the
    # backstop for this table. Everywhere else, a route that forgets its tenant
    # session returns zero rows; here it returns everything. That is exactly
    # what happened -- `app/api/v1/errors.py` reads it on a bare
    # `AsyncSession(app_engine)` and handed every tenant's org, user, job and
    # device ids to any `org-admin` (audit
    # `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1). So the
    # whole tenant boundary for this table is the permission code on the route,
    # and migration 0068 moved that code to `super-admin`, which no org-scoped
    # surface can grant.
    "platform_error_event": (
        "platform-scoped: org_id is a nullable correlation column, not a "
        "tenancy key. The boundary is the platform.errors.* permission "
        "(super-admin only, migration 0068), not RLS."
    ),
}


async def test_every_org_scoped_table_has_a_policy_that_mentions_org_id(
    admin_engine: AsyncEngine,
) -> None:
    """THE SWEEP ABOVE PASSES A TABLE WHOSE POLICY IS `USING (true)`, and one
    already was.

    `test_every_org_scoped_table_has_forced_rls_policy` asks for RLS enabled,
    forced, and at least one policy. `platform_error_event` satisfied all three
    with `USING (true) WITH CHECK (true)` -- a policy that permits every row to
    every `app_user` session and mentions no tenant at all -- and the sweep
    reported the schema clean while a route returned cross-tenant identifiers
    through it (audit 2026-08-21-tenancy-test-efficacy, F1 and R2).

    PERMISSIVE, not "any policy". Postgres ORs permissive policies together and
    ANDs restrictive ones on top, so a restrictive policy can only narrow what
    some permissive policy already allows. Three tables here (`audit_events`,
    `ticket`, `users`) carry a restrictive `portal_isolation` policy bound to
    `portal_user` that scopes by requester rather than by org; reading those as
    tenant scoping would score them protected by a policy that is not the
    tenant rail. `roles` is the mirror case: its `roles_insert` policy has a
    NULL `polqual` because its predicate lives in WITH CHECK, and it passes on
    `roles_select` instead.

    FALSIFIER: give a new tenant table a `USING (true)` policy and this test
    must go red. `tests/test_rls_invariant_sql.py` constructs exactly that
    table against the deploy stack's copy of the same rule.
    """
    async with admin_engine.connect() as conn:
        offenders = {
            r.relname
            for r in await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    " AND EXISTS (SELECT 1 FROM pg_attribute a"
                    "   WHERE a.attrelid = c.oid AND a.attname = 'org_id'"
                    "     AND a.attnum > 0 AND NOT a.attisdropped)"
                    " AND NOT EXISTS (SELECT 1 FROM pg_policy p"
                    "   WHERE p.polrelid = c.oid AND p.polpermissive"
                    "     AND pg_get_expr(p.polqual, p.polrelid) LIKE '%org_id%')"
                )
            )
        }

    unnamed = sorted(offenders - set(POLICY_WITHOUT_ORG_ID_BY_DESIGN))
    assert not unnamed, (
        "these tables carry org_id but no permissive RLS policy whose predicate "
        "mentions it, so RLS is NOT their tenant backstop and a route that "
        "forgets its tenant session will return every org's rows. Either add a "
        "tenant policy, or add the table to POLICY_WITHOUT_ORG_ID_BY_DESIGN "
        f"with a written reason: {unnamed}"
    )

    stale = sorted(set(POLICY_WITHOUT_ORG_ID_BY_DESIGN) - offenders)
    assert not stale, (
        "these tables are exempted from the org_id-predicate rule but no longer "
        "need to be -- they now have a proper tenant policy. Delete the "
        f"exemption rather than leaving it standing: {stale}"
    )


def test_the_sql_assertion_names_the_same_exemptions() -> None:
    """The deploy stack runs `deploy/postgres/assert-rls.sql` and CANNOT import
    this module, so the exemption list exists twice. Two copies of a security
    exemption drift, and the copy that drifts is the one that stops asserting
    -- so the equality is a test rather than a comment asking people to
    remember."""
    sql = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "postgres"
        / "assert-rls.sql"
    ).read_text()
    array = re.search(r"c\.relname <> ALL \(ARRAY\[([^\]]*)\]\)", sql)
    assert array is not None, "block 3's exemption array is not where this test looks"
    named = {name.strip().strip("'") for name in array.group(1).split(",") if name.strip()}
    assert named == set(POLICY_WITHOUT_ORG_ID_BY_DESIGN)

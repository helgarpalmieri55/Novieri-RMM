"""Migration 0077 against the schema `alembic upgrade head` actually produced
(`docs/modules/agent-distribution/spec-push-install.md` §3, §6.3, §8, §14).

READ THIS BEFORE ADDING A TEST HERE.

**THE RLS TESTS ARE BEHAVIOURAL, AND THEY HAVE TO BE.** A test asserting
`relrowsecurity` is true passes against a policy whose predicate was deleted,
so the enable/force check below is a smoke check only. The mutations that
falsify the two behavioural ones are `USING (true)` (read) and
`WITH CHECK (true)` (write); both were run and both outputs are in the task
report. For an `ALL`-command policy Postgres reuses `USING` as `WITH CHECK`
when the latter is omitted, so DELETING the explicit `WITH CHECK` clause
falsifies nothing.

**WHY THAT MATTERS MORE HERE THAN ALMOST ANYWHERE.** A `push_authorization`
row is a statement that this platform may present an administrative credential
inside a prefix. One leaking across tenants would let customer A's grant
authorize an administrative login on customer B's wire -- against a machine
neither has enrolled, using a secret B never scoped. That is the worst
tenancy failure this codebase can express, which is why both directions are
pinned and why the outcome is reduced to a printable string rather than left
as a bare `pytest.raises` (a `raises` that fails prints "DID NOT RAISE", which
says nothing about what the database did; this prints the row that got in).

**THE TYPE TESTS ARE STRUCTURAL ON PURPOSE.** A `text` prefix column passes
every behavioural test written here until the first `10.200.7.5` is judged to
be inside `10.20.0.0/16` because the string starts with `10.20`. Native `cidr`
cannot be asserted behaviourally without writing the containment queries the
services are made of, so `udt_name` is pinned directly.

**THE PARTIAL-INDEX TESTS ARE THE ONES THAT LOOK LIKE PEDANTRY AND ARE NOT.**
A TOTAL unique index on `(org_id, prefix)` passes every create/list test in
this suite and makes a prefix that was ever revoked unauthorizable forever --
discovered by an operator, in production, on the day they re-authorize a
segment. The tests assert the predicate is present AND that the re-grant it
exists for actually works.
"""

from uuid import UUID

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.rbac import PERMISSIONS, PLATFORM_OPERATOR_CODES
from app.core.tenancy import org_session
from tests.agent_push_fixtures import PushOrg, seed_credential, seed_org

TABLES = ["push_authorization", "push_credential_scope"]

CODES = ("agent.push.run", "agent.push.scope.manage")


def _row(table: str, org: PushOrg, credential_id: UUID, prefix: str = "10.9.0.0/24"):
    from app.models import PushAuthorization, PushCredentialScope

    if table == "push_authorization":
        return PushAuthorization(
            org_id=org.org_id, client_id=org.org_id, prefix=prefix, authorized_by=org.user_id
        )
    return PushCredentialScope(
        org_id=org.org_id,
        credential_id=credential_id,
        prefix=prefix,
        bound_by=org.user_id,
    )


# ---------------------------------------------------------------------------
# Tenancy (invariant #5)
# ---------------------------------------------------------------------------


def test_the_module_defines_exactly_two_org_scoped_tables() -> None:
    """Cycle B1 adds two tables and no more (spec §14). Pinned so the RLS
    parametrizations below cannot silently cover one."""
    assert len(TABLES) == 2
    assert len(set(TABLES)) == 2


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    """A smoke check. FORCE matters separately from ENABLE: without it the
    table owner bypasses the policy, and migrations run as the owner."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sqltext(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_read_returns_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `USING (true)`, which hands org A's grant to org B."""
    a = await seed_org(admin_engine, f"rls-ro-a-{table[5:11]}")
    b = await seed_org(admin_engine, f"rls-ro-b-{table[5:11]}")
    credential_id = await seed_credential(admin_engine, a)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_row(table, a, credential_id))
        await session.commit()

    query = sqltext(f"SELECT org_id FROM {table}")  # noqa: S608 -- table is from TABLES
    async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen_by_b = {r.org_id for r in (await session.execute(query)).all()}
    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen_by_a = {r.org_id for r in (await session.execute(query)).all()}

    assert a.org_id not in seen_by_b
    assert seen_by_a == {a.org_id}


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_insert_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `WITH CHECK (true)`, which lets org B write a row that
    says org A's fleet may present administrative credentials somewhere."""
    a = await seed_org(admin_engine, f"rls-wi-a-{table[5:11]}")
    b = await seed_org(admin_engine, f"rls-wi-b-{table[5:11]}")
    credential_id = await seed_credential(admin_engine, a)
    planted = _row(table, a, credential_id)
    try:
        async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            session.add(planted)
            await session.flush()
    except (DBAPIError, ProgrammingError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        async with admin_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext(f"SELECT org_id FROM {table} WHERE org_id = :o"),  # noqa: S608
                    {"o": a.org_id},
                )
            ).all()
        outcome = (
            f"ACCEPTED: org B wrote into {table}; org A ({a.org_id}) now holds "
            f"{len(rows)} row(s) it did not write"
        )
    assert outcome.startswith("refused: "), outcome
    assert "row-level security policy" in outcome, outcome


# ---------------------------------------------------------------------------
# Native network types (spec §3, §6.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [("push_authorization", "prefix"), ("push_credential_scope", "prefix")],
)
async def test_prefixes_are_native_cidr_not_text(
    admin_engine: AsyncEngine, table: str, column: str
) -> None:
    """In `text`, `10.200.7.5 <<= '10.20.0.0/16'` is a string comparison that
    says yes. That is a push to the wrong machine, and no behavioural test in
    this file would show it."""
    async with admin_engine.connect() as conn:
        udt = (
            await conn.execute(
                sqltext(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one()
    assert udt == "cidr"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("push_authorization", "authorized_by"),
        ("push_credential_scope", "bound_by"),
    ],
)
async def test_the_actor_column_is_not_null(
    admin_engine: AsyncEngine, table: str, column: str
) -> None:
    """There is no system-authorized push scope and no system-bound
    credential (spec §3). Falsified by making the column nullable, which is
    exactly how a background job would come to authorize an administrative
    login with nobody's name on it."""
    async with admin_engine.connect() as conn:
        nullable = (
            await conn.execute(
                sqltext(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one()
    assert nullable == "NO"


async def test_an_authorization_with_no_actor_is_refused_by_the_database(
    admin_engine: AsyncEngine,
) -> None:
    """The NOT NULL, observed rather than read off `information_schema`."""
    org = await seed_org(admin_engine, "push-noactor")
    try:
        async with AsyncSession(admin_engine) as session:
            await session.execute(
                sqltext(
                    "INSERT INTO push_authorization"
                    " (id, org_id, client_id, prefix, note, authorized_by, authorized_at)"
                    " VALUES (gen_random_uuid(), :org, :org, '10.0.0.0/24', '', NULL, now())"
                ),
                {"org": org.org_id},
            )
            await session.commit()
    except (IntegrityError, DBAPIError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        outcome = "ACCEPTED: an authorization exists with nobody's name on it"
    assert outcome.startswith("refused: "), outcome


# ---------------------------------------------------------------------------
# The partial unique indexes (spec §3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "predicate"),
    [
        # Per client since migration 0241 (plan 2026-10-26 Task 12).
        ("uq_push_authorization_client_active", "revoked_at IS NULL"),
        ("uq_push_credential_scope_active", "unbound_at IS NULL"),
    ],
)
async def test_the_active_uniqueness_index_is_partial(
    admin_engine: AsyncEngine, index: str, predicate: str
) -> None:
    """A TOTAL unique index passes every create/list test in this suite and
    makes a prefix that was ever revoked unauthorizable forever."""
    async with admin_engine.connect() as conn:
        definition = (
            await conn.execute(
                sqltext("SELECT pg_get_indexdef(indexrelid) FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"),
                {"n": index},
            )
        ).scalar_one()
    assert "UNIQUE" in definition
    assert predicate.lower() in definition.lower(), definition


async def test_a_revoked_prefix_can_be_authorized_again(
    admin_engine: AsyncEngine,
) -> None:
    """THE BEHAVIOUR THE PARTIAL INDEX EXISTS FOR, observed. With a total
    index this raises, and the operator re-authorizing a segment after an
    onboarding window is the person who finds out."""
    from app.models import PushAuthorization

    org = await seed_org(admin_engine, "push-regrant")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = PushAuthorization(
            org_id=org.org_id, client_id=org.org_id, prefix="10.11.0.0/24",
            authorized_by=org.user_id,
        )
        session.add(first)
        await session.commit()
        await session.execute(
            sqltext("UPDATE push_authorization SET revoked_at = now() WHERE id = :i"),
            {"i": first.id},
        )
        await session.commit()
        session.add(
            PushAuthorization(
                org_id=org.org_id, client_id=org.org_id, prefix="10.11.0.0/24",
                authorized_by=org.user_id,
            )
        )
        await session.commit()

    async with admin_engine.connect() as conn:
        total = (
            await conn.execute(
                sqltext(
                    "SELECT count(*) FROM push_authorization "
                    "WHERE org_id = :o AND prefix = '10.11.0.0/24'"
                ),
                {"o": org.org_id},
            )
        ).scalar_one()
    assert total == 2, "the revoked row must survive beside the new grant"


async def test_two_active_grants_for_one_prefix_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    """The other half. Without uniqueness, revoking "the" authorization for a
    prefix leaves a second one nobody was looking at still authorizing it."""
    from app.models import PushAuthorization

    org = await seed_org(admin_engine, "push-dup")
    try:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            session.add(
                PushAuthorization(
                    org_id=org.org_id, client_id=org.org_id, prefix="10.12.0.0/24",
                    authorized_by=org.user_id,
                )
            )
            session.add(
                PushAuthorization(
                    org_id=org.org_id, client_id=org.org_id, prefix="10.12.0.0/24",
                    authorized_by=org.user_id,
                )
            )
            await session.commit()
    except (IntegrityError, DBAPIError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        outcome = "ACCEPTED: one prefix carries two active authorizations"
    assert outcome.startswith("refused: "), outcome


# ---------------------------------------------------------------------------
# The permission codes (spec §8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", CODES)
async def test_the_code_is_seeded(admin_engine: AsyncEngine, code: str) -> None:
    async with admin_engine.connect() as conn:
        found = (
            await conn.execute(
                sqltext("SELECT count(*) FROM permissions WHERE code = :c"), {"c": code}
            )
        ).scalar_one()
    assert found == 1


@pytest.mark.parametrize("code", CODES)
def test_the_code_is_described_in_the_registry(code: str) -> None:
    assert code in PERMISSIONS
    assert len(PERMISSIONS[code]) > 80


@pytest.mark.parametrize("code", CODES)
def test_neither_code_is_a_platform_operator_code(code: str) -> None:
    """Both act on rows carrying an `org_id` under a forced RLS policy, so an
    organisation's own administrator holds them -- unlike
    `agent.release.manage`, which writes the one catalogue with no `org_id`
    that every tenant's endpoints run from (migration 0070)."""
    assert code not in PLATFORM_OPERATOR_CODES


@pytest.mark.parametrize("code", CODES)
async def test_the_code_is_not_seeded_to_technician_auditor_or_read_only(
    admin_engine: AsyncEngine, code: str
) -> None:
    """Spec §8, D8: admin tier deliberately. Loosening a permission later is a
    decision; tightening one is an incident.

    ASSERTED AS AN EXACT ROLE SET rather than "technician does not hold it": a
    seed that granted it to `auditor` would pass the narrower assertion, and an
    auditor able to install code on unmanaged machines is a worse outcome than
    a technician able to.
    """
    async with admin_engine.connect() as conn:
        roles = {
            r.name
            for r in (
                await conn.execute(
                    sqltext(
                        "SELECT r.name FROM roles r"
                        " JOIN role_permissions rp ON rp.role_id = r.id"
                        " JOIN permissions p ON p.id = rp.permission_id"
                        " WHERE p.code = :c AND r.org_id IS NULL"
                    ),
                    {"c": code},
                )
            ).all()
        }
    assert roles == {"super-admin", "org-admin"}, roles


async def test_the_downgrade_does_not_revoke_the_agent_release_codes() -> None:
    """`DELETE FROM permissions WHERE module = 'agent'` would take
    `agent.release.read` and `agent.release.manage` with it -- codes migrations
    0060 and 0070 own -- so `downgrade base` would leave a database that cannot
    install an agent at all. Pinned from the migration's own source because CI
    runs `upgrade head -> downgrade base -> upgrade head` and a wrong DELETE
    here is silent on the way down and fatal on the way back up.
    """
    from pathlib import Path

    import migrations.versions as versions_pkg  # type: ignore[import-untyped]

    source = (
        Path(next(iter(versions_pkg.__path__))) / "0077_agent_push.py"
    ).read_text()
    # COMMENTS STRIPPED, because this file's own downgrade explains in prose
    # why it does not delete by module -- and a substring search over the prose
    # finds the very phrase it is warning about.
    downgrade = "\n".join(
        line
        for line in source.split("def downgrade()", 1)[1].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "module = 'agent'" not in downgrade
    assert "WHERE code IN" in downgrade


def test_the_migration_is_the_head_of_the_chain() -> None:
    """0077 revises 0076. A branch here would make `upgrade head` ambiguous."""
    from pathlib import Path

    import migrations.versions as versions_pkg  # type: ignore[import-untyped]

    source = (
        Path(next(iter(versions_pkg.__path__))) / "0077_agent_push.py"
    ).read_text()
    assert 'revision: str = "0077"' in source
    assert 'down_revision: str | None = "0076"' in source


async def test_push_host_key_has_readiness_column(admin_engine) -> None:
    from sqlalchemy import text

    async with admin_engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'push_host_key' AND column_name = 'readiness'"
                )
            )
        ).all()
    assert cols == [("readiness", "jsonb")]

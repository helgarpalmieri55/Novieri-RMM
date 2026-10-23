"""`ai_autonomy_ceiling` against the schema `alembic upgrade head` produced
(docs/modules/ai-automation.md §4.1, §0.4, §9.1; §13 test 11).

READ THIS BEFORE ADDING A TEST HERE.

1. THE RLS TESTS ARE BEHAVIOURAL. A test asserting `relrowsecurity` is true
   passes against a policy whose predicate was deleted, so
   `test_rls_enabled_and_forced` below is a smoke check and nothing more. For
   an `ALL`-command policy Postgres reuses `USING` as `WITH CHECK` when the
   latter is omitted, so DELETING the `WITH CHECK` clause falsifies nothing.
   The mutations that falsify these are `WITH CHECK (true)` for the insert test
   and `USING (true)` for the read test; both were run and both outputs are in
   the task report.

2. THE CROSS-ORG READ TEST SEEDS BOTH ORGS. A read test that seeds one org and
   asserts the other sees nothing is satisfied by a query returning nothing for
   anybody. This one seeds a NEARLY-MATCHING row in the reading org too --
   same persona, same risk class, different org -- and asserts the reader sees
   exactly its own, so "isolated" and "broken" print different values.

3. THE CONSTRAINT TESTS ASSERT THE STORED ROW, NEVER `pytest.raises` ALONE. A
   `pytest.raises` that fails prints only `DID NOT RAISE`, which says nothing
   about what the table now holds. Every check below SELECTs the row back, so
   a deleted CHECK prints the row Postgres accepted -- an `ai_autonomy_ceiling`
   at `level='autonomous'`, an `end_user` at `require_approval` -- which is
   what a missing constraint actually produces. §13 test 11 is that assertion.

4. A MISSING ROW IS NOT AN ERROR AND MEANS `off`. Asserted behaviourally in
   `tests/test_ai_autonomy.py` against the resolver; named here so nobody adds
   a NOT-FOUND path to the schema tests.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.rbac import PERMISSIONS
from app.core.tenancy import org_session
from tests.test_ai_admin_migration import (
    _admin_insert_outcome,
    _insert_outcome,
    _scalar,
    seed_org_and_user,
)

TABLE = "ai_autonomy_ceiling"

ACT_PERMISSIONS = ("ai.act.propose", "ai.act.confirm")

_INSERT = (
    "INSERT INTO ai_autonomy_ceiling (org_id, persona, risk_class, level, updated_at)"
    " VALUES (:o, :p, :r, :lvl, now())"
)


async def _seed_ceiling(
    api_env: Any, org_id: UUID, *, persona: str, risk_class: str, level: str
) -> None:
    """A ceiling row, written on the RLS-bypassing admin engine.

    Exported for the later tasks in this layer's plan -- Task 5's resolver
    tests, Task 7's `off`/`suggest`/`require_approval` cases and Task 8's
    "lower the dial between preview and confirm" case all need one, and three
    private copies would be three chances to seed a row the service does not
    read.
    """
    from app.models import AiAutonomyCeiling

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            AiAutonomyCeiling(
                org_id=org_id, persona=persona, risk_class=risk_class, level=level
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# RLS
# --------------------------------------------------------------------------- #


async def test_rls_enabled_and_forced(admin_engine: AsyncEngine) -> None:
    """Smoke check only -- see the module docstring, point 1."""
    row = await _scalar(
        admin_engine,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relname = :t",
        t=TABLE,
    )
    assert row is True, f"{TABLE} is not ENABLE + FORCE ROW LEVEL SECURITY"


async def test_tenant_isolation_policy_has_both_clauses(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                sqltext(
                    "SELECT qual, with_check FROM pg_policies"
                    " WHERE tablename = :t AND policyname = 'tenant_isolation'"
                ),
                {"t": TABLE},
            )
        ).one_or_none()
    assert row is not None, f"{TABLE} has no tenant_isolation policy"
    assert row.qual and row.with_check, f"{TABLE}: {row!r}"


async def test_cross_org_read_is_isolated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Falsified by `USING (true)`, which makes `seen` carry both levels."""
    org_a, _ = await seed_org_and_user(admin_engine, "ceil-a")
    org_b, _ = await seed_org_and_user(admin_engine, "ceil-b")
    # NEARLY MATCHING: same persona, same risk class, different org and level.
    await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_a, p="admin", r="high", lvl="require_approval"
    )
    await _admin_insert_outcome(admin_engine, _INSERT, o=org_b, p="admin", r="high", lvl="suggest")

    async with org_session(org_b, engine=app_engine) as session:
        seen = [
            (r[0], r[1])
            for r in (
                await session.execute(sqltext("SELECT org_id, level FROM ai_autonomy_ceiling"))
            ).all()
        ]

    assert seen == [(org_b, "suggest")], seen


async def test_cross_org_insert_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Falsified by `WITH CHECK (true)`, which makes the outcome `ok` and the
    stored count 1."""
    org_a, _ = await seed_org_and_user(admin_engine, "ceil-ins-a")
    org_b, _ = await seed_org_and_user(admin_engine, "ceil-ins-b")

    outcome = await _insert_outcome(
        app_engine, org_b, _INSERT, o=org_a, p="admin", r="low", lvl="suggest"
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_autonomy_ceiling WHERE org_id = :o",
        o=org_a,
    )
    assert (outcome, stored) == ("refused: InsufficientPrivilege", 0)


# --------------------------------------------------------------------------- #
# The four CHECK classes. Each asserts the STORED ROW as well as the outcome.
# --------------------------------------------------------------------------- #


async def test_autonomous_is_unstorable(admin_engine: AsyncEngine) -> None:
    """§13 test 11, and the most important assertion in this file.

    §0.4's whole claim is that the AI personas' vocabulary stops one value
    short of the catalogue's, ENFORCED BY THE DATABASE rather than by
    application code. The observation if the CHECK were widened to include
    `'autonomous'` is not a missing exception -- it is an
    `ai_autonomy_ceiling` row at `level='autonomous'` that this test SELECTs
    back and prints.
    """
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-auto")
    outcome = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p="admin", r="low", lvl="autonomous"
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_autonomy_ceiling WHERE org_id = :o AND level = 'autonomous'",
        o=org_id,
    )
    assert (outcome, stored) == ("refused: CheckViolation", 0)


@pytest.mark.parametrize(
    ("level", "accepted"),
    [("off", True), ("suggest", False), ("require_approval", False), ("autonomous", False)],
)
async def test_end_user_cannot_be_raised_above_off(
    admin_engine: AsyncEngine, level: str, accepted: bool
) -> None:
    """§3.1, closed by this build as a fourth CHECK and recorded into §4.1.

    Deliberately parametrized so the cases do NOT all expect the same answer:
    `off` must still be storable, or a CHECK reading `persona <> 'end_user'`
    would pass this test while making the row unrepresentable entirely.
    """
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-eu")
    outcome = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p="end_user", r="low", lvl=level
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_autonomy_ceiling WHERE org_id = :o AND persona = 'end_user'"
        " AND level = :lvl",
        o=org_id,
        lvl=level,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


@pytest.mark.parametrize(
    ("persona", "accepted"),
    [("admin", True), ("technician", True), ("end_user", True), ("requester", False)],
)
async def test_persona_check(admin_engine: AsyncEngine, persona: str, accepted: bool) -> None:
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-persona")
    # `end_user` is only storable at `off` (the fourth CHECK), so the level
    # varies with the persona; the question here is the PERSONA vocabulary.
    level = "off" if persona == "end_user" else "suggest"
    outcome = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p=persona, r="low", lvl=level
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_autonomy_ceiling WHERE org_id = :o AND persona = :p",
        o=org_id,
        p=persona,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


@pytest.mark.parametrize(
    ("risk_class", "accepted"),
    [("low", True), ("medium", True), ("high", True), ("critical", False)],
)
async def test_risk_class_check(
    admin_engine: AsyncEngine, risk_class: str, accepted: bool
) -> None:
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-risk")
    outcome = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p="admin", r=risk_class, lvl="suggest"
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_autonomy_ceiling WHERE org_id = :o AND risk_class = :r",
        o=org_id,
        r=risk_class,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


async def test_primary_key_rejects_a_duplicate(admin_engine: AsyncEngine) -> None:
    """One row per `(org_id, persona, risk_class)`. This is a real PK, not a
    partial index, which is why `set_ceiling`'s upsert may name the columns."""
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-pk")
    first = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p="technician", r="medium", lvl="suggest"
    )
    second = await _admin_insert_outcome(
        admin_engine, _INSERT, o=org_id, p="technician", r="medium", lvl="require_approval"
    )
    stored = await _scalar(
        admin_engine,
        "SELECT level FROM ai_autonomy_ceiling WHERE org_id = :o AND persona = 'technician'",
        o=org_id,
    )
    assert (first, second, stored) == ("ok", "refused: UniqueViolation", "suggest")


# --------------------------------------------------------------------------- #
# The two permission codes (§9.2, §16).
# --------------------------------------------------------------------------- #


async def test_the_act_permission_codes_are_registered_and_seeded(
    admin_engine: AsyncEngine,
) -> None:
    for code in ACT_PERMISSIONS:
        assert code in PERMISSIONS, code
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM permissions WHERE code IN ('ai.act.propose', 'ai.act.confirm')",
    )
    assert stored == len(ACT_PERMISSIONS)


async def test_confirm_carries_high_privilege_wording_and_names_the_second_check() -> None:
    """§9.2: `ai.act.confirm` is NOT sufficient on its own, and the description
    is where an admin granting it finds that out."""
    text = PERMISSIONS["ai.act.confirm"]
    assert "HIGH PRIVILEGE" in text
    assert "NOT sufficient" in text
    assert "dispatch" in text
    # And `ai.act.propose` says what it does not grant.
    assert "NO ability to confirm or dispatch" in PERMISSIONS["ai.act.propose"]


async def test_the_grants_match_section_16(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§16: propose to the technician tier, confirm to the admin tier.

    Both halves, because a migration that granted BOTH to `technician` would
    make `ai.act.confirm` a skeleton key for the tier §9.2 keeps it away from,
    and a test that only asserted the positive would not see it.
    """
    from app.core.rbac import has_permission
    from tests.test_rbac import _seed_user_with_role

    org_id, tech_id, _ = await _seed_user_with_role(admin_engine, "technician", "ai-act-t")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, tech_id, "ai.act.propose")
        assert not await has_permission(session, tech_id, "ai.act.confirm")

    org_id, admin_id, _ = await _seed_user_with_role(admin_engine, "org-admin", "ai-act-a")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, admin_id, "ai.act.propose")
        assert await has_permission(session, admin_id, "ai.act.confirm")


def test_the_layer_ships_inert() -> None:
    """§16 seeds NO ceiling rows: a missing row means `off`, so an org that has
    not turned this on cannot have an AI persona propose anything.

    ASSERTED ON THE MIGRATION SOURCE, not on a `count(*)`. The test database is
    shared across the whole session and other tests in this very file seed
    ceilings, so a count would be asserting "no test ran before me" -- which is
    both false and not the property. What has to be true is that migration 0048
    writes none, and that is a statement about the file.
    """
    from pathlib import Path

    source = Path("migrations/versions/0048_ai_autonomy_ceiling.py").read_text()
    assert "ai_autonomy_ceiling" in source, "wrong migration file; this check is vacuous"
    lowered = source.lower()
    assert "insert into ai_autonomy_ceiling" not in lowered
    # And the seeding it DOES do is the two permission codes and nothing else.
    inserts = [line for line in lowered.splitlines() if "insert into" in line]
    assert inserts, "the migration inserts nothing at all; this check is vacuous"
    assert all(
        "permissions" in line or "role_permissions" in line for line in inserts
    ), inserts


async def test_a_missing_row_is_not_an_error(admin_engine: AsyncEngine) -> None:
    """Named here so nobody adds a NOT-FOUND path; the behavioural half (a
    missing row resolves to `off`) is `tests/test_ai_autonomy.py`."""
    org_id, _ = await seed_org_and_user(admin_engine, "ceil-missing")
    got = await _scalar(
        admin_engine,
        "SELECT level FROM ai_autonomy_ceiling WHERE org_id = :o AND persona = 'admin'"
        " AND risk_class = 'low'",
        o=org_id,
    )
    assert got is None


def test_the_seed_helper_is_importable() -> None:
    """`_seed_ceiling` is this task's stated interface to the later ones; a
    rename that broke it would otherwise only surface three tasks later."""
    assert callable(_seed_ceiling)

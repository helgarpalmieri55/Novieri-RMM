"""`ai_action` against the schema `alembic upgrade head` actually produced
(docs/modules/ai-automation.md §5.1, §5.2, §6.4, §6.5).

READ THIS BEFORE ADDING A TEST HERE. The rules are `test_ai_admin_migration.py`'s
and they are inherited deliberately:

1. THE RLS TESTS ARE BEHAVIOURAL. `relrowsecurity` being true says nothing about
   the predicate. For an `ALL`-command policy Postgres reuses `USING` as
   `WITH CHECK` when the latter is omitted, so deleting `WITH CHECK` falsifies
   nothing; the mutations that falsify these are `WITH CHECK (true)` for the
   insert test and `USING (true)` for the read test, and both were run.

2. THE CROSS-ORG READ TEST SEEDS BOTH ORGS, so `[]` and "somebody else's row"
   print different values.

3. **EVERY CONSTRAINT TEST ASSERTS THE STORED ROW, NEVER `pytest.raises` ALONE.**
   This matters most for the `ON DELETE RESTRICT` test: `pytest.raises` around
   a DELETE reports `DID NOT RAISE` and tells a reader nothing about whether the
   `ai_action` row is still there. Flipping RESTRICT to CASCADE makes the DELETE
   SUCCEED and the row VANISH -- and the assertion that observes that is the one
   on the surviving row, not the one on the exception.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_org
from tests.test_ai_admin_migration import (
    _admin_insert_outcome,
    _insert_outcome,
    _scalar,
    seed_session,
)

TABLE = "ai_action"

_INSERT = (
    "INSERT INTO ai_action (id, org_id, session_id, message_seq, persona, approved_action_id,"
    " approved_action_key, job_type, target_device_id, rendered_params, risk_class, destructive,"
    " effective_autonomy, preview_digest, state, expires_at, created_at, updated_at)"
    " VALUES (:id, :org, :sess, 1, :persona, :aa, :key, 'inventory.collect', :dev,"
    " '{}'::jsonb, :risk, false, :autonomy, :digest, :state, now() + interval '15 minutes',"
    " now(), now())"
)


class _Refs:
    def __init__(self, org_id: UUID, user_id: UUID, device_id: UUID, session_id: UUID,
                 action_id: UUID) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.device_id = device_id
        self.session_id = session_id
        self.action_id = action_id


async def _refs(engine: AsyncEngine, tag: str) -> _Refs:
    """One org with everything an `ai_action` row needs to exist at all."""
    org = await seed_org(engine, tag)
    session_id = await seed_session(engine, org.org_id, org.user_id)
    action_id = await seed_approved_action(engine, org.org_id, org.user_id)
    return _Refs(org.org_id, org.user_id, org.device_id, session_id, action_id)


def _params(refs: _Refs, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid4(),
        "org": refs.org_id,
        "sess": refs.session_id,
        "persona": "admin",
        "aa": refs.action_id,
        "dev": refs.device_id,
        "risk": "low",
        "autonomy": "require_approval",
        "digest": "0" * 64,
        "state": "previewed",
        "key": "k",
    }
    values.update(kw)
    return values


async def _seed_ai_action(engine: AsyncEngine, refs: _Refs, **kw: Any) -> UUID:
    """An `ai_action` row on the admin engine. Exported for the later tasks."""
    params = _params(refs, **kw)
    outcome = await _admin_insert_outcome(engine, _INSERT, **params)
    assert outcome == "ok", outcome
    return UUID(str(params["id"]))


# --------------------------------------------------------------------------- #
# 1. RLS
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
    """Falsified by `USING (true)`, which makes `seen` carry both keys."""
    a = await _refs(admin_engine, "act-r-a")
    b = await _refs(admin_engine, "act-r-b")
    await _seed_ai_action(admin_engine, a, approved_action_key="theirs")
    mine = await _seed_ai_action(admin_engine, b)

    async with org_session(b.org_id, engine=app_engine) as session:
        seen = [r[0] for r in (await session.execute(sqltext("SELECT id FROM ai_action"))).all()]
    assert seen == [mine], seen


async def test_cross_org_insert_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Falsified by `WITH CHECK (true)`, which makes the outcome `ok` and the
    stored count 1."""
    a = await _refs(admin_engine, "act-i-a")
    b = await _refs(admin_engine, "act-i-b")

    outcome = await _insert_outcome(app_engine, b.org_id, _INSERT, **_params(a))
    stored = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_action WHERE org_id = :o", o=a.org_id
    )
    assert (outcome, stored) == ("refused: InsufficientPrivilege", 0)


# --------------------------------------------------------------------------- #
# 2. The CHECK vocabulary. Stored row asserted, never `pytest.raises` alone.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("proposed", True),
        ("previewed", True),
        ("dispatched", True),
        ("rejected", True),
        # Not a state in §5.2's machine. Live negative, so the parametrization
        # is not "everything is accepted".
        ("previewing", False),
        ("approved", False),
    ],
)
async def test_state_check(admin_engine: AsyncEngine, state: str, accepted: bool) -> None:
    refs = await _refs(admin_engine, "act-state")
    params = _params(refs, state=state)
    outcome = await _admin_insert_outcome(admin_engine, _INSERT, **params)
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_action WHERE org_id = :o AND state = :s",
        o=refs.org_id,
        s=state,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


@pytest.mark.parametrize(
    ("autonomy", "accepted"),
    [
        ("suggest", True),
        ("require_approval", True),
        # A stored `off` is a row that should never have been minted...
        ("off", False),
        # ...and `autonomous` is unreachable by an AI persona in v1 (§0.4).
        ("autonomous", False),
    ],
)
async def test_effective_autonomy_check(
    admin_engine: AsyncEngine, autonomy: str, accepted: bool
) -> None:
    refs = await _refs(admin_engine, "act-auto")
    outcome = await _admin_insert_outcome(admin_engine, _INSERT, **_params(refs, autonomy=autonomy))
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_action WHERE org_id = :o AND effective_autonomy = :a",
        o=refs.org_id,
        a=autonomy,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


@pytest.mark.parametrize(
    ("persona", "accepted"),
    [("admin", True), ("technician", True), ("end_user", True), ("requester", False)],
)
async def test_persona_check(admin_engine: AsyncEngine, persona: str, accepted: bool) -> None:
    refs = await _refs(admin_engine, "act-persona")
    outcome = await _admin_insert_outcome(admin_engine, _INSERT, **_params(refs, persona=persona))
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_action WHERE org_id = :o AND persona = :p",
        o=refs.org_id,
        p=persona,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


async def test_target_device_id_and_expires_at_are_not_nullable(
    admin_engine: AsyncEngine,
) -> None:
    """§0.12: one row, one device -- a NULL target would be a row that names no
    machine. And §6.4: an `ai_action` with no clock is a confirmation token that
    never expires."""
    refs = await _refs(admin_engine, "act-null")
    no_device = _INSERT.replace(":dev,", "NULL,")
    no_expiry = _INSERT.replace("now() + interval '15 minutes'", "NULL")
    params = _params(refs)
    params.pop("dev")
    assert await _admin_insert_outcome(admin_engine, no_device, **params) == (
        "refused: NotNullViolation"
    )
    assert await _admin_insert_outcome(admin_engine, no_expiry, **_params(refs)) == (
        "refused: NotNullViolation"
    )
    stored = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_action WHERE org_id = :o", o=refs.org_id
    )
    assert stored == 0


# --------------------------------------------------------------------------- #
# 3. RESTRICT, and the asymmetry with `ai_message`'s CASCADE.
# --------------------------------------------------------------------------- #


async def test_deleting_a_session_with_a_dispatched_action_is_refused_and_the_row_survives(
    admin_engine: AsyncEngine,
) -> None:
    """§5.1. **THE ASSERTION IS THE SURVIVING ROW, NOT THE EXCEPTION.**

    Flipping `ondelete="RESTRICT"` to `"CASCADE"` makes the DELETE succeed, so a
    test that only asserted "it raised" would report `DID NOT RAISE` and say
    nothing about what the database now holds -- which is: an `ai_action` naming
    a signed job that ran on somebody's machine, gone.
    """
    refs = await _refs(admin_engine, "act-restrict")
    action_row = await _seed_ai_action(admin_engine, refs, state="dispatched")

    outcome = await _admin_insert_outcome(
        admin_engine, "DELETE FROM ai_session WHERE id = :s", s=refs.session_id
    )
    survived = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_action WHERE id = :i", i=action_row
    )
    session_survived = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_session WHERE id = :s", s=refs.session_id
    )
    assert (outcome, survived, session_survived) == ("refused: ForeignKeyViolation", 1, 1)


async def test_restrict_is_not_conditional_on_the_state(admin_engine: AsyncEngine) -> None:
    """The honest cost, asserted rather than discovered later: a session whose
    actions are ALL `rejected` cannot be hard-deleted either. Task 9's retention
    design depends on knowing this, and the existing transcript sweep will now
    raise on such a session unless it handles it."""
    refs = await _refs(admin_engine, "act-restrict-2")
    await _seed_ai_action(admin_engine, refs, state="rejected")

    outcome = await _admin_insert_outcome(
        admin_engine, "DELETE FROM ai_session WHERE id = :s", s=refs.session_id
    )
    assert outcome == "refused: ForeignKeyViolation"


async def test_ai_message_still_cascades_from_the_same_parent(
    admin_engine: AsyncEngine,
) -> None:
    """The other half of the asymmetry, asserted SIDE BY SIDE so a reader sees
    both: a transcript is deletable because it is not audit."""
    refs = await _refs(admin_engine, "act-cascade")
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 'user', 'x', '{}'::jsonb, now())",
        o=refs.org_id,
        s=refs.session_id,
    )
    # No `ai_action` on this session, so nothing RESTRICTs the delete.
    outcome = await _admin_insert_outcome(
        admin_engine, "DELETE FROM ai_session WHERE id = :s", s=refs.session_id
    )
    messages = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_message WHERE session_id = :s", s=refs.session_id
    )
    assert (outcome, messages) == ("ok", 0)


# --------------------------------------------------------------------------- #
# 4. The two new `ai_settings` tunables (§6.4, §6.5).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("column", "value", "accepted"),
    [
        # Both bounds, and one past each -- a CHECK written the wrong way round
        # is visible only if both ends are asserted.
        ("action_expiry_s", 60, True),
        ("action_expiry_s", 86400, True),
        ("action_expiry_s", 59, False),
        ("action_expiry_s", 86401, False),
        ("max_proposals_per_turn", 1, True),
        ("max_proposals_per_turn", 10, True),
        ("max_proposals_per_turn", 0, False),
        ("max_proposals_per_turn", 11, False),
    ],
)
async def test_ai_settings_new_ceilings(
    admin_engine: AsyncEngine, column: str, value: int, accepted: bool
) -> None:
    org = await seed_org(admin_engine, "act-settings")
    outcome = await _admin_insert_outcome(
        admin_engine,
        f"INSERT INTO ai_settings (org_id, {column}, updated_at) VALUES (:o, :v, now())",
        o=org.org_id,
        v=value,
    )
    stored = await _scalar(
        admin_engine,
        f"SELECT {column} FROM ai_settings WHERE org_id = :o",
        o=org.org_id,
    )
    assert (outcome, stored) == (("ok", value) if accepted else ("refused: CheckViolation", None))


async def test_the_new_tunables_default_to_the_documented_values(
    admin_engine: AsyncEngine,
) -> None:
    """§6.4's 15 minutes and §6.5's 3, asserted on a row that never mentions
    them -- so a changed `server_default` prints the value it changed to."""
    org = await seed_org(admin_engine, "act-defaults")
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_settings (org_id, updated_at) VALUES (:o, now())",
        o=org.org_id,
    )
    row = await _scalar(
        admin_engine,
        "SELECT action_expiry_s || '/' || max_proposals_per_turn FROM ai_settings"
        " WHERE org_id = :o",
        o=org.org_id,
    )
    assert row == "900/3"


# --------------------------------------------------------------------------- #
# 5. The indexes, on both sides.
# --------------------------------------------------------------------------- #


async def test_the_three_indexes_exist_in_the_live_schema(admin_engine: AsyncEngine) -> None:
    """`tests/test_schema_drift.py` compares metadata to the live schema in both
    directions; this names the three so a failure there is legible."""
    async with AsyncSession(admin_engine) as session:
        names = {
            r[0]
            for r in (
                await session.execute(
                    sqltext("SELECT indexname FROM pg_indexes WHERE tablename = 'ai_action'")
                )
            ).all()
        }
    assert {
        "ix_ai_action_org_state_expiry",
        "ix_ai_action_org_session",
        "ix_ai_action_org_device",
    } <= names, sorted(names)


def test_the_model_declares_the_same_three_indexes() -> None:
    from app.models import AiAction

    assert {index.name for index in AiAction.__table__.indexes} == {
        "ix_ai_action_org_state_expiry",
        "ix_ai_action_org_session",
        "ix_ai_action_org_device",
    }


def test_ai_action_carries_no_unique_constraint(admin_engine: AsyncEngine) -> None:
    """§0.5, as a property rather than as a comment. A builder adding one "for
    dedupe" reaches for a PARTIAL index over nullable columns; NULLs do not
    collide, and `ON CONFLICT ON CONSTRAINT <partial index>` fails at runtime.
    The state machine is what makes a second confirm a 409."""
    from app.models import AiAction

    uniques = [
        c
        for c in AiAction.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert uniques == [], uniques
    assert all(not index.unique for index in AiAction.__table__.indexes)

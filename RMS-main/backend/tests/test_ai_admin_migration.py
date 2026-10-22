"""The four `ai_admin` tables against the schema `alembic upgrade head`
actually produced (docs/modules/ai-admin/spec.md §3.1-§3.4, §0.10, §0.11).

READ THIS BEFORE ADDING A TEST HERE.

1. THE RLS TESTS ARE BEHAVIOURAL. A test asserting `relrowsecurity` is true
   passes against a policy whose predicate was deleted, so the enable/force
   check below is a smoke check and nothing more. For an `ALL`-command policy
   Postgres reuses `USING` as `WITH CHECK` when the latter is omitted, so
   DELETING the `WITH CHECK` clause falsifies nothing. The mutations that
   falsify these are `WITH CHECK (true)` for the insert tests and
   `USING (true)` for the read tests; both were run and both outputs are in
   the task report.

2. THE CROSS-ORG READ TESTS SEED BOTH ORGS. A read test that seeds one org and
   asserts the other sees nothing is satisfied by a query that returns nothing
   for anybody -- a typo'd table name, a `WHERE false`, an empty database. Each
   read test here seeds a NEARLY-MATCHING row in the reading org too and
   asserts the reader sees exactly its own id, so "isolated" and "broken" print
   different values.

3. THE CONSTRAINT TESTS ASSERT THE STORED ROW, NEVER `pytest.raises` ALONE. A
   `pytest.raises` test that fails prints only `DID NOT RAISE`. Every check
   here also SELECTs the table back afterwards, so a deleted CHECK prints the
   row Postgres accepted -- an `ai_message` with `role='tool'`, an `ai_session`
   with `mode='read_act'` -- which is what a missing constraint actually
   produces.

4. THERE IS NO `enabled` COLUMN AND NO `provider_key` COLUMN, AND THAT IS A
   SCHEMA PROPERTY (§0.14, the operator's decision of 2026-08-25; migration
   `0074`). This point used to say the opposite -- `enabled` DEFAULTS FALSE,
   "the surface ships off" -- and the test that carried it,
   `test_settings_enabled_defaults_false`, guarded a one-character change to
   the server_default. The decision is now that the assistant ships ON and the
   model is the same for every tenant, so the guard moved to the columns' own
   absence: `test_settings_carries_no_kill_switch_and_no_provider_column` reads
   `information_schema`, because a re-added column with any default at all
   re-creates the per-org switch the decision removed.

5. THE `ai_settings` CEILINGS ARE THIS BUILD'S ADDITION, recorded in spec §3.4.
   §3.4 gave four tunables defaults and no bounds while making every write
   `ai.admin`; without CHECKs an org-admin can set
   `max_turns_per_org_per_hour = 10_000_000` and
   `transcript_retention_days = 36500`, which turns §10.3's rate limit and
   §0.7's retention trade into decoration. Each ceiling test asserts BOTH that
   the out-of-range value was refused AND that the in-range boundary was
   accepted, so a CHECK written the wrong way round is visible.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.rbac import PERMISSIONS
from app.core.tenancy import org_session

TABLES = ("ai_session", "ai_message", "ai_injection_flag", "ai_settings")

AI_PERMISSIONS = ("ai.use", "ai.admin")


# --------------------------------------------------------------------------- #
# Seeding helpers. Exported for the later tasks in this module's plan -- they
# take an engine rather than an `api_env` so they are usable from a pure-schema
# test that never builds the app.
# --------------------------------------------------------------------------- #


async def seed_org_and_user(admin_engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    """An organization and a user row in it, via the RLS-bypassing admin engine."""
    from app.models import Organization, User

    slug = f"ai-{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(
            org_id=org.id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"{slug}@test.local",
        )
        session.add(user)
        await session.commit()
        return org.id, user.id


async def seed_session(
    admin_engine: AsyncEngine, org_id: UUID, user_id: UUID, **kw: Any
) -> UUID:
    from app.models.ai import AiSession

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = AiSession(org_id=org_id, user_id=user_id, title="t", model_id="m", **kw)
        session.add(row)
        await session.commit()
        return row.id


async def seed_settings(admin_engine: AsyncEngine, org_id: UUID, **kw: Any) -> None:
    from app.models.ai import AiSettings

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(AiSettings(org_id=org_id, **kw))
        await session.commit()


# Postgres SQLSTATEs, not exception class names: the asyncpg dialect wraps every
# driver error in one DBAPI-shim class, so `type(exc.orig).__name__` reports
# "IntegrityError" for a unique violation AND for a check violation -- a test
# comparing that string cannot tell "the CHECK rejected it" from "the UNIQUE
# rejected it", which is exactly the distinction these tests are for.
_SQLSTATES = {
    "23505": "UniqueViolation",
    "23514": "CheckViolation",
    "23503": "ForeignKeyViolation",
    "42501": "InsufficientPrivilege",  # what an RLS WITH CHECK refusal is
    "23502": "NotNullViolation",
}


def _refusal(exc: BaseException) -> str:
    err: BaseException | None = exc
    while err is not None:
        state = getattr(err, "sqlstate", None) or getattr(err, "pgcode", None)
        if state:
            return f"refused: {_SQLSTATES.get(state, state)}"
        err = err.__cause__
    return f"refused: {type(exc).__name__}"


async def _insert_outcome(engine: AsyncEngine, org_id: UUID, sql: str, **params: Any) -> str:
    """Run `sql` in `org_id`'s tenant session and reduce both worlds to a
    comparable string: `"ok"` when the database accepted it, `"refused: <sqlstate>"`
    when it refused. Compared instead of `pytest.raises` so a failure prints
    what actually happened rather than `DID NOT RAISE`."""
    try:
        async with org_session(org_id, engine=engine) as session:
            await session.execute(sqltext(sql), params)
    except DBAPIError as exc:  # IntegrityError / ProgrammingError both derive
        return _refusal(exc.orig or exc)
    return "ok"


async def _admin_insert_outcome(engine: AsyncEngine, sql: str, **params: Any) -> str:
    """As `_insert_outcome`, on the RLS-bypassing admin engine -- for CHECK and
    UNIQUE tests, where RLS must not be the thing doing the rejecting."""
    try:
        async with AsyncSession(engine) as session:
            await session.execute(sqltext(sql), params)
            await session.commit()
    except DBAPIError as exc:
        return _refusal(exc.orig or exc)
    return "ok"


async def _scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with AsyncSession(engine) as session:
        return (await session.execute(sqltext(sql), params)).scalar()


# --------------------------------------------------------------------------- #
# RLS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", TABLES)
async def test_rls_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    """Smoke check only -- see the module docstring, point 1."""
    row = await _scalar(
        admin_engine,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relname = :t",
        t=table,
    )
    assert row is True, f"{table} is not ENABLE + FORCE ROW LEVEL SECURITY"


@pytest.mark.parametrize("table", TABLES)
async def test_tenant_isolation_policy_has_both_clauses(
    admin_engine: AsyncEngine, table: str
) -> None:
    """Structural: the policy exists and states WITH CHECK explicitly. This
    proves nothing about the predicate -- the two tests below do that."""
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                sqltext(
                    "SELECT qual, with_check FROM pg_policies"
                    " WHERE tablename = :t AND policyname = 'tenant_isolation'"
                ),
                {"t": table},
            )
        ).one_or_none()
    assert row is not None, f"{table} has no tenant_isolation policy"
    assert row.qual and row.with_check, f"{table}: {row!r}"


async def test_cross_org_read_of_ai_session_is_isolated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, user_b = await seed_org_and_user(admin_engine, "b")
    await seed_session(admin_engine, org_a, user_a)
    mine = await seed_session(admin_engine, org_b, user_b)

    async with org_session(org_b, engine=app_engine) as session:
        seen = [r[0] for r in (await session.execute(sqltext("SELECT id FROM ai_session"))).all()]

    # Seeded in BOTH orgs: `[mine]` and `[]` are different failures, so an
    # empty result cannot be mistaken for isolation (docstring point 2).
    assert seen == [mine]


async def test_cross_org_read_of_ai_message_is_isolated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, user_b = await seed_org_and_user(admin_engine, "b")
    sess_a = await seed_session(admin_engine, org_a, user_a)
    sess_b = await seed_session(admin_engine, org_b, user_b)
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 'user', 'theirs', '{}'::jsonb, now())",
        o=org_a,
        s=sess_a,
    )
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 'user', 'mine', '{}'::jsonb, now())",
        o=org_b,
        s=sess_b,
    )

    async with org_session(org_b, engine=app_engine) as session:
        seen = [
            r[0] for r in (await session.execute(sqltext("SELECT content FROM ai_message"))).all()
        ]
    assert seen == ["mine"]


async def test_cross_org_read_of_ai_injection_flag_is_isolated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, user_b = await seed_org_and_user(admin_engine, "b")
    sess_a = await seed_session(admin_engine, org_a, user_a)
    sess_b = await seed_session(admin_engine, org_b, user_b)
    for org_id, sess_id, excerpt in ((org_a, sess_a, "theirs"), (org_b, sess_b, "mine")):
        await _admin_insert_outcome(
            admin_engine,
            "INSERT INTO ai_injection_flag (id, org_id, session_id, message_seq, source_tool,"
            " source_type, source_id, matched_rule, excerpt, created_at)"
            " VALUES (gen_random_uuid(), :o, :s, 1, 'health.alerts', 'ticket', 'x',"
            " 'redirection', :e, now())",
            o=org_id,
            s=sess_id,
            e=excerpt,
        )

    async with org_session(org_b, engine=app_engine) as session:
        seen = [
            r[0]
            for r in (
                await session.execute(sqltext("SELECT excerpt FROM ai_injection_flag"))
            ).all()
        ]
    assert seen == ["mine"]


async def test_cross_org_read_of_ai_settings_is_isolated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, _ = await seed_org_and_user(admin_engine, "a")
    org_b, _ = await seed_org_and_user(admin_engine, "b")
    # **THE DISCRIMINATOR CHANGED, NOT THE PROPERTY.** This read `provider_key`
    # until migration `0074` dropped that column (spec §0.14). Retention is now
    # the per-org value with two distinguishable settings, and RLS is still what
    # this test is about: the write is on the BYPASSRLS admin engine and the
    # read on the app engine, so a policy that stopped filtering shows up as two
    # rows here.
    await seed_settings(admin_engine, org_a, transcript_retention_days=111)
    await seed_settings(admin_engine, org_b, transcript_retention_days=222)

    async with org_session(org_b, engine=app_engine) as session:
        seen = [
            r[0]
            for r in (
                await session.execute(
                    sqltext("SELECT transcript_retention_days FROM ai_settings")
                )
            ).all()
        ]
    assert seen == [222]


async def test_cross_org_insert_into_ai_session_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, _ = await seed_org_and_user(admin_engine, "b")
    # Raw SQL with no RETURNING, so only the policy's WITH CHECK can reject
    # this -- an ORM insert's RETURNING is caught by the SELECT policy instead
    # and would pass against `WITH CHECK (true)`.
    outcome = await _insert_outcome(
        app_engine,
        org_b,
        "INSERT INTO ai_session (id, org_id, user_id, persona, mode, title, model_id,"
        " created_at, last_turn_at, message_count, total_input_tokens, total_output_tokens)"
        " VALUES (gen_random_uuid(), :o, :u, 'admin', 'read', 't', 'm', now(), now(), 0, 0, 0)",
        o=org_a,
        u=user_a,
    )
    planted = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_session WHERE org_id = :o", o=org_a
    )
    assert (outcome, planted) == ("refused: InsufficientPrivilege", 0)


async def test_cross_org_insert_into_ai_message_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, _ = await seed_org_and_user(admin_engine, "b")
    sess_a = await seed_session(admin_engine, org_a, user_a)
    outcome = await _insert_outcome(
        app_engine,
        org_b,
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 'user', 'x', '{}'::jsonb, now())",
        o=org_a,
        s=sess_a,
    )
    planted = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_message WHERE org_id = :o", o=org_a
    )
    assert (outcome, planted) == ("refused: InsufficientPrivilege", 0)


async def test_cross_org_insert_into_ai_injection_flag_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await seed_org_and_user(admin_engine, "a")
    org_b, _ = await seed_org_and_user(admin_engine, "b")
    sess_a = await seed_session(admin_engine, org_a, user_a)
    outcome = await _insert_outcome(
        app_engine,
        org_b,
        "INSERT INTO ai_injection_flag (id, org_id, session_id, message_seq, source_tool,"
        " source_type, source_id, matched_rule, excerpt, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 't', 'ticket', 'x', 'redirection', 'e', now())",
        o=org_a,
        s=sess_a,
    )
    planted = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_injection_flag WHERE org_id = :o", o=org_a
    )
    assert (outcome, planted) == ("refused: InsufficientPrivilege", 0)


async def test_cross_org_insert_into_ai_settings_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, _ = await seed_org_and_user(admin_engine, "a")
    org_b, _ = await seed_org_and_user(admin_engine, "b")
    outcome = await _insert_outcome(
        app_engine,
        org_b,
        "INSERT INTO ai_settings (org_id, transcript_retention_days,"
        " max_turns_per_user_per_minute, max_turns_per_org_per_hour, updated_at)"
        " VALUES (:o, 30, 6, 200, now())",
        o=org_a,
    )
    planted = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_settings WHERE org_id = :o", o=org_a
    )
    assert (outcome, planted) == ("refused: InsufficientPrivilege", 0)


# --------------------------------------------------------------------------- #
# CHECK constraints (§3.1, §3.2, §12.2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("column", "value", "accepted"),
    [
        ("persona", "admin", True),
        # Widened by 0048 (docs/modules/ai-automation.md §5.3, §10.3). THE
        # TABLE KEEPS LIVE NEGATIVES: a parametrization whose every case
        # expects `True` proves nothing about a CHECK, because deleting the
        # constraint entirely would pass it.
        ("persona", "technician", True),
        ("persona", "end_user", True),
        # Still refused: not a persona of this layer. The portal principal's
        # persona name is `end_user`, and two names for one persona is how a
        # ceiling gets set on a row nothing reads.
        ("persona", "requester", False),
        ("mode", "read", True),
        # The widening act mode needs, and the case that proves act sessions
        # became representable at all.
        ("mode", "act", True),
        # The value 0045 anticipated, and NOT the value that shipped: §0.8
        # fixes `mode` at creation and gives it no promotion path, so a
        # combined `read_act` would describe a session that cannot exist.
        ("mode", "read_act", False),
    ],
)
async def test_ai_session_persona_and_mode_checks(
    admin_engine: AsyncEngine, column: str, value: str, accepted: bool
) -> None:
    org_id, user_id = await seed_org_and_user(admin_engine, "chk")
    other = {"persona": "admin", "mode": "read"} | {column: value}
    outcome = await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_session (id, org_id, user_id, persona, mode, title, model_id,"
        " created_at, last_turn_at, message_count, total_input_tokens, total_output_tokens)"
        " VALUES (gen_random_uuid(), :o, :u, :p, :m, 't', 'm', now(), now(), 0, 0, 0)",
        o=org_id,
        u=user_id,
        p=other["persona"],
        m=other["mode"],
    )
    stored = await _scalar(
        admin_engine,
        f"SELECT count(*) FROM ai_session WHERE org_id = :o AND {column} = :v",
        o=org_id,
        v=value,
    )
    # The stored count, not just the outcome: a deleted CHECK prints
    # `('ok', 1)` -- the row the database accepted -- rather than DID NOT RAISE.
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


@pytest.mark.parametrize(
    ("role", "accepted"),
    [("user", True), ("assistant", True), ("tool", False), ("system", False)],
)
async def test_ai_message_role_check_excludes_tool(
    admin_engine: AsyncEngine, role: str, accepted: bool
) -> None:
    """Tool results are NOT stored (§3.2). The CHECK is what makes that a
    property of the schema rather than a convention of the writer."""
    org_id, user_id = await seed_org_and_user(admin_engine, "role")
    sess = await seed_session(admin_engine, org_id, user_id)
    outcome = await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, :r, 'x', '{}'::jsonb, now())",
        o=org_id,
        s=sess,
        r=role,
    )
    stored = await _scalar(
        admin_engine,
        "SELECT count(*) FROM ai_message WHERE session_id = :s AND role = :r",
        s=sess,
        r=role,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


async def test_ai_message_seq_is_unique_per_session(admin_engine: AsyncEngine) -> None:
    org_id, user_id = await seed_org_and_user(admin_engine, "seq")
    sess = await seed_session(admin_engine, org_id, user_id)
    other = await seed_session(admin_engine, org_id, user_id)
    insert = (
        "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags, created_at)"
        " VALUES (gen_random_uuid(), :o, :s, 1, 'user', 'x', '{}'::jsonb, now())"
    )
    first = await _admin_insert_outcome(admin_engine, insert, o=org_id, s=sess)
    second = await _admin_insert_outcome(admin_engine, insert, o=org_id, s=sess)
    # A DIFFERENT session at the same seq must still be accepted -- otherwise a
    # unique constraint on `seq` alone would pass the test above.
    elsewhere = await _admin_insert_outcome(admin_engine, insert, o=org_id, s=other)
    stored = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_message WHERE session_id = :s", s=sess
    )
    assert (first, second, elsewhere, stored) == (
        "ok",
        "refused: UniqueViolation",
        "ok",
        1,
    )


async def test_deleting_a_session_cascades_to_messages_and_flags(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id = await seed_org_and_user(admin_engine, "casc")
    sess = await seed_session(admin_engine, org_id, user_id)
    keep = await seed_session(admin_engine, org_id, user_id)
    for target in (sess, keep):
        for seq in (1, 2):
            await _admin_insert_outcome(
                admin_engine,
                "INSERT INTO ai_message (id, org_id, session_id, seq, role, content, flags,"
                " created_at) VALUES (gen_random_uuid(), :o, :s, :q, 'user', 'x', '{}'::jsonb,"
                " now())",
                o=org_id,
                s=target,
                q=seq,
            )
            await _admin_insert_outcome(
                admin_engine,
                "INSERT INTO ai_injection_flag (id, org_id, session_id, message_seq, source_tool,"
                " source_type, source_id, matched_rule, excerpt, created_at)"
                " VALUES (gen_random_uuid(), :o, :s, :q, 't', 'ticket', 'x', 'redirection', 'e',"
                " now())",
                o=org_id,
                s=target,
                q=seq,
            )

    async def counts() -> tuple[int, int, int, int]:
        return (
            await _scalar(
                admin_engine, "SELECT count(*) FROM ai_message WHERE session_id = :s", s=sess
            ),
            await _scalar(
                admin_engine,
                "SELECT count(*) FROM ai_injection_flag WHERE session_id = :s",
                s=sess,
            ),
            await _scalar(
                admin_engine, "SELECT count(*) FROM ai_message WHERE session_id = :s", s=keep
            ),
            await _scalar(
                admin_engine,
                "SELECT count(*) FROM ai_injection_flag WHERE session_id = :s",
                s=keep,
            ),
        )

    assert await counts() == (2, 2, 2, 2)
    await _admin_insert_outcome(admin_engine, "DELETE FROM ai_session WHERE id = :s", s=sess)
    # The neighbouring session's rows must survive: a cascade that deleted
    # everything would pass an assertion that only checked the deleted side.
    assert await counts() == (0, 0, 2, 2)


# --------------------------------------------------------------------------- #
# `ai_settings` (§3.4, §0.10)
# --------------------------------------------------------------------------- #


async def test_settings_carries_no_kill_switch_and_no_provider_column(
    admin_engine: AsyncEngine,
) -> None:
    """**THE DECISION CHANGED** (spec §0.14, operator, 2026-08-25). This was
    `test_settings_enabled_defaults_false`, and it asserted §0.10's "the AI
    surface SHIPS OFF" on the value the database stored for a row that never
    mentioned `enabled`.

    **THE NEW RULE:** the assistant ships ON, its provider is `.env`, and
    migration `0074` DROPS both columns. Asserted against `information_schema`
    rather than by selecting them, because a `SELECT enabled` that errors could
    equally mean the table is missing -- and because a re-added column with a
    different default would still pass a value assertion while re-creating
    exactly the per-org switch this decision removed.
    """
    org_id, _ = await seed_org_and_user(admin_engine, "off")
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_settings (org_id, updated_at) VALUES (:o, now())",
        o=org_id,
    )
    async with AsyncSession(admin_engine) as session:
        columns = {
            r[0]
            for r in (
                await session.execute(
                    sqltext(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'ai_settings'"
                    )
                )
            ).all()
        }
    assert "enabled" not in columns, "the per-org kill switch is back"
    assert "provider_key" not in columns, "the per-org provider selection is back"
    # The dials the decision deliberately LEFT org-configurable.
    assert {"transcript_retention_days", "max_turns_per_org_per_hour"} <= columns


async def test_settings_tunable_defaults(admin_engine: AsyncEngine) -> None:
    """§3.4's four defaults, read back off a row that set none of them."""
    org_id, _ = await seed_org_and_user(admin_engine, "def")
    await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_settings (org_id, updated_at) VALUES (:o, now())",
        o=org_id,
    )
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                sqltext(
                    "SELECT transcript_retention_days, max_turns_per_user_per_minute,"
                    " max_turns_per_org_per_hour, daily_token_budget"
                    " FROM ai_settings WHERE org_id = :o"
                ),
                {"o": org_id},
            )
        ).one()
    assert tuple(row) == (30, 6, 200, None)


@pytest.mark.parametrize(
    ("column", "value", "accepted"),
    [
        ("transcript_retention_days", 1, True),
        ("transcript_retention_days", 365, True),
        ("transcript_retention_days", 0, False),
        ("transcript_retention_days", 36500, False),
        ("max_turns_per_user_per_minute", 1, True),
        ("max_turns_per_user_per_minute", 60, True),
        ("max_turns_per_user_per_minute", 0, False),
        ("max_turns_per_user_per_minute", 61, False),
        ("max_turns_per_org_per_hour", 1, True),
        ("max_turns_per_org_per_hour", 5000, True),
        ("max_turns_per_org_per_hour", 0, False),
        ("max_turns_per_org_per_hour", 10_000_000, False),
        ("daily_token_budget", 1, True),
        ("daily_token_budget", 0, False),
        ("daily_token_budget", -1, False),
    ],
)
async def test_settings_tunable_ceilings(
    admin_engine: AsyncEngine, column: str, value: int, accepted: bool
) -> None:
    org_id, _ = await seed_org_and_user(admin_engine, "ceil")
    outcome = await _admin_insert_outcome(
        admin_engine,
        f"INSERT INTO ai_settings (org_id, {column}, updated_at) VALUES (:o, :v, now())",
        o=org_id,
        v=value,
    )
    stored = await _scalar(
        admin_engine,
        f"SELECT count(*) FROM ai_settings WHERE org_id = :o AND {column} = :v",
        o=org_id,
        v=value,
    )
    assert (outcome, stored) == (("ok", 1) if accepted else ("refused: CheckViolation", 0))


async def test_daily_token_budget_may_be_null(admin_engine: AsyncEngine) -> None:
    """NULL means "no budget" and must survive the `> 0` CHECK."""
    org_id, _ = await seed_org_and_user(admin_engine, "null")
    outcome = await _admin_insert_outcome(
        admin_engine,
        "INSERT INTO ai_settings (org_id, daily_token_budget, updated_at)"
        " VALUES (:o, NULL, now())",
        o=org_id,
    )
    stored = await _scalar(
        admin_engine,
        "SELECT daily_token_budget IS NULL FROM ai_settings WHERE org_id = :o",
        o=org_id,
    )
    assert (outcome, stored) == ("ok", True)


# --------------------------------------------------------------------------- #
# Column shapes that are load-bearing
# --------------------------------------------------------------------------- #


async def test_injection_flag_excerpt_is_bounded_by_the_column(
    admin_engine: AsyncEngine,
) -> None:
    """§3.3: `excerpt` is "the one place retrieved text is deliberately
    stored". It must be bounded by the COLUMN, not by the caller -- a `text`
    column stores whatever an attacker wrote into a ticket body."""
    length = await _scalar(
        admin_engine,
        "SELECT character_maximum_length FROM information_schema.columns"
        " WHERE table_name = 'ai_injection_flag' AND column_name = 'excerpt'",
    )
    assert length == 400


async def test_ai_settings_org_id_is_the_primary_key(admin_engine: AsyncEngine) -> None:
    """One row per org (§3.4): a second row for the same org must be refused,
    which is what makes the settings upsert well defined."""
    org_id, _ = await seed_org_and_user(admin_engine, "pk")
    insert = "INSERT INTO ai_settings (org_id, updated_at) VALUES (:o, now())"
    first = await _admin_insert_outcome(admin_engine, insert, o=org_id)
    second = await _admin_insert_outcome(admin_engine, insert, o=org_id)
    stored = await _scalar(
        admin_engine, "SELECT count(*) FROM ai_settings WHERE org_id = :o", o=org_id
    )
    assert (first, second, stored) == ("ok", "refused: UniqueViolation", 1)


# --------------------------------------------------------------------------- #
# Permissions (§0.11)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", AI_PERMISSIONS)
async def test_permission_is_registered_and_seeded(
    admin_engine: AsyncEngine, code: str
) -> None:
    assert code in PERMISSIONS
    seeded = await _scalar(
        admin_engine, "SELECT count(*) FROM permissions WHERE code = :c", c=code
    )
    assert seeded == 1


async def test_ai_admin_carries_high_privilege_wording() -> None:
    """§0.11 asks for `notification.admin`'s reasoning on `ai.admin`: this
    grant decides where control-plane data is sent."""
    assert "HIGH PRIVILEGE" in PERMISSIONS["ai.admin"]


@pytest.mark.parametrize("code", AI_PERMISSIONS)
async def test_ai_grants_are_admin_tier_only(admin_engine: AsyncEngine, code: str) -> None:
    """§0.11 and this build's recorded decision: both codes go to the two
    admin roles and to NO technician-tier role. Asserted on the actual
    `role_permissions` rows by role name -- a grant added to `technician` in a
    later migration prints the role name here."""
    async with AsyncSession(admin_engine) as session:
        roles = {
            r[0]
            for r in (
                await session.execute(
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
    assert roles == {"super-admin", "org-admin"}

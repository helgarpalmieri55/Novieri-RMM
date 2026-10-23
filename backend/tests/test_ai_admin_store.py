"""One atomic turn write, and what `ai_message` is allowed to hold (spec §3.2,
§5 step 6, §13 test 13's transcript half).

THE PROPERTY THAT IS EASY TO LOSE AND HARD TO SEE. `ai_message.tool_calls`
records what a turn DID, never what it READ. An implementation that helpfully
stashed the tool result alongside the call would look better in the UI, pass
every behavioural test in this module, and quietly build a second copy of every
record the agent ever touched -- unaudited, outside every module's own retention
rules, in a table whose RLS nobody reasoned about per-record. So the key set is
asserted EXACTLY, not with `>=`, and the assertion is on the STORED JSON.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ai.errors import AiNotFoundError
from app.core.ai.orchestrator import ToolCallRecord, TurnFinding, TurnOutcome
from app.core.ai.store import (
    arg_hash,
    create_session,
    delete_session,
    list_sessions,
    load_session,
    persist_turn,
    question_sha256,
)
from app.core.tenancy import org_session

# From the KERNEL package, not from `app.models.ai` directly:
# `app/models/__init__.py` imports this module's models, so reaching the module
# first from a test leaves the kernel package half-built and the import fails as
# a cycle. Every process imports `app.models` first; tests do too.
from app.models import AiInjectionFlag, AiMessage, AiSession
from app.modules.ai_admin.profile import ADMIN_PROFILE
from tests.test_ai_admin_migration import seed_org_and_user

pytestmark = pytest.mark.usefixtures("admin_engine")


def _outcome(**kw: Any) -> TurnOutcome:
    defaults: dict[str, Any] = {
        "answer": "Two alerts are open.",
        "tool_calls": [
            ToolCallRecord(
                tool="health.alerts",
                args={"limit": 10},
                status="ok",
                result_count=2,
                truncated=False,
                http_status=200,
            )
        ],
        "findings": [],
        "stop_reason": "answer",
        "input_tokens": 120,
        "output_tokens": 40,
        "duration_ms": 900,
        "model_id": "fake/scripted",
    }
    return TurnOutcome(**(defaults | kw))


async def _a_session(admin_engine: AsyncEngine, tag: str) -> tuple[UUID, UUID, AiSession]:
    org_id, user_id = await seed_org_and_user(admin_engine, tag)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await create_session(
            session, org_id=org_id, user_id=user_id, persona=ADMIN_PROFILE.name, model_id="m"
        )
        await session.commit()
    return org_id, user_id, row


# --------------------------------------------------------------------------- #
# The hashes.
# --------------------------------------------------------------------------- #


def test_question_sha256_is_stable_across_trivial_variation() -> None:
    """§9's normalization: NFC, whitespace collapsed, lowercased. Without it,
    "was this asked before" answers no for two spellings of one sentence."""
    canonical = question_sha256("Why did SRV-02 fail?")
    assert question_sha256("why did  srv-02 fail?") == canonical
    assert question_sha256("  Why did SRV-02 fail?\n") == canonical
    # The NFD spelling of the same string: "é" as e + combining acute.
    assert question_sha256("Café down?") == question_sha256("Café down?")
    assert question_sha256("Why did SRV-03 fail?") != canonical


def test_arg_hash_reuses_the_canonical_encoding_and_survives_a_uuid() -> None:
    """`canonical_bytes` RAISES `TypeError` on a `UUID` -- deliberately, so a
    formatting ambiguity can never silently change signed bytes. Tool arguments
    are very often UUIDs, so `arg_hash` renders them itself rather than the
    canonical encoder being relaxed for everyone who depends on it.

    Key ORDER must not matter and value TYPE must: the first pair proves the
    canonicalization, the second proves the hash is not just `str(dict)`.
    """
    device = uuid4()
    assert arg_hash({"device_id": device, "limit": 5}) == arg_hash(
        {"limit": 5, "device_id": device}
    )
    assert arg_hash({"device_id": device}) != arg_hash({"device_id": uuid4()})
    assert arg_hash({}) == arg_hash({})


# --------------------------------------------------------------------------- #
# The turn write.
# --------------------------------------------------------------------------- #


async def test_a_turn_writes_both_rows_the_counters_and_nothing_else(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-turn")
    turn_id = uuid4()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="what is alerting?",
            outcome=_outcome(),
            turn_id=turn_id,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        messages = list(
            (
                await session.execute(
                    select(AiMessage)
                    .where(AiMessage.session_id == ai_session.id)
                    .order_by(AiMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        row = await session.get(AiSession, ai_session.id)

    assert [m.role for m in messages] == ["user", "assistant"]
    assert [m.seq for m in messages] == [0, 1]
    assert messages[0].id == turn_id, "the pre-allocated turn id is the user row's id"
    assert messages[0].content == "what is alerting?"
    assert messages[1].content == "Two alerts are open."
    assert messages[1].input_tokens == 120
    assert messages[1].flags == {"stop_reason": "answer", "partial": False}
    assert row is not None
    assert row.message_count == 2
    assert row.total_input_tokens == 120
    assert row.total_output_tokens == 40
    assert row.title == "what is alerting?"


async def test_the_stored_tool_calls_carry_no_result_payload(
    admin_engine: AsyncEngine,
) -> None:
    """§3.2. The key set is asserted EXACTLY: a `>=` assertion is satisfied by an
    implementation that also stored the rows it read."""
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-tc")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(),
            turn_id=None,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(AiMessage.tool_calls).where(
                    AiMessage.session_id == ai_session.id, AiMessage.role == "assistant"
                )
            )
        ).scalar_one()

    assert stored is not None
    assert len(stored) == 1
    assert set(stored[0]) == {"tool", "arg_hash", "result_count", "truncated", "status"}
    assert stored[0]["tool"] == "health.alerts"
    assert stored[0]["arg_hash"] == arg_hash({"limit": 10})
    assert "10" not in stored[0]["arg_hash"]


async def test_a_second_turn_continues_the_sequence(admin_engine: AsyncEngine) -> None:
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-seq")

    for question in ("first", "second"):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            row = await session.get(AiSession, ai_session.id)
            assert row is not None
            await persist_turn(
                session,
                org_id=org_id,
                user_id=user_id,
                ai_session=row,
                question=question,
                outcome=_outcome(),
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        seqs = list(
            (
                await session.execute(
                    select(AiMessage.seq)
                    .where(AiMessage.session_id == ai_session.id)
                    .order_by(AiMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        row = await session.get(AiSession, ai_session.id)

    assert seqs == [0, 1, 2, 3]
    assert row is not None
    assert row.title == "first", "the title is taken from the FIRST question, not the last"


async def test_a_blank_first_question_leaves_the_conversation_untitled_forever(
    admin_engine: AsyncEngine,
) -> None:
    """THE TITLE MAY ONLY EVER COME FROM `seq 0`, because that is the anchor
    `retention.sweep_org` uses to decide when the copy has outlived the message
    it was copied from.

    `TurnIn.question` is `min_length=1`, so a single space reaches `persist_turn`
    and strips to nothing. A `if not ai_session.title` with no `seq` guard then
    adopts the SECOND turn's question -- which is a title anchored to a `seq 0`
    that never carried it, and which the sweep blanks as soon as `seq 0` expires,
    up to a full retention window early. An untitled conversation is the cheaper
    defect, and this pins which one is chosen.
    """
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-blank-title")

    for question in (" ", "the second question"):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            row = await session.get(AiSession, ai_session.id)
            assert row is not None
            await persist_turn(
                session,
                org_id=org_id,
                user_id=user_id,
                ai_session=row,
                question=question,
                outcome=_outcome(),
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = await session.get(AiSession, ai_session.id)
        contents = list(
            (
                await session.execute(
                    select(AiMessage.content)
                    .where(AiMessage.session_id == ai_session.id, AiMessage.role == "user")
                    .order_by(AiMessage.seq)
                )
            )
            .scalars()
            .all()
        )

    assert contents == [" ", "the second question"], "both turns were still recorded"
    assert row is not None
    assert row.title == "", f"the title was taken from a turn that is not seq 0: {row.title!r}"


async def test_the_turn_write_is_atomic(admin_engine: AsyncEngine, monkeypatch: Any) -> None:
    """§5 step 6. Force the AUDIT write to fail and assert ZERO message rows.

    Asserting the row count -- not merely that an exception escaped -- is the
    point: a `persist_turn` that committed the transcript and then audited would
    raise the same exception here and leave two rows behind, which is a
    conversation with no audit line.
    """
    from app.core import audit as audit_module

    org_id, user_id, ai_session = await _a_session(admin_engine, "store-atomic")

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("audit is down")

    monkeypatch.setattr(audit_module.audit_service, "record", explode)

    with pytest.raises(RuntimeError):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            row = await session.get(AiSession, ai_session.id)
            assert row is not None
            await persist_turn(
                session,
                org_id=org_id,
                user_id=user_id,
                ai_session=row,
                question="q",
                outcome=_outcome(),
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(AiMessage)
                .where(AiMessage.session_id == ai_session.id)
            )
        ).scalar_one()
    assert count == 0


async def test_injection_flags_are_written_with_their_excerpts(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-flag")
    marker = "MARKER-e91c-ignore previous instructions"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(
                findings=[
                    TurnFinding(
                        tool="health.alerts",
                        source_type="alert",
                        source_id="a1",
                        matched_rule="redirection",
                        excerpt=marker,
                    )
                ]
            ),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        flags = list(
            (
                await session.execute(
                    select(AiInjectionFlag).where(AiInjectionFlag.session_id == ai_session.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(flags) == 1
    assert flags[0].excerpt == marker
    assert flags[0].matched_rule == "redirection"
    assert flags[0].message_seq == 1


async def test_an_excerpt_longer_than_the_column_is_bounded_before_insert(
    admin_engine: AsyncEngine,
) -> None:
    """`excerpt` is `String(400)`. Postgres would refuse a longer value and take
    the whole atomic turn write down with it -- so a turn about a hostile record
    would fail to persist, which is the record the administrator most needs."""
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-long")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(
                findings=[
                    TurnFinding(
                        tool="health.alerts",
                        source_type="alert",
                        source_id="a1",
                        matched_rule="redirection",
                        excerpt="x" * 5000,
                    )
                ]
            ),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        excerpt = (
            await session.execute(
                select(AiInjectionFlag.excerpt).where(
                    AiInjectionFlag.session_id == ai_session.id
                )
            )
        ).scalar_one()
    assert len(excerpt) == 400


# --------------------------------------------------------------------------- #
# Loading, listing, deleting -- and the two predicates.
# --------------------------------------------------------------------------- #


async def test_another_users_session_in_the_same_org_is_not_found(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE (Global Constraint 4).

    RLS scopes by ORG and says nothing at all about the user, so this predicate
    has no backstop: delete it and one administrator reads another's
    conversation, in the same org, with RLS entirely satisfied. Driving it on
    the admin engine also removes RLS from the picture for the org half.
    """
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-other")
    _, other_user = await seed_org_and_user(admin_engine, "store-other2")

    async with AsyncSession(admin_engine) as session:
        loaded, _ = await load_session(
            session, org_id=org_id, user_id=user_id, session_id=ai_session.id
        )
        assert loaded.id == ai_session.id, "the fixture really is readable"

        with pytest.raises(AiNotFoundError):
            await load_session(
                session, org_id=org_id, user_id=other_user, session_id=ai_session.id
            )


async def test_another_orgs_session_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """The application-layer `org_id` predicate, pinned where RLS is not the
    thing doing the rejecting."""
    _, _, mine = await _a_session(admin_engine, "store-mine")
    other_org, other_user, theirs = await _a_session(admin_engine, "store-theirs")

    async with AsyncSession(admin_engine) as session:
        loaded, _ = await load_session(
            session, org_id=other_org, user_id=other_user, session_id=theirs.id
        )
        assert loaded.id == theirs.id

        with pytest.raises(AiNotFoundError):
            await load_session(
                session, org_id=other_org, user_id=other_user, session_id=mine.id
            )


async def test_list_sessions_returns_only_the_callers_own(admin_engine: AsyncEngine) -> None:
    """Seeded with a NEARLY-MATCHING row -- a second session in the same org,
    owned by someone else. An empty list is otherwise indistinguishable from a
    broken query."""
    org_id, user_id, mine = await _a_session(admin_engine, "store-list")
    _, other_user = await seed_org_and_user(admin_engine, "store-list2")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        theirs = await create_session(
            session, org_id=org_id, user_id=other_user, persona=ADMIN_PROFILE.name
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        listed = await list_sessions(session, org_id=org_id, user_id=user_id)
        theirs_listed = await list_sessions(session, org_id=org_id, user_id=other_user)

    assert [row.id for row in listed] == [mine.id]
    assert [row.id for row in theirs_listed] == [theirs.id]


async def test_deleting_a_session_cascades_to_messages_and_flags(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-del")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(
                findings=[
                    TurnFinding(
                        tool="health.alerts",
                        source_type="alert",
                        source_id="a1",
                        matched_rule="redirection",
                        excerpt="x",
                    )
                ]
            ),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        await delete_session(
            session, org_id=org_id, user_id=user_id, session_id=ai_session.id
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        messages = (
            await session.execute(
                select(func.count())
                .select_from(AiMessage)
                .where(AiMessage.session_id == ai_session.id)
            )
        ).scalar_one()
        flags = (
            await session.execute(
                select(func.count())
                .select_from(AiInjectionFlag)
                .where(AiInjectionFlag.session_id == ai_session.id)
            )
        ).scalar_one()
    assert (messages, flags) == (0, 0)


async def test_deleting_another_users_session_raises_not_found(
    admin_engine: AsyncEngine,
) -> None:
    org_id, _, ai_session = await _a_session(admin_engine, "store-del2")
    _, other_user = await seed_org_and_user(admin_engine, "store-del3")

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(AiNotFoundError):
            await delete_session(
                session, org_id=org_id, user_id=other_user, session_id=ai_session.id
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        still_there = await session.get(AiSession, ai_session.id)
    assert still_there is not None, "the refusal did not delete it anyway"


async def test_a_turn_persists_through_an_org_session_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The path production actually takes: the RLS-constrained app role, inside
    `org_session`. The admin-engine tests above pin the predicates; this one
    pins that the writes are permitted at all."""
    org_id, user_id = await seed_org_and_user(admin_engine, "store-rls")

    async with org_session(org_id, engine=app_engine) as session:
        row = await create_session(
            session, org_id=org_id, user_id=user_id, persona=ADMIN_PROFILE.name
        )
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(),
        )

    async with org_session(org_id, engine=app_engine) as session:
        loaded, messages = await load_session(
            session, org_id=org_id, user_id=user_id, session_id=row.id
        )
    assert loaded.message_count == 2
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_a_session_records_the_time_of_its_last_turn(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id, ai_session = await _a_session(admin_engine, "store-clock")
    before = datetime.now(UTC) - timedelta(seconds=1)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, ai_session.id)
        assert row is not None
        await persist_turn(
            session,
            org_id=org_id,
            user_id=user_id,
            ai_session=row,
            question="q",
            outcome=_outcome(),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = await session.get(AiSession, ai_session.id)
    assert row is not None
    assert row.last_turn_at >= before
    assert row.model_id == "fake/scripted"

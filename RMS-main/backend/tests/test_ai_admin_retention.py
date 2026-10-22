"""Transcript retention: the hard-delete sweep (spec §0.7, §3.2, §3.3).

THE BOUNDARY IS THE TEST. Retention bugs are almost never "it deleted nothing";
they are "it deleted a day early" or "it kept everything forever", and both look
identical from the outside for the first thirty days. So every case here seeds
rows on BOTH sides of the cutoff and asserts which ids survived -- a sweep that
deleted everything and a sweep that deleted nothing then print different values,
and so do `<` and `<=`.

THE PER-ORG CASE IS NOT A COURTESY. `transcript_retention_days` is an org
tunable; a sweep that read one number for the whole deployment would silently
give every org the first org's answer, and the org that configured 7 days would
keep 30. Two orgs with different windows are swept in ONE run here.

ORG ISOLATION IS PINNED ON THE ADMIN ENGINE (Global Constraint 4). Under
`org_session` RLS rejects a cross-org row before the sweep's own `org_id`
predicate is consulted, so the RLS-driven version of this test passes with the
predicate deleted. On the admin engine, RLS is not the thing doing the
rejecting, and the predicate is.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ai.retention import RETENTION_INTERVAL_S, sweep_all_orgs, sweep_org
from app.models import AiInjectionFlag, AiMessage, AiSession, AiSettings
from tests.test_ai_admin_migration import seed_org_and_user

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

#: What `sweep_org` returns when it changed nothing. **AS A NAMED CONSTANT, NOT
#: A LITERAL AT EACH SITE**: the counts dict grew two keys when act mode landed
#: (`rationales`, and `sessions_retained` -- the sessions an `ai_action`'s
#: `ON DELETE RESTRICT` makes undeletable, docs/modules/ai-automation.md §5.1),
#: and three separate literals had to be found and edited. A future key is one
#: edit here, and a key that STOPPED being reported still fails every one of
#: these tests, which is the property the equality is for.
_NOTHING = {
    "messages": 0,
    "flags": 0,
    "sessions": 0,
    "titles": 0,
    "rationales": 0,
    "sessions_retained": 0,
}


async def _session_with_message(
    admin_engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    *,
    age_days: float,
    with_flag: bool = False,
) -> tuple[UUID, UUID]:
    """A conversation and one message, both created `age_days` ago."""
    created = NOW - timedelta(days=age_days)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = AiSession(
            org_id=org_id,
            user_id=user_id,
            title="t",
            model_id="m",
            created_at=created,
            last_turn_at=created,
        )
        session.add(ai_session)
        await session.flush()
        message = AiMessage(
            org_id=org_id,
            session_id=ai_session.id,
            seq=0,
            role="user",
            content="q",
            created_at=created,
        )
        session.add(message)
        if with_flag:
            session.add(
                AiInjectionFlag(
                    org_id=org_id,
                    session_id=ai_session.id,
                    message_seq=0,
                    source_tool="health.alerts",
                    source_type="alert",
                    source_id="a1",
                    matched_rule="redirection",
                    excerpt="ignore previous instructions",
                    created_at=created,
                )
            )
        await session.commit()
        return ai_session.id, message.id


async def _session_with_turns(
    admin_engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    *,
    title: str,
    turns: list[tuple[str, float]],
) -> UUID:
    """One titled conversation and its `(content, age_days)` messages, numbered
    from `seq 0` in the order given."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = AiSession(
            org_id=org_id,
            user_id=user_id,
            title=title,
            model_id="m",
            created_at=NOW - timedelta(days=turns[0][1]),
            last_turn_at=NOW - timedelta(days=turns[-1][1]),
        )
        session.add(ai_session)
        await session.flush()
        for seq, (content, age_days) in enumerate(turns):
            session.add(
                AiMessage(
                    org_id=org_id,
                    session_id=ai_session.id,
                    seq=seq,
                    role="user" if seq % 2 == 0 else "assistant",
                    content=content,
                    created_at=NOW - timedelta(days=age_days),
                )
            )
        await session.commit()
        return ai_session.id


async def _title_and_bodies(
    admin_engine: AsyncEngine, session_id: UUID
) -> tuple[str | None, list[str]]:
    async with AsyncSession(admin_engine) as session:
        row = await session.get(AiSession, session_id)
        bodies = list(
            (
                await session.execute(
                    select(AiMessage.content)
                    .where(AiMessage.session_id == session_id)
                    .order_by(AiMessage.seq)
                )
            )
            .scalars()
            .all()
        )
    return (None if row is None else row.title), bodies


async def _set_days(admin_engine: AsyncEngine, org_id: UUID, days: int) -> None:
    """This org's retention window.

    `enabled=False` USED TO BE PASSED HERE and is gone with the column
    (migration `0074`, spec §0.14). It was making a real point -- retention
    sweeps an organization's transcripts whether or not that organization has
    the assistant switched on, because the rows are already written and the
    clock is a promise about them rather than about the feature. The point
    survives and needs no argument now: there is no per-org switch, and the
    sweep never consulted one.
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(AiSettings(org_id=org_id, transcript_retention_days=days))
        await session.commit()


async def _count(engine: AsyncEngine, model: Any, **filters: Any) -> int:
    async with AsyncSession(engine) as session:
        statement = select(func.count()).select_from(model)
        for column, value in filters.items():
            statement = statement.where(getattr(model, column) == value)
        return int((await session.execute(statement)).scalar_one())


async def _sweep(admin_engine: AsyncEngine, org_id: UUID) -> dict[str, int]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await sweep_org(session, org_id, now=NOW)
        await session.commit()
    return counts


# --------------------------------------------------------------------------- #
# The boundary.
# --------------------------------------------------------------------------- #


async def test_the_sweep_deletes_past_the_boundary_and_not_at_it(
    admin_engine: AsyncEngine,
) -> None:
    """`<` cutoff, not `<=`. The 30-day-old row is exactly AT the boundary and
    survives; the 31-day-old row is past it and goes.

    Both are seeded so the two implementations print different survivor sets. A
    test that seeded only the old row passes against a sweep that deletes
    everything.
    """
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-boundary")
    _, fresh = await _session_with_message(admin_engine, org_id, user_id, age_days=1)
    _, at_boundary = await _session_with_message(admin_engine, org_id, user_id, age_days=30)
    _, past = await _session_with_message(admin_engine, org_id, user_id, age_days=31)

    counts = await _sweep(admin_engine, org_id)

    async with AsyncSession(admin_engine) as session:
        surviving = set(
            (await session.execute(select(AiMessage.id).where(AiMessage.org_id == org_id)))
            .scalars()
            .all()
        )
    assert surviving == {fresh, at_boundary}
    assert past not in surviving
    assert counts["messages"] == 1


async def test_a_session_whose_messages_are_all_swept_is_deleted_too(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-empty")
    gone, _ = await _session_with_message(admin_engine, org_id, user_id, age_days=45)

    counts = await _sweep(admin_engine, org_id)

    assert counts["sessions"] == 1
    assert await _count(admin_engine, AiSession, id=gone) == 0


async def test_a_session_with_one_surviving_message_is_kept(
    admin_engine: AsyncEngine,
) -> None:
    """An old conversation that is still being used must not vanish under the
    administrator mid-thread."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-mixed")
    created = NOW - timedelta(days=45)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = AiSession(org_id=org_id, user_id=user_id, created_at=created, last_turn_at=NOW)
        session.add(ai_session)
        await session.flush()
        session.add(
            AiMessage(
                org_id=org_id,
                session_id=ai_session.id,
                seq=0,
                role="user",
                content="old",
                created_at=created,
            )
        )
        session.add(
            AiMessage(
                org_id=org_id,
                session_id=ai_session.id,
                seq=1,
                role="user",
                content="new",
                created_at=NOW - timedelta(days=2),
            )
        )
        await session.commit()
        session_id = ai_session.id

    counts = await _sweep(admin_engine, org_id)

    assert counts["messages"] == 1
    assert counts["sessions"] == 0
    assert await _count(admin_engine, AiSession, id=session_id) == 1
    assert await _count(admin_engine, AiMessage, session_id=session_id) == 1


async def test_a_brand_new_empty_session_is_never_swept(admin_engine: AsyncEngine) -> None:
    """A session created seconds ago has no messages yet -- its first turn may
    still be running. Sweeping it would delete a conversation out from under its
    own `persist_turn`."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-new")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = AiSession(org_id=org_id, user_id=user_id, created_at=NOW)
        session.add(ai_session)
        await session.commit()
        session_id = ai_session.id

    counts = await _sweep(admin_engine, org_id)

    assert counts["sessions"] == 0
    assert await _count(admin_engine, AiSession, id=session_id) == 1


async def test_the_flag_excerpts_go_with_the_transcript(admin_engine: AsyncEngine) -> None:
    """§3.3: `excerpt` is the one place retrieved text is deliberately stored,
    and it comes from unauthenticated sources. Sweeping messages while keeping
    excerpts would retain exactly the most sensitive stored bytes forever."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-flags")
    old_session, _ = await _session_with_message(
        admin_engine, org_id, user_id, age_days=60, with_flag=True
    )
    await _session_with_message(admin_engine, org_id, user_id, age_days=1, with_flag=True)

    counts = await _sweep(admin_engine, org_id)

    assert counts["flags"] == 1
    assert await _count(admin_engine, AiInjectionFlag, session_id=old_session) == 0
    assert await _count(admin_engine, AiInjectionFlag, org_id=org_id) == 1


# --------------------------------------------------------------------------- #
# `ai_session.title` is question text, and expires as question text.
# --------------------------------------------------------------------------- #

QUESTION = "does CFO-laptop-7 have the embargoed acquisition patch installed"


async def test_a_title_is_blanked_when_the_question_it_was_copied_from_is_swept(
    admin_engine: AsyncEngine,
) -> None:
    """§0.7 THROUGH THE DENORMALIZED COPY, and the case that survives the
    session delete: the conversation is still in use, so it is kept -- and it
    kept the first question's first 200 characters on `SessionOut.title` long
    after the message carrying that text was hard-deleted.

    THE ASSERTION IS ON THE TITLE'S VALUE, not on a count. A sweep that deletes
    the message and leaves the title prints the question back here, which is the
    finding; a count assertion prints `1 != 0` and says nothing about what an
    administrator could still read.
    """
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-title")
    session_id = await _session_with_turns(
        admin_engine,
        org_id,
        user_id,
        title=QUESTION,
        turns=[(QUESTION, 400.0), ("it does not", 400.0), ("and now?", 2.0), ("ok", 2.0)],
    )

    counts = await _sweep(admin_engine, org_id)

    title, bodies = await _title_and_bodies(admin_engine, session_id)
    assert bodies == ["and now?", "ok"], "the old turn was swept, the recent one kept"
    assert title == "", f"the swept question is still readable as the title: {title!r}"
    assert "CFO-laptop-7" not in (title or "")
    assert counts["titles"] == 1
    assert counts["sessions"] == 0, "a conversation still in use is kept, only renamed"


async def test_a_title_survives_as_long_as_the_question_it_was_copied_from(
    admin_engine: AsyncEngine,
) -> None:
    """THE NEGATIVE HALF, and the one that separates the exact rule from the
    cheap one. This session was OPENED 45 days ago and its first turn ran
    yesterday, so `session.created_at < cutoff` blanks it 29 days early --
    deleting data an administrator was told they had. `seq 0` is still there, so
    the title stays.
    """
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-title-keep")
    session_id = await _session_with_turns(
        admin_engine,
        org_id,
        user_id,
        title="which devices are alerting",
        turns=[("which devices are alerting", 1.0), ("two", 1.0)],
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSession, session_id)
        assert row is not None
        row.created_at = NOW - timedelta(days=45)
        await session.commit()

    counts = await _sweep(admin_engine, org_id)

    title, bodies = await _title_and_bodies(admin_engine, session_id)
    assert bodies == ["which devices are alerting", "two"]
    assert title == "which devices are alerting"
    assert counts["titles"] == 0


async def test_blanking_a_title_is_not_repeated_on_the_next_sweep(
    admin_engine: AsyncEngine,
) -> None:
    """A sweep that rewrote every already-blank session every day would report
    the same count forever and write the whole table for nothing."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-title-twice")
    await _session_with_turns(
        admin_engine,
        org_id,
        user_id,
        title=QUESTION,
        turns=[(QUESTION, 400.0), ("no", 400.0), ("and now?", 2.0), ("ok", 2.0)],
    )

    first = await _sweep(admin_engine, org_id)
    second = await _sweep(admin_engine, org_id)

    assert first["titles"] == 1
    assert second == _NOTHING


# --------------------------------------------------------------------------- #
# Per-org windows and org isolation.
# --------------------------------------------------------------------------- #


async def test_two_orgs_with_different_windows_sweep_differently_in_one_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The per-org clock. A sweep that read one number for the deployment gives
    the second org the first org's answer, which is invisible until somebody
    compares two orgs."""
    keen_org, keen_user = await seed_org_and_user(admin_engine, "ret-keen")
    patient_org, patient_user = await seed_org_and_user(admin_engine, "ret-patient")
    await _set_days(admin_engine, keen_org, 7)
    await _set_days(admin_engine, patient_org, 90)

    _, keen_message = await _session_with_message(admin_engine, keen_org, keen_user, age_days=20)
    _, patient_message = await _session_with_message(
        admin_engine, patient_org, patient_user, age_days=20
    )

    swept = await sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    async with AsyncSession(admin_engine) as session:
        remaining = set(
            (
                await session.execute(
                    select(AiMessage.id).where(AiMessage.id.in_([keen_message, patient_message]))
                )
            )
            .scalars()
            .all()
        )
    assert remaining == {patient_message}
    assert swept >= 2, f"the sweep reported {swept} deletions"


async def test_one_orgs_sweep_does_not_touch_another_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE. Under `org_session` this passes with the
    `org_id` predicate deleted, because RLS rejects the other org's rows first;
    here nothing does but the predicate."""
    mine_org, mine_user = await seed_org_and_user(admin_engine, "ret-mine")
    theirs_org, theirs_user = await seed_org_and_user(admin_engine, "ret-theirs")
    _, mine = await _session_with_message(admin_engine, mine_org, mine_user, age_days=90)
    _, theirs = await _session_with_message(admin_engine, theirs_org, theirs_user, age_days=90)

    counts = await _sweep(admin_engine, mine_org)

    async with AsyncSession(admin_engine) as session:
        remaining = set(
            (await session.execute(select(AiMessage.id).where(AiMessage.id.in_([mine, theirs]))))
            .scalars()
            .all()
        )
    assert counts["messages"] == 1
    assert remaining == {theirs}, "the other org's transcript was swept too"


async def test_an_org_with_no_settings_row_uses_the_thirty_day_default(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-default")
    _, kept = await _session_with_message(admin_engine, org_id, user_id, age_days=29)
    _, gone = await _session_with_message(admin_engine, org_id, user_id, age_days=31)

    await _sweep(admin_engine, org_id)

    async with AsyncSession(admin_engine) as session:
        remaining = set(
            (await session.execute(select(AiMessage.id).where(AiMessage.id.in_([kept, gone]))))
            .scalars()
            .all()
        )
    assert remaining == {kept}


async def test_a_sweep_with_nothing_to_do_is_a_no_op_twice(
    admin_engine: AsyncEngine,
) -> None:
    """Run twice: the failure worth catching is a sweep that deletes something
    on the second pass, which would be indistinguishable from working until a
    conversation vanished."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-quiet")
    await _session_with_message(admin_engine, org_id, user_id, age_days=1)

    assert await _sweep(admin_engine, org_id) == _NOTHING
    assert await _sweep(admin_engine, org_id) == _NOTHING


async def test_the_sweep_runs_under_rls_as_the_app_role(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The path the worker actually takes. The admin-engine tests pin the
    predicates; this one pins that the DELETEs are permitted at all under RLS."""
    org_id, user_id = await seed_org_and_user(admin_engine, "ret-app")
    _, gone = await _session_with_message(admin_engine, org_id, user_id, age_days=90)

    await sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    assert await _count(admin_engine, AiMessage, id=gone) == 0


async def test_an_unknown_org_id_sweeps_nothing(admin_engine: AsyncEngine) -> None:
    counts = await _sweep(admin_engine, uuid4())
    assert counts == _NOTHING


# --------------------------------------------------------------------------- #
# The beat entry.
# --------------------------------------------------------------------------- #


def test_the_retention_beat_entry_is_registered(pg_cluster: Any, tmp_path: Any) -> None:
    """The entry that causes any of this to run.

    The interval is read from the module rather than repeated as a literal: a
    test carrying its own copy of 86400.0 passes when the constant changes and
    the beat entry does not. If the entry were missing this observes a
    `KeyError` -- which production would show as "transcripts are never
    deleted", with nothing failing anywhere.
    """
    from app.core.config import Settings, override_settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        schedule = create_celery().conf.beat_schedule

    entry = schedule["ai-transcript-retention"]
    assert entry["task"] == "app.worker.ai_transcript_retention"
    assert entry["schedule"] == RETENTION_INTERVAL_S
    # `worker._with_expiry` gives EVERY beat entry an `options.expires` so an
    # overrunning sweep cannot backlog without bound; asserted here per-key
    # rather than as a whole-dict equality, which would break again the next
    # time a cross-cutting option is added. The property itself is pinned over
    # the whole schedule in tests/test_worker_beat_expiry.py.
    assert entry["options"]["expires"] == RETENTION_INTERVAL_S
    assert RETENTION_INTERVAL_S == 86400.0


@pytest.mark.parametrize("attribute", ["ai_transcript_retention_all_orgs"])
def test_the_worker_exposes_the_sweep_shell(attribute: str) -> None:
    from app import worker

    assert callable(getattr(worker, attribute))

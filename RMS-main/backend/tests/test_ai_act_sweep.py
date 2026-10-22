"""The three clocks an `ai_action` lives under (§5.2, §6.4, §8.3; §13 test 23).

**EVERY ASSERTION HERE IS ON A VALUE, NEVER ON AN ABSENCE.** §13 test 23 is the
one this file exists for and it is the easiest in the module to write vacuously:
"the sweep blanks `rationale`" is satisfied by a sweep that DELETED THE ROW, and
by a sweep that blanked every column on it. So the dispatched-row case asserts
`rationale == ""` **and** re-reads `job_type`, `target_device_id`,
`preview_digest`, `confirmed_by`, `job_id` and `state` and compares them to the
values they had before the sweep, by value.

**THE ORG PREDICATES ARE PINNED ON THE ADMIN ENGINE** (plan constraint 5). Both
sweeps run per org from the worker, where RLS is scoping them -- so a cross-org
test driven through `org_session` passes with the predicate deleted, which is
the defect that shipped four times in the SLA sub-project. The two tests below
that name the admin engine are the ones that bite.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ai.sweep import blank_rationales, expire_stale_actions, settle_action_for_job
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from app.models import AiAction, AiMessage, AiSession, AuditEvent, Organization
from tests.conftest import PgCluster
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_device, seed_org
from tests.test_ai_admin_migration import seed_session

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
REDIS_URL = "redis://localhost:6379/9"


class _World:
    def __init__(
        self, org_id: UUID, user_id: UUID, device_id: UUID, session_id: UUID, action_id: UUID
    ) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.device_id = device_id
        self.session_id = session_id
        self.action_id = action_id


async def _world(engine: AsyncEngine, tag: str) -> _World:
    org = await seed_org(engine, tag)
    device_id = await seed_device(engine, org.org_id, hostname=f"{tag}-host")
    session_id = await seed_session(engine, org.org_id, org.user_id, persona="admin", mode="act")
    action_id = await seed_approved_action(engine, org.org_id, org.user_id)
    return _World(org.org_id, org.user_id, device_id, session_id, action_id)


async def _seed_action(engine: AsyncEngine, world: _World, **kw: Any) -> UUID:
    """One `ai_action`, on the admin engine, with every clock under the test's
    control. The migration suite's own seeder hard-codes `now()` for both
    timestamps, which is exactly the thing every test in this file varies."""
    values: dict[str, Any] = {
        "org_id": world.org_id,
        "session_id": world.session_id,
        "message_seq": 1,
        "persona": "admin",
        "approved_action_id": world.action_id,
        "approved_action_key": "k",
        "job_type": "inventory.collect",
        "target_device_id": world.device_id,
        "rendered_params": {},
        "risk_class": "low",
        "destructive": False,
        "effective_autonomy": "require_approval",
        "preview_digest": "a" * 64,
        "rationale": "because the disk is nearly full",
        "state": "previewed",
        "expires_at": NOW + timedelta(minutes=15),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = AiAction(**values)
        session.add(row)
        await session.commit()
        return row.id


async def _read(engine: AsyncEngine, action_id: UUID) -> AiAction | None:
    async with AsyncSession(engine) as session:
        return await session.get(AiAction, action_id)


async def _expiry_events(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .where(AuditEvent.action == "ai.action_expired")
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# 1. Expiry: both halves of the window.
# --------------------------------------------------------------------------- #


async def test_a_stale_preview_expires_and_one_inside_the_window_does_not(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**BOTH HALVES, IN ONE TEST, WITH DIFFERENT OBSERVED VALUES.**

    The stale row must move and the fresh one must not. A test that only seeded
    the stale row would pass against a sweep that expires everything in state
    `previewed`, which is a sweep that cancels the preview an administrator is
    reading right now.
    """
    world = await _world(admin_engine, "sweep-expiry")
    stale = await _seed_action(
        admin_engine,
        world,
        expires_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(minutes=20),
    )
    fresh = await _seed_action(admin_engine, world, expires_at=NOW + timedelta(seconds=1))

    async with org_session(world.org_id, engine=app_engine) as session:
        moved = await expire_stale_actions(session, org_id=world.org_id, now=NOW)

    assert moved == 1, moved
    stale_row = await _read(admin_engine, stale)
    fresh_row = await _read(admin_engine, fresh)
    assert stale_row is not None and fresh_row is not None
    assert (stale_row.state, fresh_row.state) == ("expired", "previewed")

    events = await _expiry_events(admin_engine, world.org_id)
    assert len(events) == 1, [e.meta for e in events]
    meta = events[0].meta or {}
    assert meta["ai_action_id"] == str(stale)
    # 20 minutes old at the moment it was swept.
    assert meta["age_s"] == 1200, meta
    assert meta["state_before"] == "previewed", meta


async def test_a_transient_proposed_row_is_expired_too(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§5.2: a row OBSERVED in `proposed` means a crash between the insert and
    the digest. It has no digest, so it can never be confirmed; leaving it is a
    row that sits in a non-terminal state forever."""
    world = await _world(admin_engine, "sweep-proposed")
    action_id = await _seed_action(
        admin_engine,
        world,
        state="proposed",
        preview_digest="",
        expires_at=NOW - timedelta(seconds=1),
    )
    async with org_session(world.org_id, engine=app_engine) as session:
        await expire_stale_actions(session, org_id=world.org_id, now=NOW)

    row = await _read(admin_engine, action_id)
    assert row is not None and row.state == "expired"
    events = await _expiry_events(admin_engine, world.org_id)
    assert [e.meta["state_before"] for e in events] == ["proposed"]  # type: ignore[index]


@pytest.mark.parametrize("state", ["dispatched", "settled", "rejected", "failed"])
async def test_a_terminal_or_dispatched_row_is_never_expired(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, state: str
) -> None:
    """A dispatched row names a signed job that ran. Expiring it would make the
    confirmation history say a job that ran was never confirmed."""
    world = await _world(admin_engine, f"sweep-term-{state}")
    action_id = await _seed_action(
        admin_engine, world, state=state, expires_at=NOW - timedelta(days=30)
    )
    async with org_session(world.org_id, engine=app_engine) as session:
        moved = await expire_stale_actions(session, org_id=world.org_id, now=NOW)

    row = await _read(admin_engine, action_id)
    assert row is not None
    assert (moved, row.state) == (0, state)


async def test_the_expiry_org_predicate_is_pinned_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """**ON THE ADMIN ENGINE, WHERE RLS IS NOT THE THING REJECTING.**

    Through `org_session` this passes with `AiAction.org_id == org_id` deleted,
    because RLS rejects first -- which is why the sweep's docstring says the
    predicate is in addition to RLS and why this test does not use a tenant
    session. Both orgs' rows are seeded with the SAME state and the SAME
    `expires_at`, so `org_id` is the only column that can discriminate.
    """
    a = await _world(admin_engine, "sweep-org-a")
    b = await _world(admin_engine, "sweep-org-b")
    mine = await _seed_action(admin_engine, a, expires_at=NOW - timedelta(seconds=1))
    theirs = await _seed_action(admin_engine, b, expires_at=NOW - timedelta(seconds=1))

    async with AsyncSession(admin_engine) as session:
        moved = await expire_stale_actions(session, org_id=a.org_id, now=NOW)
        await session.commit()

    mine_row = await _read(admin_engine, mine)
    theirs_row = await _read(admin_engine, theirs)
    assert mine_row is not None and theirs_row is not None
    assert (moved, mine_row.state, theirs_row.state) == (1, "expired", "previewed")


# --------------------------------------------------------------------------- #
# 2-3. §13 test 23: the rationale, and everything that is not the rationale.
# --------------------------------------------------------------------------- #


async def test_the_sweep_blanks_the_rationale_and_leaves_every_other_column_by_value(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**§13 TEST 23, ASSERTED ON VALUES RATHER THAN ON ABSENCE.**

    Asserting only `rationale == ""` passes against a sweep that DELETED the row
    (there is nothing to read, so `.rationale` is never compared) and against one
    that blanked every column on it. So six other columns are read before the
    sweep and compared afterwards, by value -- and the row is a `dispatched` one,
    because that is the case §8.3 is actually about: a dispatched action names a
    signed job that ran on somebody's machine and must survive its own
    conversation.
    """
    world = await _world(admin_engine, "sweep-blank")
    job_id = await _seed_job(admin_engine, world)
    action_id = await _seed_action(
        admin_engine,
        world,
        state="dispatched",
        job_id=job_id,
        confirmed_by=world.user_id,
        confirmed_at=NOW,
        created_at=NOW - timedelta(days=400),
        expires_at=NOW - timedelta(days=400) + timedelta(minutes=15),
    )
    before = await _read(admin_engine, action_id)
    assert before is not None
    kept = (
        before.job_type,
        before.target_device_id,
        before.preview_digest,
        before.confirmed_by,
        before.job_id,
        before.state,
        before.approved_action_key,
    )
    assert before.rationale, "the fixture must seed a rationale or this proves nothing"

    async with org_session(world.org_id, engine=app_engine) as session:
        blanked = await blank_rationales(session, org_id=world.org_id, cutoff=NOW)

    after = await _read(admin_engine, action_id)
    assert after is not None, "the sweep DELETED the row; §8.3 says it blanks one column"
    assert blanked == 1
    assert after.rationale == ""
    assert (
        after.job_type,
        after.target_device_id,
        after.preview_digest,
        after.confirmed_by,
        after.job_id,
        after.state,
        after.approved_action_key,
    ) == kept


async def test_a_previewed_row_inside_the_retention_window_keeps_its_rationale(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The half the dispatched case cannot see.

    Without `created_at < cutoff` the sweep blanks the "why it was suggested"
    region of a preview a human is reading right now (§6.3), and the dispatched
    case above stays green while it happens.
    """
    world = await _world(admin_engine, "sweep-keep")
    fresh = await _seed_action(admin_engine, world, created_at=NOW)
    stale = await _seed_action(admin_engine, world, created_at=NOW - timedelta(days=400))

    async with org_session(world.org_id, engine=app_engine) as session:
        blanked = await blank_rationales(session, org_id=world.org_id, cutoff=NOW)

    fresh_row = await _read(admin_engine, fresh)
    stale_row = await _read(admin_engine, stale)
    assert fresh_row is not None and stale_row is not None
    assert blanked == 1
    assert stale_row.rationale == ""
    assert fresh_row.rationale == "because the disk is nearly full"


async def test_the_blanking_org_predicate_is_pinned_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """Same shape, same reason as the expiry predicate: RLS masks it through a
    tenant session, so the mutation is only observable here."""
    a = await _world(admin_engine, "blank-org-a")
    b = await _world(admin_engine, "blank-org-b")
    mine = await _seed_action(admin_engine, a, created_at=NOW - timedelta(days=400))
    theirs = await _seed_action(admin_engine, b, created_at=NOW - timedelta(days=400))

    async with AsyncSession(admin_engine) as session:
        blanked = await blank_rationales(session, org_id=a.org_id, cutoff=NOW)
        await session.commit()

    mine_row = await _read(admin_engine, mine)
    theirs_row = await _read(admin_engine, theirs)
    assert mine_row is not None and theirs_row is not None
    assert (blanked, mine_row.rationale, theirs_row.rationale) == (
        1,
        "",
        "because the disk is nearly full",
    )


# --------------------------------------------------------------------------- #
# 4. The RESTRICT interaction the transcript sweep would otherwise discover in
#    production.
# --------------------------------------------------------------------------- #


async def test_the_transcript_sweep_over_a_session_with_a_dispatched_action(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**THREE ASSERTIONS, AND THE FIRST ONE IS "IT DID NOT RAISE" ONLY BECAUSE
    THE OTHER TWO SAY WHAT HAPPENED INSTEAD.**

    `ai_action.session_id` is `ON DELETE RESTRICT`, so an emptied session that
    carries an action can no longer be deleted. Left unhandled, the transcript
    sweep raises `ForeignKeyViolationError`, `sweep_all_orgs` contains it, and
    that org's retention silently stops -- the plan's named "most likely
    un-caught regression". The sweep must: delete the messages, KEEP the session,
    keep the `ai_action` row, and blank its rationale.
    """
    from app.core.ai.retention import sweep_org

    world = await _world(admin_engine, "sweep-restrict")
    old = NOW - timedelta(days=400)
    job_id = await _seed_job(admin_engine, world)
    action_id = await _seed_action(
        admin_engine,
        world,
        state="dispatched",
        job_id=job_id,
        confirmed_by=world.user_id,
        created_at=old,
        expires_at=old + timedelta(minutes=15),
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = await session.get(AiSession, world.session_id)
        assert ai_session is not None
        ai_session.created_at = old
        session.add(
            AiMessage(
                org_id=world.org_id,
                session_id=world.session_id,
                seq=0,
                role="user",
                content="q",
                created_at=old,
            )
        )
        await session.commit()

    async with org_session(world.org_id, engine=app_engine) as session:
        counts = await sweep_org(session, world.org_id, now=NOW)

    async with AsyncSession(admin_engine) as session:
        messages = list(
            (
                await session.execute(
                    select(AiMessage.id).where(AiMessage.session_id == world.session_id)
                )
            ).scalars()
        )
        surviving_session = await session.get(AiSession, world.session_id)
        action = await session.get(AiAction, action_id)

    assert messages == [], "the transcript was not swept"
    assert surviving_session is not None, "RESTRICT would have refused this DELETE"
    assert action is not None and action.state == "dispatched"
    assert action.rationale == ""
    assert counts["sessions_retained"] == 1, counts
    assert counts["rationales"] == 1, counts
    assert counts["sessions"] == 0, counts


async def test_an_emptied_session_with_no_action_is_still_deleted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The other half: the RESTRICT skip must be scoped to sessions that have an
    action, not applied to every session. Without this, "retention still deletes
    emptied conversations" stops being true and nothing says so."""
    from app.core.ai.retention import sweep_org

    world = await _world(admin_engine, "sweep-empty")
    old = NOW - timedelta(days=400)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = await session.get(AiSession, world.session_id)
        assert ai_session is not None
        ai_session.created_at = old
        session.add(
            AiMessage(
                org_id=world.org_id,
                session_id=world.session_id,
                seq=0,
                role="user",
                content="q",
                created_at=old,
            )
        )
        await session.commit()

    async with org_session(world.org_id, engine=app_engine) as session:
        counts = await sweep_org(session, world.org_id, now=NOW)

    async with AsyncSession(admin_engine) as session:
        assert await session.get(AiSession, world.session_id) is None
    assert (counts["sessions"], counts["sessions_retained"]) == (1, 0), counts


# --------------------------------------------------------------------------- #
# 5. Settle.
# --------------------------------------------------------------------------- #


async def _seed_job(admin_engine: AsyncEngine, world: _World, **kw: Any) -> UUID:
    from app.models import Job

    values: dict[str, Any] = {
        "org_id": world.org_id,
        "type": "inventory.collect",
        "target_type": "device",
        "target_id": world.device_id,
        "params": {},
        "nonce": uuid4().hex,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(kw)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(**values)
        session.add(job)
        await session.commit()
        return job.id


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        ("succeeded", "settled"),
        ("failed", "failed"),
        ("refused", "failed"),
        ("expired", "failed"),
    ],
)
async def test_settle_follows_the_job_to_its_terminal_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, terminal: str, expected: str
) -> None:
    """**FOUR TERMINAL STATES, TWO OUTCOMES.** `JobEngine` has three terminal
    writers (`complete`, `refuse`, `expire_due`), not the single "result path"
    §5.2 assumes -- so the parametrization covers all four terminals a job can
    reach, and two of them map to `failed` rather than `settled`. A test that
    only exercised `succeeded` would leave three paths unasserted, which is how
    a refused job's action sits in `dispatched` forever."""
    world = await _world(admin_engine, f"settle-{terminal}")
    job_id = await _seed_job(admin_engine, world)
    action_id = await _seed_action(admin_engine, world, state="dispatched", job_id=job_id)

    async with org_session(world.org_id, engine=app_engine) as session:
        await settle_action_for_job(
            session, org_id=world.org_id, job_id=job_id, terminal_state=terminal  # type: ignore[arg-type]
        )

    row = await _read(admin_engine, action_id)
    assert row is not None and row.state == expected


async def test_settling_twice_does_not_move_an_already_settled_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The state machine IS the idempotence (§0.5: no idempotency key). The
    observable value is the SECOND state: without the `state == 'dispatched'`
    predicate a late `failed` result would overwrite a `settled` row and the
    confirmation history would report the wrong outcome."""
    world = await _world(admin_engine, "settle-twice")
    job_id = await _seed_job(admin_engine, world)
    action_id = await _seed_action(admin_engine, world, state="dispatched", job_id=job_id)

    async with org_session(world.org_id, engine=app_engine) as session:
        await settle_action_for_job(
            session, org_id=world.org_id, job_id=job_id, terminal_state="succeeded"
        )
    async with org_session(world.org_id, engine=app_engine) as session:
        await settle_action_for_job(
            session, org_id=world.org_id, job_id=job_id, terminal_state="failed"
        )

    row = await _read(admin_engine, action_id)
    assert row is not None and row.state == "settled"


async def test_a_job_nobody_proposed_settles_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Non-vacuity for the negative: a nearly-matching row is seeded -- same org,
    same session, `dispatched`, a DIFFERENT job -- so "nothing moved" is evidence
    the `job_id` predicate ran rather than evidence the table was empty."""
    world = await _world(admin_engine, "settle-none")
    mine = await _seed_job(admin_engine, world)
    other = await _seed_job(admin_engine, world)
    action_id = await _seed_action(admin_engine, world, state="dispatched", job_id=mine)

    async with org_session(world.org_id, engine=app_engine) as session:
        await settle_action_for_job(
            session, org_id=world.org_id, job_id=other, terminal_state="succeeded"
        )

    row = await _read(admin_engine, action_id)
    assert row is not None and row.state == "dispatched"


async def test_the_job_engine_settles_through_its_own_terminal_transitions(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The hook, exercised through `JobEngine` rather than through the sweep
    function -- because "this layer runs no poller" is a claim about WHO calls it,
    and a test that only called `settle_action_for_job` would be green with the
    engine hook deleted."""
    from app.jobs.engine import JobEngine
    from app.models import Job, JobStatus

    class _Signer:
        async def sign(self, payload: bytes) -> str:
            return "sig"

    world = await _world(admin_engine, "settle-engine")
    # **`hello`, NOT `inventory.collect`, AND THE REASON IS ORDER-DEPENDENCE.**
    # `JobEngine.complete` runs the job type's `result_handler` for a succeeded
    # result and CONVERTS THE OUTCOME TO `failed` if it raises. `inventory.collect`
    # has one, so this test's verdict depended on whether an earlier test in the
    # session had imported the module that registers it -- green alone, red in a
    # batch, for a reason that has nothing to do with the hook under test. `hello`
    # has no result handler, so `succeeded` stays `succeeded`.
    job_id = await _seed_job(admin_engine, world, type="hello", status=JobStatus.running)
    action_id = await _seed_action(
        admin_engine, world, state="dispatched", job_id=job_id, job_type="hello"
    )

    engine = JobEngine(signer=_Signer())  # type: ignore[arg-type]
    async with org_session(world.org_id, engine=app_engine) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await engine.complete(session, job, state="succeeded", result={})

    row = await _read(admin_engine, action_id)
    assert row is not None and row.state == "settled"


# --------------------------------------------------------------------------- #
# 6. Per-org containment (§9.1, and `3d3ac07`'s lesson).
# --------------------------------------------------------------------------- #


def _settings(pg_cluster: PgCluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
    )


async def test_one_orgs_expiry_failure_does_not_leave_another_orgs_preview_confirmable(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """**THE OBSERVABLE VALUE IS THE HEALTHY ORG'S ROW, NOT THE RETURN CODE.**

    A stale preview that never expires is a confirmation token that outlived the
    situation that justified it, so an uncontained failure here is a security
    outcome and not merely a missed cycle. The doomed org raises something that
    is not a database error at all, which is the class of failure a guard naming
    `SQLAlchemyError` contains least well.
    """
    from app import worker

    doomed = await _world(admin_engine, "expguard-doomed")
    healthy = await _world(admin_engine, "expguard-healthy")
    stale = await _seed_action(admin_engine, healthy, expires_at=NOW - timedelta(seconds=1))

    async with AsyncSession(admin_engine) as session:
        org_total = len(list((await session.execute(select(Organization.id))).scalars()))
    assert org_total >= 2

    real = worker.ai_action_sweep_module.expire_stale_actions

    async def flaky(session: Any, *, org_id: UUID, now: Any = None) -> int:
        if org_id == doomed.org_id:
            raise RuntimeError("the audit hmac key is not loaded in this process")
        return await real(session, org_id=org_id, now=now)

    escaped: BaseException | None = None
    total = 0
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker.ai_action_sweep_module, "expire_stale_actions", flaky)
        with override_settings(_settings(pg_cluster, tmp_path)):
            try:
                total = await worker.ai_action_expiry_all_orgs(NOW)
            except Exception as exc:  # noqa: BLE001 - this IS the defect under test
                escaped = exc

    row = await _read(admin_engine, stale)
    assert row is not None
    assert row.state == "expired", (
        f"a healthy org's stale preview is still confirmable ({escaped!r} escaped the "
        f"org loop), so every org after the failing one went unswept this minute"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"
    assert total >= 1

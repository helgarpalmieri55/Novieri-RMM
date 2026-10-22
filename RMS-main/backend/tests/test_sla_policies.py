"""Policy CRUD and (queue, priority) resolution (docs/modules/sla/spec.md
§2.4, §3; Task 4).

Mirrors `test_sla_calendars.py`'s shape: behavioural org-scoping tests, a
real two-session race for the ON CONFLICT create path, and a "tenancy --
load-bearing" section that pins each `org_id` predicate on a raw BYPASSRLS
`admin_engine` session (RLS masks the application predicate through
`org_session`, so a cross-org test that goes through it alone would pass even
with the predicate deleted).
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent
from app.modules.sla.errors import SlaNotFoundError, SlaValidationError
from app.modules.sla.models import SlaPolicy, TicketSla
from app.modules.sla.policies import (
    _POLICY_EDITABLE_FIELDS,
    create_policy,
    delete_policy,
    get_policy,
    list_policies,
    resolve_policy,
    update_policy,
)
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user


async def _audit(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# create_policy
# ---------------------------------------------------------------------------


async def test_create_policy_stores_the_row_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        policy = await create_policy(
            session,
            org_id,
            actor_id,
            queue_id=queue_id,
            priority="high",
            calendar_id=calendar_id,
            response_target_minutes=30,
            resolution_target_minutes=240,
        )

    assert policy.queue_id == queue_id
    assert policy.priority == "high"
    assert policy.calendar_id == calendar_id
    assert policy.response_target_minutes == 30
    assert policy.resolution_target_minutes == 240
    assert policy.active is True
    assert policy.escalate_priority is False

    events = await _audit(api_env, org_id)
    assert [(e.action, e.target_id) for e in events] == [("sla.policy_created", str(policy.id))]


async def test_create_policy_persists_every_non_default_field(api_env) -> None:
    """Task 4 review F1. Every other `create_policy` test leaves
    `escalate_priority`, `escalate_to_queue_id` and `active` at their
    defaults, so three whole columns of the INSERT were unproven: mutants that
    dropped `escalate_to_queue_id` from `.values()` or hardcoded
    `active=True, escalate_priority=False` passed the entire suite. The
    concrete failure that let through: `create_policy(active=False)` silently
    creating an ACTIVE policy, which then wins resolution for every matching
    ticket.

    So: pass a NON-DEFAULT value for every column, then RELOAD the row from
    the database (not the returned in-memory object, which a hardcoding
    mutant would populate correctly from its own `.values()`) and assert each
    one individually."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    escalation_queue_id = await _seed_queue(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        created = await create_policy(
            session,
            org_id,
            actor_id,
            queue_id=queue_id,
            priority="urgent",
            calendar_id=calendar_id,
            response_target_minutes=15,
            resolution_target_minutes=90,
            escalate_priority=True,
            escalate_to_queue_id=escalation_queue_id,
            active=False,
        )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        stored = (
            await session.execute(select(SlaPolicy).where(SlaPolicy.id == created.id))
        ).scalar_one()

    assert stored.queue_id == queue_id
    assert stored.priority == "urgent"
    assert stored.calendar_id == calendar_id
    assert stored.response_target_minutes == 15
    assert stored.resolution_target_minutes == 90
    assert stored.escalate_priority is True
    assert stored.escalate_to_queue_id == escalation_queue_id
    assert stored.active is False

    # `active=False` must MEAN inactive: resolution must not return it.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert await resolve_policy(session, org_id, queue_id=queue_id, priority="urgent") is None


async def test_create_policy_allows_all_four_wildcard_shapes(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        specific = await create_policy(
            session, org_id, actor_id, queue_id=queue_id, priority="high",
            calendar_id=calendar_id, response_target_minutes=30, resolution_target_minutes=240,
        )
        queue_only = await create_policy(
            session, org_id, actor_id, queue_id=queue_id, priority=None,
            calendar_id=calendar_id, response_target_minutes=60, resolution_target_minutes=480,
        )
        priority_only = await create_policy(
            session, org_id, actor_id, queue_id=None, priority="high",
            calendar_id=calendar_id, response_target_minutes=90, resolution_target_minutes=600,
        )
        fallback = await create_policy(
            session, org_id, actor_id, queue_id=None, priority=None,
            calendar_id=calendar_id, response_target_minutes=120, resolution_target_minutes=960,
        )

    # Four DISTINCT rows, each with the shape it was asked for -- reloaded
    # from the database, not asserted against the in-memory return values.
    # (Task 4 review F4: this assertion used to compare a set to itself.)
    assert len({specific.id, queue_only.id, priority_only.id, fallback.id}) == 4

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (await session.execute(select(SlaPolicy))).scalars().all()
    assert {(r.queue_id, r.priority) for r in rows} == {
        (queue_id, "high"),
        (queue_id, None),
        (None, "high"),
        (None, None),
    }
    assert len(rows) == 4


async def test_create_policy_rejects_a_zero_or_negative_target(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(SlaValidationError, match="response_target_minutes"):
            await create_policy(
                session, org_id, actor_id, calendar_id=calendar_id,
                response_target_minutes=0, resolution_target_minutes=480,
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(SlaValidationError, match="resolution_target_minutes"):
            await create_policy(
                session, org_id, actor_id, calendar_id=calendar_id,
                response_target_minutes=60, resolution_target_minutes=-1,
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await session.execute(select(SlaPolicy))).scalars().all() == []


async def test_create_policy_rejects_an_invalid_priority(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)

    with pytest.raises(SlaValidationError, match="priority"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_id, actor_id, priority="critical", calendar_id=calendar_id,
                response_target_minutes=60, resolution_target_minutes=480,
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await session.execute(select(SlaPolicy))).scalars().all() == []


async def test_create_policy_rejects_a_cross_org_calendar(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_b = await _seed_calendar(api_env, org_b)

    with pytest.raises(SlaValidationError, match="does not resolve in this org"):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_a, actor_a, calendar_id=calendar_b,
                response_target_minutes=60, resolution_target_minutes=480,
            )


async def test_create_policy_rejects_a_cross_org_queue(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a)
    queue_b = await _seed_queue(api_env, org_b)

    with pytest.raises(SlaValidationError, match="does not resolve in this org"):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_a, actor_a, queue_id=queue_b, calendar_id=calendar_a,
                response_target_minutes=60, resolution_target_minutes=480,
            )


async def test_create_policy_rejects_a_cross_org_escalate_to_queue(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a)
    queue_b = await _seed_queue(api_env, org_b)

    with pytest.raises(SlaValidationError, match="does not resolve in this org"):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_a, actor_a, calendar_id=calendar_a, escalate_to_queue_id=queue_b,
                response_target_minutes=60, resolution_target_minutes=480,
            )


async def test_create_policy_rejects_an_inactive_calendar_for_an_active_policy(api_env) -> None:
    """Task 4 review F5, decided: `sla_calendar.active` was inert on this
    write path, so an admin could take a calendar out of service and Task 5's
    clock would still compute every deadline from it, silently. An ACTIVE
    policy may not reference a DEACTIVATED calendar -- symmetric with
    `calendars.delete_calendar`'s active-policy check, and with
    `_seed_calendar`'s own docstring ("a calendar an active policy must not be
    allowed to reference").

    An INACTIVE policy may: a retired policy is history, resolution never
    returns it, and forcing retired rows onto a live calendar would rewrite
    what was actually promised (spec §9)."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    retired_calendar_id = await _seed_calendar(api_env, org_id, active=False)

    with pytest.raises(SlaValidationError, match="is not active"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_id, actor_id, calendar_id=retired_calendar_id,
                response_target_minutes=60, resolution_target_minutes=480,
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await session.execute(select(SlaPolicy))).scalars().all() == []

    # An inactive policy against the same calendar is allowed.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        retired = await create_policy(
            session, org_id, actor_id, calendar_id=retired_calendar_id,
            response_target_minutes=60, resolution_target_minutes=480, active=False,
        )
    assert retired.active is False
    assert retired.calendar_id == retired_calendar_id


async def test_update_policy_rejects_an_inactive_calendar_for_an_active_policy(api_env) -> None:
    """The update half of F5, including the case a `calendar_id`-only check
    would miss: REACTIVATING a policy whose calendar was deactivated in the
    meantime. Both keys are checked against the EFFECTIVE (calendar, active)
    pair, not just whichever one was supplied."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    retired_calendar_id = await _seed_calendar(api_env, org_id, active=False)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)

    # Repointing an active policy at a retired calendar is refused.
    with pytest.raises(SlaValidationError, match="is not active"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(
                session, org_id, actor_id, policy_id, {"calendar_id": retired_calendar_id}
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await get_policy(session, org_id, policy_id)).calendar_id == calendar_id

    # Retiring the policy AND repointing it in one edit is allowed...
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await update_policy(
            session, org_id, actor_id, policy_id,
            {"calendar_id": retired_calendar_id, "active": False},
        )

    # ... but reactivating it while its calendar is still retired is not --
    # this edit supplies no `calendar_id` at all.
    with pytest.raises(SlaValidationError, match="is not active"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(session, org_id, actor_id, policy_id, {"active": True})

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await get_policy(session, org_id, policy_id)).active is False


@pytest.mark.parametrize(
    "queue_kw,priority",
    [("same", "high"), ("same", None), (None, "high"), (None, None)],
)
async def test_create_policy_duplicate_at_the_same_level_is_a_typed_error(
    api_env, queue_kw, priority
) -> None:
    """One test per resolution level (spec §3) -- each of the four partial
    unique indexes must independently reject a same-level duplicate as a
    typed `SlaValidationError`, never a raw `IntegrityError`."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id) if queue_kw == "same" else None

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_policy(
            session, org_id, actor_id, queue_id=queue_id, priority=priority,
            calendar_id=calendar_id, response_target_minutes=60, resolution_target_minutes=480,
        )

    with pytest.raises(SlaValidationError, match="already exists"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_id, actor_id, queue_id=queue_id, priority=priority,
                calendar_id=calendar_id, response_target_minutes=90, resolution_target_minutes=600,
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (await session.execute(select(SlaPolicy))).scalars().all()
    assert len(rows) == 1


async def test_concurrent_creates_at_the_same_level_yield_one_row(api_env) -> None:
    """The real race, two sessions, not a simulated one (module docstring,
    CLAUDE.md's TOCTOU discipline). Targets the `(NULL, NULL)` fallback
    level -- the level whose partial index has the fewest columns, and so
    the one most likely to be silently miss-targeted by a wrong
    `index_elements`/`index_where` pair."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    barrier = asyncio.Barrier(2)

    async def attempt() -> bool:
        try:
            async with org_session(org_id, engine=api_env.app_engine) as session:
                await session.execute(select(SlaPolicy.id))
                await asyncio.wait_for(barrier.wait(), timeout=30)
                await create_policy(
                    session, org_id, actor_id, calendar_id=calendar_id,
                    response_target_minutes=60, resolution_target_minutes=480,
                )
            return True
        except SlaValidationError:
            return False

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in results), results
    assert sorted(results) == [False, True], results

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (await session.execute(select(SlaPolicy))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# get_policy / list_policies
# ---------------------------------------------------------------------------


async def test_get_policy_raises_not_found_for_a_missing_id(api_env) -> None:
    org_id = await api_env.create_org()
    with pytest.raises(SlaNotFoundError):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await get_policy(session, org_id, uuid4())


async def test_list_policies_shows_only_this_orgs_rows_under_rls(api_env) -> None:
    org_id = await api_env.create_org()
    other_org = await api_env.create_org("other")
    calendar_id = await _seed_calendar(api_env, org_id)
    other_calendar_id = await _seed_calendar(api_env, other_org)
    p1 = await _seed_policy(api_env, org_id, calendar_id=calendar_id)
    p2 = await _seed_policy(api_env, org_id, calendar_id=calendar_id, priority="high")
    await _seed_policy(api_env, other_org, calendar_id=other_calendar_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        policies = await list_policies(session, org_id)

    assert {p.id for p in policies} == {p1, p2}


# ---------------------------------------------------------------------------
# update_policy
# ---------------------------------------------------------------------------


async def test_update_policy_applies_supplied_fields_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id, priority="low")

    async with org_session(org_id, engine=api_env.app_engine) as session:
        updated = await update_policy(
            session, org_id, actor_id, policy_id,
            {"priority": "urgent", "response_target_minutes": 15},
        )

    assert updated.priority == "urgent"
    assert updated.response_target_minutes == 15

    events = await _audit(api_env, org_id)
    assert events[-1].action == "sla.policy_updated"
    assert events[-1].meta["changed"] == ["priority", "response_target_minutes"]


async def test_update_policy_persists_every_editable_field(api_env) -> None:
    """Task 4 review F1, the update half. `_POLICY_EDITABLE_FIELDS` has eight
    entries and the shipped suite exercised three; reducing it to
    `("priority", "response_target_minutes", "active")` -- i.e. making
    `update_policy` silently ignore `queue_id`, `calendar_id`,
    `resolution_target_minutes`, `escalate_priority` and
    `escalate_to_queue_id` -- passed all 32 tests.

    Each field is changed ON ITS OWN, in its own transaction, and RELOADED
    from the database, so dropping any single one from the write path turns
    this test red on that field alone. The `set(...) == set(...)` assertion is
    the tripwire: a NEW editable field added to the module without a case here
    fails immediately rather than shipping unpinned."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    other_calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    other_queue_id = await _seed_queue(api_env, org_id)
    escalation_queue_id = await _seed_queue(api_env, org_id)
    policy_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="low"
    )

    # field -> value to write (each differs from the seeded value above).
    # `active` is last: it is the only edit that changes what resolution sees.
    edits: list[tuple[str, object]] = [
        ("queue_id", other_queue_id),
        ("priority", "urgent"),
        ("calendar_id", other_calendar_id),
        ("response_target_minutes", 15),
        ("resolution_target_minutes", 90),
        ("escalate_priority", True),
        ("escalate_to_queue_id", escalation_queue_id),
        ("active", False),
    ]
    assert {field for field, _ in edits} == set(_POLICY_EDITABLE_FIELDS)

    for field, value in edits:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(session, org_id, actor_id, policy_id, {field: value})

        async with org_session(org_id, engine=api_env.app_engine) as session:
            stored = (
                await session.execute(select(SlaPolicy).where(SlaPolicy.id == policy_id))
            ).scalar_one()
        assert getattr(stored, field) == value, f"{field} did not round-trip"

    # And every earlier edit is still there -- no field clobbered a previous one.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        final = (
            await session.execute(select(SlaPolicy).where(SlaPolicy.id == policy_id))
        ).scalar_one()
    assert {field: getattr(final, field) for field, _ in edits} == dict(edits)


async def test_update_policy_rejects_an_unknown_field(api_env) -> None:
    """Task 4 review F6: a typo'd key used to return success having changed
    nothing, and audit a no-op edit. The whitelist still decides what may be
    written -- `org_id` remains unreachable either way -- but an unknown key
    is now a typed error rather than silence."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)

    with pytest.raises(SlaValidationError, match="respones_target_minutes"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(
                session, org_id, actor_id, policy_id, {"respones_target_minutes": 5}
            )

    # A non-editable REAL column is refused the same way, and no audit event
    # was written for either attempt.
    with pytest.raises(SlaValidationError, match="org_id"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(session, org_id, actor_id, policy_id, {"org_id": uuid4()})

    assert [e.action for e in await _audit(api_env, org_id)] == []


async def test_update_policy_stores_the_coerced_value(api_env) -> None:
    """Task 4 review F7: validation coerced (`int(...)`) but the assignment
    stored `fields[field]` raw, so a string that passed validation was
    persisted unconverted -- Postgres coerced it on write, but the in-memory
    attribute and the `!=` change-detection disagreed with the column."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        updated = await update_policy(
            session,
            org_id,
            actor_id,
            policy_id,
            {"response_target_minutes": "15", "resolution_target_minutes": "90"},
        )

    assert updated.response_target_minutes == 15
    assert isinstance(updated.response_target_minutes, int)
    assert updated.resolution_target_minutes == 90
    assert isinstance(updated.resolution_target_minutes, int)

    # And a string equal to the CURRENT value is not reported as a change.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await update_policy(session, org_id, actor_id, policy_id, {"response_target_minutes": "15"})
    events = await _audit(api_env, org_id)
    assert events[-1].meta["changed"] == []


async def test_update_policy_can_set_a_nullable_field_to_none(api_env) -> None:
    """`queue_id`, `priority`, `escalate_to_queue_id` are legal wildcard
    targets -- explicitly setting one to `None` must be a real edit, not a
    silent no-op (module docstring's `_POLICY_EDITABLE_FIELDS` discipline)."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    policy_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="high"
    )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        updated = await update_policy(session, org_id, actor_id, policy_id, {"priority": None})

    assert updated.priority is None
    assert updated.queue_id == queue_id


async def test_update_policy_rejects_null_for_a_non_nullable_field(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)

    with pytest.raises(SlaValidationError, match="cannot be null"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(session, org_id, actor_id, policy_id, {"active": None})


async def test_update_policy_colliding_with_another_level_is_a_typed_error(api_env) -> None:
    """The SAVEPOINT is what this test proves, and it proves it INSIDE the
    session (Task 4 review F3).

    The previous shape put `pytest.raises` OUTSIDE the `org_session` block, so
    the outer transaction rolled back on the way out no matter what: replacing
    `session.begin_nested()` with `if True:` left the whole suite green while
    the docstring claimed the savepoint was load-bearing. A failed flush
    without a savepoint poisons the enclosing transaction, so EVERY assertion
    below runs on the same still-open session after the failure:

      1. the typed error is raised at all,
      2. the session is still usable (`get_policy` succeeds rather than
         raising `PendingRollbackError`) -- this is what a `PATCH` route
         catching `SlaValidationError` to return 409 depends on,
      3. only the inner work was undone (the victim still reads `low`),
      4. a subsequent write on the SAME session still commits.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    await _seed_policy(api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="high")
    victim_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="low"
    )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(SlaValidationError, match="already exists"):
            await update_policy(session, org_id, actor_id, victim_id, {"priority": "high"})

        # The surrounding transaction survived the failed flush.
        reloaded = await get_policy(session, org_id, victim_id)
        assert reloaded.priority == "low"  # only the SAVEPOINT's work rolled back

        # ... and it can still be written to.
        await update_policy(session, org_id, actor_id, victim_id, {"response_target_minutes": 42})

    # The outer transaction committed: the recovery write landed, the
    # colliding one did not.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        victim = await get_policy(session, org_id, victim_id)
    assert victim.priority == "low"
    assert victim.response_target_minutes == 42


async def test_update_policy_rejects_a_cross_org_calendar(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a)
    calendar_b = await _seed_calendar(api_env, org_b)
    policy_id = await _seed_policy(api_env, org_a, calendar_id=calendar_a)

    with pytest.raises(SlaValidationError, match="does not resolve in this org"):
        async with org_session(org_a, engine=api_env.app_engine) as session:
            await update_policy(session, org_a, actor_a, policy_id, {"calendar_id": calendar_b})


# ---------------------------------------------------------------------------
# delete_policy
# ---------------------------------------------------------------------------


async def test_delete_policy_removes_the_row_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id)
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_policy(session, org_id, actor_id, policy_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(SlaNotFoundError):
            await get_policy(session, org_id, policy_id)

    events = await _audit(api_env, org_id)
    assert events[-1].action == "sla.policy_deleted"


async def test_delete_policy_referenced_by_ticket_sla_is_refused_and_the_row_survives(
    api_env, a_ticket_for
) -> None:
    """Spec §9 / Task 4 brief item 5: deleting a policy must leave existing
    `ticket_sla` rows intact. Since `ticket_sla.policy_id` is a RESTRICT FK
    (migration 0033), this project's deliberate choice is to REFUSE the
    delete outright (named typed error) rather than let it 500, and to
    document `active=False` as the retirement path -- see `delete_policy`'s
    docstring for the full reasoning."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    policy_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id)
    ticket = await a_ticket_for(org_id, "sla-policy-delete")

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            TicketSla(
                org_id=org_id,
                ticket_id=ticket.id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=now,
                resolution_due_at=now,
                policy_source="initial",
            )
        )
        await session.commit()

    with pytest.raises(SlaValidationError, match=str(ticket.id)) as excinfo:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await delete_policy(session, org_id, actor_id, policy_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        # The policy row survives, untouched.
        survivor = await get_policy(session, org_id, policy_id)
    assert survivor.id == policy_id

    # The escape hatch: deactivate instead.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        deactivated = await update_policy(session, org_id, actor_id, policy_id, {"active": False})
    assert deactivated.active is False

    # The refusal message names the ticket readably -- not a Python list repr
    # (Task 4 review F8).
    assert f"ticket {ticket.id}" in str(excinfo.value)
    assert "[" not in str(excinfo.value)


async def test_a_referenced_policy_can_be_retired_and_replaced_at_the_same_level(
    api_env, a_ticket_for
) -> None:
    """Task 4 review F2, end to end. This is THE test for the finding: before
    migration 0036 the retirement path `delete_policy` points admins at was a
    dead end, because the four unique indexes were not `active`-scoped. An
    admin who could not delete a referenced policy could deactivate it, and
    then could not put ANYTHING in its place -- the replacement was refused
    with "an active policy for (...) already exists in this org" when no
    active policy existed. There was no way to change the promise for a
    `(queue, priority)` cell once a ticket referenced it.

    The whole lifecycle, in order:
      1. create a policy at (queue, 'high') and resolve to it;
      2. a ticket references it via `ticket_sla`;
      3. `delete_policy` refuses (§9 -- the history must survive);
      4. `update_policy(active=False)` retires it;
      5. a REPLACEMENT at the same level is created -- this step is what
         0036 unblocks;
      6. `resolve_policy` returns the replacement, not the retired one;
      7. the retired row and its `ticket_sla` row are both still there,
         untouched.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    calendar_id = await _seed_calendar(api_env, org_id, windows=[])
    queue_id = await _seed_queue(api_env, org_id)

    # 1. The original policy, and it is what resolution returns.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        original = await create_policy(
            session, org_id, actor_id, queue_id=queue_id, priority="high",
            calendar_id=calendar_id, response_target_minutes=30, resolution_target_minutes=240,
        )
        original_id = original.id
    async with org_session(org_id, engine=api_env.app_engine) as session:
        resolved = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")
    assert resolved is not None and resolved.id == original_id

    # 2. A ticket is promised that policy.
    ticket = await a_ticket_for(org_id, "sla-policy-retire-replace")
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            TicketSla(
                org_id=org_id,
                ticket_id=ticket.id,
                policy_id=original_id,
                calendar_id=calendar_id,
                response_due_at=now,
                resolution_due_at=now,
                policy_source="initial",
            )
        )
        await session.commit()

    # 3. Deleting it is refused -- the history must survive (spec §9).
    with pytest.raises(SlaValidationError, match="cannot be deleted"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await delete_policy(session, org_id, actor_id, original_id)

    # 4. Retire it instead, exactly as that message instructs.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await update_policy(session, org_id, actor_id, original_id, {"active": False})

    # 5. The replacement at the SAME level now succeeds (pre-0036: refused).
    async with org_session(org_id, engine=api_env.app_engine) as session:
        replacement = await create_policy(
            session, org_id, actor_id, queue_id=queue_id, priority="high",
            calendar_id=calendar_id, response_target_minutes=5, resolution_target_minutes=60,
        )
        replacement_id = replacement.id
    assert replacement_id != original_id

    # 6. Resolution returns the REPLACEMENT, not the retired policy.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        resolved = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")
    assert resolved is not None
    assert resolved.id == replacement_id
    assert resolved.response_target_minutes == 5

    # 7. The retired policy and the ticket's SLA record both survive intact.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        retired = await get_policy(session, org_id, original_id)
        assert retired.active is False
        assert retired.response_target_minutes == 30
        sla_rows = (await session.execute(select(TicketSla))).scalars().all()
    assert [(r.ticket_id, r.policy_id) for r in sla_rows] == [(ticket.id, original_id)]

    # And a SECOND active policy at that level is still refused -- 0036
    # loosened uniqueness to "one ACTIVE policy per level", not to nothing.
    with pytest.raises(SlaValidationError, match="already exists"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await create_policy(
                session, org_id, actor_id, queue_id=queue_id, priority="high",
                calendar_id=calendar_id, response_target_minutes=7, resolution_target_minutes=70,
            )

    # ... and reactivating the retired one while the replacement is active is
    # refused too, by the same guard on the update path.
    with pytest.raises(SlaValidationError, match="already exists"):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await update_policy(session, org_id, actor_id, original_id, {"active": True})


# ---------------------------------------------------------------------------
# resolve_policy -- spec §3
# ---------------------------------------------------------------------------


async def test_resolve_policy_returns_none_when_nothing_matches(api_env) -> None:
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    # A policy that CANNOT match the query below (different queue).
    other_queue_id = await _seed_queue(api_env, org_id)
    await _seed_policy(api_env, org_id, calendar_id=calendar_id, queue_id=other_queue_id)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")

    assert result is None

    # And no ticket_sla row was created -- resolve_policy is read-only.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (await session.execute(select(TicketSla))).scalars().all() == []


async def test_resolve_policy_only_considers_active_policies(api_env) -> None:
    """`_seed_policy(active=False)` exists precisely so this can be tested
    (Task 4 brief). An inactive policy at the most specific level must NOT
    win, and must NOT be found at all if it's the only candidate."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="high", active=False
    )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")

    assert result is None


async def test_resolve_policy_inactive_specific_falls_through_to_active_fallback(api_env) -> None:
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="high", active=False
    )
    fallback_id = await _seed_policy(api_env, org_id, calendar_id=calendar_id, active=True)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")

    assert result is not None
    assert result.id == fallback_id


async def test_resolve_policy_order_the_most_specific_wins_and_the_fallback_walk_is_correct(
    api_env,
) -> None:
    """The order-detecting test the brief demands: all four levels are
    seeded SIMULTANEOUSLY, so a test with only one candidate present could
    never catch a reversed order. Removing the winner one level at a time
    and re-resolving after each removal proves the exact fallback sequence,
    not just that SOME policy was returned."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)

    level1 = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority="high",
        response_minutes=1,
    )
    level2 = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=None,
        response_minutes=2,
    )
    level3 = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=None, priority="high",
        response_minutes=3,
    )
    level4 = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=None, priority=None,
        response_minutes=4,
    )

    async def resolved_id():
        async with org_session(org_id, engine=api_env.app_engine) as session:
            result = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")
        return result.id if result else None

    # All four present: the most specific, level 1, wins.
    assert await resolved_id() == level1

    # Remove level 1 -> level 2 (queue, NULL) wins, not level 3 or 4.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_policy(session, org_id, await _seed_user(api_env, org_id), level1)
    assert await resolved_id() == level2

    # Remove level 2 -> level 3 (NULL, priority) wins, not level 4.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_policy(session, org_id, await _seed_user(api_env, org_id), level2)
    assert await resolved_id() == level3

    # Remove level 3 -> level 4 (NULL, NULL) is the last resort.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_policy(session, org_id, await _seed_user(api_env, org_id), level3)
    assert await resolved_id() == level4

    # Remove level 4 -> nothing left, None.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await delete_policy(session, org_id, await _seed_user(api_env, org_id), level4)
    assert await resolved_id() is None


async def test_resolve_policy_queue_only_level_wins_over_priority_only(api_env) -> None:
    """The reverse pairing to the main order test: with levels 2 and 3 both
    present (and 1/4 absent), level 2 (queue, NULL) must still beat level 3
    (NULL, priority) -- this is the one adjacent-level comparison the
    "remove one at a time" walk above never directly exercises, because it
    removes strictly in rank order."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id)
    queue_id = await _seed_queue(api_env, org_id)

    level2 = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, queue_id=queue_id, priority=None
    )
    await _seed_policy(api_env, org_id, calendar_id=calendar_id, queue_id=None, priority="high")

    async with org_session(org_id, engine=api_env.app_engine) as session:
        result = await resolve_policy(session, org_id, queue_id=queue_id, priority="high")

    assert result is not None
    assert result.id == level2


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session (see module
# docstring; mirrors test_sla_calendars.py's section of the same name).
# ---------------------------------------------------------------------------


async def test_tenancy_get_policy_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b)
    policy_b = await _seed_policy(api_env, org_b, calendar_id=calendar_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaNotFoundError):
            await get_policy(session, org_a, policy_b)


async def test_tenancy_resolve_calendar_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`_resolve_calendar`'s `org_id` predicate (shared by `create_policy`
    and `update_policy`) is what stops org A's policy from pointing at org
    B's calendar. On the BYPASSRLS admin session, `SlaCalendar.id ==
    calendar_b` alone would match regardless of org -- only the explicit
    `org_id` predicate rejects it."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_b = await _seed_calendar(api_env, org_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="does not resolve in this org"):
            await create_policy(
                session, org_a, actor_a, calendar_id=calendar_b,
                response_target_minutes=60, resolution_target_minutes=480,
            )


async def test_tenancy_resolve_queue_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`_resolve_queue`'s `org_id` predicate -- shared by BOTH the
    `queue_id` and `escalate_to_queue_id` call sites in `create_policy`/
    `update_policy`, since both go through the same function and the same
    query. Pinning it once via `queue_id` proves the shared code path;
    `escalate_to_queue_id` exercises the identical line."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a)
    queue_b = await _seed_queue(api_env, org_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="does not resolve in this org"):
            await create_policy(
                session, org_a, actor_a, queue_id=queue_b, calendar_id=calendar_a,
                response_target_minutes=60, resolution_target_minutes=480,
            )


async def test_tenancy_list_policies_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Important: `list_policies`' `org_id` predicate is the ONLY
    application-layer scoping on an otherwise unqualified list query (mirrors
    `test_sla_calendars.py`'s identical finding for `list_calendars`)."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b)
    await _seed_policy(api_env, org_b, calendar_id=calendar_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await list_policies(session, org_a) == []


async def test_tenancy_resolve_policy_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`resolve_policy`'s `org_id` predicate is what stops org A's ticket
    picking up org B's fallback policy. Constructed so org B's policy is the
    ONLY row that could possibly match -- org A has none at all -- so if the
    predicate were dropped, org A would resolve org B's policy instead of
    `None`."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b)
    await _seed_policy(api_env, org_b, calendar_id=calendar_b)  # (NULL, NULL) fallback

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await resolve_policy(session, org_a, queue_id=None, priority="high")

    assert result is None


async def test_tenancy_delete_policy_ticket_sla_check_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, a_ticket_for
) -> None:
    """The `ticket_sla` precheck in `delete_policy` is deliberately
    `org_id`-scoped: it must never NAME another org's ticket id to a caller
    in a different org (mirrors `test_sla_calendars.py`'s identical finding
    for `delete_calendar`'s own `ticket_sla` precheck). Constructed directly
    on the BYPASSRLS admin session -- the FK permits a `ticket_sla` row to
    reference another org's policy; no application path produces this."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    calendar_a = await _seed_calendar(api_env, org_a, windows=[])
    policy_a = await _seed_policy(api_env, org_a, calendar_id=calendar_a)
    ticket_b = await a_ticket_for(org_b, "cross-org-policy-delete")

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            TicketSla(
                org_id=org_b,
                ticket_id=ticket_b.id,
                policy_id=policy_a,
                calendar_id=calendar_a,
                response_due_at=now,
                resolution_due_at=now,
                policy_source="initial",
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # The org-scoped precheck legitimately finds nothing (the rogue row
        # is org B's), so this either succeeds outright or -- if it did not
        # -- would at minimum never name org B's ticket. It must NOT raise
        # naming ticket_b; the FK protects the actual delete regardless.
        with pytest.raises(SlaValidationError) as excinfo:
            await delete_policy(session, org_a, actor_a, policy_a)
        await session.rollback()
    assert str(ticket_b.id) not in str(excinfo.value)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (
            await session.execute(select(SlaPolicy).where(SlaPolicy.id == policy_a))
        ).scalar_one_or_none() is not None

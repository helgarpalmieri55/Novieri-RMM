"""The dial, which can only ever lower (docs/modules/ai-automation.md §4.2).

THE PARAMETRIZATION IS THE POINT OF THIS FILE, and it is built so that no two
cases share an answer where sharing would hide something. This project's second
named fixture-blindness trap is a `@pytest.mark.parametrize` whose every case
expects the same value: it proves nothing about the function that produced it,
and it bites hardest exactly here -- if every `(ceiling, autonomy)` pair expected
`require_approval`, deleting the v1 clamp from `effective_autonomy` would pass.

So the table below contains a case the CEILING wins, a case the CATALOGUE wins,
a case the CLAMP wins, two cases that resolve to `off`, and an identity case.
Deleting any one of the three arguments to `min_level` changes an OBSERVED
VALUE.

The org predicate is pinned ON THE ADMIN ENGINE. Through `org_session` a
cross-org read passes with `AiAutonomyCeiling.org_id == org_id` deleted, because
RLS refuses the other org's row first -- and the dial is read by the confirm
path, which runs wherever its caller's session runs.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ai.autonomy import (
    LEVELS,
    ceiling_for,
    effective_autonomy,
    get_ceilings,
    min_level,
    resolve_action_autonomy,
    set_ceiling,
)
from app.core.ai.errors import AiValidationError
from app.core.tenancy import org_session
from tests.ai_admin_fakes import ai_user
from tests.health_fixtures import seed_org
from tests.test_ai_autonomy_ceiling import _seed_ceiling

AI_PREFIX = "/api/v1/ai"


class _Row:
    """A catalogue row, as the three fields the resolver reads. A stand-in for
    `ApprovedAction` so the pure half of §4.2 is testable without a database."""

    def __init__(self, *, risk_class: str, autonomy: str, personas: list[str]) -> None:
        self.risk_class = risk_class
        self.autonomy = autonomy
        self.personas = personas


async def _add_ceiling(
    engine: AsyncEngine, org_id: Any, *, persona: str, risk_class: str, level: str
) -> None:
    from app.models import AiAutonomyCeiling

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            AiAutonomyCeiling(
                org_id=org_id, persona=persona, risk_class=risk_class, level=level
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# 1. The resolution itself.
# --------------------------------------------------------------------------- #


def test_levels_are_ordered_weakest_first_and_are_one_longer_than_the_dial() -> None:
    """`autonomous` exists in the RESOLVER's vocabulary only as an input that
    can be reduced away -- an `approved_action` may carry it, a ceiling may
    not."""
    from app.models.ai import CEILING_LEVELS

    assert LEVELS == ("off", "suggest", "require_approval", "autonomous")
    assert set(CEILING_LEVELS) < set(LEVELS)
    assert "autonomous" not in CEILING_LEVELS


@pytest.mark.parametrize(
    ("ceiling", "autonomy", "expected"),
    [
        # THE CEILING WINS: the org dialled this persona below what the row allows.
        ("suggest", "require_approval", "suggest"),
        # THE CATALOGUE WINS: the org allows more than the row does.
        ("require_approval", "suggest", "suggest"),
        # THE V1 CLAMP WINS -- AND THIS CASE PASSES A CEILING THE DATABASE
        # CANNOT STORE, DELIBERATELY. See
        # `test_the_v1_clamp_is_redundant_against_the_database_and_kept_anyway`
        # below: with a ceiling drawn from `CEILING_LEVELS` the clamp is
        # unreachable, so a case using one would go GREEN with the clamp
        # deleted. This is the input the clamp actually covers.
        ("autonomous", "autonomous", "require_approval"),
        # And the storable neighbour, which must give the same answer for a
        # different reason (the ceiling, not the clamp).
        ("require_approval", "autonomous", "require_approval"),
        # `off` BEATS EVERYTHING, from either side.
        ("off", "autonomous", "off"),
        ("require_approval", "off", "off"),
        # The identity case, so "the minimum" cannot be satisfied by a function
        # that always returns the weakest value in the vocabulary.
        ("require_approval", "require_approval", "require_approval"),
    ],
)
def test_effective_autonomy(ceiling: str, autonomy: str, expected: str) -> None:
    assert effective_autonomy(ceiling=ceiling, action_autonomy=autonomy) == expected


def test_the_v1_clamp_is_redundant_against_the_database_and_kept_anyway() -> None:
    """**A FINDING ABOUT §4.2, RECORDED RATHER THAN PAPERED OVER.**

    §4.2 writes the resolution as
    `min_level(ceiling, action.autonomy, 'require_approval')`, and the third
    argument is presented as the thing that stops an AI persona reaching
    `autonomous`. Given §4.1's CHECK it cannot be: `CEILING_LEVELS` stops at
    `require_approval`, so `min_level(ceiling, ...)` is already at most
    `require_approval` for every ceiling the database can hold. Deleting the
    clamp changes NO answer that a stored row can produce -- verified by running
    that mutation, which left this file entirely green until the case above was
    added.

    It is kept, and the reason is written down here so somebody tidying up does
    not delete a control whose effect is currently zero: the clamp is what makes
    the resolver safe against a `ceiling_for` that returns a value from OUTSIDE
    `CEILING_LEVELS` -- a widened CHECK, a fixture writing on the admin engine,
    a future migration. The database CHECK is the guarantee; this is the second
    line, and the case above is what makes it falsifiable at all.
    """
    from app.models.ai import CEILING_LEVELS

    # The claim, made precisely: for every STORABLE ceiling the clamp changes
    # nothing.
    for ceiling in CEILING_LEVELS:
        for autonomy in LEVELS:
            assert min_level(ceiling, autonomy) == min_level(ceiling, autonomy, "require_approval")
    # And for the one value it cannot store, it changes everything.
    assert min_level("autonomous", "autonomous") == "autonomous"
    assert effective_autonomy(ceiling="autonomous", action_autonomy="autonomous") == (
        "require_approval"
    )


def test_min_level_treats_an_unknown_level_as_off() -> None:
    """A level this module does not recognise is a level nobody reasoned about.
    Treating it as absent would let a future vocabulary entry read as
    permission."""
    assert min_level("require_approval", "not_a_level") == "off"
    assert min_level() == "off"
    # And it always returns a KNOWN level, never the unrecognised string.
    assert min_level("not_a_level") in LEVELS


# --------------------------------------------------------------------------- #
# 2. Deny by default, and the persona gate before the dial.
# --------------------------------------------------------------------------- #


async def test_a_missing_row_resolves_to_off(admin_engine: AsyncEngine) -> None:
    """**A DIAL THAT FAILED OPEN WOULD BE THE WHOLE LAYER FAILING OPEN**, and it
    would be invisible: an org that configured nothing would silently be at the
    catalogue's own level.

    Not vacuous: a ceiling row IS seeded for the same org and the same persona
    at a DIFFERENT risk class, so `off` is evidence the lookup discriminated
    rather than evidence the table is empty.
    """
    org = await seed_org(admin_engine, "dial-missing")
    await _add_ceiling(
        admin_engine, org.org_id, persona="admin", risk_class="low", level="require_approval"
    )

    async with AsyncSession(admin_engine) as session:
        assert (
            await ceiling_for(session, org_id=org.org_id, persona="admin", risk_class="high")
            == "off"
        )
        row = _Row(risk_class="high", autonomy="require_approval", personas=["admin"])
        assert (
            await resolve_action_autonomy(
                session, org_id=org.org_id, persona="admin", action=row
            )
            == "off"
        )
        # And the seeded risk class still answers, so the read works at all.
        assert (
            await ceiling_for(session, org_id=org.org_id, persona="admin", risk_class="low")
            == "require_approval"
        )


async def test_a_persona_the_catalogue_row_does_not_carry_is_off(
    admin_engine: AsyncEngine,
) -> None:
    """§4.2's last line: the answer is `off` BEFORE the dial is consulted at all.

    Asserted with a PERMISSIVE ceiling row in place, so the case cannot be
    satisfied by the missing-row default, and with a control row that DOES carry
    the persona so `off` is about `personas` and not about the dial.
    """
    org = await seed_org(admin_engine, "dial-persona")
    await _add_ceiling(
        admin_engine, org.org_id, persona="admin", risk_class="low", level="require_approval"
    )
    absent = _Row(risk_class="low", autonomy="autonomous", personas=["health"])
    carried = _Row(risk_class="low", autonomy="autonomous", personas=["health", "admin"])

    async with AsyncSession(admin_engine) as session:
        assert (
            await resolve_action_autonomy(
                session, org_id=org.org_id, persona="admin", action=absent
            )
            == "off"
        )
        assert (
            await resolve_action_autonomy(
                session, org_id=org.org_id, persona="admin", action=carried
            )
            == "require_approval"
        )


# --------------------------------------------------------------------------- #
# 3. The org predicate, ON THE ADMIN ENGINE.
# --------------------------------------------------------------------------- #


async def test_another_orgs_ceiling_does_not_answer_this_orgs_question(
    admin_engine: AsyncEngine,
) -> None:
    """Through `org_session` this passes with the `org_id` predicate DELETED,
    because RLS refuses org B's row first. On the admin engine the deletion
    prints org B's level.

    Org B's row is SAME persona, SAME risk class: nothing but `org_id`
    separates the two, so no other predicate can mask a missing one.
    """
    org_a = await seed_org(admin_engine, "dial-a")
    org_b = await seed_org(admin_engine, "dial-b")
    await _add_ceiling(
        admin_engine, org_b.org_id, persona="admin", risk_class="high", level="require_approval"
    )

    async with AsyncSession(admin_engine) as session:
        assert (
            await ceiling_for(session, org_id=org_a.org_id, persona="admin", risk_class="high")
            == "off"
        )
        assert await get_ceilings(session, org_id=org_a.org_id) == {}
        # The control: org B's own question IS answered, so `off` above is not
        # "this function never finds anything".
        assert (
            await ceiling_for(session, org_id=org_b.org_id, persona="admin", risk_class="high")
            == "require_approval"
        )
        assert await get_ceilings(session, org_id=org_b.org_id) == {
            ("admin", "high"): "require_approval"
        }


# --------------------------------------------------------------------------- #
# 4. The write: audited with both levels, bounded, upserted on the real PK.
# --------------------------------------------------------------------------- #


async def test_set_ceiling_upserts_and_audits_both_levels(admin_engine: AsyncEngine) -> None:
    from app.models import AuditEvent

    org = await seed_org(admin_engine, "dial-write")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = await set_ceiling(
            session,
            org_id=org.org_id,
            persona="admin",
            risk_class="low",
            level="suggest",
            actor_id=org.user_id,
        )
        second = await set_ceiling(
            session,
            org_id=org.org_id,
            persona="admin",
            risk_class="low",
            level="require_approval",
            actor_id=org.user_id,
        )
        await session.commit()

    # The upsert REPLACED rather than raising on the primary key.
    assert (first, second) == (None, "suggest")
    async with AsyncSession(admin_engine) as session:
        assert (
            await ceiling_for(session, org_id=org.org_id, persona="admin", risk_class="low")
            == "require_approval"
        )
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org.org_id)
                    .where(AuditEvent.action == "ai.autonomy_changed")
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 2
    # BOTH LEVELS. A change record that cannot say what it changed FROM answers
    # nothing the day somebody asks why an action was proposable last Tuesday.
    assert (events[0].meta["old_level"], events[0].meta["new_level"]) == ("off", "suggest")
    assert (events[1].meta["old_level"], events[1].meta["new_level"]) == (
        "suggest",
        "require_approval",
    )
    # And no free text anywhere in it (§8.3).
    assert set(events[1].meta) == {"persona", "risk_class", "old_level", "new_level"}


@pytest.mark.parametrize(
    ("persona", "risk_class", "level"),
    [
        ("admin", "low", "autonomous"),
        ("admin", "low", "on"),
        ("requester", "low", "suggest"),
        ("admin", "critical", "suggest"),
    ],
)
async def test_set_ceiling_refuses_an_unknown_value_and_writes_nothing(
    admin_engine: AsyncEngine, persona: str, risk_class: str, level: str
) -> None:
    """Refused with a 400-shaped error, AND NOTHING IS STORED -- asserted on the
    table, not on the raise. The DATABASE CHECK is still the guarantee (§0.4);
    this is the friendly refusal in front of it, and both are tested (the CHECK
    in `test_ai_autonomy_ceiling.py`)."""
    from app.models import AiAutonomyCeiling

    org = await seed_org(admin_engine, "dial-bad")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(AiValidationError):
            await set_ceiling(
                session,
                org_id=org.org_id,
                persona=persona,
                risk_class=risk_class,
                level=level,
                actor_id=org.user_id,
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = (
            (
                await session.execute(
                    select(AiAutonomyCeiling).where(AiAutonomyCeiling.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert stored == []


# --------------------------------------------------------------------------- #
# 5. The routes.
# --------------------------------------------------------------------------- #


async def test_the_dial_routes_require_ai_admin(api_env: Any) -> None:
    """§0.4: `ai.admin`, NOT `health.action.admin`. That grant governs WHAT may
    run automatically as SYSTEM/root (the catalogue); this one governs WHICH AI
    PERSONA may reach it. Both halves: an `ai.use` holder is refused, an
    `ai.admin` holder is not -- a 403-only test passes against a route that
    refuses everybody."""
    org_id = await api_env.create_org("dial-rbac")
    _, user_sub = await ai_user(api_env, org_id, "ai.use")
    headers = await api_env.headers(org_id, user_sub)

    assert (await api_env.client.get(f"{AI_PREFIX}/autonomy", headers=headers)).status_code == 403
    put = await api_env.client.put(
        f"{AI_PREFIX}/autonomy",
        headers=headers,
        json={"persona": "admin", "risk_class": "low", "level": "suggest"},
    )
    assert put.status_code == 403

    _, admin_sub = await ai_user(api_env, org_id, "ai.admin")
    admin_headers = await api_env.headers(org_id, admin_sub)
    assert (
        await api_env.client.get(f"{AI_PREFIX}/autonomy", headers=admin_headers)
    ).status_code == 200


async def test_put_stores_the_level_and_get_returns_only_stored_rows(api_env: Any) -> None:
    org_id = await api_env.create_org("dial-put")
    _, sub = await ai_user(api_env, org_id, "ai.admin")
    headers = await api_env.headers(org_id, sub)

    put = await api_env.client.put(
        f"{AI_PREFIX}/autonomy",
        headers=headers,
        json={"persona": "admin", "risk_class": "medium", "level": "require_approval"},
    )
    assert put.status_code == 200, put.text

    listed = await api_env.client.get(f"{AI_PREFIX}/autonomy", headers=headers)
    assert listed.status_code == 200
    # ABSENT COMBINATIONS ARE NOT MATERIALIZED. A response that filled in `off`
    # for every unset pair would read as configuration somebody made, and the
    # next admin would "tidy up" by writing them.
    assert [(r["persona"], r["risk_class"], r["level"]) for r in listed.json()] == [
        ("admin", "medium", "require_approval")
    ]


async def test_put_refuses_autonomous_and_writes_no_row(api_env: Any) -> None:
    """**THE STATUS ALONE IS NOT THE ASSERTION.** A handler that refused and
    wrote anyway would pass on the status; the SELECT is what makes this a
    statement about the table."""
    from app.models import AiAutonomyCeiling

    org_id = await api_env.create_org("dial-auto")
    _, sub = await ai_user(api_env, org_id, "ai.admin")
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.put(
        f"{AI_PREFIX}/autonomy",
        headers=headers,
        json={"persona": "admin", "risk_class": "low", "level": "autonomous"},
    )
    assert response.status_code == 400, response.text

    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AiAutonomyCeiling).where(AiAutonomyCeiling.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_get_is_scoped_to_the_callers_org(api_env: Any) -> None:
    """Both orgs are seeded, so `[]` and "somebody else's row" print different
    values."""
    org_id = await api_env.create_org("dial-mine")
    other = await api_env.create_org("dial-theirs")
    await _seed_ceiling(api_env, other, persona="admin", risk_class="low", level="suggest")
    await _seed_ceiling(
        api_env, org_id, persona="technician", risk_class="high", level="require_approval"
    )
    _, sub = await ai_user(api_env, org_id, "ai.admin")
    headers = await api_env.headers(org_id, sub)

    listed = await api_env.client.get(f"{AI_PREFIX}/autonomy", headers=headers)
    assert [(r["persona"], r["level"]) for r in listed.json()] == [
        ("technician", "require_approval")
    ]


async def test_the_put_writes_the_audit_event_the_service_promises(api_env: Any) -> None:
    from app.models import AuditEvent

    org_id = await api_env.create_org("dial-audit")
    user_id, sub = await ai_user(api_env, org_id, "ai.admin")
    headers = await api_env.headers(org_id, sub)
    await api_env.client.put(
        f"{AI_PREFIX}/autonomy",
        headers=headers,
        json={"persona": "technician", "risk_class": "low", "level": "suggest"},
    )

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .where(AuditEvent.action == "ai.autonomy_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].actor_id == user_id
    assert events[0].meta["persona"] == "technician"
    assert events[0].meta["old_level"] == "off"
    assert events[0].meta["new_level"] == "suggest"


async def test_the_seed_helper_and_the_service_agree(api_env: Any) -> None:
    """`_seed_ceiling` writes what `ceiling_for` reads. A seeding helper that
    wrote a row the service does not find would make every dial-dependent test
    in the later tasks vacuous, and the failure would look like a policy
    decision rather than a broken fixture."""
    org_id = await api_env.create_org("dial-seed")
    await _seed_ceiling(api_env, org_id, persona="admin", risk_class="high", level="suggest")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert (
            await ceiling_for(session, org_id=org_id, persona="admin", risk_class="high")
            == "suggest"
        )

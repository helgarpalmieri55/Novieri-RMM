"""The three rate fuses, pinned in the kernel that now owns them.

`fuse_breach` moved out of `app/modules/health/actions.py` because
`docs/modules/ai-automation.md` §6.1 reads it at preview and §6.5 gates on it at
dispatch, and §2.3 property 1 forbids `app/core/ai/` importing any
`app.modules.*` package. Health's own suite exercises it through remediation;
this file exercises it directly, which is what makes the three branches
falsifiable one at a time.

TWO THINGS EVERY TEST HERE DOES ON PURPOSE.

1. **BOTH HALVES OF EVERY WINDOW.** A test that only asserts "fused" passes
   against a branch that always fires. Each fuse below is asserted at the limit
   AND one step outside it, so the boundary is what is being read rather than
   the branch existing.

2. **EVERY `None` CASE SEEDS A NEARLY-MATCHING ROW.** Right job type, wrong
   device; or right device, just outside the window. So `None` is evidence the
   predicate discriminated, not evidence that `jobs` was empty -- which is the
   third of this project's three named fixture-blindness traps.

AND THE ORG PREDICATE IS PINNED ON THE ADMIN ENGINE. Through `org_session` a
cross-org test passes with `Job.org_id == org_id` deleted, because RLS rejects
first and the application predicate never gets to be wrong. The health sweep
and the AI layer's dispatch both evaluate these fuses on sessions where RLS is
not the thing doing the filtering, so the predicate has to be tested where it
is the only filter there is.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.approved_actions import fuse_breach
from app.core.tenancy import org_session
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_device, seed_org

JOB_TYPE = "inventory.collect"


async def _seed_job(
    engine: AsyncEngine, org_id: UUID, device_id: UUID, *, created_at: datetime, **kw: Any
) -> UUID:
    """A `jobs` row at an exact `created_at`.

    `created_at` carries a `server_default` of `now()`, so it must be passed
    explicitly -- a fuse test that could not place a job in time could only
    ever assert "something recent exists".
    """
    from app.models import Job

    values: dict[str, Any] = {
        "type": JOB_TYPE,
        "target_type": "device",
        "target_id": device_id,
        "params": {},
        "nonce": uuid4().hex,
        "issued_at": created_at,
        "expires_at": created_at + timedelta(hours=1),
        "created_at": created_at,
        "updated_at": created_at,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        job = Job(org_id=org_id, **values)
        session.add(job)
        await session.commit()
        return job.id


async def _action(env_engine: AsyncEngine, org: Any, **kw: Any) -> Any:
    from app.models import ApprovedAction

    action_id = await seed_approved_action(env_engine, org.org_id, org.user_id, **kw)
    async with AsyncSession(env_engine) as session:
        return await session.get(ApprovedAction, action_id)


async def _verdict(engine: AsyncEngine, org_id: UUID, action: Any, device_id: UUID, now: datetime):
    async with AsyncSession(engine) as session:
        return await fuse_breach(session, org_id, action, device_id=device_id, now=now)


# --------------------------------------------------------------------------- #
# 1. Cooldown
# --------------------------------------------------------------------------- #


async def test_cooldown_refuses_inside_the_window_and_names_the_job(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "fuse-cd")
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=100)
    now = datetime.now(UTC)
    job_id = await _seed_job(
        admin_engine, org.org_id, org.device_id, created_at=now - timedelta(minutes=5)
    )

    breach = await _verdict(admin_engine, org.org_id, action, org.device_id, now)

    assert breach is not None
    assert breach.startswith("cooldown:")
    # The refusal NAMES the job it counted. An operator who cannot see what
    # consumed the budget has no way to tell a sweep's dispatch from their own.
    assert str(job_id) in breach, breach


async def test_cooldown_passes_just_outside_the_window(admin_engine: AsyncEngine) -> None:
    """The half that proves the window is READ rather than the branch always
    firing. Falsified by changing `>` to `>=` on the boundary comparison."""
    org = await seed_org(admin_engine, "fuse-cd-out")
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=100)
    now = datetime.now(UTC)
    # NEARLY MATCHING: same org, same job type, same device -- exactly on the
    # boundary, which `created_at > now - cooldown` must exclude.
    await _seed_job(
        admin_engine, org.org_id, org.device_id, created_at=now - timedelta(seconds=3600)
    )

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


async def test_cooldown_ignores_another_device(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "fuse-cd-dev")
    other_device = await seed_device(admin_engine, org.org_id)
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=100)
    now = datetime.now(UTC)
    # NEARLY MATCHING: right org, right job type, right window, wrong device.
    await _seed_job(
        admin_engine, org.org_id, other_device, created_at=now - timedelta(minutes=1)
    )

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


# --------------------------------------------------------------------------- #
# 2. max_per_device_per_day
# --------------------------------------------------------------------------- #


async def test_per_device_per_day_refuses_at_the_limit(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "fuse-dev")
    action = await _action(admin_engine, org, cooldown_s=0, max_per_device_per_day=3)
    now = datetime.now(UTC)
    ids = [
        await _seed_job(
            admin_engine, org.org_id, org.device_id, created_at=now - timedelta(hours=n + 1)
        )
        for n in range(3)
    ]

    breach = await _verdict(admin_engine, org.org_id, action, org.device_id, now)

    assert breach is not None and breach.startswith("max_per_device_per_day:")
    assert any(str(job_id) in breach for job_id in ids), breach


async def test_per_device_per_day_passes_one_below_the_limit(admin_engine: AsyncEngine) -> None:
    """Falsified by deleting the per-device branch, which makes this `None`
    where the test above expects a reason -- and by an off-by-one, which makes
    this one a refusal."""
    org = await seed_org(admin_engine, "fuse-dev-under")
    action = await _action(admin_engine, org, cooldown_s=0, max_per_device_per_day=3)
    now = datetime.now(UTC)
    for n in range(2):
        await _seed_job(
            admin_engine, org.org_id, org.device_id, created_at=now - timedelta(hours=n + 1)
        )

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


async def test_per_device_per_day_ignores_jobs_older_than_a_day(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "fuse-dev-old")
    action = await _action(admin_engine, org, cooldown_s=0, max_per_device_per_day=2)
    now = datetime.now(UTC)
    # NEARLY MATCHING: right org, device and type; one hour outside the 24h
    # window, twice.
    for n in range(2):
        await _seed_job(
            admin_engine,
            org.org_id,
            org.device_id,
            created_at=now - timedelta(days=1, hours=n + 1),
        )

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


# --------------------------------------------------------------------------- #
# 3. max_per_org_per_hour
# --------------------------------------------------------------------------- #


async def test_per_org_per_hour_refuses_at_the_limit(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "fuse-org")
    other_device = await seed_device(admin_engine, org.org_id)
    action = await _action(
        admin_engine, org, cooldown_s=0, max_per_device_per_day=100, max_per_org_per_hour=2
    )
    now = datetime.now(UTC)
    # ON ANOTHER DEVICE, deliberately: the per-org fuse does not narrow on
    # `target_id`, and seeding these on the target device would let the
    # per-device branch answer first.
    ids = [
        await _seed_job(
            admin_engine, org.org_id, other_device, created_at=now - timedelta(minutes=n + 1)
        )
        for n in range(2)
    ]

    breach = await _verdict(admin_engine, org.org_id, action, org.device_id, now)

    assert breach is not None and breach.startswith("max_per_org_per_hour:")
    assert any(str(job_id) in breach for job_id in ids), breach


async def test_per_org_per_hour_passes_one_below_and_outside_the_hour(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "fuse-org-under")
    other_device = await seed_device(admin_engine, org.org_id)
    action = await _action(
        admin_engine, org, cooldown_s=0, max_per_device_per_day=100, max_per_org_per_hour=2
    )
    now = datetime.now(UTC)
    # One inside the hour, one just outside it: the count must be 1, not 2.
    await _seed_job(admin_engine, org.org_id, other_device, created_at=now - timedelta(minutes=1))
    await _seed_job(admin_engine, org.org_id, other_device, created_at=now - timedelta(hours=1))

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


async def test_a_different_job_type_never_counts(admin_engine: AsyncEngine) -> None:
    """Two approved actions sharing a job type share one fuse -- and two that do
    NOT share one must not. Seeded on the same device, in the window, so `None`
    is evidence `Job.type` was read."""
    org = await seed_org(admin_engine, "fuse-type")
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=1)
    now = datetime.now(UTC)
    await _seed_job(
        admin_engine,
        org.org_id,
        org.device_id,
        created_at=now - timedelta(minutes=1),
        type="patch.scan",
    )

    assert await _verdict(admin_engine, org.org_id, action, org.device_id, now) is None


# --------------------------------------------------------------------------- #
# 4. THE ORG PREDICATE, ON THE ADMIN ENGINE.
# --------------------------------------------------------------------------- #


async def test_another_orgs_jobs_do_not_fuse_this_org_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """**THIS TEST IS WHY THE ADMIN ENGINE, AND WHY THE TARGET ID IS SHARED.**

    Two things had to be arranged for this to be able to fail at all, and the
    second was found by running the mutation rather than by reasoning about it.

    1. **The admin engine.** Through `org_session` this passes with
       `Job.org_id == org_id` DELETED, because RLS refuses org B's rows before
       the application predicate is ever consulted -- which is the mutation
       that has shipped four times in this repository. On the admin engine RLS
       is not filtering, so the deletion is observable.

    2. **`jobs.target_id` CARRIES NO FOREIGN KEY**, so org B's jobs are seeded
       pointing at ORG A's DEVICE. Without that, the cooldown and per-device
       branches both narrow on `target_id` as well, and the device predicate
       masks the missing org predicate completely: the first version of this
       test stayed GREEN with all three `Job.org_id` filters deleted. A
       cross-org test whose other predicates already separate the two orgs is
       decoration.

    With the predicate deleted this observes a `cooldown:` refusal naming org
    B's job id.
    """
    org_a = await seed_org(admin_engine, "fuse-org-a")
    org_b = await seed_org(admin_engine, "fuse-org-b")
    action = await _action(
        admin_engine,
        org_a,
        cooldown_s=3600,
        max_per_device_per_day=1,
        max_per_org_per_hour=2,
    )
    now = datetime.now(UTC)
    for n in range(5):
        await _seed_job(
            admin_engine,
            org_b.org_id,
            org_a.device_id,
            created_at=now - timedelta(minutes=n + 1),
        )

    assert await _verdict(admin_engine, org_a.org_id, action, org_a.device_id, now) is None


async def test_the_same_seeding_does_fuse_the_org_that_owns_it(
    admin_engine: AsyncEngine,
) -> None:
    """The control for the test above. Without it, "org A saw nothing" is
    equally consistent with a `fuse_breach` that never sees anything at all --
    a typo'd table, a broken seed, a `WHERE false`."""
    org_b = await seed_org(admin_engine, "fuse-org-b2")
    action = await _action(admin_engine, org_b, cooldown_s=3600, max_per_device_per_day=1)
    now = datetime.now(UTC)
    await _seed_job(
        admin_engine, org_b.org_id, org_b.device_id, created_at=now - timedelta(minutes=1)
    )

    breach = await _verdict(admin_engine, org_b.org_id, action, org_b.device_id, now)
    assert breach is not None and breach.startswith("cooldown:")


async def test_the_verdict_is_the_same_through_a_tenant_session(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The delegation health uses is the kernel body, on the engine health uses
    it on. Asserted so the move cannot have changed which rows are visible."""
    org = await seed_org(admin_engine, "fuse-tenant")
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=100)
    now = datetime.now(UTC)
    await _seed_job(
        admin_engine, org.org_id, org.device_id, created_at=now - timedelta(minutes=1)
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        breach = await fuse_breach(
            session, org.org_id, action, device_id=org.device_id, now=now
        )
    assert breach is not None and breach.startswith("cooldown:")


async def test_health_still_calls_one_body(admin_engine: AsyncEngine) -> None:
    """`health.actions._fuse_breach` is a delegation, not a second copy. If it
    ever grows a body again there are two answers to "is this fused", and the
    AI layer's dispatch gate and the health sweep can disagree."""
    from app.modules.health import actions as health_actions

    org = await seed_org(admin_engine, "fuse-deleg")
    action = await _action(admin_engine, org, cooldown_s=3600, max_per_device_per_day=100)
    now = datetime.now(UTC)
    await _seed_job(
        admin_engine, org.org_id, org.device_id, created_at=now - timedelta(minutes=1)
    )

    async with AsyncSession(admin_engine) as session:
        via_health = await health_actions._fuse_breach(
            session, org.org_id, action, device_id=org.device_id, now=now
        )
        via_kernel = await fuse_breach(
            session, org.org_id, action, device_id=org.device_id, now=now
        )
    assert via_health == via_kernel
    assert via_health is not None

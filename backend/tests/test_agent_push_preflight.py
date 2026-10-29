"""The refusal ladder: invariant 2a's four bounds, and the fact that cycle B1
dispatches nothing (spec §3, §5.1, §5.2, §9, §14; falsifiers 6, 7, 10, 11, 16).

**SECURITY-CRITICAL, AND THIS IS THE FILE THE CYCLE SPLIT EXISTS FOR.** Spec
§14: B1 "is buildable and testable with no agent code and no push ever leaving
the building, which is the right place to find out whether the refusals hold".
So every test here asks whether a refusal holds, and one asks whether a fully
authorized request still refuses.

WHAT THESE FIXTURES DELIBERATELY VARY -- fixture blindness is this
repository's defining defect, so each of the four bounds is paired with a
fixture in which the guarded and the unguarded path produce DIFFERENT
OBSERVABLE VALUES:

* **EVERY SINGLE-BOUND TEST SATISFIES THE OTHER THREE.** A test that seeds
  nothing and asserts a refusal passes against a function that refuses
  everything and against one that never looks. Each test below builds the
  fully authorized world and then removes exactly one thing, so the assertion
  is on WHICH reason came back -- and the same fixture with the removal undone
  reaches the "everything holds" case, which is asserted separately.
* **`no_asset` SEEDS AN ASSET IN THE SAME ORG AT A DIFFERENT ADDRESS.** With
  no asset at all, "the address is not promoted" and "the org has no assets"
  are the same observation, and a lookup that ignored the address would pass.
* **`no_authorization` SEEDS A REVOKED GRANT FOR THE EXACT PREFIX**, and the
  expiry case seeds an EXPIRED one. Delete the `revoked_at IS NULL` predicate
  and this fixture authorizes a real push.
* **`credential_not_scoped` SEEDS A SECOND CREDENTIAL THAT *IS* SCOPED TO THE
  ADDRESS.** Without the `credential_id` predicate in the scope lookup, any
  bound credential in the org satisfies every target, and a single-credential
  fixture cannot see it.
* **`no_site_server` SEEDS A SITE SERVER THAT IS ONLINE AND ENABLED -- FOR A
  DIFFERENT SEGMENT**, and a second one for the right segment that is
  DISABLED. Spec §2.1 and D3: there is no fallback originator, so the failure
  this guards against is not an exception, it is a push emitted from the wrong
  host holding a plaintext administrative credential.
* **THE BATCH TESTS ASSERT WHICH ADDRESSES WERE NAMED**, not that something
  was refused. All-or-nothing means an operator must not discover mid-batch
  that four of their twenty-five were outside the authorization, and "it
  raised" cannot tell a whole-batch refusal from a partial one.
"""

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.modules.agent_push.preflight as preflight_module
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Job, Role, UserRole
from app.modules.agent_push.errors import (
    PushAuthorizationError,
    PushPermissionError,
    PushValidationError,
)
from app.modules.agent_push.net import parse_target_address
from app.modules.agent_push.preflight import (
    PUSH_BATCH_MAX,
    REFUSAL_REASONS,
    VOUCHED_PROVENANCE,
    evaluate_targets,
    request_push_batch,
)
from tests.agent_push_fixtures import (
    PushOrg,
    seed_asset,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)

TARGET = "10.60.0.17"
SEGMENT = "10.60.0.0/24"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class World:
    """The fully authorized world, from which each test removes one thing."""

    def __init__(self, org: PushOrg, credential_id: UUID) -> None:
        self.org = org
        self.credential_id = credential_id


async def _grant_role(engine: AsyncEngine, org: PushOrg, role_name: str) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == role_name, Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(
            UserRole(org_id=org.org_id, user_id=org.user_id, role_id=role.id)
        )
        await session.commit()


async def _world(
    engine: AsyncEngine,
    tag: str,
    *,
    asset_ip: str | None = TARGET,
    authorize: str | None = SEGMENT,
    authorization_revoked: bool = False,
    authorization_expires_at: datetime | None = None,
    scope: str | None = SEGMENT,
    site_segment: str | None = SEGMENT,
    site_enabled: bool = True,
    site_capabilities: dict[str, bool] | None = None,
    role: str = "org-admin",
) -> World:
    """Every bound satisfied, unless a keyword removes one.

    `role` defaults to `org-admin`, which holds both `agent.push.run` and
    `credentials.checkout` -- the conjunction spec §8 requires. A test that
    wants the conjunction to FAIL passes a role holding one and not the other.
    """
    org = await seed_org(engine, tag)
    await _grant_role(engine, org, role)
    credential_id = await seed_credential(engine, org)

    # An asset in this org at a DIFFERENT address, always -- so "no promoted
    # asset here" is never the same observation as "this org has no assets".
    await seed_asset(engine, org.org_id, primary_ip="10.60.0.200")
    if asset_ip is not None:
        await seed_asset(engine, org.org_id, primary_ip=asset_ip)

    if authorize is not None:
        await seed_push_authorization(
            engine,
            org,
            authorize,
            revoked=authorization_revoked,
            expires_at=authorization_expires_at,
        )
    if scope is not None:
        await seed_push_credential_scope(engine, org, credential_id, scope)

    # A site server that is enabled and serves a DIFFERENT segment, always --
    # so `no_site_server` can never be confused with "this org has no site
    # servers", and a fallback to "any site server" would be visible.
    await seed_push_site_server(engine, org.org_id, segments=["10.99.0.0/24"])
    if site_segment is not None:
        await seed_push_site_server(
            engine,
            org.org_id,
            segments=[site_segment],
            enabled=site_enabled,
            capabilities=site_capabilities,
        )
    return World(org, credential_id)


async def _decide(
    app_engine: AsyncEngine, world: World, address: str = TARGET, now: datetime | None = None
):
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        decisions = await evaluate_targets(
            session,
            world.org.org_id,
            addresses=[parse_target_address(address)],
            credential_id=world.credential_id,
            client_id=world.org.org_id,
            now=now,
        )
    return decisions[0]


# --------------------------------------------------------------------------- #
# The four bounds, one at a time
# --------------------------------------------------------------------------- #


async def test_every_bound_satisfied_leaves_no_refusal(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE CONTROL FIXTURE. Without it, every refusal test below passes
    against a function that refuses unconditionally."""
    world = await _world(admin_engine, "pf-ok")
    decision = await _decide(app_engine, world)
    assert decision.refusals == ()
    assert decision.device_id is not None
    assert decision.authorization_id is not None
    assert decision.credential_scope_id is not None
    assert decision.site_server_id is not None
    assert decision.site_server_device_id is not None


async def test_an_unpromoted_address_is_refused_no_asset(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant 2a (d), first authorization. A candidate is an ICMP echo
    reply; pushing to one would present administrative credentials to whatever
    answered a ping."""
    world = await _world(admin_engine, "pf-noasset", asset_ip=None)
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("no_asset",)


async def test_a_row_the_sweep_recorded_on_its_own_is_not_a_push_target(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**THE TEST THAT KEEPS THE FIRST BOUND MEANING ANYTHING AFTER THE
    2026-08-26 AUTO-RECORD RULING.**

    `spec-push-install.md` §3 condition 1 says the target must be "a `devices`
    row in this org", and argues it from promotion being "the moment a human
    asserted *this is a machine we own and manage*". On the same day invariant
    2a was approved, the operator ALSO ruled that a completed `net.sweep`
    writes a `devices` row for every host it reported, with nobody selecting
    anything (`docs/modules/asset-inventory/spec.md` §3). After that ruling the
    spec's literal words admit the coffee machine.

    THE FIXTURE IS THE WHOLE POINT: a row EXISTS at the target address, with
    every other bound satisfied, and its `provenance` is `sweep`. A check
    written to the spec's wording -- "is there a devices row here?" -- returns
    a fully authorized decision for it. *Delete the provenance predicate and
    this test observes `()` where `("no_asset",)` belongs*, which is the
    difference between a push to a managed machine and a push to whatever
    answered a ping.
    """
    world = await _world(admin_engine, "pf-sweeprow", asset_ip=None)
    await seed_asset(
        admin_engine,
        world.org.org_id,
        primary_ip=TARGET,
        source="discovery",
        provenance="sweep",
    )
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("no_asset",), (
        "a row the sweep recorded on its own was accepted as a push target: "
        "nobody vouched for that machine and it is about to be shown an "
        "administrative credential"
    )


@pytest.mark.parametrize(
    ("source", "provenance"),
    [("discovery", "promoted"), ("manual", "operator"), ("agent", "agent")],
)
async def test_a_row_a_person_stood_behind_is_a_push_target(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, source: str, provenance: str
) -> None:
    """The other direction, so "sweep rows are refused" is never confused with
    "every row is refused". All three vouched-for provenances are admitted, and
    each pairing satisfies `ck_devices_source_provenance`."""
    world = await _world(admin_engine, f"pf-vouched-{provenance}", asset_ip=None)
    await seed_asset(
        admin_engine,
        world.org.org_id,
        primary_ip=TARGET,
        source=source,
        provenance=provenance,
    )
    decision = await _decide(app_engine, world)
    assert decision.refusals == ()


def test_the_vouched_set_is_an_allowlist_that_excludes_sweep() -> None:
    """Pinned as an exact set, so a fourth `provenance` value added to
    migration 0078's CHECK cannot join it by silence -- and so `sweep` cannot
    be added to it without somebody editing this line and reading why."""
    assert set(VOUCHED_PROVENANCE) == {"agent", "operator", "promoted"}
    assert "sweep" not in VOUCHED_PROVENANCE


async def test_an_unauthorized_prefix_is_refused_even_when_a_revoked_grant_covers_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING `revoked_at IS NULL`: this exact fixture then
    authorizes a real push to a machine whose grant was withdrawn."""
    world = await _world(admin_engine, "pf-revoked", authorization_revoked=True)
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("no_authorization",)


async def test_a_lapsed_grant_no_longer_authorizes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING THE `expires_at` PREDICATE. An onboarding window
    that never closes is the ordinary way a temporary grant becomes
    permanent."""
    world = await _world(
        admin_engine, "pf-expired", authorization_expires_at=NOW - timedelta(days=1)
    )
    decision = await _decide(app_engine, world, now=NOW)
    assert decision.refusals == ("no_authorization",)
    # ...and the same row DOES authorize before it lapses, which is what makes
    # the expiry predicate observable rather than "everything is refused".
    earlier = await _decide(app_engine, world, now=NOW - timedelta(days=3))
    assert earlier.refusals == ()


async def test_a_grant_that_does_not_contain_the_address_does_not_authorize_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """CONTAINMENT, IN NATIVE `cidr`. `10.60.0.17` is not inside
    `10.6.0.0/16`, and in `text` the string comparison that says otherwise is
    exactly how a credential reaches a segment nobody authorized."""
    world = await _world(admin_engine, "pf-outside", authorize="10.6.0.0/16")
    decision = await _decide(app_engine, world)
    assert "no_authorization" in decision.refusals


async def test_a_supernet_grant_does_authorize_an_address_inside_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The other direction, so "containment works" is not confused with
    "nothing is ever authorized"."""
    world = await _world(admin_engine, "pf-supernet", authorize="10.60.0.0/16")
    decision = await _decide(app_engine, world)
    assert decision.refusals == ()


async def test_a_credential_scoped_elsewhere_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant 2a (d), third authorization: the credential itself carries
    where it may be presented."""
    world = await _world(admin_engine, "pf-scope", scope="10.70.0.0/24")
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("credential_not_scoped",)


async def test_another_credentials_scope_does_not_authorize_this_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING THE `credential_id` PREDICATE from the scope
    lookup: any bound credential in the org would then satisfy every target,
    and the bound that says "this secret, here" would say only "some secret,
    here"."""
    world = await _world(admin_engine, "pf-othercred", scope=None)
    other = await seed_credential(admin_engine, world.org)
    await seed_push_credential_scope(admin_engine, world.org, other, SEGMENT)
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("credential_not_scoped",)


async def test_no_site_server_on_the_segment_is_refused_with_no_fallback(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant 2a (a), and the load-bearing bound of the whole design.

    THE FIXTURE HOLDS AN ENABLED SITE SERVER FOR A DIFFERENT SEGMENT. A
    fallback to "any site server", or to `select_sweeper`'s "any agent with an
    observation nearby", would produce a decision here rather than a refusal --
    and would put a plaintext administrative credential in the memory of
    whichever machine happened to qualify.
    """
    world = await _world(admin_engine, "pf-nosite", site_segment=None)
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("no_site_server",)
    assert decision.site_server_id is None


async def test_a_disabled_site_server_does_not_serve_its_segment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`enabled` is an operator's off switch on LAN-wide privilege, and it
    must actually switch it off."""
    world = await _world(admin_engine, "pf-sitedisabled", site_enabled=False)
    decision = await _decide(app_engine, world)
    assert decision.refusals == ("no_site_server",)


async def test_a_site_server_declared_for_a_supernet_serves_the_address(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Containment, not equality: a branch server declared for 10.60.0.0/16
    serves 10.60.0.17, because a branch server serves a branch and not one
    VLAN. Without this the ordinary deployment refuses everything."""
    world = await _world(admin_engine, "pf-sitesuper", site_segment="10.60.0.0/16")
    decision = await _decide(app_engine, world)
    assert decision.refusals == ()


async def test_every_failing_bound_is_reported_not_only_the_first(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §9: a refusal that does not say which is a refusal nobody can act
    on -- and one that names only the first of four sends the operator back
    three more times."""
    world = await _world(
        admin_engine,
        "pf-allfour",
        asset_ip=None,
        authorize=None,
        scope=None,
        site_segment=None,
    )
    decision = await _decide(app_engine, world)
    assert set(decision.refusals) == {
        "no_asset",
        "no_authorization",
        "credential_not_scoped",
        "no_site_server",
    }


def test_every_named_reason_carries_a_remedy() -> None:
    """A refusal an operator cannot act on is one they will look for a way
    around, and the way around this one is somebody else's laptop and psexec.
    """
    assert set(REFUSAL_REASONS) == {
        "no_asset",
        "no_authorization",
        "credential_not_scoped",
        "no_site_server",
        # WinRM push (design §4): a Windows target whose segment is served only
        # by a site server too old to speak WinRM.
        "no_winrm_site_server",
        # Review Minor 1: an SSH target whose segment is served only by a site
        # server too old to hand the placed binary its enroll URL.
        "site_server_too_old",
        # MSP clients (invariant 5a): vouched-for rows of more than one client
        # hold the address, so it names no single machine.
        "ambiguous_asset",
        # Plan 2026-10-26 Task 12: the address is a vouched machine of another
        # client than the push's credential.
        "credential_missing",
    }
    for reason, help_text in REFUSAL_REASONS.items():
        assert len(help_text) > 80, reason


# --------------------------------------------------------------------------- #
# The batch: clause (f)
# --------------------------------------------------------------------------- #


async def _request(app_engine: AsyncEngine, world: World, targets: object):
    """The route's own shape: call inside the transaction, RAISE OUTSIDE IT.

    `org_session` rolls back on any exception leaving the block, so a helper
    that raised inside would silently discard the `push.refused` rows and every
    audit assertion below would pass against a service that recorded nothing.
    """
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        outcome = await request_push_batch(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            credential_id=world.credential_id,
            targets=targets,
        )
    raise outcome.as_error()


@pytest.mark.parametrize(
    "target",
    [
        "10.60.0.0/24",
        "10.60.0.0/16",
        "server-17.corp.example",
        "10.60.0.1-10.60.0.20",
        "10.60.0.1,10.60.0.2",
        "",
        "   ",
        "not-an-address",
    ],
)
async def test_a_target_that_is_not_one_named_address_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, target: str
) -> None:
    """Invariant 2a clause (f), enforced at parse time. A CIDR, a hostname, a
    range and a list are all the same mistake: a target set the operator did
    not enumerate."""
    world = await _world(admin_engine, "pf-namedonly")
    with pytest.raises(PushValidationError) as exc:
        await _request(app_engine, world, [target])
    assert "explicitly named addresses" in str(exc.value) or "IP literal" in str(
        exc.value
    )


async def test_a_batch_over_the_ceiling_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The ceiling is one of four independent bounds on what a single mistaken
    click can do, and it is checked BEFORE the per-target work so a 4000-line
    paste costs one refusal rather than 4000 queries."""
    world = await _world(admin_engine, "pf-ceiling")
    targets = [f"10.60.0.{i}" for i in range(1, PUSH_BATCH_MAX + 2)]
    with pytest.raises(PushValidationError) as exc:
        await _request(app_engine, world, targets)
    assert str(PUSH_BATCH_MAX) in str(exc.value)


def test_the_ceiling_is_twenty_five() -> None:
    """Pinned, because it is quoted in the spec and in the amendment's
    accounting of the worst case. A number nobody is forced to re-read is a
    number that drifts away from the document that justified it."""
    assert PUSH_BATCH_MAX == 25


async def test_an_empty_batch_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await _world(admin_engine, "pf-empty")
    with pytest.raises(PushValidationError):
        await _request(app_engine, world, [])


async def test_a_duplicated_address_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §10.4 requires the confirmation to list every address with no "and
    22 more", so a silently deduplicated batch would make the count on the
    screen disagree with the count in the request."""
    world = await _world(admin_engine, "pf-dupe")
    with pytest.raises(PushValidationError) as exc:
        await _request(app_engine, world, [TARGET, TARGET])
    assert TARGET in str(exc.value)


async def test_one_bad_target_refuses_the_whole_batch_and_names_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """ALL-OR-NOTHING (spec §5.2). Two targets, one authorized and one not:
    a per-target dispatcher would push to the first, and an operator would
    discover mid-batch that one of their two was outside the authorization.

    Asserted on WHICH addresses were named, because "it raised" cannot tell a
    whole-batch refusal from a partial one.
    """
    world = await _world(admin_engine, "pf-partial")
    stranger = "10.60.0.99"  # inside the authorized prefix, but not promoted
    with pytest.raises(PushAuthorizationError) as exc:
        await _request(app_engine, world, [TARGET, stranger])
    assert set(exc.value.targets) == {stranger}
    assert exc.value.targets[stranger] == ["no_asset"]


async def test_a_refused_batch_creates_nothing_and_audits_every_refusal(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The refusal must SURVIVE the transaction that produced it: `push.refused`
    is written before the raise, and the raise escapes the `org_session` block
    so the rows commit with it.

    ALSO ASSERTS THE `jobs` TABLE IS UNTOUCHED, which is the B1 claim in its
    most literal form.
    """
    world = await _world(admin_engine, "pf-audit", asset_ip=None)
    with pytest.raises(PushAuthorizationError):
        await _request(app_engine, world, [TARGET])

    async with AsyncSession(admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == world.org.org_id,
                        AuditEvent.action == "push.refused",
                    )
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            await session.execute(
                select(func.count()).select_from(Job).where(
                    Job.org_id == world.org.org_id
                )
            )
        ).scalar_one()
    assert len(events) == 1
    assert events[0].meta["target_address"] == TARGET
    assert events[0].meta["reasons"] == ["no_asset"]
    assert events[0].meta["credential_id"] == str(world.credential_id)
    assert "secret" not in events[0].meta
    assert jobs == 0


# --------------------------------------------------------------------------- #
# The RBAC conjunction (spec §8, §2.5)
# --------------------------------------------------------------------------- #


async def test_a_caller_without_credentials_checkout_is_refused_first(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`agent.push.run` alone would make a push a way to USE a stored secret
    while holding only the permission to see its name.

    REFUSED BEFORE THE BATCH IS PARSED, and the test observes that by passing a
    batch that is ALSO invalid: a caller who may not use a stored secret must
    not be told which of their addresses are authorized.
    """
    org = await seed_org(admin_engine, "pf-nocheckout")
    credential_id = await seed_credential(admin_engine, org)
    world = World(org, credential_id)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(PushPermissionError) as exc:
            await request_push_batch(
                session,
                org.org_id,
                actor_id=org.user_id,
                credential_id=credential_id,
                targets=["not-an-address"],
            )
    assert "credentials.checkout" in str(exc.value)
    assert world.credential_id == credential_id


# --------------------------------------------------------------------------- #
# The point of the cycle: everything holds, and nothing is dispatched
# --------------------------------------------------------------------------- #


async def test_a_fully_authorized_push_is_a_clean_preflight_and_the_ladder_signs_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """SPEC §3, IN CYCLE B2. The refusal LADDER (`preflight.request_push_batch`)
    is now a PURE PREFLIGHT: when every bound holds it records nothing, raises
    nothing, and returns an authorized outcome whose `as_error()` is `None`.

    **THE LADDER ITSELF STILL DISPATCHES NOTHING, AND THAT IS THE INVARIANT THAT
    DID NOT MOVE.** B2's signing lives in `dispatch.py`, which the reshaped
    dispatches-nothing guard exempts; `preflight.py` stays pure so the refusal is
    always decided on the near side of the signing boundary (spec §3). This test
    would go red the day somebody wired a `JobEngine` into the ladder -- creating
    a signature for a target the ladder was still evaluating.
    """
    world = await _world(admin_engine, "pf-clean")
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        outcome = await request_push_batch(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            credential_id=world.credential_id,
            targets=[TARGET],
        )
    assert outcome.authorized
    assert outcome.refused == {}
    assert outcome.as_error() is None, "a fully authorized preflight is not an error"

    async with AsyncSession(admin_engine) as session:
        jobs = (
            await session.execute(
                select(func.count()).select_from(Job).where(
                    Job.org_id == world.org.org_id
                )
            )
        ).scalar_one()
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == world.org.org_id,
                        AuditEvent.action.in_(["push.refused", "push.dispatched"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert jobs == 0, "the pure ladder created a job row"
    assert events == [], "the pure ladder audited a dispatch or a refusal"


# --------------------------------------------------------------------------- #
# Tenancy, ON THE ADMIN ENGINE
# --------------------------------------------------------------------------- #


async def test_another_orgs_asset_authorization_scope_and_site_server_count_for_nothing(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """FOUR PREDICATES AT ONCE, and every one of them falsifiable here.

    Org B has the complete authorized world for this address. Org A has
    nothing. On the admin engine Postgres filters nothing, so if any of the
    four lookups has lost its `org_id` predicate, org A's push is authorized
    by org B's grants -- which is a credentialed login on another customer's
    wire, and the worst thing this codebase can express.
    """
    a = await seed_org(admin_engine, "pf-ten-a")
    b = await seed_org(admin_engine, "pf-ten-b")
    a_credential = await seed_credential(admin_engine, a)
    b_credential = await seed_credential(admin_engine, b)

    await seed_asset(admin_engine, b.org_id, primary_ip=TARGET)
    await seed_push_authorization(admin_engine, b, SEGMENT)
    await seed_push_credential_scope(admin_engine, b, b_credential, SEGMENT)
    await seed_push_site_server(admin_engine, b.org_id, segments=[SEGMENT])

    decisions = await evaluate_targets(
        bypassrls_session,
        a.org_id,
        addresses=[parse_target_address(TARGET)],
        credential_id=a_credential,
        client_id=a.org_id,
    )
    assert set(decisions[0].refusals) == {
        "no_asset",
        "no_authorization",
        "credential_not_scoped",
        "no_site_server",
    }, (
        "org A was authorized by org B's rows: at least one lookup has lost its "
        "org_id predicate, and RLS is not there to hide it"
    )


# --------------------------------------------------------------------------- #
# The structural guards (spec falsifier 7)
# --------------------------------------------------------------------------- #


def _guarded_calls(function_name: str) -> list[tuple[int, str]]:
    tree = ast.parse(Path(preflight_module.__file__).read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
    )
    calls: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in (
            "evaluate_targets",
            "has_permission",
            "create",
            "signed_envelope",
            "publish_job",
            "sign",
        ):
            calls.append((node.lineno, str(name)))
    calls.sort()
    return calls


def test_the_authorization_ladder_runs_before_anything_that_could_be_signed() -> None:
    """§6.3's ORDERING, not only its outcome, asserted the way
    `tests/test_ipam_sweep_dispatch.py` asserts it for `net.sweep`.

    The behavioural tests above prove no `jobs` row exists for particular
    fixtures. This proves the property that holds for fixtures nobody wrote:
    the permission check and the authorization evaluation come first, and
    NOTHING that could produce a signature appears in this function at all.

    *If a `JobEngine.create` were added above `evaluate_targets`* this observes
    the reversed indices and names both call sites.
    """
    calls = _guarded_calls("request_push_batch")
    names = [name for _, name in calls]
    assert names[0] == "has_permission", names
    assert "evaluate_targets" in names, names
    forbidden = {"create", "signed_envelope", "publish_job", "sign"} & set(names)
    assert forbidden == set(), (
        f"request_push_batch calls {sorted(forbidden)}: cycle B1 dispatches "
        "nothing, and a signature produced here would exist for a target the "
        "ladder had not finished checking"
    )


def test_the_refusal_is_recorded_before_the_outcome_is_returned() -> None:
    """`push.refused` must be written inside the transaction and BEFORE the
    outcome is handed back, or a refused push leaves no record at all -- which
    is the difference between "we refused" and "nothing happened"."""
    source = inspect.getsource(preflight_module.request_push_batch)
    audit_at = source.index("_audit_refusal(")
    return_at = source.index("return PushBatchOutcome")
    assert audit_at < return_at

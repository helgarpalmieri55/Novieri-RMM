"""Deny-by-default scan authorization and the intersected target set
(`docs/modules/ipam/spec.md` §0.12, §6.1, §6.2, §11.3).

**SECURITY-CRITICAL.** This is the surface that decides where this platform
emits network traffic. Everything below is written so that a mutation which
WIDENS the target set fails loudly, because a mutation that narrows it merely
stops a sweep and a mutation that widens it puts packets on somebody else's
wire.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Every "yields `[]`" case is paired with a fixture that would yield a
  NON-EMPTY set if one predicate were removed.** A test that seeds nothing and
  asserts `[]` passes against a function that always returns `[]`, and against
  one that returns the whole prefix -- the seeded, nearly-matching authorization
  is what makes the empty answer evidence.
* **The supernet case asserts the exact `/24`, not "something".** The dangerous
  bug here is not an empty answer, it is a WIDE one.
* **The cross-org authorization is seeded with THIS org's `vrf_id`.** Foreign
  keys are org-blind, so that row is constructible -- and it is the only shape
  in which the service's own `org_id` predicate is observable, because
  `vrf_id` is a UUID primary key and two orgs cannot otherwise share one.
* **The revoked/expired cases each keep a second, ACTIVE authorization out of
  scope**, so "the predicate works" and "nothing is ever returned" differ.
* **There is a structural test that `sweep.py` is the ONLY dispatch path in this
  module.** Task 8 lands before Task 9 for a reason; that test began as "no
  `JobEngine.create` anywhere yet" and was narrowed, not deleted, when Task 9
  created the dispatcher. Its companion in `tests/test_ipam_sweep_dispatch.py`
  asserts from the AST that `create_sweep` calls `authorized_targets` before
  anything that can produce a signature.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.modules.ipam as ipam_pkg
from app.models import AuditEvent, IpamScanAuthorization
from app.modules.ipam.authorization import (
    DEFAULT_MAX_TARGETS,
    MAX_TARGETS_CEILING,
    authorized_targets,
    create_authorization,
    list_authorizations,
    revoke_authorization,
)
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.net import scope_digest
from app.modules.ipam.subnets import create_range, get_subnet
from tests.ipam_fixtures import seed_org, seed_subnet, seed_vrf

MODULE_DIR = Path(ipam_pkg.__file__).resolve().parent


async def _fixture(engine: AsyncEngine, tag: str, prefix: str = "10.70.0.0/24"):
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(engine, org.org_id, vrf_id=vrf_id, prefix=prefix)
    return org, vrf_id, subnet_id


async def _targets(
    engine: AsyncEngine, org_id: UUID, subnet_id: UUID, **kw
) -> list[str]:
    async with AsyncSession(engine) as session:
        subnet = await get_subnet(session, org_id, subnet_id)
        return await authorized_targets(session, org_id, subnet, **kw)


async def _authorize(
    engine: AsyncEngine, org, vrf_id: UUID, prefix: str, **kw
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = await create_authorization(
            session,
            org.org_id,
            actor_id=org.user_id,
            vrf_id=vrf_id,
            prefix=prefix,
            **kw,
        )
        await session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Deny by default (§0.12)
# ---------------------------------------------------------------------------


async def test_a_subnet_with_no_authorization_yields_nothing(
    admin_engine: AsyncEngine,
) -> None:
    """DENY BY DEFAULT. Documenting a subnet grants nothing: filing and
    authorizing are different decisions by different people, and conflating them
    is how a customer's IPAM ends up scanning a segment their contract does not
    cover.

    IF THE FUNCTION DEFAULTED TO `[subnet.prefix]` this observes
    `['10.70.0.0/24']` instead of `[]` -- 254 hosts probed on the strength of
    somebody having typed the prefix into a form.
    """
    org, _, subnet_id = await _fixture(admin_engine, "deny")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


async def test_an_empty_target_set_is_returned_not_raised(
    admin_engine: AsyncEngine,
) -> None:
    """§6.2: an empty set is a FIRST-CLASS OUTCOME here. Refusing to dispatch on
    it is Task 9's job; raising here would make "not authorized" and "something
    went wrong" the same signal at the call site."""
    org, _, subnet_id = await _fixture(admin_engine, "empty")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


# ---------------------------------------------------------------------------
# §6.2's intersection -- an authorization can never WIDEN a sweep
# ---------------------------------------------------------------------------


async def test_a_supernet_authorization_yields_only_the_subnet(
    admin_engine: AsyncEngine,
) -> None:
    """§0.12's safeguard, and the most dangerous mutation in the module.

    IF THE `∩ subnet.prefix` STEP WERE DROPPED this observes `['10.0.0.0/20']`
    (executed) -- the /8 truncated by `max_targets` to its first 4096 addresses,
    NONE of which are in this subnet. Note what that means: the cap does not
    save you. It bounds how many packets a widened scope emits, not where they
    go, so the intersection is the only thing standing between "authorized the
    /8 for documentation" and probing somebody else's segment.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "super")
    await _authorize(admin_engine, org, vrf_id, "10.0.0.0/8")
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.0/24"]


async def test_a_partial_authorization_yields_only_the_overlap(
    admin_engine: AsyncEngine,
) -> None:
    """The other direction: an authorization NARROWER than the subnet yields
    only its own half. Together with the supernet case this pins the
    intersection in both directions -- a fixture with only one of them cannot
    tell an intersection from "return whichever prefix was smaller"."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "part")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.128/25")
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.128/25"]


async def test_a_sibling_authorization_yields_nothing(admin_engine: AsyncEngine) -> None:
    """IF THE MATCH WERE ON ORG RATHER THAN CONTAINMENT this observes
    `['10.71.0.0/24']` -- a sweep of the segment next door, authorized by an
    authorization for a segment that is not it."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "sib")
    await _authorize(admin_engine, org, vrf_id, "10.71.0.0/24")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


async def test_two_authorizations_are_unioned_then_intersected(
    admin_engine: AsyncEngine,
) -> None:
    """Two halves of the subnet, authorized separately, collapse to the whole --
    minimal and canonical, because the list goes into the SIGNED params and is
    capped by `max_targets`, so a non-minimal cover is a functional defect."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "union")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/25")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.128/25")
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.0/24"]


# ---------------------------------------------------------------------------
# Active means active (§6.2)
# ---------------------------------------------------------------------------


async def test_a_revoked_authorization_yields_nothing(admin_engine: AsyncEngine) -> None:
    """A SECOND, ACTIVE AUTHORIZATION IS SEEDED OUT OF SCOPE, so this cannot
    pass by returning `[]` for everything.

    IF THE `revoked_at IS NULL` PREDICATE WERE DROPPED this observes
    `['10.70.0.0/24']` -- revocation would stop nothing, and the record of what
    was authorized when would be the only trace of a scope that still works.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "revoked")
    authorization_id = await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    await _authorize(admin_engine, org, vrf_id, "10.99.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=authorization_id
        )
        await session.commit()
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


async def test_an_expired_authorization_yields_nothing(admin_engine: AsyncEngine) -> None:
    """IF THE `expires_at` PREDICATE WERE DROPPED this observes
    `['10.70.0.0/24']`. The paired case below shows a FUTURE expiry is still
    active, so the predicate cannot be "reject anything with an expiry"."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "expired")
    await _authorize(
        admin_engine,
        org,
        vrf_id,
        "10.70.0.0/24",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


async def test_an_unexpired_authorization_is_active(admin_engine: AsyncEngine) -> None:
    org, vrf_id, subnet_id = await _fixture(admin_engine, "unexpired")
    await _authorize(
        admin_engine,
        org,
        vrf_id,
        "10.70.0.0/24",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.0/24"]


async def test_expiry_is_evaluated_against_the_supplied_now(
    admin_engine: AsyncEngine,
) -> None:
    """`now` is a parameter so the dispatcher and any replay evaluate the same
    instant. THE SAME ROW yields the prefix at one instant and nothing an hour
    later, which is a difference in values rather than in setup."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "now")
    expiry = datetime.now(UTC) + timedelta(hours=1)
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24", expires_at=expiry)
    assert await _targets(
        admin_engine, org.org_id, subnet_id, now=expiry - timedelta(minutes=1)
    ) == ["10.70.0.0/24"]
    assert (
        await _targets(
            admin_engine, org.org_id, subnet_id, now=expiry + timedelta(minutes=1)
        )
        == []
    )


async def test_an_authorization_in_another_vrf_yields_nothing(
    admin_engine: AsyncEngine,
) -> None:
    """§6.2 scopes to `S.vrf`. Overlapping address space in two routing domains
    is the ordinary case, not a corner one -- 10.0.0.0/8 in a customer's VRF and
    in ours are different networks, and authorizing one is not authorizing the
    other.

    IF THE VRF PREDICATE WERE DROPPED this observes `['10.70.0.0/24']`.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "vrf")
    other_vrf = await seed_vrf(admin_engine, org.org_id, name="customer-a")
    await _authorize(admin_engine, org, other_vrf, "10.70.0.0/24")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


# ---------------------------------------------------------------------------
# Exclusions (§6.2)
# ---------------------------------------------------------------------------


async def test_an_excluded_range_is_subtracted(admin_engine: AsyncEngine) -> None:
    """`10.70.0.0/24` minus `.100-.200` is the minimal cover and DOES NOT
    CONTAIN `.150`.

    IF THE SUBTRACTION WERE DROPPED this observes `['10.70.0.0/24']`, which
    contains `.150` -- and the agent probes an address an operator explicitly
    carved out (a fragile device, a honeypot, a segment under change freeze).
    Asserted by containment as well as by the list, because a subtraction that
    produced the right number of blocks in the wrong place would pass a
    length check.
    """
    from ipaddress import ip_address, ip_network

    org, vrf_id, subnet_id = await _fixture(admin_engine, "excl")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="excluded",
            start_address="10.70.0.100",
            end_address="10.70.0.200",
        )
        await session.commit()

    targets = await _targets(admin_engine, org.org_id, subnet_id)
    covered = {
        str(address)
        for target in targets
        for address in ip_network(target)
    }
    assert "10.70.0.150" not in covered
    assert "10.70.0.99" in covered
    assert "10.70.0.201" in covered
    assert len(covered) == 256 - 101
    assert ip_address("10.70.0.150") not in ip_network(targets[0])


async def test_a_dhcp_pool_and_a_reserved_range_are_NOT_subtracted(
    admin_engine: AsyncEngine,
) -> None:
    """ONLY `excluded` ranges are holes (§6.2).

    A DHCP pool is the part of a segment MOST worth sweeping -- it is where
    undocumented hosts live -- and a `reserved` range is documentation, not a
    no-probe instruction. IF THE SUBTRACTION MATCHED ANY KIND this observes a
    target set with the pool missing, and the module would stop finding exactly
    the thing it exists to find.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "kinds")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session, org.org_id, org.user_id, subnet_id, kind="dhcp_pool",
            start_address="10.70.0.100", end_address="10.70.0.200",
        )
        await create_range(
            session, org.org_id, org.user_id, subnet_id, kind="reserved",
            start_address="10.70.0.10", end_address="10.70.0.20",
        )
        await session.commit()
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.0/24"]


# ---------------------------------------------------------------------------
# The cap and IPv6
# ---------------------------------------------------------------------------


async def test_the_target_set_is_capped_at_max_targets(admin_engine: AsyncEngine) -> None:
    """§5.2's `max_targets` bounds ADDRESSES, not list entries -- the result
    handler checks `len(hosts) <= max_targets` against the same number.

    IF THE CAP COUNTED CIDR ENTRIES this observes one `/16` entry, 65,536
    addresses, "within" a cap of 128.
    """
    from ipaddress import ip_network

    org, vrf_id, subnet_id = await _fixture(admin_engine, "cap", prefix="10.72.0.0/16")
    await _authorize(admin_engine, org, vrf_id, "10.72.0.0/16")
    targets = await _targets(admin_engine, org.org_id, subnet_id, max_targets=128)
    assert sum(ip_network(t).num_addresses for t in targets) == 128
    assert targets == ["10.72.0.0/25"]


async def test_a_max_targets_outside_its_bounds_is_refused_not_silently_capped(
    admin_engine: AsyncEngine,
) -> None:
    """§5.2 bounds `max_targets` ITSELF, and both ends of the bound matter.

    ONLY THE `isinstance` HALF OF THAT GUARD WAS EXERCISED. Deleting
    `if not 1 <= max_targets <= MAX_TARGETS_CEILING` left 61 tests passing,
    because nothing ever passed `0` or a number above the ceiling.

    What the two ends actually prevent, and why "it raises" is not the whole
    assertion:

    * **`0` returns `[]`** -- and `[]` is this function's DENY answer. Its only
      caller reads it as "nothing covering this prefix is authorized", records
      the run `refused` with `error='not authorized'`, and raises an
      authorization error naming `ipam.scope.manage`. So a caller that passed
      `0` -- a client default, a `min` that went one too far, an operator
      typing into a form -- would send an admin off to re-grant a scope that was
      never the problem, on a subnet that IS authorized. §0.5 exists to stop
      exactly that confusion between "denied" and "not enumerable".
    * **`10**9` is a signed ceiling nine orders of magnitude above §5.2's**, and
      the result handler checks the agent's host count against the SIGNED
      number, not against `MAX_TARGETS_CEILING`. Whatever this function returns
      becomes what a job is signed for.

    The returned value is therefore captured and reported, so a deletion of the
    guard fails on WHAT CAME BACK rather than on a bare `DID NOT RAISE`.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "bounds")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")

    for bad in (0, -1, MAX_TARGETS_CEILING + 1, 10**9):
        outcome: object = "raised"
        try:
            outcome = await _targets(
                admin_engine, org.org_id, subnet_id, max_targets=bad
            )
        except IpamValidationError as exc:
            assert "between 1 and" in str(exc)
        assert outcome == "raised", (
            f"max_targets={bad} was accepted and returned {outcome!r}"
        )

    # BOTH ENDS OF THE RANGE ARE INCLUSIVE, and are exercised so the guard
    # cannot be "fixed" into an off-by-one that refuses a legitimate cap.
    assert await _targets(admin_engine, org.org_id, subnet_id, max_targets=1) == [
        "10.70.0.0/32"
    ]
    assert await _targets(
        admin_engine, org.org_id, subnet_id, max_targets=MAX_TARGETS_CEILING
    ) == ["10.70.0.0/24"]


async def test_the_default_cap_is_the_signed_params_default(
    admin_engine: AsyncEngine,
) -> None:
    from ipaddress import ip_network

    org, vrf_id, subnet_id = await _fixture(admin_engine, "capdef", prefix="10.73.0.0/16")
    await _authorize(admin_engine, org, vrf_id, "10.73.0.0/16")
    targets = await _targets(admin_engine, org.org_id, subnet_id)
    assert sum(ip_network(t).num_addresses for t in targets) == DEFAULT_MAX_TARGETS


async def test_an_ipv6_subnet_yields_nothing_even_when_authorized(
    admin_engine: AsyncEngine,
) -> None:
    """§0.5: an ICMP sweep of a /64 is a thought experiment, so this returns the
    DENY answer rather than a capped slice of 1.8x10^19 addresses.

    Task 9 must refuse an IPv6 sweep with its own message BEFORE consulting this
    function -- "not authorized" is the wrong explanation for "not enumerable".
    Returning `[]` here is the safe default underneath that, not a substitute
    for it: IF THIS RETURNED THE FIRST 4096 ADDRESSES OF THE /64, a dispatcher
    that forgot the family check would emit a sweep that could never mean
    anything.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "v6", prefix="2001:db8:9::/64")
    await _authorize(admin_engine, org, vrf_id, "2001:db8:9::/64")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


async def test_a_container_subnet_yields_nothing(admin_engine: AsyncEngine) -> None:
    """§0.2: only `assignable` subnets have sweeps."""
    org = await seed_org(admin_engine, "cont")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.74.0.0/16", kind="container"
    )
    await _authorize(admin_engine, org, vrf_id, "10.74.0.0/16")
    assert await _targets(admin_engine, org.org_id, subnet_id) == []


# ---------------------------------------------------------------------------
# Tenancy (§11.1) -- the module's most dangerous query
# ---------------------------------------------------------------------------


async def test_another_orgs_authorization_yields_nothing_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE, and constructed the only way that makes the
    service's own `org_id` predicate observable.

    The authorization row is seeded with **org B's `org_id` and org A's
    `vrf_id`**. Foreign keys are org-blind -- `ipam_scan_authorization.vrf_id`
    references `ipam_vrf(id)` with no org check -- so that row is constructible,
    and it is the only shape in which the `org_id` predicate matters: `vrf_id`
    is a UUID primary key, so two orgs cannot otherwise share one and the vrf
    predicate would mask the org predicate entirely.

    IF THE `org_id` PREDICATE WERE DELETED this observes `['10.70.0.0/24']` for
    org A, authorized by a row org A cannot see and did not sign. Through
    `org_session` it would pass either way, because RLS rejects first.
    """
    org_a, vrf_a, subnet_a = await _fixture(admin_engine, "xorga")
    org_b = await seed_org(admin_engine, "xorgb")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamScanAuthorization(
                org_id=org_b.org_id,
                vrf_id=vrf_a,
                prefix="10.70.0.0/24",
                authorized_by=org_b.user_id,
                authorized_at=datetime.now(UTC),
            )
        )
        await session.commit()
    assert await _targets(admin_engine, org_a.org_id, subnet_a) == []


async def test_a_subnet_from_another_org_is_not_found(admin_engine: AsyncEngine) -> None:
    """`authorized_targets` takes a subnet OBJECT, so it must check that the
    object belongs to the org it was asked about rather than trusting the
    caller to have loaded it correctly.

    IF THE OWNERSHIP CHECK WERE DELETED this observes org B's authorized targets
    computed under org A's identity -- and Task 9 would sign a job for org A
    against a scope org B authorized.
    """
    org_a = await seed_org(admin_engine, "ownera")
    org_b, vrf_b, subnet_b = await _fixture(admin_engine, "ownerb")
    await _authorize(admin_engine, org_b, vrf_b, "10.70.0.0/24")
    # Org A holds an authorization in org B's VRF -- constructible because the
    # foreign key is org-blind, and seeded so that dropping the ownership check
    # produces a NON-EMPTY leak rather than an empty one. Without this row the
    # mutation would merely stop refusing; with it, org A is handed a target
    # list for a subnet it does not own.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamScanAuthorization(
                org_id=org_a.org_id,
                vrf_id=vrf_b,
                prefix="10.70.0.0/24",
                authorized_by=org_a.user_id,
                authorized_at=datetime.now(UTC),
            )
        )
        await session.commit()
    outcome: list[str] | str
    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, org_b.org_id, subnet_b)
        try:
            outcome = await authorized_targets(session, org_a.org_id, subnet)
        except IpamNotFoundError as exc:
            outcome = f"refused: {exc}"
    # THE ASSERTION IS ON THE VALUE, not on "an exception was raised": with the
    # check deleted this prints the leaked target list.
    assert isinstance(outcome, str), f"org A received org B's authorized targets: {outcome}"


# ---------------------------------------------------------------------------
# CRUD, uniqueness and audit (§2.9, §11.3)
# ---------------------------------------------------------------------------


async def test_creating_and_revoking_are_audited(admin_engine: AsyncEngine) -> None:
    """§11.3's HIGH-PRIVILEGE PAIR. `ipam.scope_authorized` and
    `ipam.scope_revoked` carry the prefix, the actor and the note, because this
    is the decision that puts packets on somebody's wire and the log is the only
    place it is ever written down."""
    org, vrf_id, _ = await _fixture(admin_engine, "audit")
    authorization_id = await _authorize(
        admin_engine, org, vrf_id, "10.70.0.0/24", note="change CAB-1174"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=authorization_id
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org.org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    actions = [e.action for e in events]
    assert actions == ["ipam.scope_authorized", "ipam.scope_revoked"]
    assert events[0].meta["prefix"] == "10.70.0.0/24"
    assert events[0].meta["note"] == "change CAB-1174"
    assert events[0].actor_id == org.user_id
    assert events[1].meta["prefix"] == "10.70.0.0/24"
    assert {e.target_type for e in events} == {"ipam_scan_authorization"}


async def test_authorized_by_is_required(admin_engine: AsyncEngine) -> None:
    """§2.9: `authorized_by` is NOT NULL because THERE IS NO SYSTEM-AUTHORIZED
    SCOPE. A human with `ipam.scope.manage` put their name on every packet this
    module emits.

    IF `actor_id=None` WERE ACCEPTED the column would take it only until the
    database refused -- so the service refuses first, with a message, rather
    than surfacing a `NotNullViolationError` as a 500.
    """
    org, vrf_id, _ = await _fixture(admin_engine, "actor")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="authorized_by"):
            await create_authorization(
                session,
                org.org_id,
                actor_id=None,
                vrf_id=vrf_id,
                prefix="10.70.0.0/24",
            )


async def test_two_active_authorizations_for_one_prefix_are_refused_but_a_revoked_one_is_not(
    admin_engine: AsyncEngine,
) -> None:
    """THE PAIR IS WHAT PROVES THE INDEX IS PARTIAL.

    A duplicate ACTIVE row is refused; re-authorizing a prefix whose previous
    authorization was revoked SUCCEEDS. IF `uq_ipam_scope_active` WERE TOTAL the
    second half observes an `IntegrityError` where it should succeed, and a
    prefix could never be authorized again after one revocation -- which would
    make revocation a one-way door nobody would use.
    """
    org, vrf_id, _ = await _fixture(admin_engine, "uniq")
    first = await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already authorized"):
            await create_authorization(
                session,
                org.org_id,
                actor_id=org.user_id,
                vrf_id=vrf_id,
                prefix="10.70.0.0/24",
            )
        await session.rollback()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=first
        )
        await session.commit()
    second = await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    assert second != first


async def test_revocation_never_deletes_the_row(admin_engine: AsyncEngine) -> None:
    """§2.9: rows are never hard-deleted, so the record of WHAT WAS AUTHORIZED
    WHEN survives as evidence. IF REVOCATION DELETED, this observes zero rows
    and an estate could not answer "who authorized probing that segment in
    March"."""
    org, vrf_id, _ = await _fixture(admin_engine, "keep")
    authorization_id = await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=authorization_id
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(IpamScanAuthorization).where(
                    IpamScanAuthorization.id == authorization_id
                )
            )
        ).scalar_one()
        active = await list_authorizations(session, org.org_id)
        everything = await list_authorizations(session, org.org_id, include_revoked=True)
    assert row.revoked_at is not None
    assert row.revoked_by == org.user_id
    assert active == []
    assert [r.id for r in everything] == [authorization_id]


async def test_revoking_twice_is_refused(admin_engine: AsyncEngine) -> None:
    org, vrf_id, _ = await _fixture(admin_engine, "twice")
    authorization_id = await _authorize(admin_engine, org, vrf_id, "10.70.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=authorization_id
        )
        with pytest.raises(IpamConflictError, match="already revoked"):
            await revoke_authorization(
                session, org.org_id, actor_id=org.user_id, authorization_id=authorization_id
            )
        await session.rollback()


async def test_revoking_another_orgs_authorization_is_not_found(
    admin_engine: AsyncEngine,
) -> None:
    org_a, _, _ = await _fixture(admin_engine, "revxa")
    org_b, vrf_b, _ = await _fixture(admin_engine, "revxb")
    authorization_id = await _authorize(admin_engine, org_b, vrf_b, "10.70.0.0/24")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await revoke_authorization(
                session, org_a.org_id, actor_id=org_a.user_id, authorization_id=authorization_id
            )
        with pytest.raises(IpamNotFoundError):
            await revoke_authorization(
                session, org_a.org_id, actor_id=org_a.user_id, authorization_id=uuid4()
            )


async def test_a_vrf_from_another_org_is_refused(admin_engine: AsyncEngine) -> None:
    org_a, _, _ = await _fixture(admin_engine, "vrfxa")
    org_b, vrf_b, _ = await _fixture(admin_engine, "vrfxb")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError):
            await create_authorization(
                session,
                org_a.org_id,
                actor_id=org_a.user_id,
                vrf_id=vrf_b,
                prefix="10.70.0.0/24",
            )


async def test_an_unparseable_prefix_is_refused(admin_engine: AsyncEngine) -> None:
    org, vrf_id, _ = await _fixture(admin_engine, "badpfx")
    async with AsyncSession(admin_engine) as session:
        for bad in ("not a prefix", "", "10.70.0.0/33", "10.70.0.0/-1"):
            with pytest.raises(IpamValidationError):
                await create_authorization(
                    session, org.org_id, actor_id=org.user_id, vrf_id=vrf_id, prefix=bad
                )


async def test_a_non_canonical_prefix_is_normalised(admin_engine: AsyncEngine) -> None:
    """`10.70.0.5/24` means the /24. Postgres's `cidr` would refuse the literal
    outright ("bits set to right of mask"), so the service normalises and the
    audit log records what was STORED alongside what was typed."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "canon")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.5/24")
    assert await _targets(admin_engine, org.org_id, subnet_id) == ["10.70.0.0/24"]


# ---------------------------------------------------------------------------
# The digest, and the sequencing control
# ---------------------------------------------------------------------------


async def test_the_target_list_is_canonical_so_its_digest_is_stable(
    admin_engine: AsyncEngine,
) -> None:
    """`scope_digest` binds what the server authorized at dispatch to what the
    agent claims it probed, and it hashes the LIST IT IS GIVEN. Two runs that
    computed the same scope in a different order would produce two digests and
    the check would fail open at the next refactor -- so the list is canonical
    at the source."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "digest")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.128/25")
    await _authorize(admin_engine, org, vrf_id, "10.70.0.0/26")
    first = await _targets(admin_engine, org.org_id, subnet_id)
    second = await _targets(admin_engine, org.org_id, subnet_id)
    assert first == second == ["10.70.0.0/26", "10.70.0.128/25"]
    assert scope_digest(first) == scope_digest(second)
    assert scope_digest(first) != scope_digest(["10.70.0.0/24"])


def _code_of(path: Path) -> str:
    """The file's source with every docstring removed -- the same technique
    `tests/test_health_egress.py` uses, and for the same reason: a docstring
    that EXPLAINS why a call is absent must not read as the call."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)


def _sources() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.py"))


def _files_naming(needle: str) -> list[str]:
    return [p.name for p in _sources() if needle in _code_of(p)]


def test_sweep_py_is_the_only_file_in_this_module_that_can_dispatch() -> None:
    """THE SEQUENCING CONTROL, NARROWED BY TASK 9 EXACTLY AS IT PROMISED.

    Before Task 9 this asserted that NO file in the package named `JobEngine`,
    `signed_envelope`, `publish_job` or `job_engine` -- the module's
    authorization surface lands before anything here can put packets on a wire.
    Task 9 created the dispatcher, so the property narrows rather than
    disappears: **exactly `sweep.py`**, and (asserted from its AST in
    `tests/test_ipam_sweep_dispatch.py`) it calls `authorized_targets` before
    anything that can produce a signature.

    A second file that acquires a dispatch path fails this by existing, whether
    or not the call is reachable -- which is the whole point, because §6.3's
    guarantee is about what the module CAN do, not about what today's call
    graph happens to do.

    `publish_job` is separate and stricter: nothing in this module may publish
    to Redis at all. The signed envelope is returned to the caller
    (`app/worker.py`, and the API layer) and published only AFTER the
    transaction commits, so a job can never reach an agent describing a run that
    rolled back. That mirrors `tests/test_health_egress.py`'s rule for the
    health module.
    """
    # `JobEngine` and `signed_envelope` name the two things that CREATE and SIGN
    # a job. Exactly one file in this module may do either.
    for needle in ("JobEngine", "signed_envelope"):
        assert _files_naming(needle) == ["sweep.py"], (
            f"{needle!r} appears in {_files_naming(needle)}; §6.3 puts the refusal on "
            "the NEAR side of the signing boundary, and `sweep.py` is the one file "
            "allowed to cross it"
        )
    # `job_engine` is the kernel's ALREADY-CONSTRUCTED engine
    # (`request.app.state.job_engine`), which the route layer hands to
    # `create_sweep`. Passing it is not dispatching -- `create_sweep` still runs
    # `authorized_targets` first -- so the route layer is allowed to name it and
    # nothing else is.
    assert set(_files_naming("job_engine")) <= {"api.py", "sweep.py"}, (
        f"`job_engine` appears in {_files_naming('job_engine')}; only the route "
        "layer may hand the kernel's engine to `sweep.py`"
    )
    # `publish_job` is separate and stricter, and is asserted as CONTAINMENT
    # AGAINST A NAMED ALLOWLIST rather than as equality: exactly one file may
    # publish to Redis, and it is the route layer, which does so only AFTER its
    # `org_session` block has exited. A second file that acquires a publish path
    # fails this by existing. Containment rather than equality because the
    # allowed file arrives in a later commit than this test -- the property
    # ("no OTHER file publishes") is identical either way, and this is not the
    # `>=`-shaped hole a route-count assertion would be: the allowlist is a
    # closed set of names, not a lower bound.
    assert set(_files_naming("publish_job")) <= {"api.py"}, (
        f"`publish_job` appears in {_files_naming('publish_job')}: `create_sweep` "
        "returns the signed envelope and only the route layer publishes it, after "
        "the transaction commits"
    )


def test_the_dispatch_grep_can_actually_find_something() -> None:
    """THE MUTATION, RUN IN-TEST.

    A grep that returns zero because it is looking in the wrong directory is
    indistinguishable from a module with no dispatch path. This writes a probe
    file containing each token and asserts the same scanner reports it, so the
    empty results above are evidence rather than an artefact of the path.
    """
    probe = MODULE_DIR / "_dispatch_probe_delete_me.py"
    probe.write_text(
        "from app.jobs.engine import JobEngine\n"
        "from app.gateway.dispatch import publish_job\n"
        "\n"
        "async def x(session, engine: JobEngine):\n"
        "    job = await engine.create(session)\n"
        "    envelope = engine.signed_envelope(job)\n"
        "    await publish_job(None, None, envelope)\n"
    )
    try:
        for needle in ("JobEngine", "signed_envelope", "publish_job"):
            assert "_dispatch_probe_delete_me.py" in _files_naming(needle), (
                f"the scanner did not find {needle!r} in a file that contains it"
            )
    finally:
        probe.unlink()
    for needle in ("JobEngine", "signed_envelope", "publish_job"):
        assert "_dispatch_probe_delete_me.py" not in _files_naming(needle), (
            "the probe file outlived the test, so every result above is suspect"
        )


def test_the_module_has_source_files_to_scan() -> None:
    """The positive control for the greps: an empty directory reports zero
    dispatch call sites and is indistinguishable from a clean module."""
    names = {p.name for p in _sources()}
    assert {
        "authorization.py",
        "allocation.py",
        "addresses.py",
        "observations.py",
        "subnets.py",
        "net.py",
        "sweep.py",
    } <= names

"""The segment check: declared segments versus what inventory actually saw
(spec §11, §3.4, §0.8).

**THE BRANCH THAT MATTERS MOST IS THE ONE THAT IS HARDEST TO REACH.**
`evidence="unavailable"` only happens where IPAM is not installed, and IPAM IS
installed in the test schema — so without forcing the probe, that branch is
never exercised and the difference between "no mismatches" and "we could not
look" is untested. `_observation_evidence_available` is factored out precisely
so it can be forced, and the test asserts that the SAME fixture produces
`matched != []` with it True and `evidence == "unavailable"` with it False. Two
fixtures, two different observable bodies; asserting only the empty list would
be satisfied by a function that never ran.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.modules.site_server import segments as segments_module
from app.modules.site_server.errors import SiteServerNotFoundError
from app.modules.site_server.segments import segment_check
from tests.ipam_fixtures import seed_org, seed_vrf, upsert_observation_row
from tests.site_server_fixtures import seed_site_server

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


async def _fixture(
    admin_engine: AsyncEngine, tag: str, *, segments: list[str]
) -> tuple[Any, UUID, UUID]:
    org = await seed_org(admin_engine, tag)
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=segments
    )
    return org, vrf_id, site_server_id


async def _observe(
    admin_engine: AsyncEngine,
    org: Any,
    vrf_id: UUID,
    address: str,
    *,
    source: str = "inventory",
    device_id: UUID | None = ...,  # type: ignore[assignment]
    age: timedelta = timedelta(hours=1),
) -> None:
    kwargs: dict[str, Any] = {
        "vrf_id": vrf_id,
        "address": address,
        "source": source,
        "method": "nic" if source == "inventory" else "icmp",
        "confidence": "high" if source == "inventory" else "low",
        "now": NOW - age,
    }
    # A SWEEP observation has no `device_id` AT ALL (IPAM §6.4 pins it to NULL),
    # which is exactly why it can never be evidence about this device.
    kwargs["device_id"] = org.device_id if device_id is ... else device_id
    if source != "inventory":
        kwargs["device_id"] = None
    await upsert_observation_row(admin_engine, org.org_id, **kwargs)


# --------------------------------------------------------------------------- #
# The three sets
# --------------------------------------------------------------------------- #


async def test_an_observed_declared_segment_is_matched_and_nothing_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-a", segments=["10.20.7.0/24"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    observed = (
        check.evidence,
        check.matched,
        check.declared_unobserved,
        check.observed_undeclared,
    )
    assert observed == (
        "ipam",
        ["10.20.7.0/24"],
        [],
        [],
    )


async def test_a_declared_segment_with_no_observation_lands_in_declared_unobserved(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**THE FIXTURE DECLARES A SECOND, OBSERVED SEGMENT**, so the test observes
    WHICH segment landed where rather than "the list is non-empty".

    With one declared segment, "the unobserved one was classified correctly" and
    "everything is classified as unobserved" are the same answer.
    """
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-b", segments=["10.20.7.0/24", "10.30.0.0/16"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.declared_unobserved) == (
        ["10.20.7.0/24"],
        ["10.30.0.0/16"],
    )


async def test_an_address_outside_every_declared_segment_is_observed_undeclared(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-c", segments=["10.20.7.0/24"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")
    await _observe(admin_engine, org, vrf_id, "192.168.50.4")

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.observed_undeclared) == (
        ["10.20.7.0/24"],
        ["192.168.50.4"],
    )


async def test_a_non_inventory_observation_is_not_evidence_about_this_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**THE NON-INVENTORY ROW CARRIES A `device_id`, AND THAT IS THE WHOLE
    POINT OF THIS FIXTURE.**

    The obvious version of this test — a `source='sweep'` row left with
    `device_id IS NULL`, which is how IPAM's own handler writes them — IS
    FIXTURE-BLIND, and was written that way first. It passes with
    `AND o.source = 'inventory'` DELETED FROM THE QUERY, because the
    `o.device_id = :device` predicate already excludes a NULL-device row. Two
    columns separating the rows means the assertion never observes the one under
    test. That is this project's defining defect and it reproduced here on the
    first attempt; the mutation was run and came back green.

    So the row below is `source='import'` WITH this device's id set. Nothing in
    the schema forbids that — `ipam_observation` has no CHECK tying `source` to
    `device_id`, and an IMPORT legitimately carries device identity — so `source`
    is now the ONLY thing distinguishing the two rows, and deleting the
    predicate makes `10.30.0.0/16` move from `declared_unobserved` to `matched`.

    The property being defended: only inventory places a machine on a wire. An
    address that arrived from a sweep, an import or DHCP is not evidence that
    THIS device is on that segment, and treating it as such would let a site
    server declared for a segment it is not on look verified — by data its own
    sweep preference would have produced.
    """
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-d", segments=["10.20.7.0/24", "10.30.0.0/16"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.30.1.5",
        source="import",
        method="manual",
        confidence="low",
        device_id=org.device_id,
        now=NOW - timedelta(hours=1),
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.declared_unobserved, check.observed_undeclared) == (
        ["10.20.7.0/24"],
        ["10.30.0.0/16"],
        [],
    )


async def test_a_sweep_observation_is_not_evidence_either(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The realistic shape, kept ALONGSIDE the discriminating one above.

    A sweep sees addresses and MACs, not identity, so IPAM pins
    `ipam_observation.device_id` to NULL for every sweep row (§6.4). This test
    is deliberately NOT the guard — it passes with the `source` predicate
    deleted, because the NULL `device_id` excludes the row on its own — and it
    is kept because it pins the behaviour operators actually see. The test above
    is what makes the predicate itself falsifiable.
    """
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-d2", segments=["10.20.7.0/24", "10.30.0.0/16"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")
    await _observe(admin_engine, org, vrf_id, "10.30.1.5", source="sweep")

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.declared_unobserved) == (
        ["10.20.7.0/24"],
        ["10.30.0.0/16"],
    )


async def test_an_observation_older_than_the_ttl_is_excluded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """One row INSIDE the currency window and one OUTSIDE it, so the test
    observes which survived rather than that the list shrank.

    IPAM's default `observation_ttl_days` is 14. A stale observation is not
    evidence that a machine is still on that wire — the NIC may have moved
    months ago, and a segment check that reported it as matched would keep
    telling an operator a wrong declaration was fine.
    """
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-e", segments=["10.20.7.0/24", "10.30.0.0/16"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9", age=timedelta(days=1))
    await _observe(admin_engine, org, vrf_id, "10.30.1.5", age=timedelta(days=60))

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.declared_unobserved, check.observed_undeclared) == (
        ["10.20.7.0/24"],
        ["10.30.0.0/16"],
        [],
    )


async def test_another_devices_observation_is_not_evidence_about_this_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Two devices in one org, both on the segment; only THIS device's
    observation may count."""
    from tests.ipam_fixtures import seed_device

    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-f", segments=["10.20.7.0/24"]
    )
    neighbour = await seed_device(admin_engine, org.org_id, hostname="neighbour")
    await _observe(admin_engine, org, vrf_id, "10.20.7.50", device_id=neighbour)

    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (check.matched, check.declared_unobserved) == ([], ["10.20.7.0/24"])


# --------------------------------------------------------------------------- #
# The unavailable branch
# --------------------------------------------------------------------------- #


async def test_the_probe_returns_true_against_the_real_schema(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`to_regclass`, not `try`/`except ProgrammingError`.

    A failed statement ABORTS THE ENCLOSING TRANSACTION in PostgreSQL, so a
    "graceful" `except` around a query on a missing table would poison every
    subsequent write in the same request. This pins that the probe answers
    correctly where the table DOES exist — the other half of the branch is
    forced below.
    """
    org = await seed_org(admin_engine, "sseg-g")
    async with org_session(org.org_id, engine=app_engine) as session:
        assert await segments_module._observation_evidence_available(session) is True


async def test_unavailable_and_empty_are_different_answers(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**THE SAME FIXTURE, TWO PROBE ANSWERS, TWO DIFFERENT BODIES.**

    "No mismatches" and "we could not look" are opposite answers and must never
    render the same. With the probe True this fixture yields
    `("ipam", ["10.20.7.0/24"])`; with it False, `("unavailable", [])`.

    Asserting only the empty list on the unavailable branch would be satisfied
    by a function that never ran — `[]` is the default of everything.
    """
    org, vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-h", segments=["10.20.7.0/24"]
    )
    await _observe(admin_engine, org, vrf_id, "10.20.7.9")

    async with org_session(org.org_id, engine=app_engine) as session:
        available = await segment_check(session, org.org_id, site_server_id, now=NOW)

    async def _absent(_session: Any) -> bool:
        return False

    monkeypatch.setattr(segments_module, "_observation_evidence_available", _absent)
    async with org_session(org.org_id, engine=app_engine) as session:
        unavailable = await segment_check(session, org.org_id, site_server_id, now=NOW)

    assert (available.evidence, available.matched) == ("ipam", ["10.20.7.0/24"])
    assert (unavailable.evidence, unavailable.matched) == ("unavailable", [])
    assert (unavailable.declared_unobserved, unavailable.observed_undeclared) == ([], [])


async def test_the_unavailable_branch_does_not_poison_the_transaction(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the probe is `to_regclass` and not `try`/`except`.

    After the unavailable branch, the SAME session must still be able to write.
    A failed statement would have left the transaction aborted and this INSERT
    would raise `current transaction is aborted, commands ignored until end of
    transaction block` — the failure mode that makes the "graceful" fallback
    worse than a 500.
    """
    org, _vrf_id, site_server_id = await _fixture(
        admin_engine, "sseg-i", segments=["10.20.7.0/24"]
    )

    async def _absent(_session: Any) -> bool:
        return False

    monkeypatch.setattr(segments_module, "_observation_evidence_available", _absent)
    async with org_session(org.org_id, engine=app_engine) as session:
        check = await segment_check(session, org.org_id, site_server_id, now=NOW)
        await session.execute(
            text("UPDATE site_server SET notes = 'still writable' WHERE id = :id"),
            {"id": site_server_id},
        )

    async with AsyncSession(admin_engine) as session:
        notes = (
            await session.execute(
                text("SELECT notes FROM site_server WHERE id = :id"), {"id": site_server_id}
            )
        ).scalar_one()

    assert (check.evidence, notes) == ("unavailable", "still writable")


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_another_orgs_site_server_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE. *If `get_site_server`'s `org_id` predicate
    were deleted* this returns org B's check instead of raising, and the
    assertion reports its device id."""
    org_a = await seed_org(admin_engine, "sseg-j")
    org_b, _vrf, foreign = await _fixture(admin_engine, "sseg-k", segments=["10.20.7.0/24"])

    found: Any = "not-found"
    async with AsyncSession(admin_engine) as session:
        try:
            check = await segment_check(session, org_a.org_id, foreign, now=NOW)
            found = check.device_id
        except SiteServerNotFoundError:
            pass

    assert found == "not-found"

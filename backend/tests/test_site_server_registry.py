"""The registry service: promote, update, demote, read (spec §3.3, §10.3).

**EVERY GUARD HERE IS PINNED BY A FIXTURE WHERE THE GUARDED AND THE UNGUARDED
PATH PRODUCE DIFFERENT STORED VALUES.** Three specific traps this project has
shipped before, and how each is avoided below:

* a mutation whose only evidence is `DID NOT RAISE` — so every refusal test also
  asserts the ROW COUNT or the stored tuple afterwards;
* a parameter held constant across the fixture, so "it picked the right one" and
  "it picked the only one" are the same answer — so the agent-less-device test
  seeds TWO devices, one with an agent and one without, and promotes the wrong
  one;
* an empty answer the function's default already produces — so every refusal
  test is paired with the acceptance that would fail if the function refused
  everything.

**THE CROSS-ORG TEST RUNS ON THE ADMIN ENGINE.** Driven through `org_session`,
RLS rejects first, and the test passes with `promote`'s own `Device.org_id ==
org_id` predicate deleted. That defect has shipped four times in this repo. On
the admin engine RLS is not the thing doing the rejecting, so the application
predicate is what the assertion observes.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.modules.site_server import registry
from app.modules.site_server.errors import (
    SiteServerNotFoundError,
    SiteServerValidationError,
)
from tests.ipam_fixtures import seed_agent, seed_device, seed_org, seed_site
from tests.site_server_fixtures import seed_site_server, site_server_rows


async def _audit_actions(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action FROM audit_events WHERE org_id = :org"
                    "   AND action LIKE 'site.%' ORDER BY ts, id"
                ),
                {"org": org_id},
            )
        ).scalars().all()
    return list(rows)


async def _audit_meta(engine: AsyncEngine, org_id: UUID, action: str) -> dict[str, Any]:
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT metadata FROM audit_events"
                    " WHERE org_id = :org AND action = :action"
                    " ORDER BY ts DESC, id DESC LIMIT 1"
                ),
                {"org": org_id, "action": action},
            )
        ).scalar_one()
    return dict(row)


# --------------------------------------------------------------------------- #
# promote — the happy path
# --------------------------------------------------------------------------- #


async def test_promote_stores_the_declaration_and_audits_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "ssr-a")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_id = await seed_site(admin_engine, org.org_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await registry.promote(
            session,
            org.org_id,
            device_id=org.device_id,
            segments=["10.20.0.0/16", "10.30.7.0/24"],
            site_id=site_id,
            actor_id=org.user_id,
        )
        stored_id = row.id

    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.20.0.0/16", "10.30.7.0/24"], True)
    ]
    assert await _audit_actions(admin_engine, org.org_id) == ["site.promoted"]
    meta = await _audit_meta(admin_engine, org.org_id, "site.promoted")
    assert (meta["site_server_id"], meta["segments"], meta["roles"]) == (
        str(stored_id),
        ["10.20.0.0/16", "10.30.7.0/24"],
        ["wake"],
    )


async def test_promote_succeeds_with_no_observation_at_all(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§0.8's NEGATIVE half, and the test that stops a future author
    "helpfully" adding a subnet check here.

    Promotion accepts an operator's CLAIM without verifying it, deliberately: a
    freshly imaged branch server has no inventory observation yet, and refusing
    would make it unpromotable until one arrives. The claim is harmless because
    a claim earns sweep PREFERENCE and only evidence earns CANDIDACY (§6.1) —
    an unverified declaration cannot put a probe on a wire. The mismatch
    surfaces on the segment check (§11), where an operator can act on it.
    """
    org = await seed_org(admin_engine, "ssr-b")
    await seed_agent(admin_engine, org.org_id, org.device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.promote(
            session, org.org_id, device_id=org.device_id, segments=["10.99.0.0/16"]
        )

    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.99.0.0/16"], True)
    ]


async def test_the_name_defaults_to_the_device_hostname(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "ssr-c")
    device_id = await seed_device(admin_engine, org.org_id, hostname="branch-manila-01")
    await seed_agent(admin_engine, org.org_id, device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        default_named = await registry.promote(
            session, org.org_id, device_id=device_id, segments=["10.20.0.0/16"]
        )
        assert default_named.name == "branch-manila-01"

    other = await seed_device(admin_engine, org.org_id, hostname="branch-cebu-01")
    await seed_agent(admin_engine, org.org_id, other)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        explicit = await registry.promote(
            session, org.org_id, device_id=other, segments=["10.21.0.0/16"], name="Cebu edge"
        )
        assert explicit.name == "Cebu edge"


# --------------------------------------------------------------------------- #
# promote — the five validations, each with a discriminating fixture
# --------------------------------------------------------------------------- #


async def test_a_device_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE, where RLS is not the thing rejecting.

    *If `_device_in_org`'s `Device.org_id == org_id` predicate were deleted*,
    this promotes org B's device into org A's registry and the observed value
    becomes `[(<org B's device>, ['10.20.0.0/16'], True)]` instead of `[]`.
    Driven through `org_session` the same mutation would still pass, because RLS
    would refuse to load the row before the predicate mattered — which is why
    this test does not use `org_session`.
    """
    org_a = await seed_org(admin_engine, "ssr-d")
    org_b = await seed_org(admin_engine, "ssr-e")
    await seed_agent(admin_engine, org_b.org_id, org_b.device_id)

    refused = False
    async with AsyncSession(admin_engine) as session:
        try:
            await registry.promote(
                session, org_a.org_id, device_id=org_b.device_id, segments=["10.20.0.0/16"]
            )
            await session.commit()
        except SiteServerNotFoundError:
            refused = True
            await session.rollback()

    assert (refused, await site_server_rows(admin_engine, org_a.org_id)) == (True, [])


async def test_a_device_with_no_agent_is_refused_and_its_neighbour_is_not(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """TWO DEVICES, and the one WITHOUT an agent is the one promoted.

    A fixture holding a single agent-less device cannot tell "the check works"
    from "promotion is broken for everything" — both produce an empty table.
    Here the agented device promotes successfully in the same test, so a
    `promote` that refused unconditionally fails on the second half.
    """
    org = await seed_org(admin_engine, "ssr-f")
    with_agent = await seed_device(admin_engine, org.org_id, hostname="has-agent")
    await seed_agent(admin_engine, org.org_id, with_agent)
    without_agent = await seed_device(admin_engine, org.org_id, hostname="no-agent")

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError, match="no enrolled agent"):
            await registry.promote(
                session, org.org_id, device_id=without_agent, segments=["10.20.0.0/16"]
            )

    assert await site_server_rows(admin_engine, org.org_id) == []

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.promote(
            session, org.org_id, device_id=with_agent, segments=["10.20.0.0/16"]
        )

    assert await site_server_rows(admin_engine, org.org_id) == [
        (with_agent, ["10.20.0.0/16"], True)
    ]


async def test_double_promotion_leaves_exactly_one_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """ASSERT THE ROW COUNT, not that an exception was raised: the failure this
    guards against is a SECOND row, and "an exception happened" cannot see one.
    """
    org = await seed_org(admin_engine, "ssr-g")
    await seed_agent(admin_engine, org.org_id, org.device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.promote(
            session, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
        )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError, match="already a site server"):
            await registry.promote(
                session, org.org_id, device_id=org.device_id, segments=["10.21.0.0/16"]
            )

    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.20.0.0/16"], True)
    ]


async def test_host_bits_refused_and_a_network_prefix_accepted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """BOTH DIRECTIONS IN ONE TEST: a validator that refused every segment
    passes the refusal half and fails here."""
    org = await seed_org(admin_engine, "ssr-h")
    await seed_agent(admin_engine, org.org_id, org.device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError):
            await registry.promote(
                session, org.org_id, device_id=org.device_id, segments=["10.20.7.4/24"]
            )
    assert await site_server_rows(admin_engine, org.org_id) == []

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.promote(
            session, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
        )
    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.20.7.0/24"], True)
    ]


async def test_cache_role_refused_and_wake_accepted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE READING THIS MODULE TAKES OF §2.1's "v1 only `wake` is honoured":
    REFUSED, not accepted-and-ignored.

    A stored `roles=['cache']` on a machine that binds no socket is a registry
    row saying a service is running when none is, and the operator has no way to
    discover that. Refusing costs one later `PATCH` on the day §4.4's amendment
    is accepted. The DB CHECK stays wider on purpose (see the migration) so that
    day needs no migration.
    """
    org = await seed_org(admin_engine, "ssr-i")
    await seed_agent(admin_engine, org.org_id, org.device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError) as excinfo:
            await registry.promote(
                session,
                org.org_id,
                device_id=org.device_id,
                segments=["10.20.0.0/16"],
                roles=["wake", "cache"],
            )
    assert "§4.4" in str(excinfo.value)
    assert await site_server_rows(admin_engine, org.org_id) == []

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await registry.promote(
            session,
            org.org_id,
            device_id=org.device_id,
            segments=["10.20.0.0/16"],
            roles=["wake"],
        )
        assert row.roles == ["wake"]
    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.20.0.0/16"], True)
    ]


async def test_zero_and_thirty_three_segments_are_refused_by_the_service(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A 400 naming the ceiling, not an `IntegrityError` naming a constraint —
    and both messages distinct, so "you gave me none" is never reported as "you
    gave me too many"."""
    org = await seed_org(admin_engine, "ssr-j")
    await seed_agent(admin_engine, org.org_id, org.device_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError, match="at least one segment"):
            await registry.promote(session, org.org_id, device_id=org.device_id, segments=[])
        with pytest.raises(SiteServerValidationError, match="at most 32"):
            await registry.promote(
                session,
                org.org_id,
                device_id=org.device_id,
                segments=[f"10.{i}.0.0/16" for i in range(33)],
            )
    assert await site_server_rows(admin_engine, org.org_id) == []


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


async def test_get_site_server_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """*If `get_site_server`'s `SiteServer.org_id == org_id` predicate were
    deleted* this returns org B's row instead of raising, and the assertion
    reports that row's device id."""
    org_a = await seed_org(admin_engine, "ssr-k")
    org_b = await seed_org(admin_engine, "ssr-l")
    foreign = await seed_site_server(
        admin_engine, org_b.org_id, device_id=org_b.device_id, segments=["10.20.0.0/16"]
    )

    found: Any = "not-found"
    async with AsyncSession(admin_engine) as session:
        try:
            row = await registry.get_site_server(session, org_a.org_id, foreign)
            found = row.device_id
        except SiteServerNotFoundError:
            pass
    assert found == "not-found"


async def test_list_filters_by_enabled_site_and_containment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The `segment` filter is CONTAINMENT, not equality: asking who serves
    `10.20.7.0/24` must find the branch server declared for `10.20.0.0/16`.

    The fixture holds a second, non-matching site server throughout, so every
    assertion below observes WHICH row came back rather than "the list is
    non-empty".
    """
    org = await seed_org(admin_engine, "ssr-m")
    branch = await seed_device(admin_engine, org.org_id, hostname="branch")
    other = await seed_device(admin_engine, org.org_id, hostname="other")
    disabled = await seed_device(admin_engine, org.org_id, hostname="disabled")
    site_id = await seed_site(admin_engine, org.org_id)

    await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=branch,
        segments=["10.20.0.0/16"],
        site_id=site_id,
    )
    await seed_site_server(admin_engine, org.org_id, device_id=other, segments=["10.90.0.0/16"])
    await seed_site_server(
        admin_engine, org.org_id, device_id=disabled, segments=["10.20.0.0/16"], enabled=False
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        by_segment = await registry.list_site_servers(session, org.org_id, segment="10.20.7.0/24")
        by_enabled = await registry.list_site_servers(session, org.org_id, enabled=True)
        by_site = await registry.list_site_servers(session, org.org_id, site_id=site_id)
        everything = await registry.list_site_servers(session, org.org_id)

    assert sorted(r.device_id for r in by_segment) == sorted([branch, disabled])
    assert sorted(r.device_id for r in by_enabled) == sorted([branch, other])
    assert [r.device_id for r in by_site] == [branch]
    assert sorted(r.device_id for r in everything) == sorted([branch, other, disabled])


async def test_a_segment_filtered_list_is_ordered_most_specific_first(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """**CONTAINMENT MAKES THE RESULT SET AMBIGUOUS, SO THE ORDER IS AN ANSWER.**

    Every caller that picks one site server picks `[0]`, and with `10.20.0.0/16`
    and `10.20.7.0/24` both matching `10.20.7.5` the plain `created_at` order
    hands that caller the server which merely claims the supernet — a wake on
    `10.20.255.255` instead of `10.20.7.255`, silently, decided by which row an
    operator happened to promote first.

    The fixture puts the CORRECT row LAST by creation order, last by id and last
    by name, so "most specific first" and "whatever order the rows came out in"
    cannot produce the same list. Under an unfiltered list the order reverts to
    oldest-first, and that is asserted here too — otherwise the ordering term
    could be applied unconditionally and nothing would notice.
    """
    org = await seed_org(admin_engine, "ssr-o")
    supernet = await seed_device(admin_engine, org.org_id, hostname="aaa-supernet")
    onwire = await seed_device(admin_engine, org.org_id, hostname="zzz-onwire")

    prefix = uuid4().hex[:8]
    await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=supernet,
        segments=["10.20.0.0/16"],
        name="aaa-supernet",
        row_id=UUID(f"{prefix}-0000-4000-8000-000000000001"),
        created_at=datetime(2026, 8, 1, 9, 1, tzinfo=UTC),
    )
    await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=onwire,
        segments=["10.20.7.0/24"],
        name="zzz-onwire",
        row_id=UUID(f"{prefix}-0000-4000-8000-000000000002"),
        created_at=datetime(2026, 8, 1, 9, 2, tzinfo=UTC),
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        by_address = await registry.list_site_servers(session, org.org_id, segment="10.20.7.5")
        by_prefix = await registry.list_site_servers(
            session, org.org_id, segment="10.20.7.0/24"
        )
        unfiltered = await registry.list_site_servers(session, org.org_id)

    assert [r.device_id for r in by_address] == [onwire, supernet]
    assert [r.device_id for r in by_prefix] == [onwire, supernet]
    # No segment, no specificity to rank by: oldest first, unchanged.
    assert [r.device_id for r in unfiltered] == [supernet, onwire]


async def test_list_is_org_scoped(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_a = await seed_org(admin_engine, "ssr-n")
    org_b = await seed_org(admin_engine, "ssr-o")
    await seed_site_server(
        admin_engine, org_b.org_id, device_id=org_b.device_id, segments=["10.20.0.0/16"]
    )
    async with AsyncSession(admin_engine) as session:
        rows = await registry.list_site_servers(session, org_a.org_id)
    assert [r.device_id for r in rows] == []


# --------------------------------------------------------------------------- #
# update and demote
# --------------------------------------------------------------------------- #


async def test_update_changes_segments_and_records_before_and_after(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "ssr-p")
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.update_site_server(
            session,
            org.org_id,
            site_server_id,
            changes={"segments": ["10.21.0.0/16"], "enabled": False},
            actor_id=org.user_id,
        )

    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.21.0.0/16"], False)
    ]
    meta = await _audit_meta(admin_engine, org.org_id, "site.updated")
    assert (meta["before"]["segments"], meta["after"]["segments"]) == (
        ["10.20.0.0/16"],
        ["10.21.0.0/16"],
    )


async def test_update_refuses_a_cache_role_and_an_unknown_field(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "ssr-q")
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerValidationError, match="§4.4"):
            await registry.update_site_server(
                session, org.org_id, site_server_id, changes={"roles": ["pxe"]}
            )
        with pytest.raises(SiteServerValidationError, match="cannot update"):
            await registry.update_site_server(
                session, org.org_id, site_server_id, changes={"org_id": uuid4()}
            )
    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.20.0.0/16"], True)
    ]


async def test_demote_deletes_the_row_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "ssr-r")
    keeper = await seed_device(admin_engine, org.org_id, hostname="keeper")
    await seed_site_server(admin_engine, org.org_id, device_id=keeper, segments=["10.90.0.0/16"])
    doomed = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await registry.demote(
            session, org.org_id, doomed, reason="decommissioned", actor_id=org.user_id
        )

    # The SECOND site server is what makes this an observation rather than
    # arithmetic on a one-element set: "the right row went" and "the table was
    # emptied" are different answers.
    assert await site_server_rows(admin_engine, org.org_id) == [
        (keeper, ["10.90.0.0/16"], True)
    ]
    assert "site.demoted" in await _audit_actions(admin_engine, org.org_id)
    meta = await _audit_meta(admin_engine, org.org_id, "site.demoted")
    assert (meta["device_id"], meta["reason"]) == (str(org.device_id), "decommissioned")


async def test_demote_of_another_orgs_row_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    org_a = await seed_org(admin_engine, "ssr-s")
    org_b = await seed_org(admin_engine, "ssr-t")
    foreign = await seed_site_server(
        admin_engine, org_b.org_id, device_id=org_b.device_id, segments=["10.20.0.0/16"]
    )

    refused = False
    async with AsyncSession(admin_engine) as session:
        try:
            await registry.demote(session, org_a.org_id, foreign)
            await session.commit()
        except SiteServerNotFoundError:
            refused = True
            await session.rollback()

    assert (refused, await site_server_rows(admin_engine, org_b.org_id)) == (
        True,
        [(org_b.device_id, ["10.20.0.0/16"], True)],
    )

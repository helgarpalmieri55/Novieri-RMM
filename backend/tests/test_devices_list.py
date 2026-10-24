"""`GET /devices` and `device_page`: paging, filtering and counting IN SQL.

**WHAT BROKE, AND WHY IT LOOKED FINE.** The console fetched one capped page of
`GET /devices` and computed the search, the provenance filter and all five tab
counts in the browser, over that page. Since the operator's ruling of 2026-08-26
a `net.sweep` records every host it finds, so a /24 adds ~200 rows in one act.
Past the page limit the tab counts were wrong, the search missed rows it should
have found, and the provenance filter -- the one control that answers "which of
these did a person vouch for?" -- reported nothing on an estate full of swept
rows. Every one of those failures renders as a correct-looking empty answer,
which is why none of them was noticed.

WHAT THESE FIXTURES DELIBERATELY VARY -- each guard is paired with a fixture in
which the guarded and the unguarded path produce DIFFERENT OBSERVABLE VALUES:

* **The estate is bigger than one page, and the interesting row sorts LAST.**
  A 5-device fixture cannot tell server-side search from browser-side search:
  both find the row, because the row is on the page either way. The row this
  file searches for is the 150th by hostname, so a browser-side filter over the
  first 100 finds nothing.
* **The tab counts are asserted WITH A SEARCH ACTIVE.** A count computed over
  the whole table and a count computed over the search agree exactly when the
  search is empty, which is the state most fixtures leave it in.
* **The certificate boundary is straddled**, one device inside the window and
  one outside, so the SQL threshold is checked against a value rather than
  against zero.
* **The tenancy test runs on the BYPASSRLS admin engine.** Inside `org_session`
  Postgres filters first, so a query with its `org_id` predicate deleted returns
  exactly the rows a correct one would and no route test goes red. On the admin
  engine the application's own predicate is the only thing scoping anything.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Agent, Device, Organization
from app.services.cert_renewal import EXPIRING_REMAINING_DAYS, cert_state
from app.services.device_list import device_page

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


async def _org(admin_engine: AsyncEngine, prefix: str) -> UUID:
    # Suffixed: several tests here seed the same named estate, and the whole
    # file shares one database.
    slug = f"{prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    hostname: str | None = None,
    agent_status: str | None = None,
    cert_not_after: datetime | None = None,
    cert_revoked: bool = False,
    **fields: object,
) -> UUID:
    """One asset row, optionally with an `agents` row beside it.

    `agent_status=None` means NO AGENT -- warehouse stock, a switch, a row a
    sweep wrote. That is a different thing from `offline`, and the tab strip
    keeps them apart.
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id, hostname=hostname, **fields, client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.flush()
        if agent_status is not None:
            session.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=device.id.hex,
                    cert_serial=device.id.hex[:16],
                    cert_not_after=cert_not_after or (NOW + timedelta(days=300)),
                    cert_revoked=cert_revoked,
                    enrolled_at=NOW - timedelta(days=30),
                    status=agent_status,
                )
            )
        await session.commit()
        return device.id


# --------------------------------------------------------------------------- #
# The page, and the search that has to reach past it
# --------------------------------------------------------------------------- #


async def test_the_search_finds_a_row_the_first_page_does_not_contain(
    admin_engine: AsyncEngine,
) -> None:
    """**THE DEFECT, AS A FIXTURE.** 150 rows, page size 100, and the row being
    searched for is the LAST by hostname.

    *With the filter in the browser over one page* -- which is what this
    replaced -- the search returns nothing and the screen says "No devices match
    'wks-149'", which is indistinguishable from a correct empty answer. Nothing
    about a 5-row fixture would have shown that.
    """
    org_id = await _org(admin_engine, "dl-search")
    for n in range(150):
        await _device(admin_engine, org_id, hostname=f"wks-{n:03d}")

    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, q="wks-149", now=NOW)
    assert [d.hostname for d, *_ in page.rows] == ["wks-149"]
    assert page.total == 1
    assert page.state_counts.all == 1
    # The tiles do NOT narrow with the search: the estate is still 150 machines.
    assert page.fleet.total == 150


async def test_paging_is_stable_and_covers_the_estate_exactly_once(
    admin_engine: AsyncEngine,
) -> None:
    """Every row appears on exactly one page, and no row appears twice.

    `hostname` is nullable and duplicated across a swept estate, so ordering by
    it alone is not a total order: Postgres is free to return ties differently
    per query and rows then repeat and vanish as an operator pages. *If the
    `Device.id` tiebreak were dropped* this fixture -- three pages of rows that
    ALL share a hostname -- observes fewer than 150 distinct ids.
    """
    org_id = await _org(admin_engine, "dl-page")
    for _ in range(150):
        await _device(admin_engine, org_id, hostname="same-name")

    seen: list[UUID] = []
    async with AsyncSession(admin_engine) as session:
        for offset in (0, 60, 120):
            page = await device_page(session, org_id, limit=60, offset=offset, now=NOW)
            seen.extend(device.id for device, *_ in page.rows)
            assert page.total == 150
    assert len(seen) == 150
    assert len(set(seen)) == 150


# --------------------------------------------------------------------------- #
# The three counts, and which filters each obeys
# --------------------------------------------------------------------------- #


async def _mixed_estate(admin_engine: AsyncEngine) -> UUID:
    """Rows that differ in exactly the dimensions the strip and tiles read.

    Two searchable ("srv-"), three not, and the two searchable ones are an
    online agent and an agentless asset -- so a `state_counts` computed over the
    whole table and one computed over the search differ in every entry.
    """
    org_id = await _org(admin_engine, "dl-counts")
    await _device(admin_engine, org_id, hostname="srv-online", agent_status="online")
    await _device(
        admin_engine, org_id, hostname="srv-asset",
        source="discovery", provenance="sweep",
    )
    await _device(admin_engine, org_id, hostname="wks-online", agent_status="online")
    await _device(admin_engine, org_id, hostname="wks-offline", agent_status="offline")
    await _device(
        admin_engine, org_id, hostname="wks-revoked",
        agent_status="online", cert_revoked=True,
    )
    return org_id


async def test_the_tab_counts_obey_the_search_and_the_tiles_do_not(
    admin_engine: AsyncEngine,
) -> None:
    """Three questions, three answers, and none of them is the other two.

    `state_counts` is what each tab would show FROM THIS SEARCH; `fleet` is the
    estate; `total` is the rows on screen. *If `state_counts` ignored the search*
    the strip reads "All devices 5" above two rows -- the exact defect the
    browser-side version produced. *If `fleet` obeyed it* "Managed endpoints"
    shrinks as somebody types, which is worse.
    """
    org_id = await _mixed_estate(admin_engine)
    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, q="srv-", now=NOW)

    assert [d.hostname for d, *_ in page.rows] == ["srv-asset", "srv-online"]
    assert (
        page.state_counts.all,
        page.state_counts.online,
        page.state_counts.offline,
        page.state_counts.assets,
        page.state_counts.certs,
    ) == (2, 1, 0, 1, 0)
    assert (
        page.fleet.total,
        page.fleet.managed,
        page.fleet.online,
        page.fleet.cert_attention,
    ) == (5, 4, 3, 1)


@pytest.mark.parametrize(
    ("state", "hostnames"),
    [
        ("all", ["srv-asset", "srv-online", "wks-offline", "wks-online", "wks-revoked"]),
        ("online", ["srv-online", "wks-online", "wks-revoked"]),
        ("offline", ["wks-offline"]),
        ("assets", ["srv-asset"]),
        ("certs", ["wks-revoked"]),
        # A stale bookmark renders the list rather than 422ing.
        ("nonsense", ["srv-asset", "srv-online", "wks-offline", "wks-online", "wks-revoked"]),
    ],
)
async def test_each_tab_selects_the_rows_its_count_counted(
    admin_engine: AsyncEngine, state: str, hostnames: list[str]
) -> None:
    """**THE TILE AND THE TABLE CANNOT DISAGREE**, asserted per tab.

    `total` is `state_counts[state]` by construction in the service, and this
    checks the other half of that claim: that the rows returned for a tab are
    the rows its count counted. A count and a row filter written twice is how
    this page produced a tile/table disagreement twice before.

    `wks-revoked` is in BOTH `online` and `certs`: a revoked certificate is not
    an agent state, and the tabs are not a partition.
    """
    org_id = await _mixed_estate(admin_engine)
    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, state=state, now=NOW)
    assert sorted(d.hostname or "" for d, *_ in page.rows) == sorted(hostnames)
    assert page.total == len(hostnames)
    assert page.total == page.state_counts.of(state if state != "nonsense" else "all")


async def test_the_provenance_filter_is_a_predicate_not_a_page_scan(
    admin_engine: AsyncEngine,
) -> None:
    """"Which of these did a person vouch for?" over an estate a sweep filled.

    149 swept rows and one an operator typed in, with the typed-in row sorting
    LAST. *With the filter in the browser over the first 100* the answer to
    "entered by hand" is an empty table -- on an estate that contains exactly
    one such machine, which is the row the operator is looking for.
    """
    org_id = await _org(admin_engine, "dl-prov")
    for n in range(149):
        await _device(
            admin_engine, org_id, hostname=f"host-{n:03d}",
            source="discovery", provenance="sweep",
        )
    await _device(
        admin_engine, org_id, hostname="zz-typed-in",
        source="manual", provenance="operator",
    )

    async with AsyncSession(admin_engine) as session:
        typed = await device_page(session, org_id, provenance="operator", now=NOW)
        swept = await device_page(session, org_id, provenance="sweep", now=NOW)
    assert [d.hostname for d, *_ in typed.rows] == ["zz-typed-in"]
    assert typed.total == 1
    assert swept.total == 149
    assert typed.fleet.total == swept.fleet.total == 150


# --------------------------------------------------------------------------- #
# Certificate attention: the SQL threshold against the Python one
# --------------------------------------------------------------------------- #


async def test_certificate_attention_in_sql_matches_cert_state_in_python(
    admin_engine: AsyncEngine,
) -> None:
    """**THE TWO IMPLEMENTATIONS OF ONE RULE, CHECKED AGAINST EACH OTHER.**

    The console's certificate tab is `revoked | expiring | expired`, and
    `cert_state()` derives the last two in Python from `cert_not_after`. The tab
    is now a SQL predicate, so there are two of them, and a threshold copied as
    a literal would agree until `AGENT_CERT_TTL_DAYS` changed and then disagree
    silently -- under-reporting machines about to fall off the fleet.

    THE FIXTURE STRADDLES THE BOUNDARY BY ONE DAY IN EACH DIRECTION, and asserts
    the Python verdict alongside the SQL one. A fixture with only an expired
    certificate passes against `cert_not_after < now`, which is a materially
    different and much later alarm.
    """
    org_id = await _org(admin_engine, "dl-certs")
    inside = NOW + timedelta(days=EXPIRING_REMAINING_DAYS - 1)
    outside = NOW + timedelta(days=EXPIRING_REMAINING_DAYS + 1)
    await _device(
        admin_engine, org_id, hostname="a-expiring",
        agent_status="online", cert_not_after=inside,
    )
    await _device(
        admin_engine, org_id, hostname="b-healthy",
        agent_status="online", cert_not_after=outside,
    )
    await _device(
        admin_engine, org_id, hostname="c-expired",
        agent_status="offline", cert_not_after=NOW - timedelta(days=1),
    )
    await _device(
        admin_engine, org_id, hostname="d-revoked",
        agent_status="online", cert_not_after=outside, cert_revoked=True,
    )
    # No agent, so no certificate, so never in the set.
    await _device(admin_engine, org_id, hostname="e-asset")

    assert cert_state(inside, NOW) == "expiring"
    assert cert_state(outside, NOW) == "renewing"

    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, state="certs", now=NOW)
    assert sorted(d.hostname or "" for d, *_ in page.rows) == [
        "a-expiring",
        "c-expired",
        "d-revoked",
    ]
    assert page.total == 3
    assert page.fleet.cert_attention == 3


# --------------------------------------------------------------------------- #
# Tenancy (invariant 5)
# --------------------------------------------------------------------------- #


async def test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    """**THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.**

    Everything a request touches runs as `app_user`, which is NOBYPASSRLS, so
    inside `org_session` a query carrying no tenant predicate returns exactly
    the rows a correct one would -- deleting `WHERE org_id` from this service
    turns no route test red. This session is `its_admin`, which is BYPASSRLS:
    Postgres has stopped filtering and the application's own predicate is the
    only thing scoping anything.

    IT IS THE COUNTS THAT MAKE THIS URGENT. A page is a list in which an
    operator might notice foreign hostnames; `fleet.total` is a NUMBER on a
    tile, and another tenant's machines inflating it is invisible.

    PROVED ABLE TO FAIL, both predicates, one at a time. Deleting
    `Device.org_id == org_id` from `_filtered` reported
    `assert ['ours-1', 'theirs-0', 'theirs-1', 'theirs-2'] == ['ours-1']`.
    Deleting it from the fleet aggregate -- which `_filtered` does not cover,
    because the tiles are unfiltered by design -- reported
    `the fleet tile leaked another org's rows: assert 4 == 1`. Two predicates,
    two mutations, two different messages: that is why the assertions below are
    separate and named rather than one comparison of a whole object.
    """
    mine = await _org(admin_engine, "dl-mine")
    theirs = await _org(admin_engine, "dl-theirs")
    await _device(admin_engine, mine, hostname="ours-1", agent_status="online")
    for n in range(3):
        await _device(admin_engine, theirs, hostname=f"theirs-{n}", agent_status="online")

    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, mine, now=NOW)
        searched = await device_page(session, mine, q="theirs", now=NOW)

    assert [d.hostname for d, *_ in page.rows] == ["ours-1"]
    assert page.total == 1, "the page query leaked another org's rows"
    assert page.state_counts.all == 1, "the tab counts leaked another org's rows"
    assert page.fleet.total == 1, "the fleet tile leaked another org's rows"
    assert page.fleet.managed == 1
    # And the search cannot be used to reach across the boundary either.
    assert searched.rows == [] and searched.total == 0


# --------------------------------------------------------------------------- #
# The wire contract
# --------------------------------------------------------------------------- #


async def test_the_route_serves_the_page_the_strip_and_the_tiles(api_env) -> None:
    """`GET /devices`, through HTTP, with everything the screen renders on it.

    THE CONTRACT THE CONSOLE NOW DEPENDS ON: `items` and `total` as before,
    plus `state_counts` (search applied, tab not) and `fleet` (nothing applied).
    Both are additive and defaulted, so a caller reading only `items`/`total`
    -- `deploy`, `service-desk` and `enrollment` all do -- sees exactly what it
    saw before.

    The request carries a search, a tab AND a page size at once, because that is
    the combination the browser sends and the one in which a filter applied in
    the wrong order shows up: `state_counts.all` must be 2 (the search) while
    `total` is 1 (the search and the tab), and `fleet.total` must be 4.
    """
    org_id = await api_env.create_org("dl-route")
    async with AsyncSession(api_env.admin_engine) as session:
        for hostname, status in (
            ("srv-alpha", "online"),
            ("srv-beta", None),
            ("wks-gamma", "offline"),
            ("wks-delta", "online"),
        ):
            device = Device(org_id=org_id, hostname=hostname, client_id=org_id, site_id=org_id)
            session.add(device)
            await session.flush()
            if status is not None:
                session.add(
                    Agent(
                        org_id=org_id, device_id=device.id,
                        cert_fingerprint=device.id.hex, cert_serial=device.id.hex[:16],
                        cert_not_after=datetime.now(UTC) + timedelta(days=300),
                        enrolled_at=datetime.now(UTC), status=status,
                    )
                )
        await session.commit()

    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))
    response = await api_env.client.get(
        "/api/v1/devices?q=srv-&state=online&limit=50", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["hostname"] for item in body["items"]] == ["srv-alpha"]
    assert body["total"] == 1
    assert body["state_counts"] == {
        "all": 2, "online": 1, "offline": 0, "assets": 1, "certs": 0, "review": 0
    }
    assert body["fleet"] == {
        "total": 4, "managed": 3, "online": 2, "cert_attention": 0
    }

    # A caller that sends nothing gets the whole estate and honest tiles.
    plain = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    assert plain["total"] == 4
    assert plain["state_counts"]["all"] == plain["fleet"]["total"] == 4


# --------------------------------------------------------------------------- #
# Waiting for review (install-link spec §1.5, plan D1)
# --------------------------------------------------------------------------- #


async def test_the_review_tab_selects_the_pending_rows_and_a_rejected_row_is_nowhere(
    admin_engine: AsyncEngine,
) -> None:
    """Three rows that differ ONLY in `review_state`, so each assertion below
    is about that column and nothing else. *If the rejected tombstone leaked*,
    `all` would read 3 and the fleet tiles would count a machine a technician
    already threw out. *If the tab filtered on the wrong column*, `review`
    would select the approved row too."""
    org_id = await _org(admin_engine, "dl-review")
    await _device(admin_engine, org_id, hostname="ok-1", agent_status="online")
    await _device(
        admin_engine, org_id, hostname="new-1", agent_status="online",
        review_state="pending", link_joined_from="10.50.0.9", link_joined_at=NOW,
    )
    await _device(
        admin_engine, org_id, hostname="gone-1", agent_status="online",
        review_state="rejected", rejected_at=NOW,
    )
    async with AsyncSession(admin_engine) as session:
        everything = await device_page(session, org_id, now=NOW)
        review = await device_page(session, org_id, state="review", now=NOW)

    assert sorted(d.hostname or "" for d, *_ in everything.rows) == ["new-1", "ok-1"]
    assert (everything.state_counts.all, everything.state_counts.review) == (2, 1)
    assert (everything.fleet.total, everything.fleet.managed, everything.fleet.online) == (2, 2, 2)
    assert [d.hostname for d, *_ in review.rows] == ["new-1"]
    assert review.total == review.state_counts.of("review") == 1


async def test_the_route_says_how_and_from_where_a_pending_row_joined(api_env) -> None:
    org_id = await api_env.create_org("dl-review-route")
    joined = datetime.now(UTC) - timedelta(minutes=5)
    async with AsyncSession(api_env.admin_engine) as session:
        session.add_all(
            [
                Device(org_id=org_id, hostname="new-1", review_state="pending",
                       link_joined_from="10.50.0.9", link_joined_at=joined,
                       client_id=org_id, site_id=org_id),
                Device(org_id=org_id, hostname="old-1", client_id=org_id, site_id=org_id),
                Device(org_id=org_id, hostname="gone-1", review_state="rejected",
                       rejected_at=joined, client_id=org_id, site_id=org_id),
            ]
        )
        await session.commit()
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))

    body = (await api_env.client.get("/api/v1/devices?state=review", headers=headers)).json()
    assert body["state_counts"]["review"] == 1
    assert body["state_counts"]["all"] == body["fleet"]["total"] == 2
    [item] = body["items"]
    assert item["review_state"] == "pending"
    assert item["link_joined_from"] == "10.50.0.9"
    assert item["link_joined_at"] is not None

    plain = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    by_name = {i["hostname"]: i for i in plain["items"]}
    assert set(by_name) == {"new-1", "old-1"}
    assert by_name["old-1"]["review_state"] == "approved"
    assert by_name["old-1"]["link_joined_from"] is None


async def test_a_rejected_row_cannot_be_read_or_edited_by_id(api_env) -> None:
    """Plan D1: the tombstone is excluded EVERYWHERE, not only from the list.
    Reading it by id or editing it answers exactly as a row that does not
    exist -- and an approved row beside it still answers, so the 404 is about
    the review state and nothing else."""
    org_id = await api_env.create_org("dl-review-byid")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        gone = Device(
            org_id=org_id,
            hostname="gone-1",
            review_state="rejected",
            rejected_at=NOW,
            client_id=org_id,
            site_id=org_id,
        )
        kept = Device(org_id=org_id, hostname="kept-1", client_id=org_id, site_id=org_id)
        session.add_all([gone, kept])
        await session.commit()
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))

    kept_read = await api_env.client.get(f"/api/v1/devices/{kept.id}", headers=headers)
    assert kept_read.status_code == 200
    got = await api_env.client.get(f"/api/v1/devices/{gone.id}", headers=headers)
    assert got.status_code == 404 and got.json() == {"detail": "no such device"}

    edit = {"description": "should not land"}
    ok = await api_env.client.patch(f"/api/v1/devices/{kept.id}", json=edit, headers=headers)
    assert ok.status_code == 200, ok.text
    refused = await api_env.client.patch(f"/api/v1/devices/{gone.id}", json=edit, headers=headers)
    assert refused.status_code == 404 and refused.json() == {"detail": "no such device"}
    async with AsyncSession(api_env.admin_engine) as session:
        row = await session.get(Device, gone.id)
    assert row is not None and row.description is None


# --------------------------------------------------------------------------- #
# A rejection that left the agent behind (install-link follow-up)
# --------------------------------------------------------------------------- #

#: The outcomes after which the agent may still be on the computer.
LEFT_BEHIND = ("timeout", "agent_did_not_confirm", "agent_cannot_uninstall")


async def _tombstone(
    admin_engine: AsyncEngine,
    org_id: UUID,
    hostname: str,
    outcome: str | None,
    *,
    acknowledged: bool = False,
) -> UUID:
    """A rejected row shaped the way completion leaves it: decommissioned,
    agent OFFLINE and certificate REVOKED -- so a row that leaked out of the
    review tab would also be counted by `offline` and `certs`."""
    return await _device(
        admin_engine,
        org_id,
        hostname=hostname,
        agent_status="offline",
        cert_revoked=True,
        review_state="rejected",
        rejected_at=NOW - timedelta(days=1),
        status="decommissioned" if outcome is not None else "active",
        rejection_outcome=outcome,
        removal_acknowledged_at=NOW if acknowledged else None,
    )


async def test_a_rejection_that_left_the_agent_behind_waits_in_review_and_nowhere_else(
    admin_engine: AsyncEngine,
) -> None:
    """Rejected rows differing ONLY in how the rejection ended (and whether a
    person has since said they removed the agent). *If the review tab ignored
    the outcome*, `waiting` and `confirmed` would show beside the three the
    technician has to act on. *If the acknowledgement were ignored*, the
    `done-*` rows would come back. *If the leak reached the other tabs*,
    `all`, `offline` and `certs` would count machines a technician already
    threw out."""
    org_id = await _org(admin_engine, "dl-left")
    await _device(admin_engine, org_id, hostname="ok-1", agent_status="online")
    await _device(
        admin_engine, org_id, hostname="new-1", agent_status="online",
        review_state="pending", link_joined_from="10.50.0.9", link_joined_at=NOW,
    )
    await _tombstone(admin_engine, org_id, "waiting", None)
    await _tombstone(admin_engine, org_id, "confirmed", "agent_confirmed")
    for outcome in LEFT_BEHIND:
        await _tombstone(admin_engine, org_id, f"left-{outcome}", outcome)
        await _tombstone(admin_engine, org_id, f"done-{outcome}", outcome, acknowledged=True)

    async with AsyncSession(admin_engine) as session:
        everything = await device_page(session, org_id, now=NOW)
        review = await device_page(session, org_id, state="review", now=NOW)
        tabs = {
            state: await device_page(session, org_id, state=state, now=NOW)
            for state in ("online", "offline", "assets", "certs")
        }

    assert sorted(d.hostname or "" for d, *_ in review.rows) == sorted(
        ["new-1", *(f"left-{o}" for o in LEFT_BEHIND)]
    )
    assert review.total == review.state_counts.review == 4
    by_name = {d.hostname: d for d, *_ in review.rows}
    for outcome in LEFT_BEHIND:
        assert by_name[f"left-{outcome}"].rejection_outcome == outcome

    # Nowhere else: every other tab, every count and every tile is unchanged.
    assert sorted(d.hostname or "" for d, *_ in everything.rows) == ["new-1", "ok-1"]
    counts = everything.state_counts
    assert (counts.all, counts.online, counts.offline, counts.assets, counts.certs) == (
        2, 2, 0, 0, 0
    )
    assert counts.review == 4
    for state, page in tabs.items():
        assert all(d.review_state != "rejected" for d, *_ in page.rows), state
    assert (everything.fleet.total, everything.fleet.managed, everything.fleet.online) == (2, 2, 2)
    assert everything.fleet.cert_attention == 0


async def test_the_search_narrows_the_left_behind_rows_like_any_other(
    admin_engine: AsyncEngine,
) -> None:
    org_id = await _org(admin_engine, "dl-left-q")
    await _tombstone(admin_engine, org_id, "lab-pc-07", "timeout")
    await _tombstone(admin_engine, org_id, "desk-02", "timeout")
    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, state="review", q="lab", now=NOW)
    assert [d.hostname for d, *_ in page.rows] == ["lab-pc-07"]
    assert page.state_counts.review == 1


async def test_the_route_serves_the_outcome_and_keeps_the_row_404_by_id(api_env) -> None:
    org_id = await api_env.create_org("dl-left-route")
    left = await _tombstone(api_env.admin_engine, org_id, "left-1", "agent_did_not_confirm")
    await _device(api_env.admin_engine, org_id, hostname="old-1")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))

    body = (await api_env.client.get("/api/v1/devices?state=review", headers=headers)).json()
    [item] = body["items"]
    assert item["id"] == str(left)
    assert item["review_state"] == "rejected"
    assert item["rejection_outcome"] == "agent_did_not_confirm"
    assert body["state_counts"]["review"] == body["total"] == 1
    assert body["state_counts"]["all"] == body["fleet"]["total"] == 1

    plain = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    assert [i["hostname"] for i in plain["items"]] == ["old-1"]
    assert plain["items"][0]["rejection_outcome"] is None

    # Listed for the one thing left to do, and still not a device.
    got = await api_env.client.get(f"/api/v1/devices/{left}", headers=headers)
    assert got.status_code == 404
    edit = await api_env.client.patch(
        f"/api/v1/devices/{left}", json={"description": "x"}, headers=headers
    )
    assert edit.status_code == 404


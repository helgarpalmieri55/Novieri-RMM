"""`sla-attainment` (spec §5.4, §0.3, §2, §6.4, §12.5, §12.10, §12.11).

**THIS IS THE ONE v1 REPORT THAT DOES NOT REQUIRE `devices.read`**, and the file
asserts that rather than assuming it: a suite that only ever exercised
`devices.read` would miss a whole class of conjunction bug, and the four
`requires` sets are only genuinely distinct because of this one.

**THREE PROPERTIES ARE PINNED HERE THAT NOTHING ELSE IN THE SUITE CAN SEE:**

1. **A breach outranks a met on the same clock.** They genuinely co-occur -- a
   reply that lands after the deadline stamps `met_at` on a row whose
   `breached_at` is already set -- so the seeded row carries BOTH, and dropping
   the `breached_at IS NULL` term from the met expression moves the count from
   `breached` to `met`. That is metric laundering, and it is one deleted
   conjunct away.
2. **A ticket with no `ticket_sla` row stays in the denominator.** SLA spec §3's
   legitimate state. Making the join inner shrinks `tickets` to the promised
   subset, which makes every attainment percentage look BETTER for an org whose
   SLA configuration is broken.
3. **`meta.reproducible` is `False`.** The only v1 report where it is, which is
   why `fleet-posture` asserts the opposite: a `reproducible` hardcoded either
   way is caught only by having both cases in the suite.

**TWO ORGS WITH QUEUES OF THE SAME NAME AND TICKETS IN THE SAME WINDOW.** A queue
named differently per org makes the org predicate unobservable -- the other org's
rows would not have matched anyway. And the application predicate is pinned on
the ADMIN engine, where RLS is not the thing doing the rejecting.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.tenancy import admin_session, org_session
from app.modules.reporting.definitions.sla_attainment import (
    _RETRIAGE,
    MAX_WINDOW_DAYS,
    SlaAttainmentParams,
)
from app.modules.reporting.registry import REGISTRY
from app.modules.reporting.service import execute_report
from app.modules.reporting.sources import tables_read
from tests.reporting_fixtures import (
    ServiceDeskAnchor,
    reporting_user,
    seed_service_desk_anchor,
    seed_ticket,
    seed_ticket_sla,
)

KEY = "sla-attainment"
CODES = ("reporting.read", "ticket.read", "sla.read")


def _definition() -> Any:
    return REGISTRY[KEY]


def _window(days_back: int = 30) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"from": now - timedelta(days=days_back), "to": now + timedelta(days=1)}


def _query(days_back: int = 30, window: dict[str, Any] | None = None, **extra: Any) -> str:
    """URL-ENCODED, and that is not a style choice: an ISO timestamp ends
    `+00:00`, and a bare `+` in a query string decodes to a SPACE. The first
    version of this helper interpolated the timestamp directly and every request
    came back 422 with "unexpected extra characters at the end of the input"."""
    chosen = window if window is not None else _window(days_back)
    params: dict[str, Any] = {
        "from": chosen["from"].isoformat(),
        "to": chosen["to"].isoformat(),
        **extra,
    }
    return f"?{urlencode(params)}"


async def _run(api_env: Any, org_id: UUID, **overrides: Any) -> Any:
    params = {**_window(), **overrides}
    async with org_session(org_id, engine=api_env.app_engine) as session:
        return await execute_report(
            session, _definition(), SlaAttainmentParams(**params), org_id=org_id
        )


# --------------------------------------------------------------------------- #
# The one report that does not require devices.read (§5).
# --------------------------------------------------------------------------- #


async def test_the_definition_requires_ticket_and_sla_and_not_devices() -> None:
    definition = _definition()
    assert definition.requires == frozenset({"ticket.read", "sla.read"})
    assert "devices.read" not in definition.requires
    # And the four v1 reports really do have four distinct requirement sets --
    # the property §5 claims and the reason this report is in v1 at all.
    sets = {key: frozenset(REGISTRY[key].requires) for key in REGISTRY}
    assert len(set(sets.values())) == len(sets), sets


async def test_the_retriage_literal_is_the_sla_modules_own() -> None:
    """`breached_after_retriage` is SLA spec §0.3's mitigation, finally rendered,
    and nothing else in this codebase reads `policy_source`.

    A typo here would make the column read a permanent zero, which looks exactly
    like "nobody is gaming the clock" -- the most convincing possible wrong
    number. So the literal is pinned against the SLA module's own tuple, and
    against the DB constraint that bounds the column.
    """
    from app.models import Base
    from app.modules.sla.tracking import _WRITABLE_POLICY_SOURCES, POLICY_SOURCES

    assert _RETRIAGE in POLICY_SOURCES
    assert _RETRIAGE in _WRITABLE_POLICY_SOURCES
    constraints = " ".join(
        str(getattr(c, "sqltext", "")) for c in Base.metadata.tables["ticket_sla"].constraints
    )
    assert _RETRIAGE in constraints, constraints


# --------------------------------------------------------------------------- #
# §5.4, §0.8: no ticket bodies, no subjects, no requester identity.
# --------------------------------------------------------------------------- #


async def test_no_row_field_or_source_can_carry_ticket_content() -> None:
    """Structural. §5.4 says the report is counts by queue and priority; a
    manager who wants the tickets follows `GET /api/v1/tickets`, gated as it
    already is."""
    definition = _definition()
    forbidden = {"subject", "description", "body", "requester_id", "requester_email"}
    assert not (set(definition.row_model.model_fields) & forbidden)
    assert not (set(definition.export_columns) & forbidden)
    assert "ticket_comment" not in definition.sources

    params = SlaAttainmentParams(**_window())
    read = tables_read(definition.build(org_id=UUID(int=0), params=params))
    read |= tables_read(definition.build_totals(org_id=UUID(int=0), params=params))
    assert "ticket_comment" not in read
    assert read <= definition.sources


async def test_a_real_response_carries_no_ticket_text(api_env: Any) -> None:
    """The structural test above cannot see a value that leaked through a label.
    The seeded ticket's subject is a recognisable string; it appears nowhere in
    the response, and the response is non-empty."""
    org_id = await api_env.create_org("rep-sla-text")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await seed_ticket(api_env.admin_engine, org_id, anchor)

    _, sub = await reporting_user(api_env, org_id, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}{_query()}", headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"], "an empty response would pass the assertion below"
    assert "never appears in a report" not in response.text


# --------------------------------------------------------------------------- #
# The precedence: breached outranks met, and void outranks everything.
# --------------------------------------------------------------------------- #


async def _one_of_each(
    api_env: Any, org_id: UUID, anchor: ServiceDeskAnchor
) -> dict[str, UUID]:
    """Six tickets in one queue at one priority, one per outcome. They differ
    ONLY in their `ticket_sla` row (or its absence), so nothing but the
    precedence can produce the counts."""
    made: dict[str, UUID] = {}

    made["met"] = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, made["met"],
        response_met=True, resolution_met=True,
    )

    # BOTH STAMPS. `views.target_state` resolves this in favour of `breached`,
    # and so must this report.
    made["late_reply"] = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, made["late_reply"],
        response_met=True, response_breached=True,
        resolution_met=True, resolution_breached=True,
    )

    made["void"] = await seed_ticket(api_env.admin_engine, org_id, anchor, status="cancelled")
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, made["void"],
        response_breached=True, resolution_breached=True, voided=True,
    )

    made["running"] = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(api_env.admin_engine, org_id, anchor, made["running"])

    made["retriage_breach"] = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, made["retriage_breach"],
        policy_source=_RETRIAGE, resolution_breached=True,
    )

    # NO `ticket_sla` ROW AT ALL -- SLA spec §3's legitimate state.
    made["unpromised"] = await seed_ticket(api_env.admin_engine, org_id, anchor)
    return made


async def test_a_breach_outranks_a_met_on_the_same_clock(api_env: Any) -> None:
    """The laundering guard. Falsify by dropping `*_breached_at IS NULL` from
    the met expressions: `response_met` reads 2 instead of 1 and
    `response_attainment_pct` climbs from 50.0 to 66.67 -- a different number,
    in the direction that flatters."""
    org_id = await api_env.create_org("rep-sla-precedence")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await _one_of_each(api_env, org_id, anchor)

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.tickets == 6
    assert row.response_met == 1, "the late reply is breached, not met"
    assert row.response_breached == 1, "the late reply, which is ALSO met"
    assert row.response_attainment_pct == 50.0
    assert row.resolution_met == 1
    assert row.resolution_breached == 2, "the late reply and the retriage breach"
    assert row.void == 1
    assert row.still_running == 1
    assert row.breached_after_retriage == 1


async def test_a_void_ticket_is_in_no_other_bucket_and_in_neither_percentage(
    api_env: Any,
) -> None:
    """A cancelled ticket's promise is neither kept nor broken. The seeded void
    row carries BOTH breach stamps, so a report that ignored `voided_at` would
    count it as breached and attainment would drop -- cancelling a ticket must
    not be able to move the number in either direction."""
    org_id = await api_env.create_org("rep-sla-void")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)

    kept = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, kept, response_met=True, resolution_met=True
    )
    cancelled = await seed_ticket(
        api_env.admin_engine, org_id, anchor, status="cancelled"
    )
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, cancelled,
        response_breached=True, resolution_breached=True, voided=True,
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.tickets == 2
    assert row.void == 1
    assert row.response_breached == 0, "the void ticket's breach stamps must not count"
    assert row.resolution_breached == 0
    assert row.response_met == 1
    assert row.still_running == 0
    assert row.response_attainment_pct == 100.0, "100%, not 50% -- the void is excluded"


async def test_a_ticket_with_no_sla_row_stays_in_the_denominator(api_env: Any) -> None:
    """SLA spec §3: "no policy matched -> no `ticket_sla` row", and a broken
    calendar degrades to the same state.

    Falsify by making the `ticket_sla` join inner: `tickets` reads 1 instead of
    2 -- so an org whose SLA configuration is broken would show a SMALLER
    denominator and a BETTER-looking report, which is the exact inversion §2
    forbids.
    """
    org_id = await api_env.create_org("rep-sla-unpromised")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    promised = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, promised,
        response_met=True, resolution_met=True,
    )
    await seed_ticket(api_env.admin_engine, org_id, anchor)

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.tickets == 2, "the unpromised ticket is still a ticket"
    assert row.response_met == 1
    assert row.response_breached == 0
    assert row.void == 0
    assert row.still_running == 0, "no promise is not a running promise"
    # Deliberately NOT the sum of the columns beside it, and the field
    # description says so.
    assert row.tickets != row.response_met + row.response_breached + row.void


# --------------------------------------------------------------------------- #
# `breached_after_retriage` -- SLA spec §0.3's mitigation, finally rendered.
# --------------------------------------------------------------------------- #


async def test_breached_after_retriage_counts_only_retriaged_breaches(
    api_env: Any,
) -> None:
    """THREE tickets, each differing from the next in exactly one of the two
    conjuncts: an `initial` breach, a `retriage` breach, and a `retriage` that
    was MET. Only the middle one counts.

    A test with one ticket could not tell `policy_source == 'retriage'` from
    `TRUE`, nor the breach term from `TRUE`; with these three, dropping either
    conjunct reads 2 rather than 1.
    """
    org_id = await api_env.create_org("rep-sla-retriage")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)

    initial_breach = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, initial_breach,
        policy_source="initial", resolution_breached=True,
    )
    retriage_breach = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, retriage_breach,
        policy_source=_RETRIAGE, resolution_breached=True,
    )
    retriage_met = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, retriage_met,
        policy_source=_RETRIAGE, response_met=True, resolution_met=True,
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.resolution_breached == 2
    assert row.breached_after_retriage == 1
    assert page.totals["breached_after_retriage"] == 1


# --------------------------------------------------------------------------- #
# The percentages.
# --------------------------------------------------------------------------- #


async def test_attainment_is_null_rather_than_zero_when_nothing_has_stopped(
    api_env: Any,
) -> None:
    """A queue with three live tickets has not achieved 0% attainment. A
    dashboard rendering 0% for it is worse than one rendering nothing --
    `nullif` is what makes the difference, and this is the only test that can
    see it."""
    org_id = await api_env.create_org("rep-sla-null")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    for _ in range(3):
        ticket_id = await seed_ticket(api_env.admin_engine, org_id, anchor)
        await seed_ticket_sla(api_env.admin_engine, org_id, anchor, ticket_id)

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.tickets == 3
    assert row.still_running == 3
    assert row.response_attainment_pct is None
    assert row.resolution_attainment_pct is None


async def test_attainment_is_the_decided_fraction_and_comes_from_the_server(
    api_env: Any,
) -> None:
    """One met, two breached, one still running: 33.33%, not 25% -- the running
    ticket is not in the denominator. §11 forbids the browser recomputing this,
    so the value on the wire is the one that has to be right."""
    org_id = await api_env.create_org("rep-sla-pct")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    met = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(
        api_env.admin_engine, org_id, anchor, met, response_met=True, resolution_met=True
    )
    for _ in range(2):
        breached = await seed_ticket(api_env.admin_engine, org_id, anchor)
        await seed_ticket_sla(
            api_env.admin_engine, org_id, anchor, breached,
            response_breached=True, resolution_breached=True,
        )
    running = await seed_ticket(api_env.admin_engine, org_id, anchor)
    await seed_ticket_sla(api_env.admin_engine, org_id, anchor, running)

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.tickets == 4
    assert row.still_running == 1
    assert row.response_attainment_pct == 33.33
    assert row.resolution_attainment_pct == 33.33


# --------------------------------------------------------------------------- #
# Grouping, the window, and the params model.
# --------------------------------------------------------------------------- #


async def test_rows_are_one_per_queue_and_priority(api_env: Any) -> None:
    """Two queues x two priorities = four rows, and the counts differ per cell,
    so a `GROUP BY` that dropped either key would collapse them into a
    different number rather than the same one."""
    org_id = await api_env.create_org("rep-sla-group")
    first = await seed_service_desk_anchor(api_env.admin_engine, org_id, queue_name="Alpha")
    second = await seed_service_desk_anchor(api_env.admin_engine, org_id, queue_name="Beta")
    plan = {
        (first, "high"): 1,
        (first, "low"): 2,
        (second, "high"): 3,
        (second, "low"): 4,
    }
    for (anchor, priority), count in plan.items():
        for _ in range(count):
            await seed_ticket(api_env.admin_engine, org_id, anchor, priority=priority)

    page = await _run(api_env, org_id)
    assert len(page.rows) == 4
    by_cell = {(row.queue_name, row.priority): row.tickets for row in page.rows}
    assert by_cell == {
        ("Alpha", "high"): 1,
        ("Alpha", "low"): 2,
        ("Beta", "high"): 3,
        ("Beta", "low"): 4,
    }
    assert page.totals["tickets"] == 10
    assert page.totals["queue_priority_pairs"] == 4
    # Ordered by (queue_name, priority, queue_id).
    assert [(r.queue_name, r.priority) for r in page.rows] == [
        ("Alpha", "high"),
        ("Alpha", "low"),
        ("Beta", "high"),
        ("Beta", "low"),
    ]


async def test_the_window_is_closed_at_both_ends(api_env: Any) -> None:
    """A ticket created before `from` and one created after `to` are both
    excluded, and the SAME data reads differently at two window widths -- so the
    window is observably a predicate rather than decoration."""
    org_id = await api_env.create_org("rep-sla-window")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await seed_ticket(api_env.admin_engine, org_id, anchor, created_days_ago=2)
    await seed_ticket(api_env.admin_engine, org_id, anchor, created_days_ago=200)

    recent = await _run(api_env, org_id, **_window(30))
    assert recent.rows[0].tickets == 1
    assert recent.totals["tickets"] == 1

    everything = await _run(api_env, org_id, **_window(365))
    assert everything.rows[0].tickets == 2

    now = datetime.now(UTC)
    ancient = await _run(
        api_env, org_id, **{"from": now - timedelta(days=400), "to": now - timedelta(days=300)}
    )
    assert ancient.rows == [], "nothing was created in that window"
    assert ancient.totals["tickets"] == 0


async def test_the_window_is_required_and_bounded(api_env: Any) -> None:
    """§5.4: `from`/`to` required, max window 366 days, and a 422 rather than a
    silent clamp -- a report that quietly narrowed a three-year request would
    answer a different question under the label of the one it was asked."""
    org_id = await api_env.create_org("rep-sla-params")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await seed_ticket(api_env.admin_engine, org_id, anchor)
    _, sub = await reporting_user(api_env, org_id, *CODES)
    headers = await api_env.headers(org_id, sub)

    missing = await api_env.client.get(f"/api/v1/reports/{KEY}", headers=headers)
    assert missing.status_code == 422, missing.text
    assert "from" in missing.text and "to" in missing.text

    now = datetime.now(UTC)
    too_wide_query = urlencode(
        {
            "from": (now - timedelta(days=MAX_WINDOW_DAYS + 1)).isoformat(),
            "to": now.isoformat(),
        }
    )
    too_wide = await api_env.client.get(
        f"/api/v1/reports/{KEY}?{too_wide_query}", headers=headers
    )
    assert too_wide.status_code == 422, too_wide.text
    assert str(MAX_WINDOW_DAYS) in too_wide.text

    backwards_query = urlencode(
        {"from": now.isoformat(), "to": (now - timedelta(days=1)).isoformat()}
    )
    backwards = await api_env.client.get(
        f"/api/v1/reports/{KEY}?{backwards_query}", headers=headers
    )
    assert backwards.status_code == 422, backwards.text

    at_the_limit_query = urlencode(
        {"from": (now - timedelta(days=MAX_WINDOW_DAYS)).isoformat(), "to": now.isoformat()}
    )
    at_the_limit = await api_env.client.get(
        f"/api/v1/reports/{KEY}?{at_the_limit_query}", headers=headers
    )
    assert at_the_limit.status_code == 200, at_the_limit.text
    assert at_the_limit.json()["rows"], "the boundary must be inclusive AND non-vacuous"


async def test_the_queue_and_priority_filters_narrow_the_match_and_the_totals(
    api_env: Any,
) -> None:
    org_id = await api_env.create_org("rep-sla-filters")
    alpha = await seed_service_desk_anchor(api_env.admin_engine, org_id, queue_name="Alpha")
    beta = await seed_service_desk_anchor(api_env.admin_engine, org_id, queue_name="Beta")
    await seed_ticket(api_env.admin_engine, org_id, alpha, priority="high")
    await seed_ticket(api_env.admin_engine, org_id, alpha, priority="low")
    await seed_ticket(api_env.admin_engine, org_id, beta, priority="high")

    everything = await _run(api_env, org_id)
    assert everything.totals["tickets"] == 3

    one_queue = await _run(api_env, org_id, queue_id=alpha.queue_id)
    assert one_queue.totals["tickets"] == 2
    assert {row.queue_name for row in one_queue.rows} == {"Alpha"}

    one_priority = await _run(api_env, org_id, priority="high")
    assert one_priority.totals["tickets"] == 2
    assert {row.priority for row in one_priority.rows} == {"high"}


# --------------------------------------------------------------------------- #
# Tenancy (§12.5, Global Constraint 4 and 5).
# --------------------------------------------------------------------------- #


async def _seed_two_overlapping_orgs(
    api_env: Any,
) -> tuple[UUID, UUID, set[UUID], set[UUID]]:
    """Org A and org B with QUEUES OF THE SAME NAME, tickets at the same
    priority created in the same window, and SLA rows in the same state. A queue
    named differently per org would make the org predicate unobservable."""
    org_a = await api_env.create_org("rep-sla-a")
    org_b = await api_env.create_org("rep-sla-b")
    queues_a: set[UUID] = set()
    queues_b: set[UUID] = set()
    for org_id, queues in ((org_a, queues_a), (org_b, queues_b)):
        for queue_name in ("Shared Queue", "Second Queue"):
            anchor = await seed_service_desk_anchor(
                api_env.admin_engine, org_id, queue_name=queue_name
            )
            queues.add(anchor.queue_id)
            ticket_id = await seed_ticket(api_env.admin_engine, org_id, anchor)
            await seed_ticket_sla(
                api_env.admin_engine, org_id, anchor, ticket_id,
                response_met=True, resolution_met=True,
            )
    return org_a, org_b, queues_a, queues_b


async def test_a_report_returns_only_the_callers_org(api_env: Any) -> None:
    """§12.5, through the API, exactly as a request runs."""
    org_a, _org_b, queues_a, queues_b = await _seed_two_overlapping_orgs(api_env)
    _, sub = await reporting_user(api_env, org_a, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}{_query()}", headers=await api_env.headers(org_a, sub)
    )
    assert response.status_code == 200, response.text
    returned = {row["queue_id"] for row in response.json()["rows"]}
    assert returned == {str(q) for q in queues_a}
    assert not (returned & {str(q) for q in queues_b})
    assert response.json()["totals"]["tickets"] == 2


async def test_the_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """GLOBAL CONSTRAINT 4. `admin_session` connects as the RLS-BYPASSING role,
    so this is where the application predicates are visible at all.

    MEASURED, AND THE OBVIOUS CLAIM WAS WRONG: deleting `Ticket.org_id == org_id`
    leaves this test GREEN, because `TicketQueue.org_id == org_id` in the join
    condition already restricts the whole statement to this org's queues. The two
    predicates are mutually redundant on ordinary data, so neither is observable
    by seeding two well-formed orgs -- which is exactly the fixture blindness
    this project keeps shipping, wearing a third costume.
    `test_a_cross_org_row_pointed_at_this_orgs_queue_is_still_excluded` below is
    what makes each of them individually observable; this one pins the pair.
    """
    org_a, _org_b, queues_a, queues_b = await _seed_two_overlapping_orgs(api_env)

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), SlaAttainmentParams(**_window()), org_id=org_a
        )

    returned = {row.queue_id for row in page.rows}
    assert returned == queues_a, returned
    assert not (returned & queues_b)
    assert page.meta.total == 2
    assert page.totals["tickets"] == 2
    assert {row.tickets for row in page.rows} == {1}


async def test_a_cross_org_row_pointed_at_this_orgs_queue_is_still_excluded(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """THE TEST THAT MAKES EACH ORG PREDICATE INDIVIDUALLY OBSERVABLE.

    `ticket.queue_id` and `ticket_sla.ticket_id` are plain foreign keys with no
    cross-org check, so both of these rows are storable today:

    * a ticket in org B whose `queue_id` is one of org A's queues. Without
      `Ticket.org_id == org_id` it satisfies the queue join and is counted in org
      A's report -- another tenant's ticket in this tenant's attainment figure.
    * a `ticket_sla` row in org B attached to one of org A's tickets. Without
      `TicketSla.org_id == org_id` org A's unpromised ticket picks it up and
      reads as MET.

    Both are run on the admin engine, where RLS is not the thing doing the
    rejecting. Falsified during the build: deleting either predicate turns this
    red (2 tickets instead of 1; response_met 1 instead of 0) while every other
    tenancy test in this file stays green.
    """
    org_a = await api_env.create_org("rep-sla-cross-a")
    org_b = await api_env.create_org("rep-sla-cross-b")
    anchor_a = await seed_service_desk_anchor(
        api_env.admin_engine, org_a, queue_name="Shared Queue"
    )
    anchor_b = await seed_service_desk_anchor(
        api_env.admin_engine, org_b, queue_name="Shared Queue"
    )

    # Org A's own ticket, deliberately UNPROMISED so an org B SLA row would be
    # visible as a change of outcome rather than lost in a crowd.
    unpromised = await seed_ticket(api_env.admin_engine, org_a, anchor_a)
    # Org B's ticket, pointed at ORG A's queue.
    await seed_ticket(
        api_env.admin_engine, org_b, anchor_b, queue_id=anchor_a.queue_id
    )
    # Org B's SLA row, attached to ORG A's ticket.
    await seed_ticket_sla(
        api_env.admin_engine, org_b, anchor_b, unpromised,
        response_met=True, resolution_met=True,
    )

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), SlaAttainmentParams(**_window()), org_id=org_a
        )

    (row,) = page.rows
    assert row.queue_id == anchor_a.queue_id
    assert row.tickets == 1, "org B's ticket must not be counted through org A's queue"
    assert row.response_met == 0, "org B's SLA row must not decide org A's ticket"
    assert row.response_attainment_pct is None
    assert page.totals["tickets"] == 1


# --------------------------------------------------------------------------- #
# The envelope (§6.3, §12.10, §12.11).
# --------------------------------------------------------------------------- #


async def test_totals_are_computed_over_the_whole_match_not_the_page(
    api_env: Any,
) -> None:
    """§12.10. Three queues holding 1, 2 and 3 tickets, so a `totals` computed
    over the page produces a DIFFERENT number on each page rather than the same
    one by coincidence."""
    org_id = await api_env.create_org("rep-sla-totals")
    for index, queue_name in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        anchor = await seed_service_desk_anchor(
            api_env.admin_engine, org_id, queue_name=queue_name
        )
        for _ in range(index):
            ticket_id = await seed_ticket(api_env.admin_engine, org_id, anchor)
            await seed_ticket_sla(
                api_env.admin_engine, org_id, anchor, ticket_id, response_met=True
            )

    first = await _run(api_env, org_id, limit=1, offset=0)
    last = await _run(api_env, org_id, limit=1, offset=2)
    assert first.totals["tickets"] == 6, first.totals
    assert last.totals["tickets"] == 6, last.totals
    assert first.totals["queue_priority_pairs"] == 3
    assert first.meta.total == 3 and last.meta.total == 3
    # The premise: the two pages really do hold different rows.
    assert first.rows[0].tickets == 1
    assert last.rows[0].tickets == 3


async def test_ordering_is_stable_across_a_tie_on_the_sort_key(api_env: Any) -> None:
    """§12.11. `ticket_queue` is UNIQUE on `(org_id, key)` and NOT on `name`, so
    four queues in one org can share a display name -- and at one priority the
    sort key ties four ways. Without the `queue_id` tiebreaker PostgreSQL may
    return them in any order per statement, so paging by offset would skip one
    and repeat another."""
    org_id = await api_env.create_org("rep-sla-tie")
    expected: set[UUID] = set()
    for _ in range(4):
        anchor = await seed_service_desk_anchor(
            api_env.admin_engine, org_id, queue_name="identical"
        )
        expected.add(anchor.queue_id)
        await seed_ticket(api_env.admin_engine, org_id, anchor, priority="normal")

    seen: list[UUID] = []
    for offset in range(4):
        page = await _run(api_env, org_id, limit=1, offset=offset)
        assert len(page.rows) == 1, page.rows
        seen.append(page.rows[0].queue_id)

    assert len(seen) == len(set(seen)), seen
    assert set(seen) == expected


async def test_the_meta_envelope_reports_the_window_and_is_not_reproducible(
    api_env: Any,
) -> None:
    """`reproducible` is `False` HERE and `True` for `fleet-posture`, and both
    values are asserted on real responses -- which is the only thing that
    catches a `reproducible` hardcoded either way.

    `window_from`/`window_to` are populated too: they are derived by
    `execute_report` from the params object's attribute names, so a report that
    renamed them would silently return a null window.
    """
    org_id = await api_env.create_org("rep-sla-meta")
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await seed_ticket(api_env.admin_engine, org_id, anchor)
    _, sub = await reporting_user(api_env, org_id, *CODES)

    window = _window()
    response = await api_env.client.get(
        # THE SAME window object the assertions below compare against -- a
        # second call to `_window()` differs by microseconds, and the equality
        # this test needs is exact.
        f"/api/v1/reports/{KEY}{_query(window=window)}",
        headers=await api_env.headers(org_id, sub),
    )
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]

    assert meta["report_key"] == KEY
    assert meta["reproducible"] is False
    assert meta["window_from"] is not None
    assert meta["window_to"] is not None
    assert datetime.fromisoformat(meta["window_from"]) == window["from"]
    assert datetime.fromisoformat(meta["window_to"]) == window["to"]

    modules = {source["module"]: source for source in meta["sources"]}
    assert set(modules) == {"service_desk", "sla"}
    assert modules["sla"]["permission"] == "sla.read"
    assert "MUTABLE" in modules["sla"]["recency"]


async def test_the_row_model_carries_a_description_on_every_field() -> None:
    """Property 5 as written, not through the class-docstring fallback."""
    undocumented = [
        name
        for name, field in _definition().row_model.model_fields.items()
        if not (field.description or "").strip()
    ]
    assert not undocumented, undocumented

"""The SLA admin API: routes, RBAC completeness, tenancy, the derived reads,
and the recompute wiring (docs/modules/sla/spec.md §9, §9.1, §7; Task 11).

ROUTE DISCOVERY IS FROM THE APP, NOT FROM THE MODULE ROUTER. `FastAPI.routes`
IS `self.router.routes` (the same list object) in FastAPI 0.139.2, and
`include_router` appends a single lazy `_IncludedRouter` wrapper whose own
`.path` is `None` -- so filtering EITHER collection by a path prefix yields an
empty list forever, and every RBAC assertion built on it passes over nothing.
`_flatten` below descends through `_IncludedRouter.original_router` and
re-applies `include_context.prefix`, which is the working flattener
`tests/test_email_intake_api.py` established. `EXPECTED_ROUTES` pins the EXACT
set, not a `>=` count: a `>=` assertion cannot fail when a route goes missing.

---

THE PARAMETER EVERY FIXTURE IN THIS FILE DELIBERATELY VARIES. Every task in
this sub-project has shipped at least one test that proved nothing, always the
same way: a fixture held constant the parameter a defect would vary, so the
guarded and unguarded implementations produced identical observable values.
The candidates for THIS task are named, and each has a fixture that varies it:

* **Which permission the caller holds.** `_holder` mints a user with EXACTLY
  the codes named, so the read/admin split and the `ticket.read` exception are
  proved by a caller who holds one and not the others -- never by an
  org-admin who holds everything, against whom every gate looks identical.
  The ticket-view test is the sharp one: a caller with `ticket.read` and
  NEITHER `sla.read` nor `sla.admin` must get 200 on the ticket's SLA and 403
  on the calendars list, IN THE SAME TEST. With an org-admin fixture, a route
  wrongly gated on `sla.read` is indistinguishable.
* **Whether the target row belongs to another org.** Every 404 test seeds the
  row in a DIFFERENT org and asserts, on the admin engine, that it really
  exists -- otherwise "404 because it is not yours" and "404 because there is
  nothing there" are the same test, and the second one passes against an
  implementation with no tenancy predicate at all.
* **Whether a policy resolves at all.** The `tickets_without_sla` and
  `has_sla` fixtures put a tracked ticket BESIDE an untracked one in the same
  org, so "the count is 0" and "everything reports no SLA" both fail.
* **Whether the edited calendar/policy actually affects an open ticket.** The
  recompute tests seed a ticket whose clock is RUNNING beside one whose clock
  is STOPPED, and edit to a calendar that produces DIFFERENT instants
  (Mon-Fri 09:00-17:00 -> 09:00-13:00). An edit that produced the same
  deadlines is Task 9's cascade-guard trap verbatim: a mutant that rewrites
  frozen clocks, or one that recomputes nothing at all, would be invisible.
* **Which FIELD of the edit was changed.** A rename and an `active` toggle
  must recompute NOTHING while a timezone change and a target change must
  recompute; both halves are asserted against the same fixture, so "always
  recompute" and "never recompute" each fail.

THE CONSTANT THIS FILE'S OWN FIXTURES ALMOST FROZE: the seeded policy. The
first draft of `_recompute_policy_env` seeded one wildcard policy and one
calendar, which would have made `calendar_id` adoption untestable -- the
policy's calendar and the row's calendar were the same object, so adopting and
not adopting produce identical deadlines. `_recompute_policy_env` therefore
seeds TWO calendars whose windows differ, and the policy is repointed from one
to the other.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.rbac import route_permission

# `app.models` before any `app.modules.*` import -- see `test_sla_breach.py`'s
# note on ORM registry initialisation order.
from app.models import AuditEvent, Permission, Role, RolePermission, User, UserRole
from app.modules.service_desk.models import TicketQueue
from app.modules.sla import sweep as sweep_module
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import (
    SlaCalendar,
    SlaCalendarHoliday,
    SlaCalendarWindow,
    SlaOrgSettings,
    SlaPolicy,
    TicketSla,
)
from app.modules.sla.settings import get_auto_close_after_days, set_auto_close_after_days
from app.modules.sla.sweep import DEFAULT_AUTO_CLOSE_AFTER_DAYS
from app.modules.sla.views import (
    calendar_health_by_id,
    count_stale_ticket_sla,
    count_tickets_without_sla,
    describe_policy,
    ticket_sla_state,
)
from tests.test_sla_breach import _seed_ticket, _track
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue
from tests.test_sla_pause import _foreign_row
from tests.test_sla_sweep import _set_timezone
from tests.test_sla_tracking import LONDON, MONDAY_0900, _row, _stop_clocks

SLA_PREFIX = "/api/v1/sla"
TICKET_SLA_PATH = "/api/v1/tickets/{ticket_id}/sla"

# Spec §9's table, expanded one row per method, fully prefixed. GET/PATCH/
# DELETE share a path and each needs its own gate, so the key is (method,
# path) rather than path alone.
EXPECTED_ROUTES = {
    ("GET", f"{SLA_PREFIX}/calendars"),
    ("POST", f"{SLA_PREFIX}/calendars"),
    ("GET", f"{SLA_PREFIX}/calendars/{{calendar_id}}"),
    ("PATCH", f"{SLA_PREFIX}/calendars/{{calendar_id}}"),
    ("DELETE", f"{SLA_PREFIX}/calendars/{{calendar_id}}"),
    ("PUT", f"{SLA_PREFIX}/calendars/{{calendar_id}}/windows"),
    ("GET", f"{SLA_PREFIX}/calendars/{{calendar_id}}/holidays"),
    ("POST", f"{SLA_PREFIX}/calendars/{{calendar_id}}/holidays"),
    ("DELETE", f"{SLA_PREFIX}/calendars/{{calendar_id}}/holidays/{{holiday_id}}"),
    ("GET", f"{SLA_PREFIX}/policies"),
    ("POST", f"{SLA_PREFIX}/policies"),
    ("GET", f"{SLA_PREFIX}/policies/{{policy_id}}"),
    ("PATCH", f"{SLA_PREFIX}/policies/{{policy_id}}"),
    ("DELETE", f"{SLA_PREFIX}/policies/{{policy_id}}"),
    ("GET", f"{SLA_PREFIX}/settings"),
    ("PUT", f"{SLA_PREFIX}/settings"),
    ("GET", TICKET_SLA_PATH),
}

# Mon-Fri 09:00-17:00 and the halved week that replaces it. The two produce
# DIFFERENT instants for the same targets, which is what makes every
# recompute guard in this file testable at all.
_MON_FRI_9_TO_5 = [(d, time(9, 0), time(17, 0)) for d in range(5)]
_MON_FRI_9_TO_1 = [(d, time(9, 0), time(13, 0)) for d in range(5)]

_RESPONSE_MINUTES = 300  # 5 working hours
_RESOLUTION_MINUTES = 600  # 10 working hours

# From Mon 2026-03-02 09:00 UTC under Mon-Fri 09:00-17:00 ...
_OLD_RESPONSE_DUE = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
_OLD_RESOLUTION_DUE = datetime(2026, 3, 3, 11, 0, tzinfo=UTC)
# ... and under Mon-Fri 09:00-13:00.
_NEW_RESPONSE_DUE = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)
_NEW_RESOLUTION_DUE = datetime(2026, 3, 4, 11, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Router flattening (see the module docstring).
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper
    rather than flattening; the wrapper exposes the router it wrapped as
    `.original_router` and the prefix it was mounted under as
    `.include_context.prefix`. Descend through both, recursively.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _sla_routes(app: Any) -> list[tuple[str, Any]]:
    """Every route this module registered -- the `/sla` tree AND the
    per-ticket read, which lives outside it (spec §9). Selecting only on the
    `/sla` prefix would silently drop the one route whose permission differs
    from every other, which is the route most worth checking."""
    return [
        (path, route)
        for path, route in _flatten(app.router)
        if path.startswith(SLA_PREFIX) or path == TICKET_SLA_PATH
    ]


def _method_paths(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in _sla_routes(app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }


# --------------------------------------------------------------------------- #
# Seeding.
# --------------------------------------------------------------------------- #


async def _holder(api_env, org_id: UUID, *codes: str) -> dict[str, str]:
    """Headers for a user holding EXACTLY `codes` and nothing else.

    `api_env.create_user(org_id, "org-admin")` is deliberately NOT used for
    any gate assertion: an org-admin holds `sla.read`, `sla.admin` AND
    `ticket.read` (migration 0033 seeds the first two to super-admin/org-admin,
    and the ticket core seeds the third), so every route looks identical to
    them and a route gated on the wrong permission passes. The read/admin
    split and the `ticket.read` exception are only observable to a caller who
    holds one code and not the others.
    """
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
        session.add_all([user, role])
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


async def _admin(api_env, org_id: UUID) -> dict[str, str]:
    """A caller holding both SLA codes and `ticket.read` -- for tests whose
    subject is NOT the gate."""
    return await _holder(api_env, org_id, "sla.read", "sla.admin", "ticket.read")


async def _audit_actions(api_env, org_id: UUID) -> list[str]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _calendar_row(api_env, calendar_id: UUID) -> SlaCalendar:
    """Read on the BYPASSRLS admin engine with no org filter, so an assertion
    about what was stored cannot be satisfied by RLS hiding the truth."""
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_id))
        ).scalar_one()


async def _window_rows(api_env, calendar_id: UUID) -> list[SlaCalendarWindow]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(SlaCalendarWindow)
                    .where(SlaCalendarWindow.calendar_id == calendar_id)
                    .order_by(SlaCalendarWindow.weekday, SlaCalendarWindow.start_time)
                )
            )
            .scalars()
            .all()
        )


async def _recompute_env(api_env) -> dict[str, Any]:
    """One Mon-Fri 09:00-17:00 calendar, one 300/600-minute wildcard policy,
    and TWO tracked tickets: one with both clocks RUNNING, one with both
    clocks STOPPED (response met, resolution breached).

    Both states are present in every recompute assertion below, which is what
    makes the "only running clocks are touched" rule observable: with running
    rows alone, an implementation that rewrites met and breached deadlines
    passes; with stopped rows alone, so does one that recomputes nothing.
    """
    org_id = await api_env.create_org(f"sla-api-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI_9_TO_5)
    policy_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=_RESPONSE_MINUTES,
        resolution_minutes=_RESOLUTION_MINUTES,
    )
    running = await _seed_ticket(api_env, org_id)
    stopped = await _seed_ticket(api_env, org_id)
    for ticket in (running, stopped):
        stored = await _track(api_env, org_id, ticket)
        # The fixture is only meaningful if the ORIGINAL calendar really
        # produced these instants.
        assert stored.response_due_at == _OLD_RESPONSE_DUE
        assert stored.resolution_due_at == _OLD_RESOLUTION_DUE
    await _stop_clocks(
        api_env,
        stopped.id,
        response_met_at=MONDAY_0900 + timedelta(hours=1),
        resolution_breached_at=MONDAY_0900 + timedelta(days=2),
    )
    return {
        "org_id": org_id,
        "calendar_id": calendar_id,
        "policy_id": policy_id,
        "running": running,
        "stopped": stopped,
        "headers": await _admin(api_env, org_id),
    }


def _windows_body(windows: list[tuple[int, time, time]]) -> dict[str, Any]:
    return {
        "windows": [
            {
                "weekday": weekday,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            }
            for weekday, start, end in windows
        ]
    }


# --------------------------------------------------------------------------- #
# RBAC completeness, derived from the router (invariant #6).
# --------------------------------------------------------------------------- #


async def test_the_routes_are_mounted_under_the_versioned_prefix(api_env) -> None:
    """Pins the FLATTENER as well as the route table. If `_flatten` ever stops
    finding real routes -- a FastAPI that changes `_IncludedRouter`'s shape,
    or a `register()` that is never called -- this fails loudly instead of
    letting every RBAC assertion below pass over an empty list.

    Equality, not `>=`: a superset assertion cannot fail when a route goes
    missing, which is the failure the completeness check exists to catch.
    """
    assert _method_paths(api_env.app) == EXPECTED_ROUTES


async def test_every_route_carries_a_permission(api_env) -> None:
    """Invariant #6, DERIVED from the router rather than a hand-maintained
    list -- a route added later without `require_permission` fails here,
    whatever module added it."""
    routes = _sla_routes(api_env.app)
    # One `APIRoute` per registration, so this is `len(EXPECTED_ROUTES)`
    # exactly -- NOT a `>=`, which cannot fail when a route goes missing and
    # would silently degrade this to "at least some routes are gated".
    assert len(routes) == len(EXPECTED_ROUTES), f"found {len(routes)} SLA routes"
    for path, route in routes:
        assert route_permission(route) is not None, f"{path} has no permission"


async def test_the_completeness_check_would_fail_for_an_ungated_route(api_env) -> None:
    """THE MUTATION PROOF, in-repo rather than only in a report.

    Deleting `Depends(require_permission(...))` from one route must turn
    `test_every_route_carries_a_permission` red. That is asserted here by
    building the mutant instead of editing the shipped router: a throwaway
    route with no permission dependency, mounted at this module's own prefix
    on a throwaway app, is found by the SAME flattener and returns `None` from
    the SAME `route_permission` the completeness test asserts on.

    It also proves the inverse -- a gated route on the same throwaway app is
    NOT flagged -- so the check is not merely returning `None` for everything,
    which is exactly how this reflection fails when
    `require_permission`'s closure shape changes.
    """
    mutant = APIRouter()

    @mutant.get("/sla/ungated")
    async def _ungated() -> dict[str, str]:  # pragma: no cover -- never called
        return {}

    @mutant.get("/sla/gated")
    async def _gated(
        ctx: AuthContext = Depends(require_permission("sla.read")),  # noqa: B008
    ) -> dict[str, str]:  # pragma: no cover -- never called
        return {}

    app = FastAPI()
    app.include_router(mutant, prefix="/api/v1")

    found = {path: route_permission(route) for path, route in _sla_routes(app)}
    assert found == {
        f"{SLA_PREFIX}/ungated": None,
        f"{SLA_PREFIX}/gated": "sla.read",
    }


async def test_write_routes_require_the_admin_permission(api_env) -> None:
    """Spec §9: every mutation is `sla.admin`. `sla.admin` is high-privilege
    -- it sets what the organisation has promised its customers and can
    silence breach escalation entirely -- so a mutation that slipped to
    `sla.read` would hand that to every reader."""
    for path, route in _sla_routes(api_env.app):
        methods = getattr(route, "methods", set())
        if methods & {"POST", "PATCH", "PUT", "DELETE"}:
            assert route_permission(route) == "sla.admin", path


async def test_sla_read_routes_require_the_read_permission(api_env) -> None:
    for path, route in _sla_routes(api_env.app):
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        if methods == {"GET"} and path.startswith(SLA_PREFIX):
            assert route_permission(route) == "sla.read", path


async def test_the_per_ticket_view_is_gated_on_ticket_read(api_env) -> None:
    """Spec §9's one deliberate exception, pinned as an EQUALITY against
    `ticket.read` and an inequality against both SLA codes -- a technician
    needs to see a deadline without holding SLA administration rights, and
    both `sla.read` and `sla.admin` are seeded to admin roles only (§8)."""
    gates = {
        path: route_permission(route)
        for path, route in _sla_routes(api_env.app)
        if path == TICKET_SLA_PATH
    }
    assert gates == {TICKET_SLA_PATH: "ticket.read"}


# --------------------------------------------------------------------------- #
# RBAC, behaviourally (server-side, invariant #6).
# --------------------------------------------------------------------------- #


async def test_a_technician_sees_a_deadline_without_holding_any_sla_permission(api_env) -> None:
    """The route table's exception, proved by the caller it exists for.

    The caller holds `ticket.read` and NEITHER SLA code, so the 200 on the
    ticket's SLA and the 403 on the admin surface are asserted against the
    SAME principal in the SAME test. Against an org-admin fixture -- which
    holds all three codes -- a route wrongly gated on `sla.read` is
    indistinguishable from one gated correctly.
    """
    env = await _recompute_env(api_env)
    headers = await _holder(api_env, env["org_id"], "ticket.read")

    view = await api_env.client.get(
        f"/api/v1/tickets/{env['running'].id}/sla", headers=headers
    )
    assert view.status_code == 200, view.text
    assert view.json()["has_sla"] is True

    for path in (f"{SLA_PREFIX}/calendars", f"{SLA_PREFIX}/policies", f"{SLA_PREFIX}/settings"):
        refused = await api_env.client.get(path, headers=headers)
        assert refused.status_code == 403, path


async def test_an_sla_reader_cannot_mutate_anything(api_env) -> None:
    """Server-side, not UI-side. The caller holds `sla.read` and NOTHING
    else, so this proves the read/admin split rather than merely proving an
    unprivileged user is refused."""
    env = await _recompute_env(api_env)
    org_id, calendar_id = env["org_id"], env["calendar_id"]
    headers = await _holder(api_env, org_id, "sla.read")

    listed = await api_env.client.get(f"{SLA_PREFIX}/calendars", headers=headers)
    assert listed.status_code == 200, listed.text

    refusals = [
        await api_env.client.post(
            f"{SLA_PREFIX}/calendars",
            json={"name": "nope", "timezone": LONDON},
            headers=headers,
        ),
        await api_env.client.patch(
            f"{SLA_PREFIX}/calendars/{calendar_id}", json={"name": "nope"}, headers=headers
        ),
        await api_env.client.delete(f"{SLA_PREFIX}/calendars/{calendar_id}", headers=headers),
        await api_env.client.put(
            f"{SLA_PREFIX}/calendars/{calendar_id}/windows",
            json=_windows_body([]),
            headers=headers,
        ),
        await api_env.client.post(
            f"{SLA_PREFIX}/calendars/{calendar_id}/holidays",
            json={"holiday_date": "2026-12-25", "name": "Christmas"},
            headers=headers,
        ),
        await api_env.client.put(
            f"{SLA_PREFIX}/settings", json={"auto_close_after_days": 3}, headers=headers
        ),
    ]
    assert [r.status_code for r in refusals] == [403] * len(refusals)
    # And nothing leaked through: the calendar still has its original windows.
    assert len(await _window_rows(api_env, calendar_id)) == len(_MON_FRI_9_TO_5)


# --------------------------------------------------------------------------- #
# Tenancy: 404, never 403 (invariant #5).
# --------------------------------------------------------------------------- #


async def test_another_orgs_calendar_is_404_never_403(api_env) -> None:
    """Distinguishing "exists elsewhere" from "does not exist" is an
    org-membership oracle, so both must be 404.

    The caller holds `sla.admin` IN THEIR OWN ORG -- which is what makes this
    a tenancy test rather than a permission test -- and the foreign calendar
    is asserted to really exist on the admin engine first. Without that
    assertion a 404 could equally mean the fixture seeded nothing, and an
    implementation with no `org_id` predicate at all would pass.
    """
    mine = await api_env.create_org(f"sla-mine-{uuid4().hex[:8]}")
    theirs = await api_env.create_org(f"sla-theirs-{uuid4().hex[:8]}")
    foreign_calendar = await _seed_calendar(api_env, theirs, windows=_MON_FRI_9_TO_5)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        holiday = SlaCalendarHoliday(
            org_id=theirs,
            calendar_id=foreign_calendar,
            holiday_date=date(2026, 12, 25),
            name="Christmas",
        )
        session.add(holiday)
        await session.commit()
        foreign_holiday = holiday.id
    assert (await _calendar_row(api_env, foreign_calendar)).org_id == theirs

    headers = await _admin(api_env, mine)
    responses = {
        "get": await api_env.client.get(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}", headers=headers
        ),
        "patch": await api_env.client.patch(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}",
            json={"name": "stolen"},
            headers=headers,
        ),
        "delete": await api_env.client.delete(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}", headers=headers
        ),
        "windows": await api_env.client.put(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}/windows",
            json=_windows_body([]),
            headers=headers,
        ),
        "holidays": await api_env.client.get(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}/holidays", headers=headers
        ),
        "add_holiday": await api_env.client.post(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}/holidays",
            json={"holiday_date": "2026-01-01", "name": "New Year"},
            headers=headers,
        ),
        "remove_holiday": await api_env.client.delete(
            f"{SLA_PREFIX}/calendars/{foreign_calendar}/holidays/{foreign_holiday}",
            headers=headers,
        ),
    }
    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(responses, 404)
    # The foreign calendar and its windows are untouched -- a 404 that still
    # performed the write would be worse than a 403.
    assert (await _calendar_row(api_env, foreign_calendar)).name != "stolen"
    assert len(await _window_rows(api_env, foreign_calendar)) == len(_MON_FRI_9_TO_5)


async def test_another_orgs_policy_and_ticket_are_404_never_403(api_env) -> None:
    """Same reasoning for `sla_policy` and for the per-ticket view, whose
    `TicketNotFoundError` is re-raised as `SlaNotFoundError` precisely so it
    lands on the same 404 and adds no oracle of its own."""
    mine = await api_env.create_org(f"sla-mine-{uuid4().hex[:8]}")
    env = await _recompute_env(api_env)
    foreign_policy = env["policy_id"]
    foreign_ticket = env["running"].id

    headers = await _admin(api_env, mine)
    responses = {
        "get": await api_env.client.get(
            f"{SLA_PREFIX}/policies/{foreign_policy}", headers=headers
        ),
        "patch": await api_env.client.patch(
            f"{SLA_PREFIX}/policies/{foreign_policy}",
            json={"response_target_minutes": 1},
            headers=headers,
        ),
        "delete": await api_env.client.delete(
            f"{SLA_PREFIX}/policies/{foreign_policy}", headers=headers
        ),
        "ticket": await api_env.client.get(
            f"/api/v1/tickets/{foreign_ticket}/sla", headers=headers
        ),
    }
    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(responses, 404)
    # And the foreign org's own principal can still see all of it, which is
    # what makes the 404s above "not yours" rather than "not there".
    theirs_headers = env["headers"]
    assert (
        await api_env.client.get(
            f"{SLA_PREFIX}/policies/{foreign_policy}", headers=theirs_headers
        )
    ).status_code == 200
    assert (
        await api_env.client.get(
            f"/api/v1/tickets/{foreign_ticket}/sla", headers=theirs_headers
        )
    ).status_code == 200


async def test_a_policy_cannot_be_created_against_another_orgs_calendar(api_env) -> None:
    """A cross-org FK is refused at write time (`policies._resolve_calendar`)
    -- 400, because the id is in the BODY rather than the path: the caller is
    describing a policy that cannot exist, not asking for a resource."""
    mine = await api_env.create_org(f"sla-mine-{uuid4().hex[:8]}")
    theirs = await api_env.create_org(f"sla-theirs-{uuid4().hex[:8]}")
    foreign_calendar = await _seed_calendar(api_env, theirs, windows=_MON_FRI_9_TO_5)

    response = await api_env.client.post(
        f"{SLA_PREFIX}/policies",
        json={
            "calendar_id": str(foreign_calendar),
            "response_target_minutes": 60,
            "resolution_target_minutes": 480,
        },
        headers=await _admin(api_env, mine),
    )
    assert response.status_code == 400, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(select(SlaPolicy.id).where(SlaPolicy.org_id == mine))
        ).scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# Calendar CRUD through the API.
# --------------------------------------------------------------------------- #


async def test_a_calendar_round_trips_through_create_read_and_windows(api_env) -> None:
    """The §9.1 weekly-grid editor's whole loop: create, PUT the week, read it
    back. The GET must return the windows -- §9's table gives windows a PUT
    and no GET of their own, so without them on the calendar detail they are
    write-only and the grid can never load what it edits."""
    org_id = await api_env.create_org(f"sla-crud-{uuid4().hex[:8]}")
    headers = await _admin(api_env, org_id)

    created = await api_env.client.post(
        f"{SLA_PREFIX}/calendars",
        json={"name": "Business hours", "timezone": LONDON},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    calendar_id = created.json()["id"]
    # A brand-new calendar has no windows, and says so rather than looking
    # healthy until a ticket silently fails to get an SLA row.
    assert created.json()["health"] == "no_working_windows"

    put = await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{calendar_id}/windows",
        json=_windows_body(_MON_FRI_9_TO_5),
        headers=headers,
    )
    assert put.status_code == 200, put.text

    detail = await api_env.client.get(f"{SLA_PREFIX}/calendars/{calendar_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["health"] == "ok"
    # The timezone is on every view of a calendar (§9.1): a working window is
    # meaningless without it.
    assert body["timezone"] == LONDON
    assert [(w["weekday"], w["start_time"], w["end_time"]) for w in body["windows"]] == [
        (weekday, start.isoformat(), end.isoformat())
        for weekday, start, end in _MON_FRI_9_TO_5
    ]


async def test_overlapping_windows_are_refused_and_change_nothing(api_env) -> None:
    """A partial edit can leave a calendar with overlapping windows, which the
    clock has no defined behaviour for -- so the WHOLE set is validated before
    any row is touched (spec §9). The pre-existing week must survive."""
    env = await _recompute_env(api_env)
    response = await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}/windows",
        json=_windows_body(
            [(0, time(9, 0), time(17, 0)), (0, time(12, 0), time(18, 0))]
        ),
        headers=env["headers"],
    )
    assert response.status_code == 400, response.text
    assert "overlap" in response.json()["detail"]
    assert len(await _window_rows(api_env, env["calendar_id"])) == len(_MON_FRI_9_TO_5)


async def test_a_calendar_referenced_by_a_policy_cannot_be_deleted(api_env) -> None:
    """Spec §9: the error NAMES the policies, so an admin can act on it
    rather than guessing."""
    env = await _recompute_env(api_env)
    response = await api_env.client.delete(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}", headers=env["headers"]
    )
    assert response.status_code == 400, response.text
    assert str(env["policy_id"]) in response.json()["detail"]
    assert await _calendar_row(api_env, env["calendar_id"]) is not None


async def test_an_invalid_timezone_is_a_400_not_a_500(api_env) -> None:
    """Including the path-traversal-shaped name, which `zoneinfo` raises a
    bare `ValueError` for rather than `ZoneInfoNotFoundError` (Task 3 review
    F1) -- uncaught it is a raw 500 from an `sla.admin` API body."""
    org_id = await api_env.create_org(f"sla-tz-{uuid4().hex[:8]}")
    headers = await _admin(api_env, org_id)
    for timezone in ("Nowhere/Atlantis", "Europe/../../etc/passwd", "/etc/localtime"):
        response = await api_env.client.post(
            f"{SLA_PREFIX}/calendars",
            json={"name": f"c-{uuid4().hex[:6]}", "timezone": timezone},
            headers=headers,
        )
        assert response.status_code == 400, (timezone, response.status_code, response.text)


# --------------------------------------------------------------------------- #
# Policies through the API.
# --------------------------------------------------------------------------- #


async def test_a_policy_wildcard_survives_an_explicit_null_patch(api_env) -> None:
    """`{"queue_id": null}` is a REAL edit -- "apply to any queue" (spec §2.4,
    §3's resolution levels) -- not the absence of one. A `PolicyUpdate` dumped
    with `exclude_none` instead of `exclude_unset` would drop it silently and
    return 200 having changed nothing, making three of §3's four resolution
    levels unreachable through this API.

    The queue-specific starting state is what makes the assertion bite: with a
    policy whose `queue_id` is already `None`, both implementations produce
    the same row.
    """
    env = await _recompute_env(api_env)
    org_id, headers = env["org_id"], env["headers"]
    queue_id = await _seed_queue(api_env, org_id)
    created = await api_env.client.post(
        f"{SLA_PREFIX}/policies",
        json={
            "queue_id": str(queue_id),
            "priority": "high",
            "calendar_id": str(env["calendar_id"]),
            "response_target_minutes": 30,
            "resolution_target_minutes": 120,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    assert created.json()["queue_id"] == str(queue_id)

    patched = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{policy_id}", json={"queue_id": None}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["queue_id"] is None
    assert patched.json()["priority"] == "high"


async def test_a_policy_with_history_cannot_be_deleted_but_can_be_retired(api_env) -> None:
    """Deleting a policy leaves existing `ticket_sla` rows intact (spec §9),
    so a policy any of them still references is refused; `active=False` is the
    retirement path and must WORK, or the refusal is a dead end."""
    env = await _recompute_env(api_env)
    headers = env["headers"]

    refused = await api_env.client.delete(
        f"{SLA_PREFIX}/policies/{env['policy_id']}", headers=headers
    )
    assert refused.status_code == 400, refused.text
    assert str(env["running"].id) in refused.json()["detail"]

    retired = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}", json={"active": False}, headers=headers
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["active"] is False


async def test_a_typoed_field_is_refused_rather_than_silently_dropped(api_env) -> None:
    """Task 4 review F6, one layer up. `policies.update_policy` refuses an
    unknown key rather than ignoring it, because silently dropping it returned
    200 for a typo'd body having changed nothing and audited a no-op edit --
    but a Pydantic model that IGNORES extras (the default) strips the typo
    before the service can ever see it, putting the defect straight back for
    every caller that arrives over HTTP. `extra="forbid"` is what carries F6's
    decision across the wire boundary.

    The typo is sent ALONGSIDE a valid edit, so "the request failed" is not
    enough: the valid half must not have been applied either.
    """
    env = await _recompute_env(api_env)
    response = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"response_target_minutes": 45, "respones_target_minutes": 45},
        headers=env["headers"],
    )
    assert response.status_code == 422, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(
                select(SlaPolicy.response_target_minutes).where(SlaPolicy.id == env["policy_id"])
            )
        ).scalar_one()
    assert stored == _RESPONSE_MINUTES


# --------------------------------------------------------------------------- #
# Settings, and the misconfiguration surface.
# --------------------------------------------------------------------------- #


async def test_settings_default_then_upsert_twice(api_env) -> None:
    """An org with no `sla_org_settings` row reports the DEFAULT the sweep
    actually uses, and says it is unconfigured -- "we chose 7" and "nobody has
    ever looked at this" lead to different conversations. Then two PUTs, so
    the INSERT and the ON CONFLICT DO UPDATE halves of the upsert are both
    exercised (one PUT proves only the first)."""
    org_id = await api_env.create_org(f"sla-settings-{uuid4().hex[:8]}")
    headers = await _admin(api_env, org_id)

    first = await api_env.client.get(f"{SLA_PREFIX}/settings", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["auto_close_after_days"] == DEFAULT_AUTO_CLOSE_AFTER_DAYS
    assert first.json()["configured"] is False

    for days in (3, 30):
        put = await api_env.client.put(
            f"{SLA_PREFIX}/settings", json={"auto_close_after_days": days}, headers=headers
        )
        assert put.status_code == 200, put.text
        assert put.json() == {
            "auto_close_after_days": days,
            "configured": True,
            "tickets_without_sla": 0,
            "stale_ticket_sla": 0,
        }

    async with AsyncSession(api_env.admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(SlaOrgSettings).where(SlaOrgSettings.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    # The second PUT UPDATED the row rather than inserting a second one --
    # `org_id` is UNIQUE, so a non-upserting implementation would have raised
    # instead, and asserting only the response body would not tell them apart.
    assert len(rows) == 1
    assert rows[0].auto_close_after_days == 30
    assert (await _audit_actions(api_env, org_id)).count("sla.settings_updated") == 2


@pytest.mark.parametrize("days", [0, -1, 3651])
async def test_settings_refuses_an_out_of_range_window(api_env, days: int) -> None:
    """`ck_sla_org_settings_auto_close_positive` bounds it below; the upper
    bound is this module's, because an unbounded value lets a typo silently
    disable auto-close forever in a way that looks identical to auto-close
    being broken."""
    org_id = await api_env.create_org(f"sla-settings-bad-{uuid4().hex[:8]}")
    response = await api_env.client.put(
        f"{SLA_PREFIX}/settings",
        json={"auto_close_after_days": days},
        headers=await _admin(api_env, org_id),
    )
    assert response.status_code in (400, 422), response.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(SlaOrgSettings.id).where(SlaOrgSettings.org_id == org_id)
            )
        ).scalar_one_or_none() is None


async def test_tickets_without_an_sla_row_are_counted(api_env) -> None:
    """THE CONSEQUENCE HALF OF THE MISCONFIGURATION SURFACE.

    `tracking.sla_misconfiguration_is_not_fatal` degrades a broken calendar to
    "no `ticket_sla` row" and logs at ERROR on the API host -- which nobody in
    the org can see. This is the number that ends that blindness, and the
    fixture varies the only parameter that makes it meaningful: a TRACKED
    ticket sits beside an UNTRACKED one in the same org, so "count everything"
    and "count nothing" both fail. A terminal untracked ticket is seeded too
    and must NOT be counted -- nothing can be done about it, and a number that
    can never return to zero stops being read.
    """
    env = await _recompute_env(api_env)
    org_id, headers = env["org_id"], env["headers"]
    untracked = await _seed_ticket(api_env, org_id)
    assert await _row(api_env, untracked.id) is None
    terminal_untracked = await _seed_ticket(api_env, org_id, type_="request", status="rejected")
    assert await _row(api_env, terminal_untracked.id) is None
    # A SECOND org with its own SLA-less ticket, so the OUTER `org_id`
    # predicate is exercised rather than assumed: without it this org's number
    # would include somebody else's backlog.
    neighbour = await _recompute_env(api_env)
    await _seed_ticket(api_env, neighbour["org_id"])

    body = (await api_env.client.get(f"{SLA_PREFIX}/settings", headers=headers)).json()
    assert body["tickets_without_sla"] == 1


async def test_a_foreign_ticket_sla_row_does_not_hide_an_sla_less_ticket(api_env) -> None:
    """The INNER `org_id` predicate of the count's `NOT EXISTS`, exercised
    rather than asserted in prose.

    `ticket_sla.ticket_id` is a plain FK with no org check -- a row owned by
    one org can legally point at another org's ticket, which is exactly why
    `sweep.py`'s breach join carries the same predicate. Here a forged
    cross-org `ticket_sla` row is planted against this org's SLA-less ticket:
    without the inner `TicketSla.org_id == org_id`, that foreign row satisfies
    the correlation and the ticket vanishes from its own org's count -- a
    misconfiguration made invisible by a neighbouring tenant.

    Read on the BYPASSRLS admin engine to plant it, because RLS would
    otherwise refuse the write and the test would prove nothing.
    """
    env = await _recompute_env(api_env)
    neighbour = await _recompute_env(api_env)
    orphan = await _seed_ticket(api_env, env["org_id"])
    assert await _row(api_env, orphan.id) is None

    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            template = (
                await session.execute(
                    select(TicketSla).where(TicketSla.ticket_id == neighbour["running"].id)
                )
            ).scalar_one()
            session.add(
                TicketSla(
                    org_id=neighbour["org_id"],
                    ticket_id=orphan.id,
                    policy_id=template.policy_id,
                    calendar_id=template.calendar_id,
                    response_due_at=template.response_due_at,
                    resolution_due_at=template.resolution_due_at,
                    policy_source="initial",
                )
            )

    body = (
        await api_env.client.get(f"{SLA_PREFIX}/settings", headers=env["headers"])
    ).json()
    assert body["tickets_without_sla"] == 1
    # And the per-ticket view agrees: the foreign row is not this ticket's SLA.
    view = (
        await api_env.client.get(f"/api/v1/tickets/{orphan.id}/sla", headers=env["headers"])
    ).json()
    assert view["has_sla"] is False

    # THE SAME ASSERTIONS ON THE BYPASSRLS ADMIN ENGINE, which is the half
    # that actually tests the predicates. Through the API every one of these
    # queries runs inside `org_session`, so RLS alone produces the right
    # answer and DELETING the `org_id` predicate changes nothing observable --
    # verified by mutation, and it is the trap this sub-project has already
    # fallen into once. Here nothing is masked, so each predicate is the sole
    # guard under test.
    async with AsyncSession(api_env.admin_engine) as session:
        assert await count_tickets_without_sla(session, env["org_id"]) == 1
        assert await count_tickets_without_sla(session, neighbour["org_id"]) == 0
        # The forged row really is there and really does point at this org's
        # ticket -- which is what makes the count above a tenancy result
        # rather than an absence. (`ticket_sla_state` cannot be used to show
        # it: the TICKET belongs to `env`, so the neighbour's lookup 404s on
        # the ticket before it ever reaches the row -- itself the correct
        # answer, and the reason the two predicates are independent.)
        planted = (
            await session.execute(
                select(TicketSla).where(
                    TicketSla.ticket_id == orphan.id,
                    TicketSla.org_id == neighbour["org_id"],
                )
            )
        ).scalar_one()
        assert planted.org_id != env["org_id"]


async def test_a_wholly_failed_recompute_is_visible_as_staleness(api_env) -> None:
    """THE MEASURE THAT CATCHES A RECOMPUTE THAT FAILED AND REPORTED SUCCESS.

    `sweep._recompute_rows` contains every per-row failure in a savepoint and
    counts it; `policies.update_policy` reads that count only to pick a log
    level. So an admin who repoints a policy at a calendar whose timezone is
    corrupt gets a 200 carrying the new configuration, the edit commits, every
    affected row fails, and the whole open backlog silently keeps deadlines
    derived from the configuration that no longer exists. This is that state,
    reached through the route, and the number that ends it.

    THE PARAMETERS THIS FIXTURE VARIES, because a staleness measure is
    trivially satisfiable and this file's recurring defect is a fixture that
    lets the broken and fixed implementations agree:

    * **Edits that recompute nothing are asserted to register nothing.** A
      calendar rename and a policy `active` toggle both bump `updated_at` and
      both deliberately skip the recompute, so an implementation that compared
      `last_recomputed_at` against `updated_at` -- the obvious cheap form --
      reports staleness for both and can never return to 0 again. Both are
      exercised BEFORE the real failure, against a count that must still read
      0.
    * **A stopped row sits beside the running one.** Only the running row can
      be stale; a frozen row keeps the promise it was actually made, calendar
      included. An implementation that counts every `ticket_sla` row, or every
      row whose `calendar_id` disagrees with its policy, reports 2.
    * **`tickets_without_sla` is asserted to be 0 throughout.** It is the
      existing consequence measure and it is BLIND here: the rows exist, they
      are merely wrong. If it moved, this test would be measuring the old
      number.
    """
    env = await _recompute_policy_env(api_env)
    headers = env["headers"]

    async def _settings() -> dict[str, Any]:
        response = await api_env.client.get(f"{SLA_PREFIX}/settings", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    healthy = await _settings()
    assert healthy["stale_ticket_sla"] == 0
    assert healthy["tickets_without_sla"] == 0

    renamed = await api_env.client.patch(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}",
        json={"name": f"renamed-{uuid4().hex[:6]}"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert (await _settings())["stale_ticket_sla"] == 0

    toggled = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"active": False},
        headers=headers,
    )
    assert toggled.status_code == 200, toggled.text
    assert (await _settings())["stale_ticket_sla"] == 0

    # The calendar is valid when the rows are materialised and broken
    # afterwards, which is how an admin actually reaches this state
    # (`_validate_timezone` makes it unstorable through the API).
    await _set_timezone(api_env, env["other_calendar_id"], "Nowhere/Atlantis")
    repointed = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"calendar_id": str(env["other_calendar_id"])},
        headers=headers,
    )
    # THE DEFECT, ASSERTED RATHER THAN DESCRIBED: 200, with the new
    # configuration, while nothing reached the rows.
    assert repointed.status_code == 200, repointed.text
    assert repointed.json()["calendar_id"] == str(env["other_calendar_id"])

    running = await _row(api_env, env["running"].id)
    assert running is not None
    assert running.calendar_id == env["calendar_id"]
    assert running.response_due_at == _OLD_RESPONSE_DUE
    assert running.resolution_due_at == _OLD_RESOLUTION_DUE

    after = await _settings()
    assert after["tickets_without_sla"] == 0
    assert after["stale_ticket_sla"] == 1

    # And it CLEARS: repairing the calendar and re-applying the repoint
    # recomputes the row. A measure that cannot return to 0 stops being read,
    # which is the failure `count_tickets_without_sla` is written to avoid.
    await _set_timezone(api_env, env["other_calendar_id"], LONDON)
    repaired = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"calendar_id": str(env["calendar_id"])},
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text
    assert (await _settings())["stale_ticket_sla"] == 0


async def test_staleness_sees_a_deadline_the_configuration_would_not_produce(
    api_env,
) -> None:
    """The other half of the measure: the row's calendar is RIGHT and its
    stored deadline is still wrong.

    A `calendar_id` comparison alone passes this file's repoint test and is
    blind here, which is why both shapes are pinned. The state is reached by
    breaking the calendar, editing its windows (the recompute fails on every
    row), and then repairing the timezone -- leaving a healthy calendar whose
    windows no longer produce the instants the rows store. That is exactly
    what `replace_windows` leaves behind when its recompute failed, and no
    count of rows-by-presence can see it.

    The frozen row is the discriminator again: its deadline also disagrees
    with the new windows, and it is NOT stale -- `_recompute_row` leaves a met
    or breached clock alone on purpose, so measuring it against today's
    configuration would report a permanent non-zero number for a row behaving
    exactly as specified.
    """
    env = await _recompute_env(api_env)
    headers = env["headers"]
    await _set_timezone(api_env, env["calendar_id"], "Nowhere/Atlantis")

    replaced = await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}/windows",
        json=_windows_body(_MON_FRI_9_TO_1),
        headers=headers,
    )
    assert replaced.status_code == 200, replaced.text
    await _set_timezone(api_env, env["calendar_id"], LONDON)

    running = await _row(api_env, env["running"].id)
    assert running is not None
    # The windows moved; the row did not.
    assert running.calendar_id == env["calendar_id"]
    assert running.response_due_at == _OLD_RESPONSE_DUE
    assert running.response_due_at != _NEW_RESPONSE_DUE

    body = (await api_env.client.get(f"{SLA_PREFIX}/settings", headers=headers)).json()
    assert body["tickets_without_sla"] == 0
    assert body["stale_ticket_sla"] == 1


async def test_the_staleness_count_is_org_scoped_on_the_bypassrls_engine(api_env) -> None:
    """Invariant #5 on the new read, pinned where it can actually fail.

    Through the API every query runs inside `org_session`, so RLS alone
    produces the right answer and deleting an `org_id` predicate changes
    nothing observable. Here nothing is masked. Both orgs hold identically
    shaped configuration and ONE of them is broken, so a missing predicate
    returns the neighbour's answer rather than an empty one.
    """
    mine = await _recompute_policy_env(api_env)
    theirs = await _recompute_policy_env(api_env)
    await _set_timezone(api_env, mine["other_calendar_id"], "Nowhere/Atlantis")
    repointed = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{mine['policy_id']}",
        json={"calendar_id": str(mine["other_calendar_id"])},
        headers=mine["headers"],
    )
    assert repointed.status_code == 200, repointed.text

    async with AsyncSession(api_env.admin_engine) as session:
        assert await count_stale_ticket_sla(session, mine["org_id"]) == 1
        assert await count_stale_ticket_sla(session, theirs["org_id"]) == 0


async def test_a_calendar_with_no_windows_is_reported_unhealthy(api_env) -> None:
    """THE CAUSE HALF: `no_working_windows` is exactly the state that makes
    `add_working_minutes` raise `UnsatisfiableCalendar` (spec §4) -- the
    condition the tolerant wrapper swallows. A HEALTHY calendar is seeded
    beside it, so an implementation that reports every calendar as broken (or
    every calendar as fine) fails."""
    org_id = await api_env.create_org(f"sla-health-{uuid4().hex[:8]}")
    healthy = await _seed_calendar(api_env, org_id, windows=_MON_FRI_9_TO_5)
    empty = await _seed_calendar(api_env, org_id, windows=[])
    broken_tz = await _seed_calendar(api_env, org_id, windows=_MON_FRI_9_TO_5)
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            calendar = (
                await session.execute(select(SlaCalendar).where(SlaCalendar.id == broken_tz))
            ).scalar_one()
            # Validated at write time, so only a direct DB edit reaches it --
            # which is precisely why the READ has to report it: nothing else
            # in the product ever would.
            calendar.timezone = "Nowhere/Atlantis"

    listed = await api_env.client.get(
        f"{SLA_PREFIX}/calendars", headers=await _admin(api_env, org_id)
    )
    assert listed.status_code == 200, listed.text
    health = {row["id"]: row["health"] for row in listed.json()}
    assert health == {
        str(healthy): "ok",
        str(empty): "no_working_windows",
        str(broken_tz): "invalid_timezone",
    }


async def test_every_derived_read_is_org_scoped_on_the_bypassrls_engine(api_env) -> None:
    """INVARIANT #5, PINNED WHERE IT CAN ACTUALLY FAIL.

    Every query this task added carries an explicit `org_id` predicate as well
    as running under RLS. Asserting that through the API proves nothing: the
    handlers open `org_session`, RLS masks the other tenant anyway, and
    deleting the predicate leaves every HTTP assertion green. So each read is
    exercised here on the BYPASSRLS admin engine, where RLS masks NOTHING and
    the predicate is the sole guard -- the discipline `test_sla_policies.py`
    and `test_sla_sweep.py` established.

    Two orgs, each holding a calendar with the SAME windows and a ticket in
    the same state, so a missing predicate returns the neighbour's answer
    rather than an empty one -- with a single org in the fixture, an
    implementation with no tenancy predicate at all passes.
    """
    mine = await _recompute_env(api_env)
    theirs = await _recompute_env(api_env)
    await _seed_ticket(api_env, mine["org_id"])
    await _seed_ticket(api_env, theirs["org_id"])
    await _seed_ticket(api_env, theirs["org_id"])

    async with AsyncSession(api_env.admin_engine) as session:
        # count_tickets_without_sla: both `org_id` predicates.
        assert await count_tickets_without_sla(session, mine["org_id"]) == 1
        assert await count_tickets_without_sla(session, theirs["org_id"]) == 2

        # calendar_health_by_id: the window count is `org_id`-scoped, so MY
        # calendar asked about under THEIR org must find no windows. Without
        # the predicate the same calendar reports "ok" for both.
        my_calendar = await _calendar_row(api_env, mine["calendar_id"])
        assert await calendar_health_by_id(session, mine["org_id"], [my_calendar]) == {
            my_calendar.id: "ok"
        }
        assert await calendar_health_by_id(session, theirs["org_id"], [my_calendar]) == {
            my_calendar.id: "no_working_windows"
        }

        # ticket_sla_state: a ticket from the other org is not found at all.
        # This half exercises the `get_ticket` guard ONLY -- it is satisfied
        # before the `ticket_sla` SELECT is reached, so it says nothing about
        # that query's own `org_id` predicate. The test below this one is what
        # pins that.
        with pytest.raises(SlaValidationError):
            await ticket_sla_state(session, mine["org_id"], theirs["running"].id)
        _, row = await ticket_sla_state(session, theirs["org_id"], theirs["running"].id)
        assert row is not None

        # The settings read: `sla_org_settings.org_id` is UNIQUE across the
        # whole table, so without the predicate this returns whichever
        # tenant's row the planner hands back first.
        await set_auto_close_after_days(
            session, theirs["org_id"], theirs["running"].requester_id, days=30
        )
        await session.commit()
    async with AsyncSession(api_env.admin_engine) as session:
        assert await get_auto_close_after_days(session, theirs["org_id"]) == 30
        assert (
            await get_auto_close_after_days(session, mine["org_id"])
            == DEFAULT_AUTO_CLOSE_AFTER_DAYS
        )


async def test_the_ticket_sla_state_row_lookup_is_org_scoped_on_the_bypassrls_engine(
    api_env,
) -> None:
    """`views.ticket_sla_state`'s `TicketSla.org_id` predicate, which the
    two-orgs test above cannot reach.

    That test asks about the OTHER org's ticket, so `get_ticket` refuses
    first and the `ticket_sla` SELECT never runs -- delete its `org_id`
    predicate and that assertion stays green. The shape that reaches it is a
    ticket THIS org owns (so `get_ticket` succeeds) carrying a `ticket_sla`
    row that belongs to somebody ELSE. `ticket_sla.ticket_id` is UNIQUE and
    the FK has no org concept, so this row is what a missing predicate returns
    instead of `None`: another tenant's policy, deadlines and breach stamps,
    rendered on this org's ticket page.

    No application path produces it -- `start_tracking` rejects the cross-org
    ticket before writing -- so it is built directly on the admin engine, and
    the read runs there too: through `org_session` RLS would mask the
    neighbour's row and the predicate could be deleted with the suite green.
    """
    mine = await _recompute_env(api_env)
    theirs = await _recompute_env(api_env)
    # Untracked, so the foreign row can claim it.
    unclaimed = await _seed_ticket(api_env, mine["org_id"])
    foreign_id = await _foreign_row(
        api_env,
        owner_org=theirs["org_id"],
        ticket_id=unclaimed.id,
        calendar_id=theirs["calendar_id"],
        policy_id=theirs["policy_id"],
    )

    async with AsyncSession(api_env.admin_engine) as session:
        ticket, row = await ticket_sla_state(session, mine["org_id"], unclaimed.id)

    assert ticket.id == unclaimed.id
    assert row is None

    # The fixture is only meaningful if the row a missing predicate would have
    # returned really is there.
    stored = await _row(api_env, unclaimed.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == theirs["org_id"]


# --------------------------------------------------------------------------- #
# The per-ticket view (spec §9, §9.1).
# --------------------------------------------------------------------------- #


async def test_a_ticket_with_no_sla_row_is_200_with_has_sla_false(api_env) -> None:
    """Spec §3: an org with no policy configured must SEE "no SLA" -- never a
    fabricated target, and never a 404 that would be indistinguishable from a
    ticket that does not exist. A TRACKED ticket in the same org is asserted
    in the same test, so "everything reports no SLA" cannot pass."""
    env = await _recompute_env(api_env)
    untracked = await _seed_ticket(api_env, env["org_id"])

    absent = await api_env.client.get(
        f"/api/v1/tickets/{untracked.id}/sla", headers=env["headers"]
    )
    assert absent.status_code == 200, absent.text
    assert absent.json()["has_sla"] is False
    assert absent.json()["response_state"] == "none"
    assert absent.json()["response_due_at"] is None

    present = await api_env.client.get(
        f"/api/v1/tickets/{env['running'].id}/sla", headers=env["headers"]
    )
    assert present.json()["has_sla"] is True
    assert present.json()["response_state"] == "running"


async def test_the_server_owns_the_met_breached_and_paused_verdicts(api_env) -> None:
    """Spec §9.1: "Breach state is never computed in the browser." The two
    clocks stop INDEPENDENTLY, so the fixture stamps a met response beside a
    breached resolution on ONE row -- a per-ROW verdict, or a mismatched
    column-to-clock pairing (Task 5's F1), reports both the same and is
    invisible to a fixture that exercises one target at a time."""
    env = await _recompute_env(api_env)
    body = (
        await api_env.client.get(
            f"/api/v1/tickets/{env['stopped'].id}/sla", headers=env["headers"]
        )
    ).json()
    assert body["response_state"] == "met"
    assert body["resolution_state"] == "breached"
    assert body["response_met_at"] is not None
    assert body["resolution_breached_at"] is not None
    # §0.3's attribution: "breached because we were slow" must stay
    # distinguishable from "breached retroactively because we re-triaged".
    assert body["policy_source"] == "initial"


async def test_a_breached_target_is_not_laundered_by_a_late_reply(api_env) -> None:
    """`breached` OUTRANKS `met` in `views.target_state`, and the two genuinely
    co-occur: a reply landing after the deadline stamps `met_at` on a row
    whose `breached_at` is already set. Reporting that as "met" would let any
    breach be erased by eventually answering."""
    env = await _recompute_env(api_env)
    ticket = await _seed_ticket(api_env, env["org_id"])
    await _track(api_env, env["org_id"], ticket)
    await _stop_clocks(
        api_env,
        ticket.id,
        response_breached_at=_OLD_RESPONSE_DUE + timedelta(minutes=1),
        response_met_at=_OLD_RESPONSE_DUE + timedelta(hours=3),
    )
    body = (
        await api_env.client.get(f"/api/v1/tickets/{ticket.id}/sla", headers=env["headers"])
    ).json()
    assert body["response_state"] == "breached"


async def test_a_paused_clock_is_visibly_paused(api_env) -> None:
    """§9.1: a countdown that silently stops moving reads as a bug and prompts
    nobody to act. The running ticket beside it is what makes `paused` a real
    distinction rather than a constant."""
    env = await _recompute_env(api_env)
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(TicketSla).where(TicketSla.ticket_id == env["running"].id)
                )
            ).scalar_one()
            row.paused_at = MONDAY_0900 + timedelta(hours=1)

    paused = (
        await api_env.client.get(
            f"/api/v1/tickets/{env['running'].id}/sla", headers=env["headers"]
        )
    ).json()
    assert paused["response_state"] == "paused"
    assert paused["paused_at"] is not None

    untracked = await _seed_ticket(api_env, env["org_id"])
    await _track(api_env, env["org_id"], untracked)
    other = (
        await api_env.client.get(f"/api/v1/tickets/{untracked.id}/sla", headers=env["headers"])
    ).json()
    assert other["response_state"] == "running"


# --------------------------------------------------------------------------- #
# The recompute wiring (spec §7) -- Task 11's whole reason for touching
# `calendars.py` and `policies.py`.
# --------------------------------------------------------------------------- #


async def test_a_windows_put_recomputes_running_deadlines_only(api_env) -> None:
    """§7's "calendar and policy edits recompute affected open tickets",
    reached through the ROUTE rather than by calling the sweep helper --
    `recompute_for_calendar_change` was implemented, tested and had NO
    production caller, which is dead code that tests keep alive.

    The halved week produces DIFFERENT instants, which is what makes the
    running/stopped split observable: an edit that produced the same deadlines
    would hide both a mutant that rewrites frozen clocks and one that
    recomputes nothing.
    """
    env = await _recompute_env(api_env)
    response = await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}/windows",
        json=_windows_body(_MON_FRI_9_TO_1),
        headers=env["headers"],
    )
    assert response.status_code == 200, response.text

    running = await _row(api_env, env["running"].id)
    assert running is not None
    assert running.response_due_at == _NEW_RESPONSE_DUE
    assert running.resolution_due_at == _NEW_RESOLUTION_DUE

    stopped = await _row(api_env, env["stopped"].id)
    assert stopped is not None
    # A met or breached deadline is history reporting reads; rewriting it
    # would let a calendar edit retroactively turn a breach into a non-breach.
    assert stopped.response_due_at == _OLD_RESPONSE_DUE
    assert stopped.resolution_due_at == _OLD_RESOLUTION_DUE


async def test_a_holiday_add_and_remove_recompute(api_env) -> None:
    """Both holiday mutators, in sequence, so the recompute is proved to be
    reversible rather than one-directional -- and the removal is asserted to
    return the deadline to its exact original instant, which a recompute wired
    into only ONE of the two would fail."""
    env = await _recompute_env(api_env)
    headers = env["headers"]
    # The Monday the tickets were filed on is already spent; the Tuesday is
    # where the 300-minute response target lands.
    added = await api_env.client.post(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}/holidays",
        json={"holiday_date": "2026-03-03", "name": "A Tuesday off"},
        headers=headers,
    )
    assert added.status_code == 201, added.text
    holiday_id = added.json()["id"]

    after_add = await _row(api_env, env["running"].id)
    assert after_add is not None
    # The response target (5 working hours from Mon 09:00) still lands on the
    # Monday, so only the resolution moves -- which is itself the assertion
    # that this recompute is per-deadline arithmetic and not a blanket shift.
    assert after_add.response_due_at == _OLD_RESPONSE_DUE
    assert after_add.resolution_due_at == datetime(2026, 3, 4, 11, 0, tzinfo=UTC)

    removed = await api_env.client.delete(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}/holidays/{holiday_id}", headers=headers
    )
    assert removed.status_code == 204, removed.text
    after_remove = await _row(api_env, env["running"].id)
    assert after_remove is not None
    assert after_remove.resolution_due_at == _OLD_RESOLUTION_DUE


async def test_a_timezone_patch_recomputes_but_a_rename_does_not(api_env) -> None:
    """THE FIELD-LEVEL GUARD. Only the timezone is an input to the clock; a
    rename cannot move an instant, so recomputing on one would be churn
    proportional to the org's backlog.

    Both halves are asserted against the SAME fixture and the SAME row, so
    "always recompute" and "never recompute" each fail exactly one of them.
    `last_recomputed_at` is the witness for the rename, because the deadline
    is unchanged either way -- without it, a rename that DID recompute is
    invisible.
    """
    env = await _recompute_env(api_env)
    headers = env["headers"]
    before = await _row(api_env, env["running"].id)
    assert before is not None
    stamp_before = before.last_recomputed_at

    renamed = await api_env.client.patch(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}",
        json={"name": f"renamed-{uuid4().hex[:6]}"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    after_rename = await _row(api_env, env["running"].id)
    assert after_rename is not None
    assert after_rename.last_recomputed_at == stamp_before
    assert after_rename.response_due_at == _OLD_RESPONSE_DUE

    # Europe/London -> Asia/Tokyo (UTC+9): the same 09:00-17:00 LOCAL window
    # is a different UTC instant, so every deadline moves.
    retimed = await api_env.client.patch(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}",
        json={"timezone": "Asia/Tokyo"},
        headers=headers,
    )
    assert retimed.status_code == 200, retimed.text
    after_retime = await _row(api_env, env["running"].id)
    assert after_retime is not None
    assert after_retime.last_recomputed_at > stamp_before
    assert after_retime.response_due_at != _OLD_RESPONSE_DUE
    # And the frozen row is still frozen.
    stopped = await _row(api_env, env["stopped"].id)
    assert stopped is not None
    assert stopped.response_due_at == _OLD_RESPONSE_DUE


async def _recompute_policy_env(api_env) -> dict[str, Any]:
    """`_recompute_env` plus a SECOND calendar whose week is halved.

    Two calendars is the parameter the `calendar_id`-adoption decision turns
    on: with one, the policy's calendar and the row's calendar are the same
    object, adopting and not adopting produce identical deadlines, and the
    decision is untestable. This is the fixture failure this sub-project keeps
    shipping, caught in this file's own draft.
    """
    env = await _recompute_env(api_env)
    env["other_calendar_id"] = await _seed_calendar(
        api_env, env["org_id"], timezone=LONDON, windows=_MON_FRI_9_TO_1
    )
    return env


async def test_a_policy_retarget_recomputes_running_clocks(api_env) -> None:
    """A target edit lands on the open backlog at the audited admin edit,
    not lazily at whatever event happens to recompute the row next."""
    env = await _recompute_policy_env(api_env)
    response = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        # 600 working minutes from Mon 09:00 on Mon-Fri 09:00-17:00 -> Tue 11:00.
        json={"response_target_minutes": _RESOLUTION_MINUTES},
        headers=env["headers"],
    )
    assert response.status_code == 200, response.text

    running = await _row(api_env, env["running"].id)
    assert running is not None
    assert running.response_due_at == _OLD_RESOLUTION_DUE
    assert running.resolution_due_at == _OLD_RESOLUTION_DUE
    # The frozen row keeps the promise it was actually made.
    stopped = await _row(api_env, env["stopped"].id)
    assert stopped is not None
    assert stopped.response_due_at == _OLD_RESPONSE_DUE


async def test_a_repointed_policy_calendar_is_adopted_by_running_rows_only(api_env) -> None:
    """THE DECISION TASK 10 LEFT OPEN, and the reason it needed two calendars.

    A row's TARGETS already track the policy row -- nothing copies
    `response_target_minutes` onto `ticket_sla`, and `resume` loads the policy
    by id -- so leaving `calendar_id` behind would derive the policy's NEW
    target on a calendar the policy no longer names: a (target, calendar) pair
    that exists nowhere in the org's configuration and that no admin action
    short of a human re-triage can reconcile.

    A frozen row is NOT repointed, so history keeps the calendar it was really
    promised under. That asymmetry is what a one-calendar fixture cannot see:
    both rows would report the same `calendar_id` whatever the code did.
    """
    env = await _recompute_policy_env(api_env)
    response = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"calendar_id": str(env["other_calendar_id"])},
        headers=env["headers"],
    )
    assert response.status_code == 200, response.text

    running = await _row(api_env, env["running"].id)
    assert running is not None
    assert running.calendar_id == env["other_calendar_id"]
    assert running.response_due_at == _NEW_RESPONSE_DUE
    assert running.resolution_due_at == _NEW_RESOLUTION_DUE

    stopped = await _row(api_env, env["stopped"].id)
    assert stopped is not None
    assert stopped.calendar_id == env["calendar_id"]
    assert stopped.response_due_at == _OLD_RESPONSE_DUE
    assert stopped.resolution_due_at == _OLD_RESOLUTION_DUE


async def test_a_policy_edit_never_repoints_policy_id_or_policy_source(api_env) -> None:
    """§0.4's door stays shut. What that rule forbids is an ESCALATION
    changing WHICH policy applies, because a later `resume` would then apply
    the escalated target to a ticket nobody re-triaged. The policy-edit
    recompute writes neither `policy_id` nor `policy_source`, so nothing about
    which policy applies -- or why -- moves.

    A RIVAL policy that a re-resolution would prefer is seeded, which is what
    makes this bite: with one wildcard policy in the fixture, an
    implementation that re-resolves returns the same row and the assertion
    passes against the bug (Task 9's trap, verbatim).
    """
    env = await _recompute_policy_env(api_env)
    before = await _row(api_env, env["running"].id)
    assert before is not None
    rival = await _seed_policy(
        api_env,
        env["org_id"],
        calendar_id=env["calendar_id"],
        # More specific than the both-wildcard policy the tickets are under,
        # so §3's resolution order would prefer it.
        priority="normal",
        response_minutes=15,
        resolution_minutes=45,
    )
    assert rival != before.policy_id

    response = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"resolution_target_minutes": _RESOLUTION_MINUTES + 60},
        headers=env["headers"],
    )
    assert response.status_code == 200, response.text

    after = await _row(api_env, env["running"].id)
    assert after is not None
    assert after.policy_id == before.policy_id
    assert after.policy_source == before.policy_source == "initial"
    # The rival's 45 minutes would have produced Mon 09:45; the row's own
    # policy produces a Tuesday.
    assert after.resolution_due_at == datetime(2026, 3, 3, 12, 0, tzinfo=UTC)


async def test_a_policy_activation_toggle_recomputes_nothing(api_env) -> None:
    """`active`, `queue_id` and `priority` decide which policy a FUTURE ticket
    resolves to (spec §3); they cannot move the deadline of a row that already
    points at this policy. `last_recomputed_at` is the witness, because the
    deadline is unchanged either way -- the mirror of the calendar rename
    guard, and the half that makes "recompute on every policy edit" fail."""
    env = await _recompute_policy_env(api_env)
    before = await _row(api_env, env["running"].id)
    assert before is not None

    response = await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{env['policy_id']}",
        json={"active": False},
        headers=env["headers"],
    )
    assert response.status_code == 200, response.text

    after = await _row(api_env, env["running"].id)
    assert after is not None
    assert after.last_recomputed_at == before.last_recomputed_at
    assert after.response_due_at == _OLD_RESPONSE_DUE


async def test_a_failing_recompute_rolls_back_the_whole_calendar_edit(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE `org_session` SHAPE, proved rather than asserted in prose.

    `calendars.update_calendar` audits and flushes the timezone change and
    THEN recomputes. If the handler caught `SlaValidationError` INSIDE the
    `org_session` block and let the block exit normally, `org_session` would
    COMMIT -- leaving the timezone changed, an `sla.calendar_updated` audit row
    written, and the backlog still measured against the old calendar, with
    nothing recording which rows were recomputed and which were not.

    The recompute is monkeypatched to raise the typed error the handler is
    written to catch, which is the only way to reach that ordering: every
    ordinary refusal in this module happens BEFORE any write, so it cannot
    tell the two shapes apart.
    """
    env = await _recompute_env(api_env)
    original_timezone = (await _calendar_row(api_env, env["calendar_id"])).timezone
    audits_before = await _audit_actions(api_env, env["org_id"])

    async def _boom(session: Any, org_id: UUID, calendar_id: UUID) -> dict[str, int]:
        raise SlaValidationError("recompute exploded")

    # `calendars._recompute_open_clocks` imports the name INSIDE the function
    # (the cycle with `sweep` makes a module-level import impossible), so the
    # lookup happens at call time and patching the module attribute is what
    # reaches it.
    monkeypatch.setattr(sweep_module, "recompute_for_calendar_change", _boom)
    response = await api_env.client.patch(
        f"{SLA_PREFIX}/calendars/{env['calendar_id']}",
        json={"timezone": "Asia/Tokyo"},
        headers=env["headers"],
    )
    monkeypatch.undo()

    assert response.status_code == 400, response.text
    assert (await _calendar_row(api_env, env["calendar_id"])).timezone == original_timezone
    assert await _audit_actions(api_env, env["org_id"]) == audits_before
    still = await _row(api_env, env["running"].id)
    assert still is not None
    assert still.response_due_at == _OLD_RESPONSE_DUE


async def test_the_recompute_through_the_route_does_not_move_another_orgs_deadlines(
    api_env,
) -> None:
    """Invariant #5 END TO END, through the route: two orgs hold
    identically-shaped calendars and identically-tracked tickets, and editing
    ONE org's calendar must not touch the other's deadlines.

    WHAT THIS TEST DOES AND DOES NOT PROVE, stated because its first draft
    claimed the second half and was wrong (Task 11 review F3). It proves the
    ROUTE is tenant-safe -- the whole `PUT /windows` -> `replace_windows` ->
    `recompute_for_calendar_change` chain, under the tenant the request was
    authenticated as. It does NOT prove
    `recompute_for_calendar_change`'s own `TicketSla.org_id` predicate: the
    handler opens `org_session`, so the WRITE ran under RLS and could not have
    reached another tenant whatever the query said. Reading the neighbour's
    rows back on the BYPASSRLS admin engine does not change that -- what is
    read on cannot rescue what was written under. Deleting the predicate
    leaves this test green.

    That predicate is pinned where it can actually be exercised:
    `test_sla_sweep.py`'s
    `test_tenancy_recompute_for_{calendar,policy}_change_org_filter_is_load_bearing`,
    which run the recompute ITSELF on the admin engine over a batch containing
    a row another org owns.

    The neighbour org is still load-bearing here for a different reason: with
    one org in the fixture, a `replace_windows` that recomputed the whole
    TABLE would have nothing to damage and this test would pass against it.
    """
    mine = await _recompute_env(api_env)
    theirs = await _recompute_env(api_env)
    assert mine["org_id"] != theirs["org_id"]

    response = await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{mine['calendar_id']}/windows",
        json=_windows_body(_MON_FRI_9_TO_1),
        headers=mine["headers"],
    )
    assert response.status_code == 200, response.text

    moved = await _row(api_env, mine["running"].id)
    assert moved is not None
    assert moved.response_due_at == _NEW_RESPONSE_DUE

    untouched = await _row(api_env, theirs["running"].id)
    assert untouched is not None
    assert untouched.response_due_at == _OLD_RESPONSE_DUE
    assert untouched.resolution_due_at == _OLD_RESOLUTION_DUE


# --------------------------------------------------------------------------- #
# Audit (invariant #4, spec §8).
# --------------------------------------------------------------------------- #


async def test_every_mutation_is_audited(api_env) -> None:
    """§8's audited list, driven end-to-end through the routes: calendar
    create/update/delete, windows, holidays, policy create/update/delete, and
    the settings change."""
    org_id = await api_env.create_org(f"sla-audit-{uuid4().hex[:8]}")
    headers = await _admin(api_env, org_id)

    calendar_id = (
        await api_env.client.post(
            f"{SLA_PREFIX}/calendars",
            json={"name": "Audited", "timezone": LONDON},
            headers=headers,
        )
    ).json()["id"]
    await api_env.client.patch(
        f"{SLA_PREFIX}/calendars/{calendar_id}", json={"name": "Audited II"}, headers=headers
    )
    await api_env.client.put(
        f"{SLA_PREFIX}/calendars/{calendar_id}/windows",
        json=_windows_body(_MON_FRI_9_TO_5),
        headers=headers,
    )
    holiday_id = (
        await api_env.client.post(
            f"{SLA_PREFIX}/calendars/{calendar_id}/holidays",
            json={"holiday_date": "2026-12-25", "name": "Christmas"},
            headers=headers,
        )
    ).json()["id"]
    await api_env.client.delete(
        f"{SLA_PREFIX}/calendars/{calendar_id}/holidays/{holiday_id}", headers=headers
    )
    policy_id = (
        await api_env.client.post(
            f"{SLA_PREFIX}/policies",
            json={
                "calendar_id": calendar_id,
                "response_target_minutes": 60,
                "resolution_target_minutes": 480,
            },
            headers=headers,
        )
    ).json()["id"]
    await api_env.client.patch(
        f"{SLA_PREFIX}/policies/{policy_id}",
        json={"response_target_minutes": 90},
        headers=headers,
    )
    await api_env.client.delete(f"{SLA_PREFIX}/policies/{policy_id}", headers=headers)
    await api_env.client.put(
        f"{SLA_PREFIX}/settings", json={"auto_close_after_days": 5}, headers=headers
    )
    await api_env.client.delete(f"{SLA_PREFIX}/calendars/{calendar_id}", headers=headers)

    assert await _audit_actions(api_env, org_id) == [
        "sla.calendar_created",
        "sla.calendar_updated",
        "sla.calendar_windows_replaced",
        "sla.calendar_holiday_added",
        "sla.calendar_holiday_removed",
        "sla.policy_created",
        "sla.policy_updated",
        "sla.policy_deleted",
        "sla.settings_updated",
        "sla.calendar_deleted",
    ]


async def test_the_ticket_view_names_its_policy_and_calendar(api_env) -> None:
    """The ticket page shows names, not ids. The ids stay for older clients."""
    env = await _recompute_env(api_env)
    calendar = await _calendar_row(api_env, env["calendar_id"])
    body = (
        await api_env.client.get(
            f"/api/v1/tickets/{env['running'].id}/sla", headers=env["headers"]
        )
    ).json()
    assert body["calendar_name"] == calendar.name
    assert body["policy_name"] == "Every ticket"
    assert body["policy_id"] == str(env["policy_id"])
    assert body["calendar_id"] == str(env["calendar_id"])

    untracked = await _seed_ticket(api_env, env["org_id"])
    absent = (
        await api_env.client.get(f"/api/v1/tickets/{untracked.id}/sla", headers=env["headers"])
    ).json()
    assert absent["policy_name"] is None
    assert absent["calendar_name"] is None


async def test_a_queue_scoped_policy_is_named_after_its_queue(api_env) -> None:
    env = await _recompute_env(api_env)
    queue_id = await _seed_queue(api_env, env["org_id"])
    async with AsyncSession(api_env.admin_engine) as session:
        queue_name = (
            await session.execute(select(TicketQueue.name).where(TicketQueue.id == queue_id))
        ).scalar_one()
        await session.execute(
            update(SlaPolicy)
            .where(SlaPolicy.id == env["policy_id"])
            .values(queue_id=queue_id, priority="urgent")
        )
        await session.commit()
    body = (
        await api_env.client.get(
            f"/api/v1/tickets/{env['running'].id}/sla", headers=env["headers"]
        )
    ).json()
    assert body["policy_name"] == f"Urgent tickets in the {queue_name} queue"


@pytest.mark.parametrize(
    ("queue_name", "priority", "expected"),
    [
        ("Hardware", "urgent", "Urgent tickets in the Hardware queue"),
        ("Hardware", "high", "High priority tickets in the Hardware queue"),
        ("Hardware", None, "Every ticket in the Hardware queue"),
        (None, "low", "Low priority tickets in any queue"),
        (None, None, "Every ticket"),
    ],
)
def test_a_policy_is_named_by_what_it_matches(
    queue_name: str | None, priority: str | None, expected: str
) -> None:
    assert describe_policy(queue_name=queue_name, priority=priority) == expected

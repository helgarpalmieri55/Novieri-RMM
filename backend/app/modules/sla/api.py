"""SLA admin API (docs/modules/sla/spec.md §9).

Seventeen route registrations: sixteen under `/api/v1/sla` and one -- the
per-ticket read -- under `/api/v1/tickets/{ticket_id}/sla`. `register(app)`
mounts this router under `/api/v1`; this module dispatches no agent job and
registers no job type (spec §1: control-plane only, no signed-job path).

**RBAC (invariant #6, spec §8/§9).** Every route depends on
`require_permission` DIRECTLY: reads take `sla.read`, every mutation takes
`sla.admin`, and the per-ticket read takes **`ticket.read`**. That last one is
the deliberate exception, not an oversight -- a technician looking at a ticket
needs to see its deadline without holding SLA administration rights, and
`sla.admin` is high-privilege enough (it sets what the organisation has
promised its customers, and can silence breach escalation entirely) that
handing it out to make deadlines visible would be the wrong trade. Gating that
route on `sla.read` instead would have the same effect one tier down, since
§8 seeds `sla.read` to admin roles only. Completeness is DERIVED from the
router by `app.core.rbac.route_permission` (`tests/test_sla_api.py`), not from
a hand-maintained list a later route addition could silently miss.
`org_id`/`actor_id` always come from `AuthContext`, never from the body or the
path (invariant #5).

**ROUTE ORDERING** (the Phase-11/12 lesson, carried through the ticket core,
the notification module and email intake): literal segments before `{id}`
parameters. `/sla/calendars` and `/sla/policies` (collections) precede their
by-id routes, and `/sla/settings` is a literal sibling that must not be
reachable as a `{calendar_id}`.

**ERROR TRANSLATION HAPPENS OUTSIDE THE `org_session` BLOCK**, exactly as
`email_intake.api` documents. `org_session` wraps `session.begin()`, so the
transaction commits when the block exits NORMALLY and rolls back when any
exception leaves it. Catching inside would not orphan anything BY ITSELF --
`_http_error` re-raises, and an `HTTPException` still leaves the block -- but
it makes the fatal variant writable: catch, then let the block exit normally
(return a value, `pass`, stash-and-raise-after), and a partially applied edit
commits. This project shipped that defect once already. With the `except`
outside there is no point at which a caught error can reach the end of the
block, so the mistake is structurally impossible rather than merely unwritten.

The stake here is concrete and larger than it looks, because Task 11 wired
§7's recompute INTO the mutators: `replace_windows`, `add_holiday`,
`remove_holiday`, `update_calendar` (timezone) and `update_policy` (targets)
each rewrite the deadlines of every open ticket on the calendar or policy they
touched, in the same transaction. A handler that swallowed an error mid-way
would commit an edit whose recompute half did not finish -- half the org's
backlog measured against the new calendar and half against the old, with
nothing recording which is which.

**NOT-MINE IS 404, NEVER 403.** `SlaNotFoundError` -- raised indistinguishably
for "absent" and "belongs to another org" by `calendars.get_calendar`,
`policies.get_policy`, `calendars.remove_holiday` and
`views.ticket_sla_state` -- becomes 404. A 403 would confirm the id exists
somewhere, which is an org-membership oracle. Every other
`SlaValidationError` becomes 400. Routing is by TYPE, never by matching
exception-message prose (mirrors `service_desk.api._http_error`).

**BUSINESS LOGIC IS NOT HERE.** Calendars are `calendars.py`, policies are
`policies.py`, the auto-close setting is `settings.py`, and the derived
reads -- a target's met/breached/paused state, a calendar's health, the count
of tickets with no SLA -- are `views.py`. These handlers resolve the
principal, call one service function, and map errors.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.tenancy import org_session
from app.modules.sla.calendars import (
    add_holiday,
    create_calendar,
    delete_calendar,
    get_calendar,
    list_calendars,
    remove_holiday,
    replace_windows,
    update_calendar,
)
from app.modules.sla.errors import SlaNotFoundError, SlaValidationError
from app.modules.sla.models import SlaCalendar, SlaCalendarHoliday, SlaCalendarWindow, TicketSla
from app.modules.sla.policies import (
    create_policy,
    delete_policy,
    get_policy,
    list_policies,
    update_policy,
)
from app.modules.sla.schema import (
    CalendarCreate,
    CalendarDetail,
    CalendarOut,
    CalendarUpdate,
    HolidayCreate,
    HolidayOut,
    PolicyCreate,
    PolicyOut,
    PolicyUpdate,
    SettingsOut,
    SettingsUpdate,
    TicketSlaOut,
    WindowOut,
    WindowsReplace,
)
from app.modules.sla.settings import (
    get_auto_close_after_days,
    get_settings_row,
    set_auto_close_after_days,
)
from app.modules.sla.views import (
    CalendarHealth,
    calendar_health,
    calendar_health_by_id,
    count_stale_ticket_sla,
    count_tickets_without_sla,
    policy_and_calendar_names,
    target_state,
    ticket_sla_state,
)

router = APIRouter(tags=["sla"])


def _http_error(exc: SlaValidationError) -> HTTPException:
    """By TYPE, never by message text. `SlaNotFoundError` (a subclass) is 404
    -- and it is the SAME error for "absent" and "another org's", so this adds
    no oracle; every other `SlaValidationError` is 400."""
    if isinstance(exc, SlaNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _calendar_out(row: SlaCalendar, health: CalendarHealth) -> CalendarOut:
    return CalendarOut(
        id=row.id,
        name=row.name,
        timezone=row.timezone,
        active=row.active,
        health=health,
        created_at=row.created_at,
    )


def _calendar_detail(
    row: SlaCalendar, windows: list[SlaCalendarWindow], health: CalendarHealth
) -> CalendarDetail:
    return CalendarDetail(
        id=row.id,
        name=row.name,
        timezone=row.timezone,
        active=row.active,
        health=health,
        created_at=row.created_at,
        windows=[WindowOut.model_validate(window) for window in sorted_windows(windows)],
    )


def sorted_windows(windows: list[SlaCalendarWindow]) -> list[SlaCalendarWindow]:
    """Weekday then start time -- the order the §9.1 weekly grid reads in. The
    service returns `replace_windows`' rows in the order they were SUPPLIED,
    which the client chose and the grid should not inherit."""
    return sorted(windows, key=lambda window: (window.weekday, window.start_time))


def _ticket_sla_out(
    ticket_id: UUID,
    row: TicketSla | None,
    names: tuple[str | None, str | None] = (None, None),
) -> TicketSlaOut:
    """The ONLY `TicketSlaOut` builder. Both state strings come from
    `views.target_state` (spec §9.1: the server owns whether a deadline was
    met), and every instant is a STORED column -- nothing here is recomputed
    at read time, so two reads of an untouched row can never disagree."""
    if row is None:
        return TicketSlaOut(
            ticket_id=ticket_id,
            has_sla=False,
            response_state=target_state(None, target="response"),
            resolution_state=target_state(None, target="resolution"),
        )
    return TicketSlaOut(
        ticket_id=ticket_id,
        has_sla=True,
        response_state=target_state(row, target="response"),
        resolution_state=target_state(row, target="resolution"),
        policy_id=row.policy_id,
        calendar_id=row.calendar_id,
        policy_name=names[0],
        calendar_name=names[1],
        response_due_at=row.response_due_at,
        resolution_due_at=row.resolution_due_at,
        response_met_at=row.response_met_at,
        resolution_met_at=row.resolution_met_at,
        response_breached_at=row.response_breached_at,
        resolution_breached_at=row.resolution_breached_at,
        paused_at=row.paused_at,
        paused_minutes_total=row.paused_minutes_total,
        voided_at=row.voided_at,
        escalated_at=row.escalated_at,
        policy_source=row.policy_source,
    )


async def _load_windows(
    session: AsyncSession, org_id: UUID, calendar_id: UUID
) -> list[SlaCalendarWindow]:
    """This module's only direct window read. Org-scoped explicitly
    (invariant #5) as well as calendar-scoped: `sla_calendar_window` has no
    org-aware constraint tying it to its calendar's org."""
    return list(
        (
            await session.execute(
                select(SlaCalendarWindow).where(
                    SlaCalendarWindow.org_id == org_id,
                    SlaCalendarWindow.calendar_id == calendar_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def _load_holidays(
    session: AsyncSession, org_id: UUID, calendar_id: UUID
) -> list[SlaCalendarHoliday]:
    """Same explicit `org_id` predicate, same reason."""
    return list(
        (
            await session.execute(
                select(SlaCalendarHoliday)
                .where(
                    SlaCalendarHoliday.org_id == org_id,
                    SlaCalendarHoliday.calendar_id == calendar_id,
                )
                .order_by(SlaCalendarHoliday.holiday_date)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# GET/POST /sla/calendars (spec §2.1, §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/calendars", response_model=list[CalendarOut])
async def list_calendars_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> list[CalendarOut]:
    """Every calendar with its health flag -- see `views.py` on why a
    misconfigured calendar must be visible from the list rather than only
    discoverable by a ticket failing to get an SLA row."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_calendars(session, ctx.org_id)
        health = await calendar_health_by_id(session, ctx.org_id, rows)
        return [_calendar_out(row, health[row.id]) for row in rows]


@router.post("/sla/calendars", status_code=status.HTTP_201_CREATED, response_model=CalendarOut)
async def create_calendar_route(
    body: CalendarCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> CalendarOut:
    try:
        # The error must escape the `org_session` block so the transaction
        # rolls back -- see the module docstring.
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            calendar = await create_calendar(
                session,
                ctx.org_id,
                ctx.user_id,
                name=body.name,
                timezone=body.timezone,
                active=body.active,
            )
            # A calendar is created with no windows, so this is
            # `no_working_windows` by construction -- reported rather than
            # hidden, because that IS the state it is in until the windows are
            # PUT, and it is the state that makes tickets get no SLA row.
            out = _calendar_out(
                calendar, calendar_health(timezone=calendar.timezone, window_count=0)
            )
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/PATCH/DELETE /sla/calendars/{calendar_id} (spec §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/calendars/{calendar_id}", response_model=CalendarDetail)
async def get_calendar_route(
    calendar_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> CalendarDetail:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            calendar = await get_calendar(session, ctx.org_id, calendar_id)
            windows = await _load_windows(session, ctx.org_id, calendar_id)
            out = _calendar_detail(
                calendar,
                windows,
                calendar_health(timezone=calendar.timezone, window_count=len(windows)),
            )
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.patch("/sla/calendars/{calendar_id}", response_model=CalendarDetail)
async def update_calendar_route(
    calendar_id: UUID,
    body: CalendarUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> CalendarDetail:
    """A timezone change recomputes every still-running clock on this calendar
    (spec §7), inside this transaction -- `calendars.update_calendar` owns
    that, and it is why an error here must roll the whole thing back."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            calendar = await update_calendar(
                session, ctx.org_id, ctx.user_id, calendar_id, body.supplied()
            )
            windows = await _load_windows(session, ctx.org_id, calendar_id)
            out = _calendar_detail(
                calendar,
                windows,
                calendar_health(timezone=calendar.timezone, window_count=len(windows)),
            )
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.delete("/sla/calendars/{calendar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calendar_route(
    calendar_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> None:
    """A calendar referenced by a policy or by any `ticket_sla` row cannot be
    deleted; the 400 names them (spec §9)."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_calendar(session, ctx.org_id, ctx.user_id, calendar_id)
    except SlaValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# PUT /sla/calendars/{calendar_id}/windows (spec §9)
# --------------------------------------------------------------------------- #


@router.put("/sla/calendars/{calendar_id}/windows", response_model=list[WindowOut])
async def replace_windows_route(
    calendar_id: UUID,
    body: WindowsReplace,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> list[WindowOut]:
    """Windows are replaced as a SET, never individually (spec §9): a
    weekday's windows only make sense together, and a partial edit can leave a
    calendar with overlapping or inverted windows -- which the clock has no
    defined behaviour for. The whole set is validated before any row is
    touched, and every still-running deadline on this calendar is recomputed
    in the same transaction (spec §7)."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            rows = await replace_windows(
                session,
                ctx.org_id,
                ctx.user_id,
                calendar_id,
                [(w.weekday, w.start_time, w.end_time) for w in body.windows],
            )
            out = [WindowOut.model_validate(row) for row in sorted_windows(rows)]
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/POST /sla/calendars/{calendar_id}/holidays (spec §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/calendars/{calendar_id}/holidays", response_model=list[HolidayOut])
async def list_holidays_route(
    calendar_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> list[HolidayOut]:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            # The calendar is loaded first so a cross-org id 404s instead of
            # returning an empty list -- "no holidays" and "not your calendar"
            # are different answers.
            await get_calendar(session, ctx.org_id, calendar_id)
            rows = await _load_holidays(session, ctx.org_id, calendar_id)
            out = [HolidayOut.model_validate(row) for row in rows]
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.post(
    "/sla/calendars/{calendar_id}/holidays",
    status_code=status.HTTP_201_CREATED,
    response_model=HolidayOut,
)
async def add_holiday_route(
    calendar_id: UUID,
    body: HolidayCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> HolidayOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            holiday = await add_holiday(
                session,
                ctx.org_id,
                ctx.user_id,
                calendar_id,
                holiday_date=body.holiday_date,
                name=body.name,
            )
            out = HolidayOut.model_validate(holiday)
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# DELETE /sla/calendars/{calendar_id}/holidays/{holiday_id} (spec §9)
# --------------------------------------------------------------------------- #


@router.delete(
    "/sla/calendars/{calendar_id}/holidays/{holiday_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_holiday_route(
    calendar_id: UUID,
    holiday_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> None:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await remove_holiday(session, ctx.org_id, ctx.user_id, calendar_id, holiday_id)
    except SlaValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/POST /sla/policies (spec §2.4, §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/policies", response_model=list[PolicyOut])
async def list_policies_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> list[PolicyOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_policies(session, ctx.org_id)
        return [PolicyOut.model_validate(row) for row in rows]


@router.post("/sla/policies", status_code=status.HTTP_201_CREATED, response_model=PolicyOut)
async def create_policy_route(
    body: PolicyCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.admin")),
) -> PolicyOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            policy = await create_policy(
                session,
                ctx.org_id,
                ctx.user_id,
                queue_id=body.queue_id,
                priority=body.priority,
                calendar_id=body.calendar_id,
                response_target_minutes=body.response_target_minutes,
                resolution_target_minutes=body.resolution_target_minutes,
                escalate_priority=body.escalate_priority,
                escalate_to_queue_id=body.escalate_to_queue_id,
                active=body.active,
            )
            out = PolicyOut.model_validate(policy)
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/PATCH/DELETE /sla/policies/{policy_id} (spec §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/policies/{policy_id}", response_model=PolicyOut)
async def get_policy_route(
    policy_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> PolicyOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            out = PolicyOut.model_validate(await get_policy(session, ctx.org_id, policy_id))
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.patch("/sla/policies/{policy_id}", response_model=PolicyOut)
async def update_policy_route(
    policy_id: UUID,
    body: PolicyUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.admin")),
) -> PolicyOut:
    """A change to either target or to `calendar_id` recomputes every
    still-running clock under this policy, in this transaction (spec §7) --
    and an affected row adopts the policy's `calendar_id`. See
    `sweep.recompute_for_policy_change` for that decision and for why it does
    not reopen §0.4's door."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            policy = await update_policy(
                session, ctx.org_id, ctx.user_id, policy_id, body.supplied()
            )
            out = PolicyOut.model_validate(policy)
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.delete("/sla/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy_route(
    policy_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.admin")),
) -> None:
    """Deleting a policy leaves existing `ticket_sla` rows intact -- so a
    policy that any of them still references is REFUSED with a 400 naming the
    tickets, and `active=False` is the retirement path (spec §9,
    `policies.delete_policy`). Rewriting history because an admin deleted a
    row would destroy the audit value of the record."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_policy(session, ctx.org_id, ctx.user_id, policy_id)
    except SlaValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/PUT /sla/settings (spec §2.6, §9)
# --------------------------------------------------------------------------- #


@router.get("/sla/settings", response_model=SettingsOut)
async def get_settings_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("sla.read")),
) -> SettingsOut:
    """The auto-close window, whether it was ever configured, and the two
    misconfiguration counts: non-terminal tickets with no SLA row, and running
    clocks whose stored deadline the current configuration would not produce.

    Neither number is behind a route of its own: spec §9's table is the
    authoritative route list, and this is the SLA administration screen's own
    payload. See `views.py` for why they have to exist at all --
    `tracking.sla_misconfiguration_is_not_fatal` degrades a broken calendar to
    "no SLA row" and logs, and `sweep`'s recompute contains a per-row failure
    and counts it, and until each was surfaced nothing in the product could
    see the result.

    WHY `stale_ticket_sla` IS A STANDING MEASURE AND NOT A FIELD ON THE EDIT
    THAT CAUSED IT. A policy or calendar edit whose recompute failed on every
    row still returns 200 with the new configuration, and that is correct:
    the edit itself succeeded and is durable, the per-row savepoint
    containment that let it succeed is deliberate (`sweep._recompute_rows`),
    and an error status would tell the admin their edit did not apply when it
    did. Returning the failed count on `PolicyOut`/the calendar responses was
    the alternative and was rejected: it is a transient number on a resource
    representation that `GET`/list also return, where it would be meaningless,
    and it is visible only to whoever made that one request at that one
    moment -- so an edit made by an automation, or by an admin who closed the
    tab, would still leave nothing behind. The whole defect is that the
    failure left no durable trace; a field on the mutation response leaves one
    only if somebody happened to be reading. This number persists until the
    rows are actually recomputed, on the screen the fix is made from.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = await get_settings_row(session, ctx.org_id)
        return SettingsOut(
            auto_close_after_days=await get_auto_close_after_days(session, ctx.org_id),
            configured=row is not None,
            tickets_without_sla=await count_tickets_without_sla(session, ctx.org_id),
            stale_ticket_sla=await count_stale_ticket_sla(session, ctx.org_id),
        )


@router.put("/sla/settings", response_model=SettingsOut)
async def update_settings_route(
    body: SettingsUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("sla.admin")),
) -> SettingsOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await set_auto_close_after_days(
                session, ctx.org_id, ctx.user_id, days=body.auto_close_after_days
            )
            out = SettingsOut(
                auto_close_after_days=row.auto_close_after_days,
                configured=True,
                tickets_without_sla=await count_tickets_without_sla(session, ctx.org_id),
                stale_ticket_sla=await count_stale_ticket_sla(session, ctx.org_id),
            )
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET /tickets/{ticket_id}/sla -- gated on `ticket.read` (spec §9)
# --------------------------------------------------------------------------- #


@router.get("/tickets/{ticket_id}/sla", response_model=TicketSlaOut)
async def get_ticket_sla_route(
    ticket_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> TicketSlaOut:
    """A ticket's response and resolution state: due, met, breached, paused or
    void (spec §9.1).

    **`ticket.read`, not `sla.read`** -- the one deliberate deviation from
    this module's own permission pair. A technician looking at a ticket needs
    to see its deadline without holding SLA administration rights, and
    `sla.read`/`sla.admin` are both seeded to admin roles only (spec §8), so
    gating this on either would make every ticket's deadline invisible to the
    people working the ticket.

    A ticket with no SLA row is `200 {"has_sla": false, ...}`, NOT a 404: spec
    §3 requires "no SLA" to be a visible, legitimate state, and conflating it
    with "no such ticket" would both hide the misconfiguration this task
    exists to surface and make the 404 useless. A ticket that does not exist,
    or belongs to another org, is the 404.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            _, row = await ticket_sla_state(session, ctx.org_id, ticket_id)
            names = (
                await policy_and_calendar_names(session, ctx.org_id, row)
                if row is not None
                else (None, None)
            )
            out = _ticket_sla_out(ticket_id, row, names)
    except SlaValidationError as exc:
        raise _http_error(exc) from None
    return out

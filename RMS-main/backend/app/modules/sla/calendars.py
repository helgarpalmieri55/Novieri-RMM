"""Calendar CRUD: windows, holidays, and the bridge to the pure clock
(docs/modules/sla/spec.md §2.1-2.3, §9).

Mirrors `app.modules.email_intake.mailboxes`/`app.modules.budget.budgets`'s
shape: an explicit `org_id` predicate on every query (invariant #5),
audit-then-flush on every mutation (invariant #4), and `ON CONFLICT DO
NOTHING` + `RETURNING id` on create rather than check-then-insert -- this
project has run two sweeps to remove that bug class (see
`create_calendar`).

TIMEZONE VALIDATION (spec §2.1). `_validate_timezone` resolves the name
through `zoneinfo.ZoneInfo` at every write (create, and update when
`timezone` is supplied) and converts a failure into a typed
`SlaValidationError`. `zoneinfo.ZoneInfo` fails THREE distinct ways, all
caught: `ZoneInfoNotFoundError` (a `KeyError` subclass) for a well-formed but
unknown name; a plain `ValueError` for a malformed key -- `''`, an absolute
path (`/etc/localtime`), or a `..`-containing, path-traversal-shaped name
(`Europe/../../etc/passwd`) are all `ValueError`, NOT `ZoneInfoNotFoundError`
(Task 3 review F1: the original guard caught only the latter, so these three
escaped as a raw 500 from an `sla.admin` API body -- the traversal-shaped one
unrefused); and a `TypeError` for a non-string key, which mypy's `tz: str`
annotation does not enforce at runtime. Uncaught, any of the three is a raw
500 on this write path and, later, an abort of the whole sweep the first
time it loads this calendar. A bad timezone must be impossible to store,
never something the sweep discovers.

WINDOW VALIDATION (spec §2.2, §9). `replace_windows` replaces a calendar's
entire window set in one transaction: every window is validated BEFORE any
row is touched (`_validate_windows` runs first, mirroring
`budget.budgets.put_lines`'s two-pass shape), so a batch that fails validation
leaves the calendar's existing windows untouched rather than half-replaced.
`end_time == time(0, 0)` is legal and means midnight-end-of-day -- the only
way to express an evening shift's second half or a genuine 24-hour day
(`app/modules/sla/calendar.py`'s `CalendarSpec` docstring, migration 0035).
The DB CHECK (`ck_sla_calendar_window_start_before_end`, migration 0035) is
`start_time < end_time OR end_time = '00:00:00'` for exactly this reason --
this function encodes the same rule in Python rather than deferring to the
DB error shape, so a bad window is a typed `SlaValidationError`, not an
`IntegrityError`.

CASCADE VS RESTRICT ON DELETE (spec §9). `sla_calendar_window.calendar_id`,
`sla_calendar_holiday.calendar_id`, `sla_policy.calendar_id`, and
`ticket_sla.calendar_id` are all plain (RESTRICT) foreign keys -- migration
0033 sets no `ondelete` on any of them. `delete_calendar` treats these two
families differently on purpose:

- Windows and holidays are the calendar's OWN configuration, not independent
  objects with their own identity or history worth preserving. Deleting a
  calendar therefore deletes its windows and holidays first, in the same
  transaction, as an application-level cascade -- the RESTRICT FK exists to
  stop an accidental orphan, not to require every caller to clean up two
  child tables by hand before every calendar delete.
- Policies are independent, audited objects that reference a calendar; their
  `policy_id` shows up in `ticket_sla` rows as a historical record of what
  was promised. `delete_calendar` never touches them. Spec §9 says an
  ACTIVE policy blocks deletion outright, naming the policies. An INACTIVE
  policy is not mentioned by the spec, but the FK does not distinguish
  active from inactive rows -- Postgres RESTRICT would refuse the DELETE
  either way, so leaving it to surface as a raw `IntegrityError` for
  inactive-only references (while active references get a clean typed
  error) would be an arbitrary difference in error quality for the same
  underlying refusal. `delete_calendar` therefore pre-checks and names BOTH:
  active references block with the spec's exact reasoning, and inactive-only
  references block too, with a typed error that says so, so the DB's
  RESTRICT is never the thing the caller has to interpret.
- `ticket_sla` rows are independent history too (Task 3 review F2): a
  `ticket_sla.calendar_id` reference is exactly the ordinary shape spec §9
  itself describes surviving a policy delete -- one calendar retired,
  tickets already promised a deadline under it kept their record of what
  that deadline was. The original pre-check named only `sla_policy`, so a
  same-org `ticket_sla` reference fell through to the generic cross-org
  fallback below and was reported with a message that is simply FALSE for
  this case ("referenced by another record this organization cannot see" --
  the admin can see it perfectly well). `delete_calendar` now pre-checks
  `ticket_sla` beside `sla_policy` and names the real reason.

EVERY EDIT THAT MOVES A DEADLINE RECOMPUTES THE OPEN TICKETS ON THIS
CALENDAR (spec §7, wired in Task 11). `response_due_at`/`resolution_due_at`
are MATERIALISED instants derived from this calendar; move the windows, add a
holiday or fix a wrong timezone and every stored deadline on it is an answer
to a question nobody asked. `_recompute_open_clocks` below calls
`sweep.recompute_for_calendar_change` from `replace_windows`, `add_holiday`,
`remove_holiday`, and from `update_calendar` WHEN the timezone actually
changed -- inside the same transaction as the edit, so the edit and the
recompute commit or roll back together. A rename or an `active` toggle does
NOT call it: neither is an input to `add_working_minutes`, so recomputing
would be guaranteed churn on every row. Only still-running clocks are
touched; that rule lives in `sweep._recompute_row`, not here.

The import is function-local, and it has to be: `sweep` imports
`calendars.load_calendar_spec` at module scope, so a module-level import back
would be a cycle. Same discipline as `app.modules.*.register`'s deferred
`api` import.

FALLBACK NARROWING (Task 3 review F5, extended by Task 11 review F5). BOTH
`try/except IntegrityError` blocks in this module -- `delete_calendar`'s and
`update_calendar`'s -- wrap a `session.flush()`, and a flush emits the
CALLER'S whole unit of work, not only the statement the block is about. An
unrelated unique/FK violation from something the caller `session.add`ed
earlier therefore arrives at these handlers too, and an un-narrowed catch
reports it to the admin as this calendar being blocked (`delete_calendar`) or
as a duplicate calendar name (`update_calendar`) -- a diagnosis that is not
merely unhelpful but wrong, with `from None` erasing the real cause on the way
out.

Each catch therefore inspects `exc.orig`'s underlying asyncpg
`constraint_name` and converts ONLY its own constraint:
`_CALENDAR_INBOUND_FK_CONSTRAINTS` (the four FKs that reference
`sla_calendar.id`) for the delete, `_CALENDAR_NAME_UNIQUE_CONSTRAINT` for the
rename. Everything else re-raises un-mangled, and the original exception is
kept via `from exc` either way. This is the same shape
`policies.update_policy`/`delete_policy` use, and the three are meant to stay
recognisably identical.
"""

import logging
from datetime import date, time
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.sla.calendar import CalendarSpec
from app.modules.sla.errors import SlaNotFoundError, SlaValidationError
from app.modules.sla.models import (
    SlaCalendar,
    SlaCalendarHoliday,
    SlaCalendarWindow,
    SlaPolicy,
    TicketSla,
)

__all__ = [
    "add_holiday",
    "create_calendar",
    "delete_calendar",
    "get_calendar",
    "list_calendars",
    "load_calendar_spec",
    "remove_holiday",
    "replace_windows",
    "update_calendar",
]

logger = logging.getLogger("its.sla.calendars")

_CALENDAR_NON_NULLABLE_FIELDS = frozenset({"name", "timezone", "active"})
_CALENDAR_EDITABLE_FIELDS = ("name", "timezone", "active")
_MINUTES_PER_DAY = 24 * 60


def _validate_timezone(tz: str) -> None:
    """Resolve `tz` through `zoneinfo` and raise a typed error on failure.

    `ZoneInfo(name)` raises `ZoneInfoNotFoundError` (a `KeyError` subclass) for
    an unknown-but-well-formed name, `ValueError` for a malformed key (empty,
    an absolute path, or a `..`-containing path-traversal-shaped name -- Task
    3 review F1), and `TypeError` for a non-string. All three are caught: any
    of them uncaught is a 500 here and a sweep-aborting exception later.
    Caught here, a bad timezone can never be stored (spec §2.1)."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise SlaValidationError(
            tr(
                "{tz} is not a valid IANA timezone name",
                tz=repr(tz),
            )
        ) from exc


_NAME_MAX_LENGTH = 100


def _validate_name_length(name: str, *, field: str = "name") -> None:
    """`sla_calendar.name` and `sla_calendar_holiday.name` are `String(100)`
    (spec §2.1, §2.3); an over-length value would otherwise reach Postgres as
    `StringDataRightTruncationError` -> `DataError`, which neither the ON
    CONFLICT path nor `update_calendar`'s `IntegrityError` catch converts, so
    it is a raw 500 (Task 3 review F7). Rejected here as a typed 400 instead,
    mirroring `_reject_null_for_non_nullable`'s pre-empt-the-DB-shaped-500
    discipline."""
    if len(name) > _NAME_MAX_LENGTH:
        raise SlaValidationError(
            tr(
                "{field} must be at most {limit} characters; got {length}",
                field=field,
                limit=_NAME_MAX_LENGTH,
                length=len(name),
            )
        )


# The four RESTRICT FKs into `sla_calendar.id` (migration 0033), by their
# Postgres-default `<table>_<column>_fkey` constraint names -- confirmed
# against a live database (Task 3 review F5). `delete_calendar`'s fallback
# catch uses this to tell "this calendar is still referenced" (convert to a
# typed error) from "something unrelated in this flush failed" (re-raise
# un-mangled).
_CALENDAR_INBOUND_FK_CONSTRAINTS = frozenset(
    {
        "sla_calendar_window_calendar_id_fkey",
        "sla_calendar_holiday_calendar_id_fkey",
        "sla_policy_calendar_id_fkey",
        "ticket_sla_calendar_id_fkey",
    }
)

# `UNIQUE(org_id, name)` on `sla_calendar` (models.py, migration 0033). Named
# once and used by BOTH the writers that depend on it -- `create_calendar`'s
# `ON CONFLICT` arbiter and `update_calendar`'s fallback narrowing -- because
# Postgres cannot infer the arbiter and the catch cannot recognise the
# violation unless the two strings agree exactly.
_CALENDAR_NAME_UNIQUE_CONSTRAINT = "uq_sla_calendar_org_name"


def _reject_null_for_non_nullable(fields: dict[str, object], non_nullable: frozenset[str]) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `budget.budgets`/`refdata.py` pattern)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise SlaValidationError(tr("{field} cannot be null", field=field))


def _window_minutes(start: time, end: time) -> tuple[int, int]:
    """A window's boundaries as minutes from local midnight.

    `end == time(0, 0)` is the midnight-end-of-day sentinel (spec §2.2,
    migration 0035) and is worth 1440, not 0 -- mirrors
    `app/modules/sla/calendar.py::_merge_windows`'s own conversion so the
    write-time validation and the clock agree on what a window means."""
    start_min = start.hour * 60 + start.minute
    end_min = _MINUTES_PER_DAY if end == time(0, 0) else end.hour * 60 + end.minute
    return start_min, end_min


def _validate_windows(windows: list[tuple[int, time, time]]) -> None:
    """Validate a full replacement window set BEFORE any row is touched.

    Rejects `start_time >= end_time` (except the `end == 00:00`
    midnight-end-of-day sentinel, which is legal -- spec §4,
    `CalendarSpec`'s docstring) and any two windows on the same weekday that
    overlap. Touching windows (one ending exactly where the next starts) are
    NOT an overlap -- `[start, end)` is half-open, matching the clock's own
    `_merge_windows`, so a lunch break split into two adjacent windows is
    ordinary configuration, not an error.
    """
    by_weekday: dict[int, list[tuple[int, int]]] = {}
    for weekday, start, end in windows:
        if not 0 <= weekday <= 6:
            raise SlaValidationError(
                tr(
                    "weekday {weekday} out of range 0-6 (Monday=0)",
                    weekday=weekday,
                )
            )
        start_min, end_min = _window_minutes(start, end)
        if start_min >= end_min:
            raise SlaValidationError(
                tr(
                    "window on weekday {weekday} has start {start} >= end {end}; end_time = "
                    "00:00 (midnight-end-of-day) is the one legal exception",
                    weekday=weekday,
                    start=start,
                    end=end,
                )
            )
        by_weekday.setdefault(weekday, []).append((start_min, end_min))

    for weekday, spans in by_weekday.items():
        running_end: int | None = None
        for start_min, end_min in sorted(spans):
            if running_end is not None and start_min < running_end:
                raise SlaValidationError(
                    tr(
                        "overlapping windows on weekday {weekday}: a window starting at "
                        "minute {start_min} overlaps one already running until {running_end}",
                        weekday=weekday,
                        start_min=start_min,
                        running_end=running_end,
                    )
                )
            running_end = end_min if running_end is None else max(running_end, end_min)


async def _recompute_open_clocks(
    session: AsyncSession, org_id: UUID, calendar_id: UUID, *, because: str
) -> dict[str, int]:
    """Re-derive every still-running deadline materialised on `calendar_id`
    (spec §7) -- see the module docstring for which edits call this and why a
    rename does not.

    Logged, not audited (spec §8). §8's audited list is the calendar/policy/
    settings CHANGE itself, which the caller has already recorded; the
    recompute is that change's mechanical consequence, and a per-edit row
    saying how many derived values moved would grow with the backlog rather
    than with the decisions a human made. The counts still have to be visible
    somewhere, because `recompute_for_calendar_change` contains its own
    per-row failures (an unsatisfiable calendar fails one row, not the pass),
    and a non-zero `failed` is exactly the "this org's calendar is broken and
    nobody knows" blindness Task 11 exists to end -- so it is logged at
    WARNING when anything failed and INFO otherwise.
    """
    # Function-local: `sweep` imports this module. See the module docstring.
    from app.modules.sla.sweep import recompute_for_calendar_change

    counts = await recompute_for_calendar_change(session, org_id, calendar_id)
    log = logger.warning if counts["failed"] else logger.info
    log(
        "sla.calendar %s changed (%s) in org %s: recomputed=%d skipped=%d failed=%d",
        calendar_id,
        because,
        org_id,
        counts["recomputed"],
        counts["skipped"],
        counts["failed"],
    )
    return counts


async def get_calendar(session: AsyncSession, org_id: UUID, calendar_id: UUID) -> SlaCalendar:
    """Org-scoped load. A calendar id from another org is indistinguishable
    from a nonexistent one -- both raise `SlaNotFoundError` (invariant #5)."""
    calendar = (
        await session.execute(
            select(SlaCalendar).where(SlaCalendar.org_id == org_id, SlaCalendar.id == calendar_id)
        )
    ).scalar_one_or_none()
    if calendar is None:
        raise SlaNotFoundError(tr("calendar {calendar_id} not found", calendar_id=calendar_id))
    return calendar


async def list_calendars(session: AsyncSession, org_id: UUID) -> list[SlaCalendar]:
    return list(
        (
            await session.execute(
                select(SlaCalendar).where(SlaCalendar.org_id == org_id).order_by(SlaCalendar.name)
            )
        )
        .scalars()
        .all()
    )


async def create_calendar(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    name: str,
    timezone: str,
    active: bool = True,
) -> SlaCalendar:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` against
    `uq_sla_calendar_org_name`, not check-then-insert: two concurrent creates
    of the same `(org_id, name)` can both miss an in-memory pre-check before
    either has written, and this project has already run two sweeps to
    remove that bug class. The race loser gets the SAME typed
    `SlaValidationError` a pre-check would raise, and never the winner's
    row -- `inserted_id is None` means someone else's create committed
    first."""
    _validate_timezone(timezone)
    _validate_name_length(name)

    result = await session.execute(
        pg_insert(SlaCalendar)
        .values(org_id=org_id, name=name, timezone=timezone, active=active, created_by=actor_id)
        .on_conflict_do_nothing(constraint=_CALENDAR_NAME_UNIQUE_CONSTRAINT)
        .returning(SlaCalendar.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        raise SlaValidationError(
            tr(
                "a calendar named {name} already exists in this org",
                name=repr(name),
            )
        )

    calendar = await get_calendar(session, org_id, inserted_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_created",
        target_type="sla_calendar",
        target_id=str(calendar.id),
        metadata={"name": name, "timezone": timezone, "active": active},
    )
    await session.flush()
    return calendar


async def update_calendar(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    calendar_id: UUID,
    fields: dict[str, object],
) -> SlaCalendar:
    """Apply the SUPPLIED fields (`name`, `timezone`, `active`) to a
    calendar, org-scoped-loaded. Renaming to a name another calendar in this
    org already holds is a typed `SlaValidationError`, never a raw
    `IntegrityError` -- the flush runs inside a SAVEPOINT
    (`session.begin_nested`), opened BEFORE the row is mutated (mirrors
    `email_intake.mailboxes.update_mailbox`: `begin_nested()` autoflushes
    pending dirty state the instant it opens, so mutating first would issue
    the UPDATE *outside* the savepoint).

    That same autoflush is why the catch below is constraint-narrowed and not
    keyed on `renaming` alone: it flushes the CALLER'S whole unit of work, so
    an unrelated violation reaches this handler and must re-raise un-mangled
    (module docstring, FALLBACK NARROWING)."""
    calendar = await get_calendar(session, org_id, calendar_id)

    if "timezone" in fields and fields["timezone"] is not None:
        _validate_timezone(str(fields["timezone"]))
    if "name" in fields and fields["name"] is not None:
        _validate_name_length(str(fields["name"]))
    _reject_null_for_non_nullable(fields, _CALENDAR_NON_NULLABLE_FIELDS)

    renaming = (
        "name" in fields and fields["name"] is not None and fields["name"] != calendar.name
    )

    changed: list[str] = []
    try:
        async with session.begin_nested():
            for field in _CALENDAR_EDITABLE_FIELDS:
                if field not in fields or fields[field] is None:
                    continue
                if getattr(calendar, field) != fields[field]:
                    setattr(calendar, field, fields[field])
                    changed.append(field)
            session.add(calendar)
            await session.flush()
    except IntegrityError as exc:
        # NARROWED, the same discipline as `delete_calendar` below and
        # `policies.update_policy`/`delete_policy` (see the module docstring's
        # "FALLBACK NARROWING" section). `renaming` alone is not enough: the
        # `session.flush()` above flushes the CALLER'S whole unit of work, not
        # just this UPDATE, so an unrelated unique or FK violation from
        # something the caller `session.add`ed earlier arrives here too -- and
        # would be reported to the admin as "a calendar named X already
        # exists", with the real cause erased by `from None`. Only convert
        # when the violated constraint is actually the org-scoped calendar-name
        # unique; everything else re-raises un-mangled, and the cause is
        # preserved (`from exc`) either way.
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if not (renaming and constraint == _CALENDAR_NAME_UNIQUE_CONSTRAINT):
            raise
        raise SlaValidationError(
            tr(
                "a calendar named {name} already exists in this org",
                name=repr(fields["name"]),
            )
        ) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_updated",
        target_type="sla_calendar",
        target_id=str(calendar.id),
        metadata={"changed": sorted(changed)},
    )
    await session.flush()
    # Only the TIMEZONE is an input to the clock (spec §4: both pure functions
    # convert through `zoneinfo` before walking windows). A rename or an
    # `active` toggle cannot move an instant, so recomputing on those would be
    # churn proportional to the org's open backlog for a guaranteed no-op --
    # and `changed` is keyed on the value having ACTUALLY differed, so
    # re-saving the same timezone recomputes nothing either.
    if "timezone" in changed:
        await _recompute_open_clocks(session, org_id, calendar.id, because="timezone changed")
    return calendar


async def delete_calendar(
    session: AsyncSession, org_id: UUID, actor_id: UUID, calendar_id: UUID
) -> None:
    """Refuses if ANY policy in this org references the calendar, active or
    not (see module docstring for why inactive references block too), or if
    any `ticket_sla` row in this org still references it (Task 3 review F2),
    then deletes the calendar's own windows and holidays before the calendar
    row itself -- see module docstring for the RESTRICT-vs-cascade reasoning.
    A reference from a DIFFERENT org (which should never exist, but is not
    ruled out by the FK) is caught as a fallback, below, and refused without
    naming it."""
    calendar = await get_calendar(session, org_id, calendar_id)

    policy_rows = (
        await session.execute(
            select(SlaPolicy.id, SlaPolicy.active).where(
                SlaPolicy.org_id == org_id, SlaPolicy.calendar_id == calendar_id
            )
        )
    ).all()
    if policy_rows:
        active_ids = sorted(str(row.id) for row in policy_rows if row.active)
        inactive_ids = sorted(str(row.id) for row in policy_rows if not row.active)
        if active_ids:
            # Singular and plural are separate templates: Spanish does not make
            # a plural by changing the end of the English noun.
            if len(active_ids) == 1:
                active_template = tr(
                    "calendar {calendar_id} is referenced by active policy {active_ids}; "
                    "deactivate or delete them before deleting this calendar",
                    calendar_id=calendar_id,
                    active_ids=active_ids,
                )
            else:
                active_template = tr(
                    "calendar {calendar_id} is referenced by active policies {active_ids}; "
                    "deactivate or delete them before deleting this calendar",
                    calendar_id=calendar_id,
                    active_ids=active_ids,
                )
            raise SlaValidationError(active_template)
        if len(inactive_ids) == 1:
            inactive_template = tr(
                "calendar {calendar_id} is referenced by inactive policy {inactive_ids}; "
                "the foreign key does not distinguish active from inactive policies, so they "
                "must be deleted or repointed before this calendar can be",
                calendar_id=calendar_id,
                inactive_ids=inactive_ids,
            )
        else:
            inactive_template = tr(
                "calendar {calendar_id} is referenced by inactive policies {inactive_ids}; "
                "the foreign key does not distinguish active from inactive policies, so they "
                "must be deleted or repointed before this calendar can be",
                calendar_id=calendar_id,
                inactive_ids=inactive_ids,
            )
        raise SlaValidationError(inactive_template)

    ticket_ids = (
        await session.execute(
            select(TicketSla.ticket_id).where(
                TicketSla.org_id == org_id, TicketSla.calendar_id == calendar_id
            )
        )
    ).scalars().all()
    if ticket_ids:
        # Review F2: this is the ordinary same-org shape spec §9 itself says
        # survives a policy delete -- a ticket already promised a deadline
        # under this calendar keeps that record. Without this pre-check the
        # DELETE below hits `ticket_sla_calendar_id_fkey` and falls into the
        # generic cross-org fallback, which is FALSE here (this organization
        # can see these tickets perfectly well) and unactionable.
        ids = sorted(str(t) for t in ticket_ids)
        if len(ids) == 1:
            ticket_message = tr(
                "calendar {calendar_id} cannot be deleted: an SLA record exists for "
                "ticket {ids} on this calendar",
                calendar_id=calendar_id,
                ids=ids,
            )
        else:
            ticket_message = tr(
                "calendar {calendar_id} cannot be deleted: an SLA record exists for "
                "tickets {ids} on this calendar",
                calendar_id=calendar_id,
                ids=ids,
            )
        raise SlaValidationError(ticket_message)

    await session.execute(
        delete(SlaCalendarWindow).where(
            SlaCalendarWindow.org_id == org_id, SlaCalendarWindow.calendar_id == calendar_id
        )
    )
    await session.execute(
        delete(SlaCalendarHoliday).where(
            SlaCalendarHoliday.org_id == org_id, SlaCalendarHoliday.calendar_id == calendar_id
        )
    )
    target_id = str(calendar.id)
    name = calendar.name
    await session.delete(calendar)
    try:
        await session.flush()
    except IntegrityError as exc:
        # DEFENSE IN DEPTH, not the primary guard. The pre-checks above are
        # deliberately org-scoped (invariant #5: they must never NAME another
        # org's row to this caller), but the FKs into `sla_calendar.id` have
        # no org concept at all -- each is satisfied by ANY row in ANY org
        # that references this calendar_id. A row like that should never
        # exist under normal operation (Task 4's `create_policy` resolves
        # `calendar_id` org-scoped, the same discipline as
        # `email_intake.mailboxes._resolve_category`), but if one does
        # (a bypassed write, a bug, a manual DB edit), the org-scoped
        # pre-checks above cannot see it and this DELETE would otherwise
        # surface as a raw `IntegrityError` -- a 500 -- instead of the typed
        # error every other refusal in this module raises. The message
        # intentionally does NOT name the referencing row: it belongs to a
        # tenant this caller has no visibility into.
        #
        # NARROWED (Task 3 review F5): this flush can also emit any OTHER
        # pending state the caller added earlier in the same unit of work --
        # an unrelated unique/FK violation from that state must not be
        # mis-reported as "this calendar is blocked". Only convert when the
        # violated constraint is actually one of the four FKs that reference
        # `sla_calendar.id`; everything else re-raises un-mangled, and the
        # cause is preserved (`from exc`, not `from None`) either way.
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint not in _CALENDAR_INBOUND_FK_CONSTRAINTS:
            raise
        raise SlaValidationError(
            tr(
                "calendar {calendar_id} cannot be deleted: it is still referenced by another "
                "record this organization cannot see",
                calendar_id=calendar_id,
            )
        ) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_deleted",
        target_type="sla_calendar",
        target_id=target_id,
        metadata={"name": name},
    )
    await session.flush()


async def replace_windows(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    calendar_id: UUID,
    windows: list[tuple[int, time, time]],
) -> list[SlaCalendarWindow]:
    """Replace a calendar's entire window set in one transaction (spec §9).

    Validates the WHOLE incoming set before any row is touched
    (`_validate_windows`), so a batch that fails leaves the calendar's
    existing windows untouched rather than half-replaced -- a partial edit
    can otherwise leave a calendar with inverted or overlapping windows,
    which the clock has no defined behaviour for."""
    calendar = await get_calendar(session, org_id, calendar_id)
    _validate_windows(windows)

    await session.execute(
        delete(SlaCalendarWindow).where(
            SlaCalendarWindow.org_id == org_id, SlaCalendarWindow.calendar_id == calendar_id
        )
    )
    new_rows = [
        SlaCalendarWindow(
            org_id=org_id,
            calendar_id=calendar.id,
            weekday=weekday,
            start_time=start,
            end_time=end,
        )
        for weekday, start, end in windows
    ]
    session.add_all(new_rows)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_windows_replaced",
        target_type="sla_calendar",
        target_id=str(calendar.id),
        metadata={"window_count": len(new_rows)},
    )
    await session.flush()
    # AFTER the flush, never before: `recompute_for_calendar_change` re-reads
    # the windows through `load_calendar_spec`, so recomputing first would
    # re-derive every deadline from the windows this call just replaced.
    await _recompute_open_clocks(session, org_id, calendar.id, because="windows replaced")
    return new_rows


async def add_holiday(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    calendar_id: UUID,
    *,
    holiday_date: date,
    name: str,
) -> SlaCalendarHoliday:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` against
    `uq_sla_calendar_holiday_cal_date`, the same race-safe shape as
    `create_calendar` -- a duplicate `(calendar_id, holiday_date)` is a typed
    `SlaValidationError`, never a raw `IntegrityError`."""
    calendar = await get_calendar(session, org_id, calendar_id)
    _validate_name_length(name)

    result = await session.execute(
        pg_insert(SlaCalendarHoliday)
        .values(org_id=org_id, calendar_id=calendar.id, holiday_date=holiday_date, name=name)
        .on_conflict_do_nothing(constraint="uq_sla_calendar_holiday_cal_date")
        .returning(SlaCalendarHoliday.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        raise SlaValidationError(
            tr(
                "calendar {calendar_id} already has a holiday on {date}",
                calendar_id=calendar_id,
                date=holiday_date.isoformat(),
            )
        )

    # `org_id` here is defence-in-depth that cannot be exercised by any test
    # (Task 3 review F3): `inserted_id` is this row's own primary key, just
    # generated by the INSERT above with `org_id=org_id`, so no row with a
    # DIFFERENT `org_id` can ever share it -- PK uniqueness alone guarantees
    # the predicate is a no-op on every possible input, not merely one no
    # application path produces. Kept for the same reason every other query
    # in this module carries it (invariant #5), not because it is provably
    # load-bearing here.
    holiday = (
        await session.execute(
            select(SlaCalendarHoliday).where(
                SlaCalendarHoliday.org_id == org_id, SlaCalendarHoliday.id == inserted_id
            )
        )
    ).scalar_one()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_holiday_added",
        target_type="sla_calendar_holiday",
        target_id=str(holiday.id),
        metadata={
            "calendar_id": str(calendar.id),
            "holiday_date": holiday_date.isoformat(),
            "name": name,
        },
    )
    await session.flush()
    await _recompute_open_clocks(session, org_id, calendar.id, because="holiday added")
    return holiday


async def remove_holiday(
    session: AsyncSession, org_id: UUID, actor_id: UUID, calendar_id: UUID, holiday_id: UUID
) -> None:
    """Org- and calendar-scoped delete. A holiday id from another calendar
    (even within the same org) is indistinguishable from a nonexistent one."""
    holiday = (
        await session.execute(
            select(SlaCalendarHoliday).where(
                SlaCalendarHoliday.org_id == org_id,
                SlaCalendarHoliday.calendar_id == calendar_id,
                SlaCalendarHoliday.id == holiday_id,
            )
        )
    ).scalar_one_or_none()
    if holiday is None:
        raise SlaNotFoundError(
            tr(
                "holiday {holiday_id} not found on calendar {calendar_id}",
                holiday_id=holiday_id,
                calendar_id=calendar_id,
            )
        )

    holiday_date_str = holiday.holiday_date.isoformat()
    await session.delete(holiday)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.calendar_holiday_removed",
        target_type="sla_calendar_holiday",
        target_id=str(holiday_id),
        metadata={"calendar_id": str(calendar_id), "holiday_date": holiday_date_str},
    )
    await session.flush()
    await _recompute_open_clocks(session, org_id, calendar_id, because="holiday removed")


async def load_calendar_spec(
    session: AsyncSession, org_id: UUID, calendar_id: UUID
) -> CalendarSpec:
    """The bridge from `sla_calendar`/`sla_calendar_window`/
    `sla_calendar_holiday` rows to Task 2's pure `CalendarSpec` value object
    -- every later task (deadlines, pause, the sweep) calls this rather than
    touching the ORM rows itself.

    `CalendarSpec.windows` is `tuple[tuple[int, time, time], ...]` (weekday,
    start, end) -- plain `time` objects, exactly as `sla_calendar_window`
    stores them; `CalendarSpec.__post_init__` is what derives the internal
    minute-offset grouping (`_by_weekday`) that `calendar.py`'s arithmetic
    actually walks. Passing `time` objects here, not minute integers, is
    required by `CalendarSpec`'s real constructor signature.

    Both child SELECTs carry an explicit `ORDER BY` (Task 3 review F4):
    `CalendarSpec` is a frozen dataclass whose `windows` tuple participates in
    `__eq__`/`__hash__`, so without a deterministic row order two loads of the
    identical calendar could compare unequal -- harmless to the clock (which
    sorts and merges before any arithmetic) but a trap for anything later
    keyed on spec equality, such as a per-spec cache."""
    calendar = await get_calendar(session, org_id, calendar_id)

    window_rows = (
        await session.execute(
            select(
                SlaCalendarWindow.weekday, SlaCalendarWindow.start_time, SlaCalendarWindow.end_time
            )
            .where(SlaCalendarWindow.org_id == org_id, SlaCalendarWindow.calendar_id == calendar_id)
            .order_by(SlaCalendarWindow.weekday, SlaCalendarWindow.start_time)
        )
    ).all()
    holiday_rows = (
        await session.execute(
            select(SlaCalendarHoliday.holiday_date)
            .where(
                SlaCalendarHoliday.org_id == org_id, SlaCalendarHoliday.calendar_id == calendar_id
            )
            .order_by(SlaCalendarHoliday.holiday_date)
        )
    ).scalars().all()

    return CalendarSpec(
        timezone=calendar.timezone,
        windows=tuple((row.weekday, row.start_time, row.end_time) for row in window_rows),
        holidays=frozenset(holiday_rows),
    )

"""Maintenance-window matching: a pure-stdlib cron matcher shared by any
module that gates dispatch on a maintenance window (patch — docs/modules/patch/
spec.md — and deploy).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.core.i18n import tr


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (comma list of `*`, `*/n`, `a-b`, `a-b/n`, or a
    plain int) into the set of matching integers in `[lo, hi]`.

    Raises `ValueError` if any expanded value falls outside `[lo, hi]` — an
    out-of-range value would otherwise silently never match, which could
    disable a device's maintenance window forever with no error.

    A range is judged by its two ends BEFORE it is expanded: the values of
    `range(start, end + 1, step)` all lie between its first and last, so the
    ends decide exactly what expanding then checking decided -- without first
    building a set of every number in `0-999999999`, which held the process
    (and its memory) for as long as that took. What an expression yields is
    unchanged; which out-of-range value the refusal names may differ.
    """
    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, end_s = base.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(base)
        span = range(start, end + 1, step)
        if span:
            low, high = min(span[0], span[-1]), max(span[0], span[-1])
            if low < lo or high > hi:
                raise ValueError(
                    tr(
                        "cron field value {value} out of range [{lo}, {hi}] in field {field}",
                        value=low if low < lo else high,
                        lo=lo,
                        hi=hi,
                        field=repr(field),
                    )
                )
        values.update(span)
    return values


@dataclass(frozen=True)
class CronFields:
    """A five-field expression, read: the matching values of each field, and
    whether day-of-month and day-of-week are restricted (for the OR rule)."""

    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]
    dom_restricted: bool
    dow_restricted: bool


def parse_cron(cron: str) -> CronFields:
    """Read `cron` the way `last_fire` does, or raise `ValueError`. Day of
    week 7 is Sunday, as 0 is."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(tr("invalid cron expression (expected 5 fields): {cron}", cron=repr(cron)))
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    minutes = _parse_cron_field(minute_f, 0, 59)
    hours = _parse_cron_field(hour_f, 0, 23)
    months = _parse_cron_field(month_f, 1, 12)
    doms = _parse_cron_field(dom_f, 1, 31)
    dows = _parse_cron_field(dow_f, 0, 7)
    if 7 in dows:
        dows.add(0)
    return CronFields(
        minutes=minutes,
        hours=hours,
        doms=doms,
        months=months,
        dows=dows,
        dom_restricted=dom_f.strip() != "*",
        dow_restricted=dow_f.strip() != "*",
    )


def window_open(cron: str | None, now: datetime) -> bool:
    """`None` means always open. Otherwise `cron` is a standard 5-field
    expression (minute hour day-of-month month day-of-week) matched to the
    minute, using a pure-stdlib matcher (no cron dependency) supporting `*`,
    `*/n` steps, comma lists, and `a-b` ranges on every field.

    Standard cron behavior applies when BOTH day-of-month and day-of-week are
    restricted (neither is a bare `*`): the day matches if EITHER field
    matches (they are OR'd, not AND'd). If only one is restricted, that one
    alone gates the day; if neither is restricted, every day matches.
    """
    if cron is None:
        return True

    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(tr("invalid cron expression (expected 5 fields): {cron}", cron=repr(cron)))
    minute_f, hour_f, dom_f, month_f, dow_f = parts

    if now.minute not in _parse_cron_field(minute_f, 0, 59):
        return False
    if now.hour not in _parse_cron_field(hour_f, 0, 23):
        return False
    if now.month not in _parse_cron_field(month_f, 1, 12):
        return False

    dom_restricted = dom_f.strip() != "*"
    dow_restricted = dow_f.strip() != "*"

    if dom_restricted or dow_restricted:
        day_ok = False
        if dom_restricted and now.day in _parse_cron_field(dom_f, 1, 31):
            day_ok = True
        if dow_restricted:
            # Cron day-of-week: 0 and 7 both mean Sunday, 1..6 Mon..Sat.
            # Python's isoweekday() is 1=Mon..7=Sun; map Sun(7) -> 0 to match.
            cron_dow = now.isoweekday() % 7
            dow_values = _parse_cron_field(dow_f, 0, 7)
            if 7 in dow_values:
                dow_values.add(0)
            if cron_dow in dow_values:
                day_ok = True
        if not day_ok:
            return False

    return True


SLOT = timedelta(hours=1)
_SLOT_HORIZON = timedelta(days=8)


def in_window(cron: str | None, now: datetime) -> bool:
    """Is `now` inside the window, reading each fire time as the start of a
    one-hour slot?

    This is what the console's week grid means: it writes `0 2-5 * * 6` for
    "Saturday 02:00 to 06:00". `window_open` matches the minute exactly, so a
    15-minute sweep that does not land on :00 would never see that window
    open. `None` is always open.
    """
    if cron is None:
        return True
    return last_fire(cron, now, lookback=SLOT - timedelta(seconds=1)) is not None


def window_slot_end(cron: str | None, now: datetime) -> datetime | None:
    """When the window open at `now` closes: the end of the last slot in the
    unbroken run of slots containing `now`. None when the window is closed, has
    no end (`cron` is None), or does not close within eight days."""
    if cron is None:
        return None
    fire = last_fire(cron, now, lookback=SLOT - timedelta(seconds=1))
    if fire is None:
        return None
    while fire + SLOT - now <= _SLOT_HORIZON:
        # The latest fire inside the current slot extends the run.
        following = last_fire(cron, fire + SLOT, lookback=SLOT)
        if following is None or following <= fire:
            return fire + SLOT
        fire = following
    return None


def next_window_start(cron: str | None, now: datetime) -> datetime | None:
    """When a sweep will next find the window open: `now` if it is open (or
    there is no window), else the next fire time within eight days, else
    None."""
    if cron is None or in_window(cron, now):
        return now
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(tr("invalid cron expression (expected 5 fields): {cron}", cron=repr(cron)))
    # A fire time is a minute; scanning forward a slot at a time and asking
    # `last_fire` for that slot reuses the one matcher rather than a mirror.
    cursor = now.replace(second=0, microsecond=0) + SLOT
    while cursor - now <= _SLOT_HORIZON:
        fire = last_fire(cron, cursor, lookback=SLOT - timedelta(minutes=1))
        if fire is not None and fire > now:
            earliest = fire
            # The slot may hold several fires; the first one after `now` counts.
            while True:
                before = last_fire(cron, earliest - timedelta(minutes=1), lookback=SLOT)
                if before is None or before <= now:
                    return earliest
                earliest = before
        cursor += SLOT
    return None


def last_fire(
    cron: str, now: datetime, *, lookback: timedelta = timedelta(days=8)
) -> datetime | None:
    """The latest minute at or before `now` that `cron` matches, looking back at
    most `lookback`; `None` if there is none.

    A sweep that runs every N minutes asks "has a fire time passed since I last
    looked", which `window_open` cannot answer: it only matches the current
    minute. Same field grammar and day-of-month/day-of-week OR rule as
    `window_open`, evaluated in `now`'s timezone.
    """
    fields = parse_cron(cron)
    minutes = sorted(fields.minutes, reverse=True)
    hours = sorted(fields.hours, reverse=True)
    months = fields.months
    dom_restricted = fields.dom_restricted
    dow_restricted = fields.dow_restricted
    doms = fields.doms
    dows = fields.dows

    def day_matches(day: date) -> bool:
        if day.month not in months:
            return False
        if not (dom_restricted or dow_restricted):
            return True
        if dom_restricted and day.day in doms:
            return True
        return dow_restricted and day.isoweekday() % 7 in dows

    ceiling = now.replace(second=0, microsecond=0)
    floor = now - lookback
    day = ceiling.date()
    while datetime.combine(day, time.max, tzinfo=now.tzinfo) >= floor:
        if day_matches(day):
            for hour in hours:
                for minute in minutes:
                    candidate = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
                    if candidate > ceiling:
                        continue
                    return candidate if candidate >= floor else None
        day -= timedelta(days=1)
    return None

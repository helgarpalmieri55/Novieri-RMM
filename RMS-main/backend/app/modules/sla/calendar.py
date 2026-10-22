"""Working-time arithmetic over a business calendar (spec §4).

Two pure functions -- no database, no ORM, no async -- that every other part of
the SLA module is built on: deadlines, pause, breach detection and auto-close
are all thin layers over these.

**DST is the hazard, not an edge case.** Both functions convert to the
calendar's timezone, walk day by day applying that day's windows, and convert
back to UTC at the boundary. Two distinct failures live here:

1. The *day-boundary* failure: adding a `timedelta` to a UTC instant and calling
   the result four working hours, which is silently an hour wrong for half the
   year. The local day walk prevents this.
2. The *within-window* failure: resolving a window's two boundaries as
   `ZoneInfo`-aware datetimes and subtracting them. CPython's `datetime.__sub__`
   and `_cmp` short-circuit to naive arithmetic when `self.tzinfo is
   other.tzinfo`, and `ZoneInfo` instances are cached -- so a window that
   *covers* a transition instant is measured in wall-clock rather than elapsed
   time. Every window boundary is therefore resolved to UTC in `_day_windows`
   before any comparison or subtraction happens, and all arithmetic downstream
   is instant-based.

`CalendarSpec` is a plain frozen value object so this module never imports the
ORM; Task 3's `load_calendar_spec` builds one from rows.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.i18n import tr
from app.modules.sla.errors import UnsatisfiableCalendar

__all__ = [
    "MAX_LOOKAHEAD_DAYS",
    "CalendarSpec",
    "UnsatisfiableCalendar",
    "add_working_minutes",
    "working_minutes_between",
]

# How far `add_working_minutes` will walk before declaring the calendar unable
# to deliver the target, and the widest span `working_minutes_between` will
# sweep. A year and a day: long enough that any sane target on any sane
# calendar resolves, short enough that an empty or all-holiday calendar cannot
# hang the sweep, which runs over every open ticket in every org (spec §4, §7).
MAX_LOOKAHEAD_DAYS = 366

_MINUTES_PER_DAY = 24 * 60


def _minute_of_day(value: time) -> int:
    """A local window boundary as minutes from local midnight.

    Seconds and microseconds are ignored: windows are minute-granular
    (`sla_calendar_window` stores `TIME` values the API validates to the
    minute, spec §2.2).
    """
    return value.hour * 60 + value.minute


def _merge_windows(
    windows: tuple[tuple[int, time, time], ...],
) -> dict[int, tuple[tuple[int, int], ...]]:
    """Group windows by weekday as minute offsets, sorted and overlaps merged.

    Merging keeps the two functions consistent: without it, a calendar that
    (mis)configured 09:00-13:00 and 12:00-17:00 would have the overlapping hour
    counted twice by `working_minutes_between` but only once by
    `add_working_minutes`. Adjacent windows merge too, which changes nothing
    because windows are half-open.

    Minute offsets rather than `time` values because a window ending at
    midnight is 1440, which `time` cannot represent.
    """
    grouped: dict[int, list[tuple[int, int]]] = {}
    for weekday, start, end in windows:
        start_min = _minute_of_day(start)
        end_min = _MINUTES_PER_DAY if end == time(0, 0) else _minute_of_day(end)
        if start_min >= end_min:  # defensive: the DB CHECK forbids it
            continue
        grouped.setdefault(weekday, []).append((start_min, end_min))

    merged: dict[int, tuple[tuple[int, int], ...]] = {}
    for weekday, spans in grouped.items():
        out: list[tuple[int, int]] = []
        for start_min, end_min in sorted(spans):
            if out and start_min <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], end_min))
            else:
                out.append((start_min, end_min))
        merged[weekday] = tuple(out)
    return merged


@dataclass(frozen=True)
class CalendarSpec:
    """An immutable snapshot of a business calendar.

    `windows` is `(weekday, start_time, end_time)` with Monday=0, matching
    `date.weekday()` and `sla_calendar_window.weekday` (spec §2.2). Multiple
    windows per weekday are expected -- a lunch break is two windows, not a
    special case here. Windows never wrap midnight; a night shift is two
    windows on adjacent days (22:00-00:00 on one, 00:00-06:00 on the next).

    **An `end_time` of `00:00` means midnight at the *end* of that day.** It is
    the only way to express a window that runs to midnight, and therefore the
    only way to express a genuinely 24-hour day: `00:00-00:00` is 1440 minutes,
    where the `00:00-23:59` workaround silently loses a minute a day. This is
    the one place `start_time < end_time` (the DB CHECK) does not hold, and it
    is read as end-of-day rather than rejected because a 24/7 calendar is a
    normal MSP configuration, not an error. Any other `start_time >= end_time`
    window is dropped.

    `holidays` are local dates on which every window is removed entirely
    (spec §2.3).
    """

    timezone: str
    windows: tuple[tuple[int, time, time], ...]
    holidays: frozenset[date]
    # Derived once at construction rather than per call: the sweep (spec §7)
    # calls the two functions once per open ticket per cycle against the same
    # spec. Excluded from `compare` so the value object's identity, equality
    # and hash still come from the three real fields.
    _by_weekday: dict[int, tuple[tuple[int, int], ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_weekday", _merge_windows(self.windows))


def _to_utc(name: str, value: datetime) -> datetime:
    """Reject naive input, and normalise anything aware to true UTC.

    A naive datetime reaching here means a caller lost a timezone somewhere;
    assuming UTC would turn that into a deadline that is quietly hours off.

    Normalising rather than merely checking awareness is the other half of the
    `_day_windows` hazard: an aware non-UTC `start` would carry a `ZoneInfo`
    that could be *identical* to the calendar's, and every clipping comparison
    against it would then take CPython's naive same-tzinfo shortcut. After this
    call every datetime in the module has `tzinfo=UTC`, so no comparison or
    subtraction can silently become wall-clock arithmetic.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(  # i18n-exempt: arithmetic guard, never a request value; 500 at most
            f"{name} must be timezone-aware; got naive {value!r}"
        )
    return value.astimezone(UTC)


def _offset_of(tz: ZoneInfo, instant: datetime) -> timedelta:
    """The zone's UTC offset AT a UTC instant.

    Note the direction: `ZoneInfo.utcoffset()` reads its argument as a local
    wall clock, which is ambiguous on a fall-back day and undefined in a
    spring-forward gap. Converting the instant first asks the unambiguous
    question -- what did the clock read at this moment -- which is the only one
    with a single answer.
    """
    offset = instant.astimezone(tz).utcoffset()
    assert offset is not None  # narrowed: a ZoneInfo-aware datetime always has one
    return offset


_TRANSITION_RESOLUTION = timedelta(seconds=1)
_TRANSITION_PROBE = timedelta(hours=1)


def _offset_runs(
    tz: ZoneInfo, lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime, timedelta]]:
    """Partition `[lo, hi)` into maximal runs of constant UTC offset.

    `ZoneInfo` does not publish its transition table, so this probes for it. The
    common case -- a day with no transition at all -- costs three lookups: if
    the offset at the start, the middle and the last second of the span all
    agree, the span is one run. Only when they disagree does it walk the span
    hourly and bisect each hour in which the offset moved, so a span containing
    two DISTINCT transitions is still resolved correctly rather than collapsed
    onto the first.

    ACCEPTED BLIND SPOT: a span whose offset leaves and returns to the same
    value between two probes reads as one run. Within the ~26 hours this is ever
    called on, no zone in the IANA database does that.
    """
    base = _offset_of(tz, lo)
    if hi - lo <= _TRANSITION_RESOLUTION:
        return [(lo, hi, base)]
    if (
        _offset_of(tz, lo + (hi - lo) / 2) == base
        and _offset_of(tz, hi - _TRANSITION_RESOLUTION) == base
    ):
        return [(lo, hi, base)]

    runs: list[tuple[datetime, datetime, timedelta]] = []
    cursor = lo
    while cursor < hi:
        probe = min(cursor + _TRANSITION_PROBE, hi)
        if _offset_of(tz, probe) == base:
            if probe >= hi:
                break
            cursor = probe
            continue
        # The offset changed somewhere in `(cursor, probe]`. Bisect for the
        # first instant that no longer reads `base`; `low` always reads `base`
        # and `high` never does, so the loop closes on the transition itself.
        low, high = cursor, probe
        while high - low > _TRANSITION_RESOLUTION:
            mid = low + (high - low) / 2
            if _offset_of(tz, mid) == base:
                low = mid
            else:
                high = mid
        if high >= hi:
            break
        runs.append((runs[-1][1] if runs else lo, high, base))
        base = _offset_of(tz, high)
        cursor = high
    runs.append((runs[-1][1] if runs else lo, hi, base))
    return runs


def _day_windows(
    cal: CalendarSpec,
    day: date,
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    """That local date's windows as UTC instants, or `[]` for a holiday or a
    non-working weekday.

    **A LOCAL DAY IS NOT ONE UTC INTERVAL PER WINDOW.** Resolving a window's two
    local boundaries to UTC and calling everything between them working time is
    wrong whenever a DST transition sits inside the window rather than outside
    it, and wrong in both directions (review F1):

    * On a FALL-BACK day the local clock re-enters a range it has already
      passed. A window ENDING inside the repeated range (New York, 2026-11-01,
      00:00-01:30) stops at the first 01:30 and drops the second pass -- 90
      minutes credited where 120 were worked. A window STARTING inside it
      (01:30-09:00) runs from the first 01:30 and so credits 01:00-01:30 EST,
      half an hour the desk was shut -- 510 where 480 were worked.
    * On a SPRING-FORWARD day a boundary inside the gap is a local time that
      never happens. `astimezone` resolves it at the pre-transition offset,
      which places it after the moment the clock actually reaches it: 02:30-06:00
      on 2026-03-08 in New York is worth 180 minutes and was credited 150.

    Neither is exotic; both need only a window boundary near the small hours,
    which any night shift or 24/7 desk has. And `astimezone` cannot be argued
    out of it by setting `fold`, because a single interval cannot express a
    range the clock visits twice however its ends are resolved.

    SO THE DAY IS SPLIT BY OFFSET FIRST. Inside a run of constant UTC offset,
    local time is UTC plus a constant, so `local_midnight - offset + minutes` is
    exact and a local range maps to exactly one UTC range. Clipping each window
    to each run and keeping the non-empty pieces yields precisely the instants
    whose local wall clock reads inside the window on this local date -- a
    repeated local range contributes twice because it is in two runs, and a
    skipped one contributes nothing because it is in neither. That is the
    definition of working time restated, rather than a special case per
    transition shape.

    The day's own extent is `[local midnight, local midnight tomorrow)`, which
    is right even when local midnight is itself ambiguous (fold 0 is the first
    instant of the date) or does not exist (fold 0 lands on the transition,
    which is the first instant of the date).

    Every boundary is still resolved to UTC before any comparison or
    subtraction: two `ZoneInfo`-aware datetimes sharing one cached tzinfo take
    CPython's naive same-tzinfo shortcut in `__sub__` and `_cmp`, which would
    make a window measured in wall clock rather than elapsed time.
    """
    if day in cal.holidays:
        return []
    windows = cal._by_weekday.get(day.weekday(), ())
    if not windows:
        return []

    day_start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    if day_end <= day_start:  # defensive: no IANA zone deletes a whole day
        return []

    naive_midnight = datetime.combine(day, time.min)

    spans: list[tuple[datetime, datetime]] = []
    for run_start, run_end, offset in _offset_runs(tz, day_start, day_end):
        if run_end <= run_start:
            continue
        # Within this run local time is UTC plus a constant, so the local
        # midnight this run measures its windows from is exact.
        base = (naive_midnight - offset).replace(tzinfo=UTC)
        for start_min, end_min in windows:
            w_start = max(base + timedelta(minutes=start_min), run_start)
            w_end = min(base + timedelta(minutes=end_min), run_end)
            # Empty once clipped: the window does not overlap this run at all,
            # which is how a local hour that never happens ends up worth zero
            # minutes rather than sixty.
            if w_end > w_start:
                spans.append((w_start, w_end))

    # Runs are walked in UTC order and each run's windows are already sorted, so
    # `spans` is ascending and disjoint. A window open across a transition
    # arrives as two touching pieces; join them so a day's windows come back in
    # the same canonical shape whether or not a transition happened to fall
    # inside one.
    merged: list[tuple[datetime, datetime]] = []
    for w_start, w_end in spans:
        if merged and w_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w_end))
        else:
            merged.append((w_start, w_end))
    return merged


def working_minutes_between(cal: CalendarSpec, start: datetime, end: datetime) -> int:
    """Minutes of working time inside `[start, end)` on this calendar.

    Both arguments are timezone-aware; the result is floored to whole minutes
    and is never negative -- `end` before `start` is zero, not a negative
    credit (a paused ticket must never buy time back).

    Raises `ValueError` if the span exceeds `MAX_LOOKAHEAD_DAYS`. The bound
    matches `add_working_minutes`': no target reachable on any calendar can sit
    further out than the lookahead horizon, so a wider span is a corrupt
    timestamp rather than a long-lived ticket, and this is the one place such a
    value would turn into an unbounded day sweep.
    """
    start = _to_utc("start", start)
    end = _to_utc("end", end)
    if end <= start:
        return 0

    tz = ZoneInfo(cal.timezone)
    day = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    # Checked before the empty-calendar shortcut: a corrupt timestamp is worth
    # surfacing whatever the calendar happens to contain.
    span_days = (last_day - day).days
    if span_days > MAX_LOOKAHEAD_DAYS:
        raise ValueError(  # i18n-exempt: corrupt-timestamp guard; the sweep logs the type only
            f"span of {span_days} days from {start.isoformat()} to {end.isoformat()} "
            f"exceeds MAX_LOOKAHEAD_DAYS={MAX_LOOKAHEAD_DAYS}; this is a corrupt "
            f"timestamp, not a working-time question"
        )
    if not cal._by_weekday:
        return 0

    total = timedelta(0)
    while day <= last_day:
        for w_start, w_end in _day_windows(cal, day, tz):
            overlap_start = max(w_start, start)
            overlap_end = min(w_end, end)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        day += timedelta(days=1)
    return int(total.total_seconds() // 60)


def add_working_minutes(cal: CalendarSpec, start: datetime, minutes: int) -> datetime:
    """The UTC instant `minutes` of working time after `start`.

    A `start` outside working hours does not accrue anything until the next
    window opens, so adding zero minutes returns the next working moment rather
    than `start` itself. Landing exactly on a window's end returns that end --
    the deadline is that instant, not the next window's open.

    Raises `UnsatisfiableCalendar` if the budget is still unconsumed after
    `MAX_LOOKAHEAD_DAYS`, which is what a calendar with no windows (or one
    whose every day is a holiday) does.
    """
    start = _to_utc("start", start)
    if minutes < 0:
        raise ValueError(  # i18n-exempt: arithmetic guard, never a request value; 500 at most
            f"minutes must be non-negative; got {minutes}"
        )

    tz = ZoneInfo(cal.timezone)
    budget = timedelta(minutes=minutes)
    cursor = start
    day = start.astimezone(tz).date()

    for _ in range(MAX_LOOKAHEAD_DAYS + 1):
        for w_start, w_end in _day_windows(cal, day, tz):
            if w_end <= cursor:
                continue
            if cursor < w_start:
                cursor = w_start
            available = w_end - cursor
            if budget <= available:
                return (cursor + budget).astimezone(UTC)
            budget -= available
            cursor = w_end
        day += timedelta(days=1)

    outstanding_minutes = int(budget.total_seconds() // 60)
    raise UnsatisfiableCalendar(
        tr(
            "calendar in {timezone} cannot deliver {minutes} working minutes "
            "from {start} within {days} days ({outstanding} minutes still outstanding)",
            timezone=cal.timezone,
            minutes=minutes,
            start=start.isoformat(),
            days=MAX_LOOKAHEAD_DAYS,
            outstanding=outstanding_minutes,
        ),
        timezone=cal.timezone,
        requested_minutes=minutes,
        start=start,
        outstanding_minutes=outstanding_minutes,
    )

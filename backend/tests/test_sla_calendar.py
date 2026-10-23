"""Working-time arithmetic (spec §4, §10.1-10.3).

Pure. Every test states a wall-clock expectation in the calendar's LOCAL time,
because a UTC-only assertion passes against an implementation that is an hour
wrong for half the year.
"""

import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.sla.calendar import (
    CalendarSpec,
    UnsatisfiableCalendar,
    add_working_minutes,
    working_minutes_between,
)

LONDON = "Europe/London"
# Mon-Fri 09:00-17:00, with a 12:00-13:00 lunch break expressed as two windows.
BUSINESS = CalendarSpec(
    timezone=LONDON,
    windows=tuple(
        (wd, s, e)
        for wd in range(5)
        for s, e in [(time(9, 0), time(12, 0)), (time(13, 0), time(17, 0))]
    ),
    holidays=frozenset(),
)


def _local(y: int, m: int, d: int, hh: int, mm: int, tz: str = LONDON) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def test_within_one_window() -> None:
    start = _local(2026, 3, 2, 9, 30)  # Monday
    end = _local(2026, 3, 2, 11, 30)
    assert working_minutes_between(BUSINESS, start, end) == 120


def test_spanning_the_lunch_break() -> None:
    """11:00 to 14:00 is three wall-clock hours but two working hours."""
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 2, 11, 0), _local(2026, 3, 2, 14, 0))
        == 120
    )


def test_spanning_a_weekend() -> None:
    """Friday 16:00 to Monday 10:00 = 1h Friday + 1h Monday."""
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 6, 16, 0), _local(2026, 3, 9, 10, 0))
        == 120
    )


def test_spanning_a_holiday() -> None:
    cal = CalendarSpec(BUSINESS.timezone, BUSINESS.windows, frozenset({date(2026, 3, 3)}))
    # Monday 16:00 -> Wednesday 10:00, with Tuesday a holiday.
    assert (
        working_minutes_between(cal, _local(2026, 3, 2, 16, 0), _local(2026, 3, 4, 10, 0)) == 120
    )


def test_starting_outside_working_hours() -> None:
    """A ticket filed at 03:00 accrues nothing until 09:00."""
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 2, 3, 0), _local(2026, 3, 2, 10, 0)) == 60
    )


def test_starting_exactly_at_a_window_boundary() -> None:
    """Spec §10.1 names this case: 09:00 is inside the window, 12:00 is not."""
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 2, 9, 0), _local(2026, 3, 2, 12, 0))
        == 180
    )


def test_window_boundaries_are_half_open_and_not_double_counted() -> None:
    """Windows are [start, end), so splitting a span at a boundary sums to the
    whole rather than counting the boundary minute twice."""
    # The lunch gap itself is worth nothing.
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 2, 12, 0), _local(2026, 3, 2, 13, 0)) == 0
    )
    morning = working_minutes_between(
        BUSINESS, _local(2026, 3, 2, 9, 0), _local(2026, 3, 2, 12, 0)
    )
    afternoon = working_minutes_between(
        BUSINESS, _local(2026, 3, 2, 12, 0), _local(2026, 3, 2, 17, 0)
    )
    whole = working_minutes_between(BUSINESS, _local(2026, 3, 2, 9, 0), _local(2026, 3, 2, 17, 0))
    assert morning + afternoon == whole == 420


def test_start_after_end_is_zero_not_negative() -> None:
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 2, 11, 0), _local(2026, 3, 2, 9, 0)) == 0
    )


def test_add_lands_on_the_next_working_moment() -> None:
    """Filed 16:00 Friday, 4 working hours -> Monday 12:00 LOCAL
    (1h Fri 16-17, then Mon 09-12 with lunch not yet reached)."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 6, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 3, 9, 12, 0)


def test_add_from_outside_working_hours_starts_at_the_next_window() -> None:
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 3, 0), 60)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.hour, local.minute) == (10, 0)


def test_add_across_spring_forward() -> None:
    """Spec §10.2. Europe/London springs forward 01:00->02:00 on 2026-03-29
    (a Sunday). A target starting Friday and landing Monday must be correct in
    LOCAL wall-clock time -- the naive implementation adds a timedelta to a UTC
    instant and lands an hour off."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 27, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (3, 30, 12, 0)


def test_add_across_autumn_back() -> None:
    """Europe/London falls back 02:00->01:00 on 2026-10-25 (a Sunday)."""
    due = add_working_minutes(BUSINESS, _local(2026, 10, 23, 16, 0), 240)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (10, 26, 12, 0)


def test_between_across_dst_counts_working_minutes_not_elapsed() -> None:
    """The weekend containing a DST shift still contributes only the working
    windows either side of it."""
    assert (
        working_minutes_between(BUSINESS, _local(2026, 3, 27, 16, 0), _local(2026, 3, 30, 10, 0))
        == 120
    )


def test_an_empty_calendar_raises_rather_than_looping() -> None:
    """Spec §4, §10.3: a calendar with no windows is otherwise a way to hang
    the sweep."""
    empty = CalendarSpec(LONDON, (), frozenset())
    with pytest.raises(UnsatisfiableCalendar):
        add_working_minutes(empty, _local(2026, 3, 2, 9, 0), 60)


def test_a_target_longer_than_a_working_week_still_resolves() -> None:
    """40 working hours on this 35-hour calendar (7h/day after lunch) lands the
    following week, not never: 35h consumes Mon-Fri, the remaining 5h consume
    Mon 09:00-12:00 and 13:00-15:00."""
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 9, 0), 40 * 60)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (3, 9, 15, 0)


def test_add_zero_minutes_returns_the_next_working_moment() -> None:
    due = add_working_minutes(BUSINESS, _local(2026, 3, 2, 3, 0), 0)
    assert due.astimezone(ZoneInfo(LONDON)).hour == 9


def test_a_holiday_covering_every_remaining_day_raises() -> None:
    """Bounded iteration, not an infinite search: every day inside the lookahead
    horizon is a holiday, so the budget can never be consumed."""
    holidays = frozenset(date(2026, 3, 2) + timedelta(days=i) for i in range(400))
    cal = CalendarSpec(LONDON, BUSINESS.windows, holidays)
    with pytest.raises(UnsatisfiableCalendar):
        add_working_minutes(cal, _local(2026, 3, 2, 9, 0), 60 * 200)


def test_naive_datetimes_are_rejected() -> None:
    """Naive input is a caller bug; guessing a zone is how an hour goes missing."""
    naive = datetime(2026, 3, 2, 9, 0)
    with pytest.raises(ValueError):
        working_minutes_between(BUSINESS, naive, _local(2026, 3, 2, 10, 0))
    with pytest.raises(ValueError):
        add_working_minutes(BUSINESS, naive, 60)


# ---------------------------------------------------------------------------
# Calendars whose windows COVER a DST transition instant (review F1).
#
# The BUSINESS fixture above cannot see this class of bug: no IANA zone
# transitions between 09:00 and 17:00 local, so a plain business-hours calendar
# is safe in every zone. The bug needs a window that is *open* across the
# transition -- a 24/7 calendar or a night shift, both of which spec §2.2
# supports and an MSP configures routinely.
# ---------------------------------------------------------------------------

# A genuinely 24-hour, 7-day calendar: 00:00 to midnight-end-of-day, every day.
ALWAYS = CalendarSpec(
    timezone=LONDON,
    windows=tuple((wd, time(0, 0), time(0, 0)) for wd in range(7)),
    holidays=frozenset(),
)

# The night shift `CalendarSpec`'s own docstring prescribes: 22:00-midnight on
# one day and midnight-06:00 on the next, expressed as two windows per weekday.
NIGHT_SHIFT = CalendarSpec(
    timezone=LONDON,
    windows=tuple(
        (wd, s, e)
        for wd in range(7)
        for s, e in [(time(0, 0), time(6, 0)), (time(22, 0), time(0, 0))]
    ),
    holidays=frozenset(),
)


def test_a_24_7_day_containing_spring_forward_is_23_hours_not_24() -> None:
    """2026-03-29 in London is 1380 real minutes long. A window open across the
    01:00->02:00 jump must be measured in elapsed time, not wall-clock: the
    naive version credits a full 1440 for a day that never happened."""
    assert (
        working_minutes_between(ALWAYS, _local(2026, 3, 29, 0, 0), _local(2026, 3, 30, 0, 0))
        == 1380
    )


def test_a_24_7_day_containing_autumn_back_is_25_hours_not_24() -> None:
    """2026-10-25 is 1500 real minutes long -- the repeated hour is real time a
    24/7 desk is staffed for, and the ticket clock must credit it."""
    assert (
        working_minutes_between(ALWAYS, _local(2026, 10, 25, 0, 0), _local(2026, 10, 26, 0, 0))
        == 1500
    )


def test_add_over_a_24_7_calendar_across_spring_forward() -> None:
    """48 working hours from Friday noon on a 24/7 calendar is 48 hours of real
    elapsed time; the clocks going forward move the LOCAL landing to 13:00."""
    due = add_working_minutes(ALWAYS, _local(2026, 3, 27, 12, 0), 2880)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (3, 29, 13, 0)


def test_add_over_a_24_7_calendar_across_autumn_back() -> None:
    """The same 48 hours the other way: the clocks going back move the LOCAL
    landing to 11:00."""
    due = add_working_minutes(ALWAYS, _local(2026, 10, 23, 12, 0), 2880)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (10, 25, 11, 0)


def test_add_over_a_night_shift_across_spring_forward() -> None:
    """5 working hours from Saturday 22:00: 2h to midnight, then 3h of the
    Sunday 00:00-06:00 window whose middle loses an hour -> 04:00 LOCAL."""
    due = add_working_minutes(NIGHT_SHIFT, _local(2026, 3, 28, 22, 0), 300)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (3, 29, 4, 0)


def test_add_over_a_night_shift_across_autumn_back() -> None:
    """The same shift in October: the repeated hour is worked, so 5 working
    hours land at 02:00 LOCAL rather than 03:00."""
    due = add_working_minutes(NIGHT_SHIFT, _local(2026, 10, 24, 22, 0), 300)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (10, 25, 2, 0)


def test_between_is_additive_across_a_dst_transition() -> None:
    """Spec §5 accumulates `working_minutes_between` over sub-intervals to
    subtract pauses, so a split that does not sum to the whole is directly a
    wrong deadline. A window fully inside the span and one clipped by
    `start`/`end` must be measured the same way."""
    for a, b, c, expected in [
        (
            _local(2026, 3, 29, 0, 0),
            _local(2026, 3, 29, 12, 0),
            _local(2026, 3, 30, 0, 0),
            1380,
        ),
        (
            _local(2026, 10, 25, 0, 0),
            _local(2026, 10, 25, 12, 0),
            _local(2026, 10, 26, 0, 0),
            1500,
        ),
    ]:
        whole = working_minutes_between(ALWAYS, a, c)
        split = working_minutes_between(ALWAYS, a, b) + working_minutes_between(ALWAYS, b, c)
        assert whole == split == expected


def test_a_local_hour_that_does_not_exist_is_worth_no_working_minutes() -> None:
    """01:00-02:00 on 2026-03-29 is a local hour that never occurs. The window
    collapses to nothing; crediting it 60 minutes is time the desk was not
    open. `add` therefore has to reach the following Sunday."""
    gap = CalendarSpec(LONDON, ((6, time(1, 0), time(2, 0)),), frozenset())
    assert working_minutes_between(gap, _local(2026, 3, 29, 0, 0), _local(2026, 3, 30, 0, 0)) == 0
    due = add_working_minutes(gap, _local(2026, 3, 29, 0, 0), 60)
    local = due.astimezone(ZoneInfo(LONDON))
    assert (local.month, local.day, local.hour, local.minute) == (4, 5, 2, 0)


@pytest.mark.parametrize(
    ("cal_name", "start", "minutes"),
    [
        ("always", (2026, 3, 27, 12, 0), 2880),
        ("always", (2026, 10, 23, 12, 0), 2880),
        ("night", (2026, 3, 28, 22, 0), 300),
        ("night", (2026, 10, 24, 22, 0), 300),
    ],
)
def test_the_two_functions_agree_across_a_transition(
    cal_name: str, start: tuple[int, int, int, int, int], minutes: int
) -> None:
    """`between(s, add(s, M)) == M` is the invariant that binds the two
    functions. It is the property that fails when a window is measured in
    wall-clock: one function loses the hour and the other does not."""
    cal = {"always": ALWAYS, "night": NIGHT_SHIFT}[cal_name]
    s = _local(*start)
    assert working_minutes_between(cal, s, add_working_minutes(cal, s, minutes)) == minutes


# ---------------------------------------------------------------------------
# A window BOUNDARY inside a DST transition (review F1, second pass).
#
# The tests above cover windows that are OPEN across a transition -- 24/7 and
# night-shift shapes, where the transition sits strictly between the window's
# two boundaries. Every one of them passes against a day modelled as one
# contiguous UTC interval, because a contiguous interval spanning the whole
# transition still measures the right elapsed time.
#
# The shape that does not is a boundary sitting INSIDE the repeated (or the
# skipped) local range. On a fall-back day the local clock re-enters a range it
# already passed, so a single interval is wrong in both directions: a boundary
# at the start swallows the part of the repeat below it, and a boundary at the
# end stops at the first pass and drops the second entirely. `astimezone`
# resolves the ambiguity to fold 0 in both cases, so the error is silent.
#
# These are stated against the LOCAL clock and hand-counted, not derived from
# any helper, so they cannot agree with a wrong implementation by construction.
# ---------------------------------------------------------------------------


def _sunday_window(zone: str, day: date, start: time, end: time) -> CalendarSpec:
    return CalendarSpec(zone, ((day.weekday(), start, end),), frozenset())


def _whole_local_day(zone: str, day: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(zone)
    return (
        datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC),
    )


def test_a_window_ending_inside_the_repeated_hour_keeps_both_passes() -> None:
    """New York falls back 02:00 EDT -> 01:00 EST on 2026-11-01, so local
    00:00-01:30 is lived TWICE below 01:30: 00:00-01:30 EDT (90 min) and then
    01:00-01:30 EST (30 min). 120 real minutes, not 90. Modelled as one
    interval the day stops at the FIRST 01:30 and the second pass is lost."""
    day = date(2026, 11, 1)
    cal = _sunday_window("America/New_York", day, time(0, 0), time(1, 30))
    start, end = _whole_local_day("America/New_York", day)
    assert working_minutes_between(cal, start, end) == 120


def test_a_window_starting_inside_the_repeated_hour_excludes_the_time_below_it() -> None:
    """The mirror image on the same day: 01:30-09:00 is 01:30-02:00 EDT (30 min)
    plus 01:30-09:00 EST (450 min) = 480. One interval from the FIRST 01:30 to
    09:00 EST is 510 -- it credits 01:00-01:30 EST, half an hour the desk was
    shut."""
    day = date(2026, 11, 1)
    cal = _sunday_window("America/New_York", day, time(1, 30), time(9, 0))
    start, end = _whole_local_day("America/New_York", day)
    assert working_minutes_between(cal, start, end) == 480


def test_the_same_defect_on_a_45_minute_offset_and_a_30_minute_dst_step() -> None:
    """Nothing above depends on a whole-hour UTC offset or a 60-minute DST step,
    so neither does this.

    Chatham is +13:45/+12:45 and falls back 03:45 -> 02:45 on 2026-04-05. A
    03:00-12:55 window is 03:00-03:45 on the first pass (45) plus 03:00-12:55 on
    the second (595) = **640**. The second pass contributes from 03:00 and not
    from 02:45, because the window is not open below its own start; a single
    interval running from the first 03:00 to 12:55 credits 655, the extra 15
    being 02:45-03:00 after the clocks went back.

    Lord Howe steps by THIRTY minutes, 02:00 -> 01:30. A 01:45-09:00 window is
    01:45-02:00 (15) plus 01:45-09:00 (435) = **450**; one interval credits 465.
    """
    day = date(2026, 4, 5)
    chatham = _sunday_window("Pacific/Chatham", day, time(3, 0), time(12, 55))
    start, end = _whole_local_day("Pacific/Chatham", day)
    assert working_minutes_between(chatham, start, end) == 640

    lord_howe = _sunday_window("Australia/Lord_Howe", day, time(1, 45), time(9, 0))
    start, end = _whole_local_day("Australia/Lord_Howe", day)
    assert working_minutes_between(lord_howe, start, end) == 450


def test_add_stops_short_when_a_window_starts_inside_the_repeated_hour() -> None:
    """`add` reads the same day model, so the same defect becomes a deadline
    that is 30 working minutes early. 450 working minutes from the first
    (EDT) 01:30 on 2026-11-01: 30 to 02:00 EDT, then 420 from 01:30 EST lands
    08:30 EST. Measuring the day as one interval lands 08:00 EST, at which
    point only 420 working minutes have actually elapsed."""
    tz = ZoneInfo("America/New_York")
    cal = _sunday_window("America/New_York", date(2026, 11, 1), time(1, 30), time(9, 0))
    start = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0).astimezone(UTC)
    due = add_working_minutes(cal, start, 450)
    local = due.astimezone(tz)
    assert (local.month, local.day, local.hour, local.minute) == (11, 1, 8, 30)
    # ... and the round trip agrees, which it cannot when both sides share the
    # day model: `between` reported 450 for the 08:00 answer too.
    assert working_minutes_between(cal, start, due) == 450


def test_a_window_boundary_inside_the_spring_forward_gap_is_not_credited() -> None:
    """The other direction. New York springs 02:00 EST -> 03:00 EDT on
    2026-03-08, so local 02:30 never happens: the desk is open from the first
    instant the clock READS 02:30 or later, which is 03:00 EDT, through 06:00
    EDT -- 180 minutes.

    Resolving the 02:30 boundary at the pre-transition offset (fold 0's answer
    for a local time that does not exist) puts the window's start at 02:30 EST =
    07:30Z, half an hour AFTER the day's first working instant (07:00Z), and
    silently shortens the day to 150."""
    day = date(2026, 3, 8)
    cal = _sunday_window("America/New_York", day, time(2, 30), time(6, 0))
    start, end = _whole_local_day("America/New_York", day)
    assert working_minutes_between(cal, start, end) == 180


# --- Review F2: a window ending at midnight ---------------------------------


def test_a_window_ending_at_midnight_runs_to_the_end_of_the_day() -> None:
    """`end_time == 00:00` is midnight-end-of-day, not a zero-length window
    silently dropped. It is the only encoding for an evening shift, and the
    only one that makes a true 24-hour day expressible (00:00-23:59 loses a
    minute a day)."""
    evening = CalendarSpec(LONDON, ((0, time(18, 0), time(0, 0)),), frozenset())
    assert (
        working_minutes_between(evening, _local(2026, 3, 2, 18, 0), _local(2026, 3, 3, 0, 0)) == 360
    )
    # A full day is 1440 minutes, not 1439.
    assert (
        working_minutes_between(ALWAYS, _local(2026, 3, 2, 0, 0), _local(2026, 3, 3, 0, 0)) == 1440
    )


def test_a_window_starting_at_midnight_still_starts_the_day() -> None:
    """`00:00` as a *start* keeps its ordinary meaning; only the end is
    reinterpreted."""
    early = CalendarSpec(LONDON, ((0, time(0, 0), time(6, 0)),), frozenset())
    assert (
        working_minutes_between(early, _local(2026, 3, 2, 0, 0), _local(2026, 3, 2, 23, 0)) == 360
    )


# --- Review F3: aware-but-not-UTC input -------------------------------------


def test_aware_non_utc_input_is_normalised_to_utc() -> None:
    """Input carrying the calendar's own `ZoneInfo` is the second half of the
    F1 hazard: `ZoneInfo` is cached, so every clipping comparison against it
    would take CPython's naive same-tzinfo path. Normalising on entry means the
    answer cannot depend on which zone the caller happened to hand us."""
    tz = ZoneInfo(LONDON)
    local_start = datetime(2026, 3, 29, 0, 30, tzinfo=tz)
    local_end = datetime(2026, 3, 30, 0, 0, tzinfo=tz)
    assert working_minutes_between(ALWAYS, local_start, local_end) == working_minutes_between(
        ALWAYS, local_start.astimezone(UTC), local_end.astimezone(UTC)
    )
    assert working_minutes_between(ALWAYS, local_start, local_end) == 1350
    assert add_working_minutes(ALWAYS, local_start, 60) == add_working_minutes(
        ALWAYS, local_start.astimezone(UTC), 60
    )


# --- Review F4: `working_minutes_between` is bounded ------------------------


def test_an_absurd_span_is_rejected_rather_than_swept() -> None:
    """No satisfiable SLA target sits beyond the lookahead horizon, so a wider
    span is a corrupt timestamp. Bounding it matches `add_working_minutes` and
    keeps the sweep's per-ticket cost bounded."""
    start = _local(2026, 3, 2, 9, 0)
    with pytest.raises(ValueError, match="MAX_LOOKAHEAD_DAYS"):
        working_minutes_between(BUSINESS, start, start + timedelta(days=400))
    # A span inside the horizon is still answered, not rejected.
    assert working_minutes_between(BUSINESS, start, start + timedelta(days=360)) > 0


# ---------------------------------------------------------------------------
# Seeded differential test against an independent reference (review F1).
#
# THE REFERENCE USES A DIFFERENT *MODEL*, NOT A DIFFERENT ORDER OF OPERATIONS.
# That distinction is the whole value of this test and the previous reference
# did not have it. It resolved each window's two local boundaries to UTC by
# explicit offset subtraction (`naive - tz.utcoffset(naive)`) and treated the
# result as ONE contiguous UTC interval -- which is precisely the model the
# implementation used, and precisely the model that was wrong. `utcoffset` on a
# naive datetime resolves ambiguity to fold 0, exactly as `astimezone` did, so
# on a fall-back day the reference reproduced the bug boundary-for-boundary.
# A round-trip invariant (`between(s, add(s, M)) == M`) could not see it either,
# because both functions read the same `_day_windows`. The safety net agreed
# with the defect by construction.
#
# The reference below never constructs an interval. It CLASSIFIES INSTANTS: for
# each UTC minute it asks `t.astimezone(tz)` what the local wall clock reads and
# whether that reading falls inside one of the weekday's spans. That is the
# definition of "working time" restated directly, with no notion of a day having
# a start and an end, so a local time-of-day that occurs twice is counted twice
# and one that never occurs is counted never -- without either being a case the
# reference knows about. It is O(minutes) and therefore slow; for a test whose
# only job is to disagree with a wrong implementation, that is the right trade.
#
# It also derives its own spans from `cal.windows` rather than reading
# `cal._by_weekday`, and answers overlap by membership (`any(a <= m < b)`)
# rather than by merging, so it does not inherit `_merge_windows` either.
#
# Windows are placed ANYWHERE in the day, which is what the review found
# necessary: confined to 06:00-22:00 the sweep finds nothing.
#
# Seeded, so a failure is reproducible rather than a flake.
# ---------------------------------------------------------------------------

DIFF_ZONES = (
    "Europe/London",
    "Australia/Sydney",  # southern: fall back in April, spring forward in October
    "Asia/Kolkata",  # +05:30, no DST at all
    "Australia/Lord_Howe",  # +10:30/+11:00 -- a 30-minute DST step, southern
    "America/Santiago",
    "Pacific/Chatham",  # +12:45
)
DIFF_SEED = 20260731
DIFF_CASES = 300


def _ref_transitions(zone: str, year: int) -> list[date]:
    tz = ZoneInfo(zone)
    out: list[date] = []
    day = date(year, 1, 1)
    prev = tz.utcoffset(datetime(year, 1, 1, 12))
    while day.year == year:
        offset = tz.utcoffset(datetime(day.year, day.month, day.day, 12))
        if offset != prev:
            out.append(day)
            prev = offset
        day += timedelta(days=1)
    return out


def _ref_spans(cal: CalendarSpec) -> dict[int, tuple[tuple[int, int], ...]]:
    """The calendar's windows as local minute-of-day ranges, per weekday.

    Derived from `cal.windows` rather than read off `cal._by_weekday`, so the
    reference does not inherit `_merge_windows`. Overlaps are NOT merged --
    membership below is an `any()`, which cannot double-count an overlap the
    way an interval sum can.
    """
    out: dict[int, list[tuple[int, int]]] = {}
    for weekday, start_t, end_t in cal.windows:
        start_min = start_t.hour * 60 + start_t.minute
        end_min = 1440 if end_t == time(0, 0) else end_t.hour * 60 + end_t.minute
        if start_min >= end_min:
            continue
        out.setdefault(weekday, []).append((start_min, end_min))
    return {weekday: tuple(spans) for weekday, spans in out.items()}


def _ref_is_working(
    cal: CalendarSpec,
    spans: dict[int, tuple[tuple[int, int], ...]],
    tz: ZoneInfo,
    instant: datetime,
) -> bool:
    """Is the minute beginning at this UTC instant working time?

    The ONLY question the reference asks. It reads the local wall clock at that
    instant and checks it against that local weekday's spans -- so a repeated
    local hour is asked twice and answered twice, and a local hour that never
    happens is never asked. Neither is a case this function knows exists.
    """
    local = instant.astimezone(tz)
    if local.date() in cal.holidays:
        return False
    minute = local.hour * 60 + local.minute
    return any(lo <= minute < hi for lo, hi in spans.get(local.weekday(), ()))


def _ref_between(cal: CalendarSpec, start: datetime, end: datetime) -> int:
    """Count the working minutes in `[start, end)`, one minute at a time."""
    if end <= start:
        return 0
    tz = ZoneInfo(cal.timezone)
    spans = _ref_spans(cal)
    step = timedelta(minutes=1)
    total = 0
    instant = start
    while instant < end:
        if _ref_is_working(cal, spans, tz, instant):
            total += 1
        instant += step
    return total


def _ref_add(cal: CalendarSpec, start: datetime, minutes: int, horizon: int) -> datetime | None:
    """The instant `minutes` of working time after `start`, or `None` if the
    calendar cannot deliver it within `horizon` days.

    Stated as a fact about minutes rather than as a cursor walk over intervals:
    the answer is the instant just after the `minutes`-th working minute at or
    after `start`. `minutes == 0` is the one case that is not "just after"
    anything -- it is the next working minute itself, which is what "adding zero
    returns the next working moment" means.
    """
    tz = ZoneInfo(cal.timezone)
    spans = _ref_spans(cal)
    step = timedelta(minutes=1)
    limit = start + timedelta(days=horizon)
    seen = 0
    instant = start
    while instant < limit:
        if _ref_is_working(cal, spans, tz, instant):
            if minutes == 0:
                return instant
            seen += 1
            if seen == minutes:
                return instant + step
        instant += step
    return None


def _diff_time(minute: int) -> time:
    """1440 is midnight-end-of-day, which `time` spells `00:00`."""
    return time(0, 0) if minute == 1440 else time(minute // 60, minute % 60)


def _diff_calendar(rng: random.Random, zone: str, anchor: date) -> CalendarSpec:
    """Disjoint windows placed anywhere in the day, at quarter-hour
    granularity, on a random subset of weekdays -- plus the occasional
    holiday."""
    windows: list[tuple[int, time, time]] = []
    for weekday in rng.sample(range(7), rng.randint(1, 7)):
        points = sorted(rng.sample(range(0, 1441, 15), 2 * rng.randint(1, 3)))
        for i in range(0, len(points), 2):
            windows.append((weekday, _diff_time(points[i]), _diff_time(points[i + 1])))
    holidays = frozenset(
        anchor + timedelta(days=rng.randint(-3, 6)) for _ in range(rng.randint(0, 2))
    )
    return CalendarSpec(zone, tuple(windows), holidays)


def test_differential_against_an_independent_reference() -> None:
    """Both functions, against a from-scratch reference, over zones with a
    southern-hemisphere transition ordering, a non-whole-hour offset and a
    30-minute DST step. This is the shape of test that found F1: nothing here
    assumes northern ordering or a 60-minute shift."""
    rng = random.Random(DIFF_SEED)
    transitions = {zone: _ref_transitions(zone, 2026) for zone in DIFF_ZONES}
    horizon = 45
    checked_add = 0

    for _ in range(DIFF_CASES):
        zone = rng.choice(DIFF_ZONES)
        near = transitions[zone]
        if near and rng.random() < 0.7:
            anchor = rng.choice(near) + timedelta(days=rng.randint(-2, 2))
        else:
            anchor = date(2026, 1, 1) + timedelta(days=rng.randint(0, 364))
        cal = _diff_calendar(rng, zone, anchor)
        tz = ZoneInfo(zone)

        start = datetime.combine(
            anchor, time(rng.randrange(24), rng.randrange(60)), tzinfo=tz
        ).astimezone(UTC)
        end = start + timedelta(days=rng.randint(0, 4), minutes=rng.randrange(1, 1440))

        got = working_minutes_between(cal, start, end)
        assert got == _ref_between(cal, start, end), (zone, cal, start, end)
        # A hard physical ceiling, independent of both implementations: working
        # time cannot exceed the elapsed time it sits inside.
        assert got <= int((end - start).total_seconds() // 60)

        minutes = rng.choice([0, 1, 59, 60, 180, 479, 600, 1440, 2880, 5000])
        expected_due = _ref_add(cal, start, minutes, horizon)
        if expected_due is not None:
            checked_add += 1
            assert add_working_minutes(cal, start, minutes) == expected_due, (
                zone,
                cal,
                start,
                minutes,
            )

    # Guard against the case set silently degenerating into all-unsatisfiable.
    assert checked_add > DIFF_CASES // 2

"""Tests for the shared maintenance-window matcher (app.core.schedule),
lifted out of the patch module so patch and deploy share one implementation.
"""

import datetime as _dt

import pytest

from app.core.schedule import in_window, next_window_start, window_open, window_slot_end


def test_window_open_none_is_always_open() -> None:
    assert window_open(None, _dt.datetime(2026, 7, 19, 3, 17)) is True


def test_window_open_step_matches_and_rejects_by_minute() -> None:
    assert window_open("*/5 * * * *", _dt.datetime(2026, 7, 19, 3, 10)) is True
    assert window_open("*/5 * * * *", _dt.datetime(2026, 7, 19, 3, 11)) is False


def test_window_open_comma_list_hour() -> None:
    cron = "0 2,3,4 * * *"
    assert window_open(cron, _dt.datetime(2026, 7, 19, 3, 0)) is True
    assert window_open(cron, _dt.datetime(2026, 7, 19, 5, 0)) is False


def test_window_open_range() -> None:
    cron = "0 1-4 * * *"
    assert window_open(cron, _dt.datetime(2026, 7, 19, 2, 0)) is True
    assert window_open(cron, _dt.datetime(2026, 7, 19, 6, 0)) is False


def test_window_open_dom_and_dow_are_ored_when_both_restricted() -> None:
    # 2026-07-19 is a Sunday. dom=1 (not today) OR dow=0/Sunday (today) -> open.
    cron = "0 0 1 * 0"
    assert window_open(cron, _dt.datetime(2026, 7, 19, 0, 0)) is True
    # Neither dom(=1) nor dow(=Mon/1) match a Sunday-the-19th -> closed.
    cron_closed = "0 0 1 * 1"
    assert window_open(cron_closed, _dt.datetime(2026, 7, 19, 0, 0)) is False


def test_window_open_out_of_range_field_raises() -> None:
    """An out-of-range field value must raise, not silently never-match —
    otherwise a typo could disable a device's maintenance window forever
    with no error."""
    now = _dt.datetime(2026, 7, 19, 3, 0)
    with pytest.raises(ValueError):
        window_open("60 * * * *", now)


# ---------------------------------------------------------------------------
# in_window / window_slot_end: the hour-slot reading the week grid writes.
# ---------------------------------------------------------------------------


def test_in_window_holds_for_the_whole_painted_hour() -> None:
    # The grid writes `0 2-5 * * 6` for "Saturday 02:00 to 06:00". A sweep
    # that fires at 02:37 must see the window open, not only one at 02:00.
    cron = "0 2-5 * * 6"
    sat = _dt.datetime(2026, 9, 26, 2, 37, tzinfo=_dt.UTC)
    assert in_window(cron, sat) is True
    assert in_window(cron, sat.replace(hour=5, minute=59)) is True
    assert in_window(cron, sat.replace(hour=6, minute=0)) is False
    assert in_window(cron, sat.replace(hour=1, minute=59)) is False
    assert in_window(cron, sat - _dt.timedelta(days=1)) is False


def test_in_window_none_is_always_open() -> None:
    assert in_window(None, _dt.datetime(2026, 9, 26, 2, 37, tzinfo=_dt.UTC)) is True


def test_window_slot_end_is_the_end_of_the_contiguous_run() -> None:
    cron = "0 2-5 * * 6"
    now = _dt.datetime(2026, 9, 26, 3, 10, tzinfo=_dt.UTC)
    assert window_slot_end(cron, now) == _dt.datetime(2026, 9, 26, 6, 0, tzinfo=_dt.UTC)


def test_window_slot_end_runs_across_midnight() -> None:
    cron = "0 22,23,0,1 * * *"
    now = _dt.datetime(2026, 9, 26, 23, 30, tzinfo=_dt.UTC)
    assert window_slot_end(cron, now) == _dt.datetime(2026, 9, 27, 2, 0, tzinfo=_dt.UTC)


def test_window_slot_end_is_none_when_closed_or_unbounded() -> None:
    closed = _dt.datetime(2026, 9, 26, 10, 0, tzinfo=_dt.UTC)
    assert window_slot_end("0 2-5 * * 6", closed) is None
    assert window_slot_end(None, closed) is None
    # Every hour of every day never closes inside the horizon.
    assert window_slot_end("0 * * * *", closed) is None


# ---------------------------------------------------------------------------
# next_window_start: when a waiting restart will next be dispatched.
# ---------------------------------------------------------------------------


def test_next_window_start_is_now_when_open_or_unbounded() -> None:
    sat = _dt.datetime(2026, 9, 26, 2, 37, tzinfo=_dt.UTC)
    assert next_window_start(None, sat) == sat
    assert next_window_start("0 2-5 * * 6", sat) == sat


def test_next_window_start_is_the_next_fire_when_closed() -> None:
    fri = _dt.datetime(2026, 9, 25, 14, 10, 30, tzinfo=_dt.UTC)
    assert next_window_start("0 2-5 * * 6", fri) == _dt.datetime(
        2026, 9, 26, 2, 0, tzinfo=_dt.UTC
    )
    # Once Saturday's last slot has closed, the next one is a week away.
    late = _dt.datetime(2026, 9, 26, 6, 0, tzinfo=_dt.UTC)
    assert next_window_start("0 2-5 * * 6", late) == _dt.datetime(
        2026, 10, 3, 2, 0, tzinfo=_dt.UTC
    )


def test_next_window_start_is_none_for_a_window_that_does_not_open_soon() -> None:
    # 31 February never comes.
    now = _dt.datetime(2026, 9, 25, tzinfo=_dt.UTC)
    assert next_window_start("0 2 31 2 *", now) is None


# ---------------------------------------------------------------------------
# Bounds before expansion (re-review 2 of the Task 13 fix rounds): a range is
# judged by its ends, never expanded first. The result for every expression
# is what it was; only the cost of refusing an out-of-range one changes.
# ---------------------------------------------------------------------------


def _parse_before(field: str, lo: int, hi: int) -> set[int]:
    """`_parse_cron_field` as it was before the change, kept to compare."""
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
        values.update(range(start, end + 1, step))
    for value in values:
        if value < lo or value > hi:
            raise ValueError(value)
    return values


def _outcome(parse: object, field: str, lo: int, hi: int) -> object:
    try:
        return parse(field, lo, hi)  # type: ignore[operator]
    except ValueError:
        return ValueError


def test_the_parser_reads_every_expression_as_it_did() -> None:
    """Every field shape the grammar has -- and the ones `int()` happens to
    take -- with ends on and off each bound, steps up, down and zero, alone
    and in comma lists: the same set, or the same refusal, as before."""
    from app.core.schedule import _parse_cron_field

    ends = ["-1", "0", "1", "5", "7", "12", "23", "24", "31", "32", "59", "60", "99"]
    steps = ["", "/1", "/2", "/5", "/7", "/15", "/59", "/60", "/100", "/0", "/-1", "/-5"]
    parts = (
        [f"*{s}" for s in steps]
        + [f"{a}{s}" for a in ends for s in steps]
        + [f"{a}-{b}{s}" for a in ends for b in ends for s in steps]
    )
    fields = parts + [f"{a},{b}" for a in parts[:40] for b in parts[-40:]] + ["", ",", "x", "1-"]
    for lo, hi in ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)):
        for field in fields:
            assert _outcome(_parse_cron_field, field, lo, hi) == _outcome(
                _parse_before, field, lo, hi
            ), (field, lo, hi)


def test_an_out_of_range_range_is_refused_before_it_is_expanded() -> None:
    """`0-2000000` in the minute field: refused with no set of two million
    numbers ever built (it was built, then judged). A stored value of a
    billion would have held the window sweep's process the same way."""
    import tracemalloc

    from app.core.schedule import _parse_cron_field

    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            _parse_cron_field("0-2000000", 0, 59)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000, peak

"""Due-based scheduling and job lifetime for backups
(spec-server-backup.md §3.3, defects D1 and D3).

The sweep runs every 15 minutes, so it asks "has a scheduled time passed that
nobody has acted on", not "is this minute a scheduled minute". A fire is acted
on exactly once: by a run started since it, or by one `missed` row once its
start deadline has passed. A NULL schedule is on demand only (spec.md §4).

The deadline and runtime are per-workload columns in the spec; until workloads
exist they are these defaults, applied to every policy.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.core.i18n import N_, tr_marked
from app.core.schedule import last_fire

# How long after a scheduled time a backup may still start.
START_DEADLINE = timedelta(minutes=360)
# How long one backup may run once started.
MAX_RUNTIME = timedelta(minutes=1440)
# A restore is operator-initiated: it should start soon or not at all.
RESTORE_START_DEADLINE = timedelta(minutes=60)

# The job must stay valid for a start at the deadline plus a full run: the
# agent cancels a job's context at its `expires_at`.
BACKUP_JOB_TTL = START_DEADLINE + MAX_RUNTIME
RESTORE_JOB_TTL = RESTORE_START_DEADLINE + MAX_RUNTIME


def rfc3339(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


#: `backup_policies.window_cron` is a `String(120)`.
_WINDOW_MAX = 120
INVALID_WINDOW = N_(
    "window_cron must be a cron expression of five fields (minute hour day-of-month month "
    "day-of-week), or null for no window"
)


class InvalidWindow(ValueError):
    """A `window_cron` the sweep could not read. Its text is the sentence the
    API answers, in the person's language."""

    def __init__(self) -> None:
        super().__init__(tr_marked(INVALID_WINDOW))


def checked_window(cron: str | None) -> str | None:
    """The `window_cron` a policy may store (re-review of b805432, Important).

    `None` is no window: backups on demand only. An empty or blank string
    means the same and is stored as `None`, so "send nothing" and "send an
    empty field" are one thing rather than a value the sweep would choke on.
    Anything else is read with `last_fire` -- the parser `decide` uses at every
    sweep -- and refused (`InvalidWindow`) if it raises: the window sweep runs
    one transaction per organisation, so one expression it cannot read rolls
    back every device's window in that organisation, every 15 minutes. What
    passes is stored stripped of surrounding blanks.

    Two refusals come BEFORE the parser, because it runs here on a request:
    an expression longer than the column (120), and a number of more than two
    digits -- no cron field reaches 100, and `_parse_cron_field` expands a
    range into a set before it checks the bounds, so "0-999999999" would
    hold the request (and its memory) for as long as that takes."""
    if cron is None or not cron.strip():
        return None
    if len(cron) > _WINDOW_MAX or any(len(n) > 2 for n in re.findall(r"\d+", cron)):
        raise InvalidWindow()
    try:
        # Every field is parsed before any date is looked at, so one minute
        # of lookback reads the whole expression without scanning eight days.
        last_fire(cron, datetime(2026, 1, 1, tzinfo=UTC), lookback=timedelta(minutes=1))
    except ValueError:
        raise InvalidWindow() from None
    return cron.strip()


@dataclass(frozen=True)
class ScheduleDecision:
    action: Literal["none", "dispatch", "missed"]
    fire_at: datetime | None = None


def decide(
    cron: str | None,
    now: datetime,
    *,
    protected_since: datetime,
    last_started_at: datetime | None,
    running: bool,
) -> ScheduleDecision:
    """What the sweep should do for one (device, policy) at `now`.

    `last_started_at` is the newest run's `started_at` (any state, including
    `missed`, whose `started_at` is the fire it stands for). `protected_since`
    stops a new device or policy from inheriting a fire that predates it.
    """
    if cron is None:
        return ScheduleDecision("none")
    fire = last_fire(cron, now)
    if fire is None or fire < protected_since:
        return ScheduleDecision("none")
    if last_started_at is not None and last_started_at >= fire:
        return ScheduleDecision("none")
    if now - fire > START_DEADLINE:
        return ScheduleDecision("missed", fire)
    if running:
        return ScheduleDecision("none")
    return ScheduleDecision("dispatch", fire)


def run_start_deadline(params: dict[str, Any]) -> datetime | None:
    """A `backup.run` job's signed `not_after_start`, if it carries one."""
    raw = params.get("not_after_start")
    return parse_rfc3339(raw) if isinstance(raw, str) else None


def grant_window_end(
    *,
    start_deadline: datetime | None,
    started_at: datetime | None,
    expires_at: datetime,
    now: datetime,
) -> tuple[datetime | None, str]:
    """Until when an exchange may grant for a live job, or `(None, reason)`.

    With a start deadline, a job is served only if it started by then, and only
    until started + `MAX_RUNTIME`: the long TTL exists to cover a late start
    plus a full run, not to give a late start extra time. Without one (a job
    dispatched before this rule) `expires_at` stays the only bound.
    """
    end = expires_at
    if start_deadline is not None:
        if (started_at or now) > start_deadline:
            return None, "started_after_deadline"
        if started_at is not None:
            end = min(end, started_at + MAX_RUNTIME)
    if end <= now:
        return None, "run_time_exceeded"
    return end, ""

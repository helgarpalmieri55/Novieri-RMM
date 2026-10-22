"""The platform's own backup, off-host copy and restore drill, as probes.

The work runs on the HOST (`deploy/scripts/scheduled-backup.sh`,
`restore-drill.sh`), which is the only place it can run: it needs the Docker
socket, and no container is given that. The scripts write three small JSON
files into a status directory the api and worker containers mount READ-ONLY;
these probes read them, and the ordinary sampler and
`app.core.health.notify` turn a failure into a notification.

READS FILES, NO NETWORK, NO SECRETS. The files hold result words and times
only. A missing file means "not set up", reported `ok` with
`configured: false` (the `keycloak_events` precedent): visible, not alarming.
"""

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.health.types import ProbeResult, ReasonCode, Status

BACKUP_MAX_AGE = timedelta(hours=26)
DRILL_MAX_AGE = timedelta(days=35)


class _Unreadable(Exception):
    """The file exists and is not what the scripts write."""


def _read(directory: Path, name: str) -> dict[str, Any] | None:
    path = directory / name
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unreadable from None
    try:
        body = json.loads(text)
    except ValueError:
        raise _Unreadable from None
    if not isinstance(body, dict):
        raise _Unreadable
    return body


def _time(body: dict[str, Any], key: str) -> datetime | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Unreadable
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Unreadable from None
    if parsed.tzinfo is None:
        raise _Unreadable
    return parsed


def _age(then: datetime, now: datetime) -> timedelta:
    return max(timedelta(0), now - then)


def _result(
    name: str,
    status: Status,
    reason: ReasonCode | None = None,
    **detail: str | int | bool,
) -> ProbeResult:
    return ProbeResult(name, status, 0, reason, detail or None)


def evaluate_backup(directory: Path, *, now: datetime) -> ProbeResult:
    name = "backup.nightly"
    try:
        body = _read(directory, "backup.json")
        if body is None:
            return _result(name, Status.OK, configured=False)
        last = _time(body, "last_success")
    except _Unreadable:
        return _result(name, Status.DEGRADED, ReasonCode.MISCONFIGURED)
    if last is None or _age(last, now) > BACKUP_MAX_AGE:
        detail = {} if last is None else {"hours_since_success": _hours(last, now)}
        return _result(name, Status.DOWN, ReasonCode.STALE, configured=True, **detail)
    if body.get("result") != "ok":
        return _result(
            name,
            Status.DEGRADED,
            ReasonCode.FAILED,
            configured=True,
            hours_since_success=_hours(last, now),
        )
    return _result(name, Status.OK, configured=True, hours_since_success=_hours(last, now))


def evaluate_offsite(directory: Path, *, now: datetime) -> ProbeResult:
    name = "backup.offsite"
    try:
        body = _read(directory, "offsite.json")
        if body is None or body.get("configured") is not True:
            return _result(name, Status.OK, configured=False)
        last = _time(body, "last_success")
    except _Unreadable:
        return _result(name, Status.DEGRADED, ReasonCode.MISCONFIGURED)
    if last is None or _age(last, now) > BACKUP_MAX_AGE:
        detail = {} if last is None else {"hours_since_success": _hours(last, now)}
        return _result(name, Status.DOWN, ReasonCode.STALE, configured=True, **detail)
    if body.get("result") != "ok":
        return _result(
            name,
            Status.DEGRADED,
            ReasonCode.FAILED,
            configured=True,
            hours_since_success=_hours(last, now),
        )
    return _result(name, Status.OK, configured=True, hours_since_success=_hours(last, now))


def evaluate_drill(directory: Path, *, now: datetime) -> ProbeResult:
    name = "backup.drill"
    try:
        drill = _read(directory, "drill.json")
        if drill is None:
            # Never run: count from the first nightly backup, so a fresh
            # install is not alarmed on day one and a forgotten drill is.
            backup = _read(directory, "backup.json")
            first = None if backup is None else _time(backup, "first_run")
            if first is None:
                return _result(name, Status.OK, configured=False)
            if _age(first, now) > DRILL_MAX_AGE:
                return _result(name, Status.DOWN, ReasonCode.STALE, configured=True, ever_run=False)
            return _result(name, Status.OK, configured=True, ever_run=False)
        at = _time(drill, "at")
    except _Unreadable:
        return _result(name, Status.DEGRADED, ReasonCode.MISCONFIGURED)
    if at is None:
        return _result(name, Status.DEGRADED, ReasonCode.MISCONFIGURED)
    days = _age(at, now).days
    if drill.get("result") != "pass":
        return _result(name, Status.DOWN, ReasonCode.FAILED, configured=True, days_since_drill=days)
    if _age(at, now) > DRILL_MAX_AGE:
        return _result(name, Status.DOWN, ReasonCode.STALE, configured=True, days_since_drill=days)
    return _result(name, Status.OK, configured=True, days_since_drill=days)


def _hours(then: datetime, now: datetime) -> int:
    return int(_age(then, now).total_seconds() // 3600)


async def _probe(state: Any, evaluate: Callable[..., ProbeResult]) -> ProbeResult:
    started = time.monotonic()
    directory = Path(state.settings.backup_status_dir)
    result = await asyncio.to_thread(evaluate, directory, now=datetime.now(UTC))
    return ProbeResult(
        result.name,
        result.status,
        int((time.monotonic() - started) * 1000),
        result.reason,
        result.detail,
    )


async def probe_backup(state: Any) -> ProbeResult:
    return await _probe(state, evaluate_backup)


async def probe_backup_offsite(state: Any) -> ProbeResult:
    return await _probe(state, evaluate_offsite)


async def probe_restore_drill(state: Any) -> ProbeResult:
    return await _probe(state, evaluate_drill)

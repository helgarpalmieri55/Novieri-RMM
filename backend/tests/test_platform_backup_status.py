"""The platform backup, its off-host copy and the restore drill, as probes.

The host scripts write small JSON files into a directory mounted read-only
into the api and worker containers; these probes read them. From there the
existing sampler and `app.core.health.notify` route a failure to a person.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.health.backup_status import (
    evaluate_backup,
    evaluate_drill,
    evaluate_offsite,
    probe_backup,
)
from app.core.health.types import ReasonCode, Status

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(directory: Path, name: str, body: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(body))


def _backup(
    directory: Path,
    *,
    result: str = "ok",
    success_ago: timedelta | None,
    first_ago: timedelta = timedelta(days=90),
) -> None:
    _write(
        directory,
        "backup.json",
        {
            "result": result,
            "at": _iso(timedelta(hours=1)),
            "last_success": None if success_ago is None else _iso(success_ago),
            "first_run": _iso(first_ago),
        },
    )


# --- nightly backup -----------------------------------------------------------


def test_no_status_directory_means_not_set_up(tmp_path: Path) -> None:
    result = evaluate_backup(tmp_path / "absent", now=NOW)
    assert result.status is Status.OK
    assert result.detail == {"configured": False}


def test_a_recent_success_is_ok(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=10))
    result = evaluate_backup(tmp_path, now=NOW)
    assert result.status is Status.OK
    assert result.detail == {"configured": True, "hours_since_success": 10}


def test_a_success_older_than_26_hours_is_stale(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=26, minutes=1))
    result = evaluate_backup(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.STALE)


def test_never_having_succeeded_is_stale(tmp_path: Path) -> None:
    _backup(tmp_path, result="failed", success_ago=None)
    result = evaluate_backup(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.STALE)


def test_a_failed_run_is_reported_before_it_goes_stale(tmp_path: Path) -> None:
    _backup(tmp_path, result="failed", success_ago=timedelta(hours=3))
    result = evaluate_backup(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DEGRADED, ReasonCode.FAILED)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"last_success": 5}'])
def test_an_unreadable_status_is_misconfigured(tmp_path: Path, content: str) -> None:
    (tmp_path / "backup.json").write_text(content)
    result = evaluate_backup(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DEGRADED, ReasonCode.MISCONFIGURED)


# --- off-host copy --------------------------------------------------------------


def test_offsite_not_configured_is_ok(tmp_path: Path) -> None:
    _write(tmp_path, "offsite.json", {"configured": False, "mode": "none"})
    assert evaluate_offsite(tmp_path, now=NOW).detail == {"configured": False}
    assert evaluate_offsite(tmp_path / "absent", now=NOW).status is Status.OK


def test_offsite_failed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "offsite.json",
        {
            "configured": True,
            "mode": "s3",
            "result": "failed",
            "at": _iso(timedelta(hours=1)),
            "last_success": _iso(timedelta(hours=25)),
        },
    )
    result = evaluate_offsite(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DEGRADED, ReasonCode.FAILED)


def test_offsite_stale(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "offsite.json",
        {
            "configured": True,
            "mode": "rsync",
            "result": "ok",
            "at": _iso(timedelta(hours=30)),
            "last_success": _iso(timedelta(hours=30)),
        },
    )
    result = evaluate_offsite(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.STALE)


def test_offsite_ok(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "offsite.json",
        {
            "configured": True,
            "mode": "s3",
            "result": "ok",
            "at": _iso(timedelta(hours=2)),
            "last_success": _iso(timedelta(hours=2)),
        },
    )
    assert evaluate_offsite(tmp_path, now=NOW).status is Status.OK


# --- restore drill -----------------------------------------------------------------


def test_a_failed_drill(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=1))
    _write(tmp_path, "drill.json", {"result": "fail", "at": _iso(timedelta(days=2))})
    result = evaluate_drill(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.FAILED)


def test_a_passed_drill_within_35_days(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=1))
    _write(tmp_path, "drill.json", {"result": "pass", "at": _iso(timedelta(days=34))})
    result = evaluate_drill(tmp_path, now=NOW)
    assert result.status is Status.OK
    assert result.detail == {"configured": True, "days_since_drill": 34}


def test_no_drill_in_35_days_is_stale(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=1))
    _write(tmp_path, "drill.json", {"result": "pass", "at": _iso(timedelta(days=36))})
    result = evaluate_drill(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.STALE)


def test_no_drill_ever_counts_from_the_first_backup(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(hours=1), first_ago=timedelta(days=10))
    assert evaluate_drill(tmp_path, now=NOW).status is Status.OK

    _backup(tmp_path, success_ago=timedelta(hours=1), first_ago=timedelta(days=40))
    result = evaluate_drill(tmp_path, now=NOW)
    assert (result.status, result.reason) == (Status.DOWN, ReasonCode.STALE)
    assert result.detail == {"configured": True, "ever_run": False}


def test_no_backups_set_up_means_no_drill_expected(tmp_path: Path) -> None:
    assert evaluate_drill(tmp_path, now=NOW).detail == {"configured": False}


# --- wiring ------------------------------------------------------------------------


async def test_the_probe_reads_the_configured_directory(tmp_path: Path) -> None:
    _backup(tmp_path, success_ago=timedelta(days=3))
    state = SimpleNamespace(settings=SimpleNamespace(backup_status_dir=tmp_path))
    result = await probe_backup(state)
    assert result.name == "backup.nightly"
    assert result.reason is ReasonCode.STALE


def test_the_backup_probes_are_registered_labelled_and_sampled() -> None:
    from app.api.probes import _ALL_PROBES, HARD_PROBES, PROBE_LABELS, WORKER_SAMPLED_PROBES

    names = {"backup.nightly", "backup.offsite", "backup.drill"}
    assert names <= set(_ALL_PROBES)
    assert names <= WORKER_SAMPLED_PROBES
    assert not names & HARD_PROBES, "a stale backup must never take the API out of rotation"
    assert {PROBE_LABELS[n] for n in names} == {
        "Nightly platform backup",
        "Off-host backup copy",
        "Restore drill",
    }


def test_the_status_directory_is_mounted_read_only_into_api_and_worker() -> None:
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml").read_text()
    )
    mount = "/var/lib/it-suite/backup-status:/var/lib/its/backup-status:ro"
    for service in ("api", "worker"):
        assert mount in compose["services"][service]["volumes"], service
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            if str(service.get("image", "")).startswith("itsuite/backend"):
                assert "docker.sock" not in str(volume), name
            if str(volume).startswith("/var/lib/it-suite"):
                assert str(volume).endswith(":ro"), (name, volume)


def test_the_alert_email_names_the_check_in_plain_words() -> None:
    from uuid import uuid4

    from app.models import DomainEvent
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        id=uuid4(),
        org_id=uuid4(),
        event_type="platform.probe_unhealthy",
        subject_type="platform_probe",
        subject_id=uuid4(),
        payload={
            "probe": "backup.drill",
            "status": "down",
            "reason": "failed",
            "since": NOW.isoformat(),
            "consecutive_samples": 3,
        },
        occurred_at=NOW,
    )
    subject, body = _render_email(event)
    assert subject == "[Toeshee] Platform problem: Restore drill is down"
    assert "Reason: the last run reported a failure" in body


def test_the_default_setting_is_the_container_path() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["backup_status_dir"].default == Path("/var/lib/its/backup-status")

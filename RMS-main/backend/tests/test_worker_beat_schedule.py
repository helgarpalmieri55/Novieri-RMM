"""Celery beat's schedule file must be writable, asserted against the deploy.

WHAT WENT WRONG, ON A LIVE DEPLOYMENT. `worker` runs
`celery -A app.worker worker --beat`. With no `--schedule`, beat writes
`celerybeat-schedule` into the working directory, which is `/srv/app` --
root-owned, because `backend.Dockerfile` drops to the unprivileged `its` user.
Beat therefore died at startup with:

    _gdbm.error: [Errno 13] Permission denied: 'celerybeat-schedule'

and every periodic task in `app/worker.py` -- the patch scan sweep, the health
alert and offline sweeps, the backup window sweep, SLA, notification delivery,
the platform error drain, every retention job -- silently never ran. Twenty-nine
tasks, none of them firing, on a platform whose whole value proposition is that
it notices things on a schedule.

WHY NOTHING CAUGHT IT. The worker's healthcheck reads *the worker's* heartbeat
out of Redis. The Celery consumer starts fine and keeps heartbeating, so the
container reports HEALTHY while the beat process inside it is a corpse. The
platform status page reads that same heartbeat and agrees. The only evidence was
a traceback in the container log at startup, which scrolls away.

WHAT THIS FILE CAN AND CANNOT PROVE. It asserts the deploy is *configured* so
beat has somewhere to write: an explicit `--schedule` path, that path inside a
mount the worker actually has, and the directory created in the image owned by
the runtime user (the pattern `backend.Dockerfile` already documents for
`its-ca`, `its-tls` and `its-secrets`). It cannot prove beat then runs -- that
needs the container, and the log line that proves it is in `deploy/README.md`.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "deploy" / "docker-compose.yml"
DOCKERFILE = REPO / "deploy" / "backend.Dockerfile"


def _worker() -> dict:
    compose = yaml.safe_load(COMPOSE.read_text())
    return compose["services"]["worker"]


def _schedule_flag(command: str) -> str:
    """The value of --schedule / -s in the worker's command, or ""."""
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token.startswith("--schedule="):
            return token.split("=", 1)[1]
        if token in ("--schedule", "-s") and index + 1 < len(tokens):
            return tokens[index + 1]
    return ""


def test_beat_is_given_an_explicit_schedule_path() -> None:
    """Without this flag beat writes to the CWD, which it cannot write to."""
    worker = _worker()
    assert "--beat" in worker["command"], (
        "this test exists for the embedded beat; if beat moved to its own "
        "service, assert against that service instead of deleting the check"
    )
    schedule = _schedule_flag(worker["command"])
    assert schedule, (
        "worker runs --beat with no --schedule, so beat writes "
        "celerybeat-schedule into WORKDIR and dies with EACCES as the "
        "unprivileged user. Every periodic task then silently never runs."
    )


def test_the_schedule_path_is_inside_a_volume_the_worker_mounts() -> None:
    """A writable directory in the image is not enough: it must survive.

    Beat's schedule file holds each task's last-run time. On a path that is not
    a volume it is lost on every `docker compose up -d`, and every periodic task
    -- including hourly and daily ones -- fires immediately on restart. The
    sweeps are idempotent, so that is survivable rather than dangerous, but it
    is a thundering herd on every deploy and not what anyone intends.
    """
    worker = _worker()
    schedule = _schedule_flag(worker["command"])
    mounts = [entry.split(":")[1] for entry in worker.get("volumes", [])]
    assert any(schedule.startswith(f"{mount}/") for mount in mounts), (
        f"--schedule={schedule} is not inside any of the worker's mounts "
        f"({mounts}), so beat's last-run state is discarded on every recreate"
    )


def test_the_schedule_directory_is_created_in_the_image_owned_by_its() -> None:
    """Docker seeds a fresh named volume from the image path, ownership included.

    A mount point that does NOT exist in the image is created root-owned
    instead, and the unprivileged process cannot write into it -- which is the
    same failure this whole file is about, just moved one directory along.
    `backend.Dockerfile` states this reasoning for the other three volumes.
    """
    worker = _worker()
    schedule = _schedule_flag(worker["command"])
    directory = schedule.rsplit("/", 1)[0]
    dockerfile = DOCKERFILE.read_text()
    assert directory in dockerfile, (
        f"{directory} is a volume mount point but is never created in "
        f"backend.Dockerfile, so Docker creates it root-owned and beat still "
        f"cannot write its schedule"
    )
    # The chown that makes it writable covers the whole tree.
    assert "chown -R its /var/lib/its" in dockerfile


def test_the_schedule_volume_is_declared() -> None:
    worker = _worker()
    schedule = _schedule_flag(worker["command"])
    named = [
        entry.split(":")[0]
        for entry in worker.get("volumes", [])
        if schedule.startswith(entry.split(":")[1] + "/")
    ]
    declared = yaml.safe_load(COMPOSE.read_text()).get("volumes") or {}
    for volume in named:
        assert volume in declared, f"{volume} is mounted but not declared"

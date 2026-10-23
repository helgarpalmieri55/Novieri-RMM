"""Per-instance liveness for the three processes, through Redis.

The API cannot probe the other two: the gateway is a mutual-TLS WebSocket
server with no HTTP surface, and the worker is a Celery consumer. Both are
reachable only by means the API must not use. So each process writes a
heartbeat and the API reads ages.

PER-INSTANCE, INCLUDING THE API'S OWN. A process that answered your status
request is alive -- that one replica. With several API containers behind
traefik, a dead replica is invisible to a request the live one served. Keys are
per-instance so the page can report `2 registered, 1 healthy` instead of `ok`.

TTL'd at 10x the interval so a retired container disappears rather than
accumulating for ever as a phantom `down`.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.core.health.types import Status

HEARTBEAT_INTERVAL_S = 15.0
_DEGRADED_AFTER = HEARTBEAT_INTERVAL_S * 3
_DOWN_AFTER = HEARTBEAT_INTERVAL_S * 10
_PREFIX = "platform:heartbeat"


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    process: str
    instances: int
    healthy_instances: int
    oldest_age_s: float
    status: Status
    version: str | None = None


async def write_heartbeat(
    redis: Any, process: str, instance: str, version: str, *, now: float
) -> None:
    await redis.set(
        f"{_PREFIX}:{process}:{instance}",
        json.dumps({"ts": now, "version": version}),
        ex=int(_DOWN_AFTER),
    )


async def read_heartbeats(redis: Any, *, now: float) -> list[ProcessHealth]:
    keys = sorted(await redis.keys(f"{_PREFIX}:*"))
    if not keys:
        return []
    raw = await redis.mget(keys)
    by_process: dict[str, list[tuple[float, str]]] = {}
    for key, value in zip(keys, raw, strict=True):
        if value is None:
            continue
        process = key.split(":")[2]
        payload = json.loads(value)
        by_process.setdefault(process, []).append((float(payload["ts"]), payload["version"]))
    out: list[ProcessHealth] = []
    for process, entries in sorted(by_process.items()):
        ages = [now - ts for ts, _v in entries]
        healthy = sum(1 for age in ages if age <= _DEGRADED_AFTER)
        oldest = max(ages)
        if healthy == 0:
            status = Status.DOWN if oldest > _DOWN_AFTER else Status.DEGRADED
        elif healthy < len(entries):
            status = Status.DEGRADED
        else:
            status = Status.OK
        out.append(
            ProcessHealth(
                process=process,
                instances=len(entries),
                healthy_instances=healthy,
                oldest_age_s=oldest,
                status=status,
                version=entries[0][1],
            )
        )
    return out

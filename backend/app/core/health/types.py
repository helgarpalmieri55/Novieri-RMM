"""Probe vocabulary. The reason enum is the security-relevant part.

REASONS ARE CODES, NEVER TEXT. `/livez` and `/readyz` are unauthenticated by
necessity -- a Docker healthcheck cannot present a token, and a probe that
needed the auth stack could not report the auth stack being broken. The single
likeliest thing a probe will ever report is a connection failure, and a
connection error's `str()` carries the DSN, which carries a password. The
logging kernel (sub-project 1) spent four fix waves on exactly that class; this
enum is how that lesson is carried into the health surface.
"""

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class Status(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class ReasonCode(enum.StrEnum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    SEALED = "sealed"
    EMPTY_CACHE = "empty_cache"
    MISCONFIGURED = "misconfigured"
    STALE = "stale"
    # A recorded run reported failure (the platform backup and restore drill).
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    status: Status
    duration_ms: int
    reason: ReasonCode | None = None
    # Measured scalars only -- counts, ages, booleans. Never an exception's
    # text, never a URL, never a hostname.
    detail: dict[str, str | int | bool] | None = None


Probe = Callable[[Any], Awaitable[ProbeResult]]

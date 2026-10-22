"""Error monitoring: capture, fingerprinting, recording (docs/superpowers/specs
/2026-08-15-error-monitoring-design.md).

Kernel infrastructure, like platform health: it reports on the platform every
module runs on, so it does not register through `modules.register_all`.

THE INVARIANT (spec §0.1): a captured record carries no exception text. Not
the message, not the traceback, not the locals. See `capture.CapturedError`
for what is kept instead.
"""

from app.core.errors.capture import (
    FINGERPRINT_VERSION,
    QUEUE_MAX,
    CapturedError,
    capture_exception,
    drained,
    dropped_count,
    fingerprint_of,
)
from app.core.errors.store import (
    EVENT_RETENTION_DAYS,
    drain_once,
    prune_error_events,
)

__all__ = [
    "EVENT_RETENTION_DAYS",
    "FINGERPRINT_VERSION",
    "QUEUE_MAX",
    "CapturedError",
    "capture_exception",
    "drain_once",
    "drained",
    "dropped_count",
    "fingerprint_of",
    "prune_error_events",
]

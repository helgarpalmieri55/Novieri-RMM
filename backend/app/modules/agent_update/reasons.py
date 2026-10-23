"""Stable reason tokens the agent reports for `agent.update` (spec §4.4).

The Go twin is `agent/internal/update/reasons.go`. Refusals arrive as
`jobs.result = {"refused": token}`, handler failures as `{"error": token}`,
and deferred outcomes as `{"outcome": ..., "reason": token}`.
"""

from typing import Any

__all__ = [
    "RETRYABLE_REFUSALS",
    "UNKNOWN_TYPE",
    "reason_from_result",
]

#: Refusals that return a target to `pending` with a backoff (spec §6.3).
RETRYABLE_REFUSALS = frozenset({"busy", "update_in_progress"})

#: The runner's refusal for a job type the agent does not know: an agent that
#: predates self-update.
UNKNOWN_TYPE = "unknown_type"

_MAX_TOKEN = 64


def _token(text: str) -> str:
    """The leading token of an agent-written reason, e.g. `unknown_type: ...`."""
    head = text.strip().split(":", 1)[0].strip()
    return head[:_MAX_TOKEN]


def reason_from_result(result: dict[str, Any] | None) -> str:
    """The reason token in a stored `jobs.result`, or `""`."""
    if not result:
        return ""
    for key in ("reason", "refused", "error"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return _token(value)
    return ""

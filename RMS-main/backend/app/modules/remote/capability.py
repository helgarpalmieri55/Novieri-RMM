"""Can this agent share its screen? One rule, read by two places.

The agent reports the job types it can run (`agents.capabilities`, written by
the gateway from the agent's self-report header). A screen session is a
`remote.screen.open` job, so an agent that has not reported that capability is
refused with 409 "agent too old to share its screen" (`service._start_screen`).

`GET /devices` serves the same answer per device as `screen_capable`, so the
console can say so BEFORE the operator asks for a session rather than after the
server refuses one (`docs/design/cross-module-guidance.md` §2 Remote #2). Both
call `can_share_screen`; neither carries a copy of the rule.
"""

from __future__ import annotations

SCREEN_JOB_TYPE = "remote.screen.open"


def can_share_screen(capabilities: object) -> bool:
    """True only when the agent reported `remote.screen.open` as supported.

    `capabilities` is typed `object` because the column is JSON and an agent
    row predating the self-report holds `{}` -- or, from a hand-edited row,
    anything. Only a dict with a truthy entry counts.
    """
    return isinstance(capabilities, dict) and bool(capabilities.get(SCREEN_JOB_TYPE))

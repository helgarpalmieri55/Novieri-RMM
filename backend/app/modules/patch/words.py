"""Why a restart closed, said in the request's language.

`PatchReboot.reason` and the `patch.reboot_*` audit entries are written in
English and stay English (the audit chain is never translated). The console
prints the reason as the restart's explanation (`restartCopy.ts`), so
`said_reason` translates it at the response, from the stored English.

The fixed reasons are `N_` constants here, used both where the restart is closed
and where the answer is built, so the two cannot drift. The one reason that
carries a value (`the restart job {status} three times`) is matched against its
template and said with the console's word for the job status
(`enums.JobStatus` in `frontend/src/i18n/es/enums.ts`).
"""

import re

from app.core.i18n import N_, tr, tr_marked
from app.core.wire_words import say

REASON_DEVICE_RESTARTED = N_("the device restarted")
REASON_OS_NO_LONGER_NEEDS = N_("the OS no longer needs a restart")
REASON_POLICY_NEVER_RESTARTS = N_("the policy no longer restarts devices")
REASON_AGENT_CANNOT_RESTART = N_("the agent cannot restart this device; update the agent")

_FIXED_REASONS = (
    REASON_DEVICE_RESTARTED,
    REASON_OS_NO_LONGER_NEEDS,
    REASON_POLICY_NEVER_RESTARTS,
    REASON_AGENT_CANNOT_RESTART,
)

#: `JobStatus` wire value -> catalogue key. The console shows these as the job's
#: status ("Fallido"); the key carries its own context for the flat catalogue.
JOB_STATUS_WORDS = {
    "queued": N_("job status: queued"),
    "dispatched": N_("job status: dispatched"),
    "running": N_("job status: running"),
    "succeeded": N_("job status: succeeded"),
    "failed": N_("job status: failed"),
    "expired": N_("job status: expired"),
    "refused": N_("job status: refused"),
    "cancelled": N_("job status: cancelled"),
}

_JOB_FAILED_THREE_TIMES = re.compile(r"^the restart job (?P<status>[a-z_]+) three times$")


def said_reason(reason: str | None) -> str | None:
    """The stored English reason in the current language; anything this module
    does not recognise (an older or hand-written reason) is returned as stored."""
    if reason is None:
        return None
    if reason in _FIXED_REASONS:
        return tr_marked(reason)
    matched = _JOB_FAILED_THREE_TIMES.match(reason)
    if matched is not None:
        return tr(
            "the restart job {status} three times",
            status=say(JOB_STATUS_WORDS, matched["status"], marked=True),
        )
    return reason

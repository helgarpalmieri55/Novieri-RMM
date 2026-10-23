"""The technician persona, as a frozen profile (§2.2, §3.2).

`may_propose=True` and `replay_policy="neutralize_flagged"`, for the same reasons
the admin persona carries them -- and with more force. This persona reads TICKET
THREADS, which contain text created by `email_intake` from messages anyone who
can reach the helpdesk mailbox can send. It is the persona most likely to have a
laundered instruction in its own transcript.

**IT PROPOSES ONLY AGAINST THE TICKET'S LINKED DEVICE** (§3.2), and that bound is
not expressed here: it is `ProposeBinding.ticket_device_id`, set per turn by this
persona's router from `ticket.device_id`, and checked as step 6 of `propose`. It
cannot be expressed on a frozen profile because it is a fact about one ticket.

`executor` is the STAFF loopback: a technician has a real `AuthContext` and a
real bearer token, so `require_permission` and RLS re-run verbatim and the
copilot has no power the technician does not. (The end-user resolver is the one
that cannot use this executor -- §0.2 -- because a portal principal is
deliberately not an `AuthContext`.)
"""

from app.core.ai import PersonaProfile, register_persona
from app.core.ai.executor_staff import invoke_tool
from app.modules.ai_copilot.prompt import SYSTEM_PROMPT
from app.modules.ai_copilot.tools import COPILOT_TOOLS

COPILOT_PROFILE = register_persona(
    PersonaProfile(
        name="technician",
        package="app.modules.ai_copilot",
        system_prompt=SYSTEM_PROMPT,
        executor=invoke_tool,
        tools=COPILOT_TOOLS,
        may_propose=True,
        replay_policy="neutralize_flagged",
    )
)

__all__ = ["COPILOT_PROFILE"]

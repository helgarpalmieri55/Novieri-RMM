"""The `end_user` persona, as a frozen profile (§2.2, §3.1, §0.9).

**`may_propose=False`, AND IT IS NOT THE MECHANISM.** A flag defaults, gets
flipped once, and nothing fails. The mechanisms are structural and there are
three: this profile's `tools` mapping contains no act tool and the router builds
none, this package registers no action route (`tests/test_ai_portal.py` derives
that from the flattened router), and `ai_autonomy_ceiling`'s
`ck_ai_autonomy_ceiling_end_user_is_off` CHECK makes an `end_user` row above
`off` unstorable -- so even a build that flipped the flag would resolve `off` and
mint nothing. The flag is the DECLARATION; those three are the enforcement.

`replay_policy="verbatim"` and that is correct HERE, for the reason §0.8 gives
and only because the premise still holds: a turn that cannot act cannot be made
to act by laundered instructions replayed into its context. This persona cannot
act at any dial position, so its read-mode argument never expires the way the
admin persona's did. It is also the only persona for which a stub would EDIT WHAT
A CUSTOMER WAS TOLD ABOUT THEIR OWN TICKET, which is a worse trade than the one
it would buy. If this persona is ever given any act capability -- which §0.9 and
§14 both refuse -- this line must flip in the same commit.

`executor` is `invoke_portal_tool`, NOT the staff one (§0.2). Two executors, two
types, no branch: this one refuses any tool outside `/api/v1/portal/` before a
URL exists, and there is no `AuthContext` in reach to build a staff request from.
"""

from app.core.ai import PersonaProfile, register_persona
from app.core.ai.executor_portal import invoke_portal_tool
from app.modules.ai_portal.prompt import SYSTEM_PROMPT
from app.modules.ai_portal.tools import PORTAL_TOOLS

RESOLVER_PROFILE = register_persona(
    PersonaProfile(
        name="end_user",
        package="app.modules.ai_portal",
        system_prompt=SYSTEM_PROMPT,
        executor=invoke_portal_tool,
        tools=PORTAL_TOOLS,
        may_propose=False,
        replay_policy="verbatim",
    )
)

__all__ = ["RESOLVER_PROFILE"]

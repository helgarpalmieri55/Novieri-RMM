"""The admin persona, as a frozen profile (docs/modules/ai-automation.md §2.2).

This is the whole of what the admin persona contributes to the orchestration
service: a name, its own system prompt, its read-tool set, the loopback
executor it runs under, and two policy facts. Everything else -- the turn loop,
the providers, the envelope, the scrubber, the heuristic, the store, the
retention sweep -- is kernel code under `app/core/ai/` and does not know this
persona exists.

**`tools=REGISTRY` IS A DELIBERATE IDENTITY, AND IT IS THIS PERSONA'S ONLY
PLACE.** The read-tool allowlist is KERNEL (`app/core/ai/tools/`, §19.9): it is
platform capability, and every staff persona is built from the same objects so
one `response_fields` list stays under one drift test. The allowlist is the one
`docs/modules/ai-admin/spec.md` §4 authored for THIS persona and §3.3 leaves
unchanged in read mode, so this profile takes the mapping whole, where
`app/modules/ai_copilot/tools.py` writes its names down -- a narrower persona
declares its subset, the widest one does not restate the list. Neither is
`dict(REGISTRY)`: sharing the mapping OBJECT is what makes a tool added to the
kernel reach the admin persona, which is the intent, and `MappingProxyType` is
what stops any code widening it at runtime.

`replay_policy="neutralize_flagged"` IS THE PERSONA'S OPT-IN, NOT THE CONDITION.
`app/core/ai/orchestrator.py::replay_policy_for` combines it with the SESSION's
mode: this persona's read sessions still replay flagged answers verbatim, and
only an act-capable session gets the server-authored stub. §0.8 states the rule
as a property of the session ("for a session whose `mode` is act-capable"), and
that is the only reading under which §13 test 14 -- the same flagged answer,
stubbed in one mode and verbatim in the other -- can be true of one profile.

Read mode's verbatim replay is still CORRECT for read mode, for the reason §0.8
gives: a turn that cannot act cannot be made to act by laundered instructions
replayed back into its context, because a fully successful injection reaches a
frozen GET allowlist and nothing else. `may_propose` is what removes that
premise, and it is now `True`.

**`may_propose=True` IS NOT SUFFICIENT ON ITS OWN, AND THAT IS THE POINT.** It is
one of THREE conditions the router checks before an act tool is built for a turn:
the persona opts in here, the SESSION must have been created act-capable (§0.8 --
fixed at creation, with no promotion path), and the caller must hold
`ai.act.propose`. And even then the tool can only mint a PROPOSAL: dispatch needs
a second human, `ai.act.confirm`, the owning module's own dispatch permission,
and a digest that still matches (§9.2).

`tools` STAYS A MAPPING OF `ReadTool` ONLY. The act tool is built per turn by the
router and passed to `run_turn`, never registered here (§18.9): it carries one
turn's session id, `message_seq`, actor and taint, and a tool object holding
those inside a frozen profile would carry them into the next turn.
"""

from app.core.ai import PersonaProfile, register_persona
from app.core.ai.executor_staff import invoke_tool
from app.core.ai.tools import REGISTRY
from app.modules.ai_admin.prompt import SYSTEM_PROMPT

ADMIN_PROFILE = register_persona(
    PersonaProfile(
        name="admin",
        package="app.modules.ai_admin",
        system_prompt=SYSTEM_PROMPT,
        executor=invoke_tool,
        tools=REGISTRY,
        may_propose=True,
        replay_policy="neutralize_flagged",
    )
)

__all__ = ["ADMIN_PROFILE"]

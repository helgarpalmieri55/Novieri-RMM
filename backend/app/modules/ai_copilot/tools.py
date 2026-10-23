"""The technician copilot's read-tool set (§3.2).

**A NAMED SUBSET OF THE PLATFORM'S READ ALLOWLIST, DECLARED HERE AND RESOLVED
FROM ONE DEFINITION.** §3.2 gives this persona "the admin tool set intersected
with what the technician's grants actually permit". The second half is a RUNTIME
property and is not this file's job: `tools_for_permissions` prefilters and the
loopback executor re-runs `require_permission` verbatim, so a technician without
`devices.read` gets a 403 from the real route whatever this dict says. The first
half is this file: the names, written down, so widening the copilot's reach is a
reviewed code change rather than a consequence of somebody widening the admin
persona's.

**THE OBJECTS COME FROM THE KERNEL, WHICH IS §19.9's FIX AND NOT ITS
WORKAROUND.** This file shipped importing them from `app.modules.ai_admin.tools`
-- one persona package depending on another, which is not what §0.1 wants. The
three options were:

1. **Copy the two `ReadTool` definitions into this file.** Each carries a
   `response_fields` frozenset that `tests/test_ai_read_tools.py` compares
   against the live response model. A second copy is a second list that the
   drift test does not check, so the day the health module adds a field, the
   admin persona's build goes red and THIS one keeps serving the new field to a
   model. That is precisely the defect `registry.py`'s own docstring exists to
   prevent ("a set computed from the live model would grow WITH the model"),
   reproduced by hand.
2. **Import the objects from the admin persona.** One definition, one drift
   test, one wrong-direction import. What v1 shipped, and what §19.9 recorded.
3. **Move the registry to `app/core/ai/tools/`**, where `registry.py`'s own
   docstring already said the concept belongs ("a read tool is PLATFORM
   CAPABILITY, authored by the platform", as against `approved_action`, which is
   org policy).

**(3) IS NOW DONE**, so the import below reaches kernel code both personas
depend on, in the direction every other module obeys.
`tests/test_ai_boundaries.py::test_no_persona_package_imports_another_persona_
package` is what keeps it that way: it derives the package list from `PERSONAS`,
so the option-(2) import cannot come back quietly, and the persona added next is
covered the day it registers. (1) remains the only option that is silently
wrong, and it is silently wrong whichever package the registry lives in.
"""

from collections.abc import Mapping
from types import MappingProxyType

from app.core.ai.tools import REGISTRY as _PLATFORM_READ_TOOLS
from app.core.ai.types import ReadTool

#: The names this persona may call, written down rather than derived.
#:
#: **A NAME LIST, NOT `dict(REGISTRY)`.** `COPILOT_TOOLS = REGISTRY` would make
#: the copilot's reach a function of the admin persona's, so a tool added there
#: for a platform-ops question -- audit search, say -- would silently become
#: available inside a ticket, to a persona reading text an internet sender wrote.
#: Every name here is one somebody decided a technician should have while
#: working a ticket.
COPILOT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # The ticket's linked device, and why it is unhealthy. This is the
        # question a technician actually asks inside a ticket.
        "devices.health",
        # The alerts that would explain the symptom the requester reported.
        "health.alerts",
    }
)

_MISSING = COPILOT_TOOL_NAMES - set(_PLATFORM_READ_TOOLS)
if _MISSING:  # pragma: no cover - import-time guard, see below
    # AT IMPORT TIME, NOT AT CALL TIME. A name that no longer resolves would
    # otherwise produce a persona whose tool set is quietly one tool smaller,
    # and the only symptom would be a copilot that stopped being able to answer
    # a question nobody was watching.
    raise RuntimeError(  # i18n-exempt: import-time assertion, the process never starts
        f"the copilot names read tools that do not exist: {sorted(_MISSING)}; "
        f"a tool set that silently shrinks is a capability change nobody reviewed"
    )

#: A `MappingProxyType` for the reason the platform registry is one: the type
#: annotation stops a type checker, not code. `COPILOT_TOOLS[name] = tool` from
#: any module in the process must be a `TypeError`, because §0.2's claim is that
#: the allowlist is compiled into the image.
COPILOT_TOOLS: Mapping[str, ReadTool] = MappingProxyType(
    {name: _PLATFORM_READ_TOOLS[name] for name in sorted(COPILOT_TOOL_NAMES)}
)

__all__ = ["COPILOT_TOOLS", "COPILOT_TOOL_NAMES"]

"""The AI orchestration service, shared by every persona.

docs/modules/ai-automation.md §0.1, §2.1, §2.3.

**THE BOUNDARY IN CODE IS EXACT.** Anything that touches a model provider, a
retrieved payload, or the audit log lives here. Anything that knows a persona's
NAME lives in that persona's module. `app/core/ai/` imports no `app.modules.*`
package -- `tests/test_ai_boundaries.py` asserts that over this package's
imports -- and a persona module imports `app.core.ai` and its own domain.

WHAT THAT COSTS, AND WHY IT IS PAID. Read mode's machinery shipped inside
`app/modules/ai_admin/`, and the security guarantees it carries were anchored to
`tests/test_ai_admin_boundaries.py`'s `PACKAGE_ROOT`. Moving the code without
moving those assertions would have silently un-asserted the egress-site count,
the tool-type guard and the prompt-constant property -- "otherwise the first
act-mode task is also the task that quietly deleted read mode's proof", which
§0.1 names in as many words. So `tests/test_ai_boundaries.py` scans THIS root,
and the module-scoped file keeps the assertions that are genuinely about the
admin persona and now additionally asserts that a persona package constructs no
egress client of its own.

WHY NOT LEAVE IT WHERE IT WAS AND HAVE THE OTHER PERSONAS IMPORT IT. That makes
one business module a de facto kernel that is not reviewed as one, and inverts
the dependency direction every other module obeys. It is also how
`approved_action` was nearly built; the kernel registry's own docstring records
the reasoning for `app/core/` instead, and this is the same argument about the
same problem.

This module re-exports the concept the way `app/core/approved_actions.py` does,
so a caller writes `from app.core.ai import PersonaProfile, run_turn` rather
than learning the file layout.
"""

from app.core.ai.errors import (
    AiDisabledError,
    AiError,
    AiNotFoundError,
    AiOrgMismatchError,
    AiValidationError,
    NonceLeakError,
)
from app.core.ai.orchestrator import (
    DEFAULTS,
    ToolCallRecord,
    TurnBudgets,
    TurnFinding,
    TurnOutcome,
    finish_turn,
    run_turn,
    tools_for_permissions,
)
from app.core.ai.personas import (
    PERSONAS,
    LoopbackExecutor,
    PersonaProfile,
    ReplayPolicy,
    register_persona,
)
from app.core.ai.types import ReadTool, Tool, ToolResult, ToolSpec, TurnContext

__all__ = [
    "DEFAULTS",
    "PERSONAS",
    "AiDisabledError",
    "AiError",
    "AiNotFoundError",
    "AiOrgMismatchError",
    "AiValidationError",
    "LoopbackExecutor",
    "NonceLeakError",
    "PersonaProfile",
    "ReadTool",
    "ReplayPolicy",
    "Tool",
    "ToolCallRecord",
    "ToolResult",
    "ToolSpec",
    "TurnBudgets",
    "TurnContext",
    "TurnFinding",
    "TurnOutcome",
    "finish_turn",
    "register_persona",
    "run_turn",
    "tools_for_permissions",
]

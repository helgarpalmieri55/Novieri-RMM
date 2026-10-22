"""The platform's read-tool allowlist, and the kernel seam it is expressed in.

docs/modules/ai-admin/spec.md §4, §12.1; docs/modules/ai-automation.md §2.1,
§2.2, §19.9.

**THIS IS KERNEL, AND IT USED TO BE THE ADMIN PERSONA'S.** `registry.py`'s own
docstring already said what a read tool is -- "PLATFORM CAPABILITY, authored by
the platform", as against `approved_action`, which is ORG POLICY authored by an
org-admin. It nonetheless lived in `app/modules/ai_admin/tools/`, so the
technician copilot had to import its `ReadTool` objects from another persona's
package (§19.9). Living here, one persona no longer depends on another and the
`response_fields` drift test has exactly one list to keep honest.

**THE REJECTED ALTERNATIVE WAS COPYING THE `ReadTool` DEFINITIONS**, and it is
worth naming because it is the one that looks cheapest. Each entry carries a
`response_fields` frozenset that `tests/test_ai_read_tools.py` compares against
the LIVE response model. A second copy is a second list that drift test does not
check, so the day another module adds a field, one persona's build goes red and
the other keeps serving the new field to a model -- precisely the defect
`registry.py` exists to prevent, reproduced by hand.

Importing `REGISTRY` from here rather than from `registry` keeps the denylists
(`_NEVER_A_TOOL`, `_FORBIDDEN_GATES`) private-by-convention to the package and
to its tests, which is where they are asserted.

`ReadTool` AND ITS FRIENDS LIVE IN `app/core/ai/types.py` and are re-exported
here for the readers that already import them from this path. The type is
kernel-owned because every persona's tool set is made of it, and because
`ReadTool.method` being `Literal["GET"]` is a property of the ORCHESTRATION
SERVICE rather than of any one persona -- a second copy under a module would be
a second answer to "can a tool mutate".
"""

from app.core.ai.tools.registry import REGISTRY
from app.core.ai.types import ReadTool, Tool, ToolResult, ToolSpec, TurnContext

__all__ = ["REGISTRY", "ReadTool", "Tool", "ToolResult", "ToolSpec", "TurnContext"]

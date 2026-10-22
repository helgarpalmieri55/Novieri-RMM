"""The tool seam (spec §12.1, §12.2) and the one tool type read mode has.

`ReadTool` IS THE TYPE THAT CANNOT EXPRESS A MUTATION (§0.4). `method` is
`Literal["GET"]`, not a `read_only: bool` flag. A flag defaults, gets set wrong
once, and nothing fails; a type that cannot express a mutation cannot be set
wrong. That makes the boundary between read mode and act mode something a BUILD
can fail on rather than something a reviewer has to notice.

**Nothing here goes past spec §12.2.** No `ActTool`, no action registry, no
autonomy field, no per-action risk configuration, nothing that reads
`app/core/approved_actions.py`. Act mode is a hard dependency on the AI
Automation substrate, and growing this file past the seam is the failure mode
the roadmap names.

ONE DELIBERATE DEVIATION FROM §12.1, RECORDED RATHER THAN PAPERED OVER. The
spec writes the protocol member as `def describe(self) -> ToolSpec`. A generated
description (§4.1) is assembled from the LIVE ROUTE's summary and response-model
schema, and a `ReadTool` deliberately holds no reference to the app -- it is a
frozen row of data compiled into the image, and holding an app reference would
make the registry a runtime object rather than a constant. So in v1 the
description is produced by the free function `describe.describe_tool(tool,
route)`, which the orchestrator calls with the route it resolved. `ReadTool`
therefore satisfies `Tool`'s `name` and `invoke` members but not its zero-arg
`describe`; the protocol is written below exactly as the spec states it so act
mode's `ActTool` -- which DOES hold its own `approved_action` row and can
describe itself with no argument -- implements it unchanged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    """What the model is shown about one tool: a stable name, a GENERATED
    description (§4.1 -- never hand-written prose, which drifts from the API
    silently and no test can catch), and the closed JSON Schema of its
    arguments."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """One successful tool call's outcome.

    `mutating` is reserved by §12.2 and is **always `False` in read mode**. The
    audit writer and the UI branch on it today, so act mode adds no branch to
    either. Nothing in this module ever sets it `True`; a test greps for that.
    """

    payload: Any
    status: int
    result_count: int
    truncated: bool
    mutating: bool = False


@dataclass(frozen=True)
class TurnContext:
    """Everything one turn's tool calls need, and nothing that outlives the turn.

    `bearer` IS EXCLUDED FROM THE REPR. §0.1 keeps the caller's token off
    `AuthContext` because that dataclass is passed into every module and a token
    on it is one careless `logger.debug(ctx)` from living in a log file forever.
    `TurnContext` is module-private and per-turn, but the same accident is one
    line away, so the repr redacts and a test asserts it.
    """

    app: Any
    bearer: str = field(repr=False)
    deadline: float = 0.0
    org_id: UUID | None = None
    user_id: UUID | None = None

    def __repr__(self) -> str:
        return (
            f"TurnContext(org_id={self.org_id!r}, user_id={self.user_id!r}, "
            f"deadline={self.deadline!r}, bearer=<redacted>)"
        )

    __str__ = __repr__


class Tool(Protocol):
    """§12.1. The orchestrator holds a `list[Tool]` and knows nothing else about
    the read/act distinction."""

    name: str

    def describe(self) -> ToolSpec: ...

    async def invoke(self, ctx: TurnContext, args: Mapping[str, Any]) -> ToolResult: ...


@dataclass(frozen=True)
class ReadTool:
    """One allowlisted `GET` route, exactly spec §4.1's fields.

    `permission` is the code the route is ACTUALLY gated on, read out of
    `route_permission(route)` at registration time and asserted equal to it by
    `test_ai_admin_registry.py`. It is never the code somebody expected.

    **THERE IS NO PER-TOOL FIELD EXEMPTION HERE, AND THERE MUST NEVER BE ONE.**
    This dataclass carried `sensitive_fields: frozenset[str]`, which
    `scrub._walk` consulted BEFORE the secret-key pattern and `continue`d past --
    so adding `sensitive_fields=frozenset({"password"})` to an entry in
    `registry.py` converted a fail-closed block into a silent drop in one line,
    while `scrub.py`'s own docstring said no exemption mechanism existed. No
    registered tool ever used it. A field on THIS type is the shape such a hatch
    takes, because it never touches `scrub`'s signature and so never trips the
    guard test that watches the signature; `test_ai_admin_scrub.py` now asserts
    the behaviour instead -- a bypass collection attached to a tool, under any
    name, does not make a secret-shaped key pass.
    """

    name: str
    # §0.4. The read/act boundary, as a type.
    method: Literal["GET"]
    path: str
    permission: str
    args: type[BaseModel]
    # Additive to the generated description, never a replacement for it (§4.1).
    # A tool needing more than one sentence of hint is a signal the module's API
    # is not clean enough (§0.3), and the fix belongs in that module.
    hint: str = ""
    max_limit: int | None = None
    # SECURITY GATE 3's CONTROL (§7.3). Every key name this tool's response may
    # contain, AT ANY DEPTH; `scrub` blocks the entire result on any key that is
    # not in here. It is the opposite polarity to the `sensitive_fields` hatch
    # described above -- it can only ever make a result MORE likely to be
    # blocked, and declaring a name does not stop the secret-key pattern from
    # blocking it -- which is why it is a declaration and not an exemption.
    #
    # THE DEFAULT IS EMPTY, AND EMPTY MEANS "BLOCK EVERY KEY". A tool added
    # without a declaration returns nothing to the model rather than everything,
    # which is the only safe direction for a field a reviewer forgot.
    #
    # It is a FROZEN CONSTANT, deliberately not read off the live
    # `route.response_model`: a set derived from the model at runtime would let
    # a newly added field allowlist itself, which is precisely the case this
    # gate exists for. `test_ai_admin_registry.py` compares it to the live model
    # so drift is a red build naming the field.
    response_fields: frozenset[str] = frozenset()

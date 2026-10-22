"""Request and response models for the technician copilot (§12).

**NO MODEL HERE CAN EXPRESS A TICKET WRITE.** There is no comment body, no
status, no assignee and no priority field anywhere in this file, and that is the
enforcement of §0.3 at the edge of the system: a route cannot post what its
request model cannot carry. A draft reaches the technician as ANSWER TEXT in
`CopilotTurnOut`, which their editor pastes and they send under their own name --
because a human did send it.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CopilotSessionCreate(BaseModel):
    """`mode` and nothing else, exactly as the admin surface (§0.8).

    No `persona`: this router IS the technician persona's surface and passes
    `COPILOT_PROFILE.name`. A client-supplied persona would let a technician open
    a session labelled `admin`, served by this persona's tool set, on the column
    the autonomy dial and every `ai_action` row are keyed by.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["read", "act"] = "read"


class CopilotSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    persona: str
    mode: str
    model_id: str
    message_count: int


class CopilotTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)


class CopilotTurnOut(BaseModel):
    """One turn's answer, its flags, and what it proposed.

    `proposed` carries IDS ONLY. The preview a human confirms is served by the
    admin surface's `GET /ai/actions/{id}` -- one renderer, one shape, one place
    §6.3's two-region layout is implemented. A second preview payload here would
    be a second place that separation could be got wrong.
    """

    answer: str
    stop_reason: str
    partial: bool
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    flagged: list[dict[str, str]] = Field(default_factory=list)
    proposed: list[UUID] = Field(default_factory=list)

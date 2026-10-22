"""Request and response models for the resolver (§12).

**THERE IS NO ACTION MODEL HERE, AND NO `mode` FIELD.** A session on this surface
is always `read`: §0.9 gives this persona no act at any dial position, and a
`mode` field would be a field a client could send `act` in, which the create
handler would then have to refuse. A field that does not exist cannot be refused
wrongly -- the same argument §0.10 makes about the provider base URL.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResolverSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    mode: str
    message_count: int


class ResolverTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class ResolverTurnOut(BaseModel):
    """The answer, and NOTHING ABOUT THE PLATFORM.

    No `model_id`, no token counts, no `stop_reason` vocabulary and no tool
    names: this response is read by a customer, and every one of those is
    internal detail about how the answer was produced. `partial` survives because
    a truncated answer that does not say it is truncated is a wrong answer, and
    `flagged` survives as a COUNT rather than as sources -- "something in this
    conversation looked odd" is actionable; a rule name and a record id are
    reconnaissance.
    """

    answer: str
    partial: bool
    flagged_count: int = 0

    @classmethod
    def of(cls, *, answer: str, partial: bool, findings: list[Any]) -> "ResolverTurnOut":
        return cls(answer=answer, partial=partial, flagged_count=len(findings))

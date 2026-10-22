"""The resolver's read-tool set: the portal's own routes, and nothing else.

docs/modules/ai-automation.md §3.1, §11.1, §11.2.

**FIVE TOOLS, ALL UNDER `/api/v1/portal/`, ALL GATED `portal.ticket.read`.**
That is the entire read surface: the requester's own ticket list, one ticket's
detail (whose `comments` the portal's own query already restricts to PUBLIC ones
-- this persona gets no route the portal UI does not have), the org's ticket
categories, and the help articles staff marked visible in the portal (search
and read one; migration 0160's policy keeps every other article out).
`executor_portal.invoke_portal_tool` refuses anything outside that prefix
before a URL exists, so this list being wrong is caught by the executor
rather than by review.

**DECLARED HERE RATHER THAN IMPORTED FROM ANOTHER PERSONA**, unlike the
copilot's, and the difference is not inconsistency: the copilot's tools ARE the
staff read allowlist and sharing the objects keeps one `response_fields` list
under one drift test. These are portal routes, which no other persona may reach,
so there is nothing to share and an import would only create a dependency.

`response_fields` IS THE FAIL-CLOSED CONTROL, NOT DOCUMENTATION (§7.3). Every key
name the response may carry AT ANY DEPTH is listed; `scrub` blocks the ENTIRE
result on anything else. The lists below are read off `app/modules/portal/
schema.py`'s models, and `tests/test_ai_portal.py` compares them to those models
so the day the portal adds a field, the build goes red and the tool goes dark
until somebody looks at the new field. **That is the point**: this persona's
retrieved text is written by whoever can email the helpdesk, and a field that
allowlists itself is how a staff-authored internal string reaches them.
"""

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.ai.types import ReadTool

__all__ = ["PORTAL_TOOLS"]


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _MyTicketsArgs(BaseModel):
    """`GET /api/v1/portal/tickets`.

    NO `requester_id` FIELD, AND THERE MUST NEVER BE ONE. Which requester's
    tickets are returned is decided by `portal_session`'s `app.current_requester`
    GUC, pinned by the route from the authenticated principal. A field here would
    be a requester id the MODEL supplies -- downstream of a ticket body an
    internet sender wrote -- and the route would have to decide whether to
    believe it.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1, le=50, description="How many tickets.")


class _MyTicketArgs(BaseModel):
    """`GET /api/v1/portal/tickets/{ticket_id}`.

    A ticket id belonging to somebody else is a 404, and that is enforced by RLS
    -- `portal_session` pins both GUCs and the policy predicate resolves to the
    caller's own `requester_id`. This tool does not filter; the database does.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID = Field(description="One of your own tickets.")


class _HelpSuggestArgs(BaseModel):
    """`GET /api/v1/portal/help/suggest`. Only articles staff marked visible in
    the portal can come back; the database enforces that, not this tool."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=300, description="What the problem is about.")
    limit: int | None = Field(default=None, ge=1, le=10, description="How many articles.")


class _HelpArticleArgs(BaseModel):
    """`GET /api/v1/portal/help/articles/{article_id}`. Any article not marked
    visible in the portal is a 404."""

    model_config = ConfigDict(extra="forbid")

    article_id: UUID = Field(description="A help article from a suggestion.")


_TOOLS = (
    ReadTool(
        name="portal.my_tickets",
        method="GET",
        path="/api/v1/portal/tickets",
        permission="portal.ticket.read",
        args=_MyTicketsArgs,
        hint="the tickets this person raised, most recently updated first",
        max_limit=25,
        response_fields=frozenset(
            {
                "id",
                "number",
                "type",
                "status",
                "priority",
                "subject",
                "created_at",
                "updated_at",
            }
        ),
    ),
    ReadTool(
        name="portal.my_ticket",
        method="GET",
        path="/api/v1/portal/tickets/{ticket_id}",
        permission="portal.ticket.read",
        args=_MyTicketArgs,
        hint="one of this person's own tickets, with its public replies",
        response_fields=frozenset(
            {
                # PortalTicketDetail
                "id",
                "number",
                "type",
                "status",
                "priority",
                "subject",
                "description",
                "created_at",
                "updated_at",
                "comments",
                "status_history",
                "attachments",
                "approval",
                # PortalCommentOut -- PUBLIC comments only; the portal's own
                # query and the RLS policy both exclude internal notes, and
                # there is no `visibility` field to filter on because only one
                # value is ever reachable.
                "body",
                # PortalStatusHistoryOut. `reason` is deliberately absent from
                # that model -- unbounded staff free text with no visibility
                # column -- so it is absent here too.
                "from_status",
                "to_status",
                # PortalAttachmentOut
                "filename",
                "byte_size",
                # PortalApprovalOut
                "state",
                "reason",
                "decided_at",
            }
        ),
    ),
    ReadTool(
        name="portal.categories",
        method="GET",
        path="/api/v1/portal/categories",
        permission="portal.ticket.read",
        args=_NoArgs,
        hint="the categories this organization offers, for suggesting one",
        response_fields=frozenset({"id", "key", "name", "type"}),
    ),
    ReadTool(
        name="portal.help_suggest",
        method="GET",
        path="/api/v1/portal/help/suggest",
        permission="portal.ticket.read",
        args=_HelpSuggestArgs,
        hint="help articles this organization published for its users, matching a problem",
        max_limit=10,
        response_fields=frozenset({"id", "title"}),
    ),
    ReadTool(
        name="portal.help_article",
        method="GET",
        path="/api/v1/portal/help/articles/{article_id}",
        permission="portal.ticket.read",
        args=_HelpArticleArgs,
        hint="the text of one published help article",
        response_fields=frozenset({"id", "title", "body"}),
    ),
)

#: A `MappingProxyType` for the reason every registry in this layer is one: the
#: `Mapping` annotation stops a type checker, not code, and §0.2's claim is that
#: an allowlist is compiled into the image rather than widenable at runtime.
PORTAL_TOOLS: Mapping[str, ReadTool] = MappingProxyType({tool.name: tool for tool in _TOOLS})

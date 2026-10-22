"""The end-user tier-1 resolver: the persona with the most hostile input and the
least reach, and that inversion IS the design.

docs/modules/ai-automation.md §0.2, §0.9, §3.1, §11.1, §11.2, §12.

**IT PROPOSES NOTHING, AT ANY DIAL POSITION, EVER** (§0.9). Not by a flag alone:
the profile carries no act tool, this package registers no action route, and
`ai_autonomy_ceiling`'s `ck_ai_autonomy_ceiling_end_user_is_off` CHECK makes an
`end_user` row above `off` unstorable in the database. Three independent
mechanisms, because the text this persona reads arrives through the
`email_intake` ingress -- anyone who can send a message to the helpdesk mailbox
writes the ticket bodies it reads.

**IT USES ITS OWN EXECUTOR** (§0.2). A `PortalContext` is deliberately not an
`AuthContext`, portal reads run under `portal_session` with BOTH GUCs pinned, and
the staff executor's whole security argument is that it carries a STAFF token
through the stack. Handed a portal principal's token every staff route 403s --
and the "fix" for that is a service token, which is the privilege-escalation
primitive the staff executor's own docstring warns about.

**TWO SCOPE CUTS, MADE TESTABLE RATHER THAN INTENDED** (§11.1, §11.2). The tool
set contains no device tool -- the platform cannot tell which device a requester
owns, and `ticket.device_id` is asserted by whoever created the ticket, which for
an email-created ticket is a parser reading text an unauthenticated sender wrote.
And it reaches no `/docs` route -- the library is an internal runbook store. The
operator approved one narrow exception (2026-09-23): articles staff mark
"visible in the portal", read through the portal's own `/portal/help` routes,
which the database confines to that slice (migration 0160). Anything wider has
hit §11.1 or §11.2 and must stop and report, not derive one.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.ai_portal.api import router

    app.include_router(router, prefix="/api/v1")

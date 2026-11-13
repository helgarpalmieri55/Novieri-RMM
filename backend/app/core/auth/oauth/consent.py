"""The consent screen's two questions (design §5.2, §2.5, §2.7; plan D11,
B3-09, B3-10, B3-15; operator decisions C1 and D2).

`details`: what the person is asked, or why there is nothing to ask.
`decide`: their answer. Both say FIRST whether this person may connect AI
apps here at all, before the request is read (A3-31): the session's
organization is the deployment's (`deployment_org`, the one the token
endpoint redeems codes in; B3-09), MCP is switched on there and the person
holds `mcp.use` on some client -- what `app/mcp/server.py::_gate` checks on
every MCP call. While they may not, the request is checked against what is
already stored and no client metadata document is read (`_not_read`), and
nothing is written (`record_use=False`).

THE ANSWERS (Appendix A.2; the console page, Task 4):
- `consent`: the question;
- `refused`: shown and sent nowhere -- a bad client or redirect (RFC 6749
  §4.1.2.1), a person refused before the redirect could be trusted, or
  `temporarily_unavailable` when a metadata document cannot be read just now;
- `back`: what the app must hear -- an error, a decline, a person who may not
  connect -- with the host it goes to; the page follows it only when the
  person clicks (RFC 9700 §4.11.2: "SHOULD only automatically redirect the
  user agent if it trusts the redirection URI"; B3-15);
- `redirect`: only an approval, whose redirect carries the code. `details`
  never answers it, and neither does a decline.
An installation that cannot connect anybody -- a console address with no
issuer (`_ready`), no deployment organization (`_refusal`) -- answers 503
instead, to everybody alike.

AN APPROVAL needs the second factor confirmed in the last ten minutes (the
route's `require_step_up`; C1). It issues a 60-second code (Task 7's
`issue_code`) bound to the app, the exact redirect, the PKCE challenge and
the resource; it is audited `mcp.app_authorized` (§7), naming the app and
the host it returns to, never the code; and, as the LAST act of the
transaction (mail does not roll back), the person is told "You connected
<host>" (`notices.tell(..., "app_connected")`). Declining issues nothing and
is neither audited nor mailed (Task 8, decision 3).

THE APPROVAL'S TRANSACTION TAKES THE PERSON'S ROW FIRST (own authentication
phase 2's rule, `guards.lock_user_row`; operator decision D2, 2026-11-12;
`oauth.store`'s LOCK ORDER). The request is validated, and its app resolved
(`clients.resolve_client`, which may store a document's copy or purge an idle
one, each in a transaction of its own), BEFORE this transaction opens: that
purge gives way on a lock wait, and would otherwise wait on the hold this
same request takes below. Then, in one transaction:

1. the person's `users` row FOR NO KEY UPDATE, re-read, the person still
   active and the session still live under it (`guards.locked_user`). An act
   that ended the session -- signing out everywhere, a disable, a reset, a
   deletion -- took that row first and has committed by now, so an approval
   that waited for it is refused (401 `auth_required`) and issues nothing;
2. the app's row, `clients.hold`: FOR KEY SHARE, re-read, the idle condition
   re-checked; an app purged since is "not registered here";
3. the code (`grants.issue_code`, which takes no lock of its own);
4. the app's use, `clients.touch`, flushed before the chain;
5. the audit chain;
6. the notice, the last act (its relay checkout audits in the same chain).

WHAT THE PERSON IS SHOWN (`_client_out`; B3-10): the app's name as it gave
it, the host its answer goes back to (MCP: "MUST clearly display the redirect
URI hostname" [S30]), the host that publishes its metadata document (CIMD-03
§10.10), and whether every address it registered is this computer ([S30]:
"SHOULD display additional warnings for localhost-only redirect URIs"). Every
host is `redirects.host_of`'s, Task 1's one reading of a URL, which a WHATWG
browser agrees with: never another parse of the redirect or the `client_id`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from app.core.audit import audit_service
from app.core.auth import notices
from app.core.auth.guards import locked_user, sign_in_again
from app.core.auth.oauth import cimd
from app.core.auth.oauth.authorize import (
    AuthorizationRequest,
    NotRedirectable,
    Redirectable,
    answer,
    not_registered,
    validate,
)
from app.core.auth.oauth.clients import hold, mcp_switched_on, touch
from app.core.auth.oauth.errors import DECLINED, PERSON_CANNOT, Problem
from app.core.auth.oauth.grants import issue_code
from app.core.auth.oauth.policy import SCOPE, issuer
from app.core.auth.oauth.redirects import host_of, stays_on_this_computer
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource
from app.core.rbac import codes_held_anywhere
from app.core.tenancy import org_session

if TYPE_CHECKING:
    # The door's context, as the route hands it over; a type only, so the
    # core never imports the API layer at run time.
    from app.api.deps import AuthContext

Answer = dict[str, Any]


def _ready(request: Request) -> str:
    settings = request.app.state.settings
    iss = issuer(settings)
    if iss is None or mcp_resource(settings) is None:
        raise HTTPException(
            503,
            tr(
                "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without "
                "a path."
            ),
        )
    return iss


async def _not_read(client_id: str) -> cimd.ClientDocument:
    """The fetch while the person may not connect AI apps: nothing is read
    (A3-31). Defence in depth, never reached today: it goes with
    `record_use=False`, and with that `resolve_client` answers before any
    fetch -- the stored copy of a document, expired or not, so the way back
    can be offered, or `unavailable` without one. Should that order ever
    change, this still reads nothing."""
    raise cimd.DocumentRefused("unavailable")  # i18n-exempt: a reason code


async def _refusal(request: Request, ctx: AuthContext) -> str | None:
    """Why this person may not connect AI apps here, or None (plan D11, B3-09).

    An installation with no deployment organization -- `ITS_DEFAULT_ORG_SLUG`
    unset, or naming none -- is nobody's refusal: the token endpoint redeems
    codes in that organization only, so nobody can connect here. The
    installation's 503, as `_ready`'s (Task 8 review, fix round 1)."""
    deployment = await deployment_org(request.app)
    if deployment is None:
        raise HTTPException(
            503,
            tr(
                "MCP is not available: ITS_DEFAULT_ORG_SLUG names no organization on this "
                "installation."
            ),
        )
    if ctx.org_id != deployment:
        return tr(
            "AI apps connect to this installation's own organization, and you are signed in to "
            "another one"
        )
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        if not await mcp_switched_on(session, ctx.org_id):
            return tr(
                "MCP is turned off for this organization. An org-admin turns it on in "
                "Settings → AI connections."
            )
        if "mcp.use" not in await codes_held_anywhere(session, ctx.user_id):
            return tr("requires {code}", code="mcp.use")
    return None


def _back(
    to: AuthorizationRequest | Redirectable, iss: str, reason: str, detail: str, problem: Problem
) -> Answer:
    return {
        "outcome": "back",
        "reason": reason,
        "detail": detail,
        "redirect_host": host_of(to.redirect_uri),
        "redirect_to": answer(
            to.redirect_uri,
            to.state,
            iss,
            error=problem.error,
            error_description=problem.description,
        ),
    }


async def _question(
    request: Request, ctx: AuthContext, query: str
) -> tuple[str, AuthorizationRequest | Answer]:
    """What both answers share: who may connect, then the request."""
    iss = _ready(request)
    refusal = await _refusal(request, ctx)
    try:
        checked: AuthorizationRequest | Redirectable = await validate(
            request.app,
            org_id=ctx.org_id,
            query=query,
            now=datetime.now(UTC),
            fetch=None if refusal is None else _not_read,
            record_use=refusal is None,
        )
    except NotRedirectable as refused:
        if refusal is not None:
            return iss, {"outcome": "refused", "error": PERSON_CANNOT.error, "detail": refusal}
        return iss, {"outcome": "refused", "error": refused.error, "detail": refused.detail}
    except Redirectable as error:
        checked = error
    if refusal is not None:
        return iss, _back(checked, iss, "denied", refusal, PERSON_CANNOT)
    if isinstance(checked, Redirectable):
        detail = tr("this AI app's request is malformed; going back tells the app what was wrong")
        return iss, _back(checked, iss, "error", detail, checked.problem)
    return iss, checked


def _document_host(checked: AuthorizationRequest) -> str | None:
    """The host that publishes a metadata document client's description, or
    None for a registered app."""
    client = checked.client
    if client.kind != "metadata_document":
        return None
    return host_of(client.client_id) or None


def _app_host(checked: AuthorizationRequest) -> str:
    """The host the screen's heading names (B3-10): the metadata document's,
    or the redirect's."""
    return _document_host(checked) or host_of(checked.redirect_uri)


def _client_out(checked: AuthorizationRequest) -> Answer:
    client = checked.client
    return {
        "name": client.client_name,
        "client_id": client.client_id,
        "kind": client.kind,
        "client_host": _document_host(checked),
        "redirect_host": host_of(checked.redirect_uri),
        "loopback_only": all(stays_on_this_computer(uri) for uri in client.redirect_uris),
    }


def _gone() -> Answer:
    """The app was purged, or went idle, between the question and the
    approval's hold: what the question itself answers for an app this
    organization does not have (`authorize.not_registered`)."""
    return {"outcome": "refused", "error": "invalid_client", "detail": not_registered()}


async def details(request: Request, ctx: AuthContext, *, query: str) -> Answer:
    _, checked = await _question(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    return {
        "outcome": "consent",
        "client": _client_out(checked),
        "scope": SCOPE,
        "account": ctx.email,
    }


async def decide(request: Request, ctx: AuthContext, *, query: str, approve: bool) -> Answer:
    """The route has already asked for the recent second factor when
    `approve` (C1). An approval runs in the order of the module docstring:
    the person's row, the app's, the code, the use, the chain, the notice."""
    iss, checked = await _question(request, ctx, query)
    if isinstance(checked, dict):
        return checked
    if not approve:
        detail = tr("you did not connect this AI app; going back tells it so")
        return _back(checked, iss, "declined", detail, DECLINED)
    principal = ctx.principal
    if principal is None:  # never: the route admits only a console session
        raise sign_in_again()
    code: str | None = None
    async with oauth_session(ctx.org_id, request.app.state.app_engine) as session:
        # 1. The person's row, and their session still live under it (D2).
        user = await locked_user(session, principal)
        now = datetime.now(UTC)
        # 2. The app's row: an idle app may be gone by now.
        client = await hold(session, org_id=ctx.org_id, app_id=checked.client.id, now=now)
        if client is not None:
            # 3. The code.
            code = await issue_code(
                session,
                org_id=ctx.org_id,
                user_id=user.id,
                oauth_client_id=client.id,
                redirect_uri=checked.redirect_uri,
                code_challenge=checked.code_challenge,
                resource=checked.resource,
                now=now,
            )
            # 4. The use, flushed: the chain's statement does not autoflush.
            touch(client, now)
            await session.flush()
            # 5. The chain.
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=user.id,
                action="mcp.app_authorized",
                target_type="user",
                target_id=str(user.id),
                metadata={
                    "app": client.client_id,
                    "name": client.client_name or "",
                    "redirect_host": host_of(checked.redirect_uri),
                },
            )
            # 6. THE LAST ACT of the transaction: mail does not roll back (C1).
            await notices.tell(
                session,
                user,
                "app_connected",
                vault=request.app.state.vault,
                actor_id=user.id,
                host=_app_host(checked),
            )
    if code is None:
        return _gone()
    return {
        "outcome": "redirect",
        "redirect_to": answer(checked.redirect_uri, checked.state, iss, code=code),
    }

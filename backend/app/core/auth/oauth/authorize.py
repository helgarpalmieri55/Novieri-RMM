"""An authorization request, as the consent screen hands it over (design
§5.2; plan D2, D8, B3-05, B3-23).

The console page `/oauth/authorize` passes the request's query here exactly
as the browser has it (Task 4). It is validated on EVERY call -- the details
and the decision both -- in RFC 6749 §4.1.2.1's two halves:

- what cannot be sent back because the address to send it to is not to be
  trusted: no client, an unknown client, a metadata document that cannot be
  this client (or cannot be read just now), no redirect where the client
  registered several, or one it did not register ("MUST NOT automatically
  redirect the user-agent to the invalid redirection URI").
  `NotRedirectable`, worded for the person;
- everything else is an error the app must hear: `Redirectable`, with the
  redirect, the state to carry and RFC 6749's problem. The consent service
  shows it with its host and sends it, with `state` (§4.1.2) and `iss` (RFC
  9207 §2: "including error responses"), only on the person's click (plan
  B3-15).

WHAT IS CHECKED: `client_id` and `redirect_uri` once each; the redirect one
the app registered (`redirects.matches`: exact, but for a loopback port), or
none when it registered exactly one (OAuth 2.1-17 §4.1.1: "OPTIONAL if only
one redirect URI is registered for this client. REQUIRED if multiple redirect
URIs are registered for this client"; plan B3-23); then no other parameter
twice (§3.1: "MUST NOT be included more than once"), `resource` excepted (RFC
8707 §2 lets it repeat); `response_type=code`; `code_challenge` of 43
base64url characters WITH `code_challenge_method=S256` said out loud (RFC 7636
§4.3: a missing method "defaults to "plain""; §4.4.1: `invalid_request`);
every `resource` the MCP resource, its scheme and host in any case
(`grants.names_resource`), and none meaning it (RFC 8707 §2.1: "MAY process
the request ... by using a predefined default resource value"; plan B3-05).
`scope` is ignored beyond `mcp` (plan D7): the token's scope is `mcp`
whatever was asked.

A HOST NAMED TO THE PERSON is `redirects.host_of`'s, Task 1's one reading,
which a WHATWG browser agrees with (B3-10, A3-01): never another parse of
the redirect or the `client_id`.

`fetch` and `record_use` are `resolve_client`'s: while the person may not
connect AI apps the consent passes a `fetch` that reads nothing and
`record_use=False`, so the question neither downloads a document nor keeps
an idle app alive (Task 8, decision 4; re-review f3-2, M-3). `resolve_client`
runs here in transactions of its own, before any transaction of the caller's
holds the app's row: it may purge an idle document's row, and that purge
gives way on a lock wait.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

from app.core.auth.oauth import cimd
from app.core.auth.oauth.clients import ClientView, Fetch, resolve_client
from app.core.auth.oauth.errors import (
    AUTHZ_CHALLENGE,
    AUTHZ_METHOD,
    AUTHZ_REPEATED,
    AUTHZ_RESPONSE_TYPE,
    BAD_TARGET,
    Problem,
)
from app.core.auth.oauth.grants import names_resource
from app.core.auth.oauth.pkce import valid_challenge
from app.core.auth.oauth.redirects import host_of, matches, with_params
from app.core.i18n import tr
from app.core.mcp_channel import mcp_resource

#: The parameter RFC 8707 §2 lets repeat.
_REPEATABLE = frozenset({"resource"})


@dataclass(frozen=True)
class AuthorizationRequest:
    client: ClientView
    redirect_uri: str
    state: str | None
    code_challenge: str
    resource: str


class NotRedirectable(Exception):
    """Shown to the person; never sent to the redirect. `detail` is a
    translated sentence; `error` RFC 6749's code (or `temporarily_unavailable`),
    a keyword so the i18n guard reads only the sentence."""

    def __init__(self, detail: str, *, error: str) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


class Redirectable(Exception):
    """An error the app must hear: the redirect it goes to, the state to
    carry (None when the request's own cannot be trusted) and the problem."""

    def __init__(self, redirect_uri: str, state: str | None, problem: Problem) -> None:
        super().__init__(problem.error)
        self.redirect_uri = redirect_uri
        self.state = state
        self.problem = problem


def parse_query(query: str) -> dict[str, list[str]]:
    """Every parameter's values; an empty one is absent (RFC 6749 §3.1)."""
    params: dict[str, list[str]] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if value:
            params.setdefault(name, []).append(value)
    return params


def not_registered() -> str:
    """The person's sentence for an app this organization does not have:
    never registered, idle past `CLIENT_IDLE`, or purged between the
    question and the approval's hold (`consent._gone`). One sentence, one
    place."""
    return tr(
        "this AI app is not registered here, or its registration expired. Connect it again "
        "from the app."
    )


def answer(redirect_uri: str, state: str | None, iss: str, **params: str) -> str:
    """The redirect back to the app: its own query kept, then `params`, then
    `state` when the request had one, then `iss`."""
    extra = dict(params)
    if state is not None:
        extra["state"] = state
    extra["iss"] = iss
    return with_params(redirect_uri, extra)


async def validate(
    app: Any,
    *,
    org_id: UUID,
    query: str,
    now: datetime,
    fetch: Fetch | None = None,
    record_use: bool = True,
) -> AuthorizationRequest:
    resource = mcp_resource(app.state.settings)
    assert resource is not None  # consent._ready refused it otherwise
    params = parse_query(query)

    def once(name: str) -> str | None:
        values = params.get(name, [])
        return values[0] if len(values) == 1 else None

    if len(params.get("client_id", [])) > 1 or len(params.get("redirect_uri", [])) > 1:
        raise NotRedirectable(
            tr("this connection request names its AI app or its return address twice"),
            error="invalid_request",
        )
    client_id = once("client_id")
    if client_id is None:
        raise NotRedirectable(
            tr("this connection request names no AI app"), error="invalid_request"
        )
    try:
        client = await resolve_client(
            app, org_id=org_id, client_id=client_id, now=now, fetch=fetch, record_use=record_use
        )
    except cimd.DocumentRefused as exc:
        # Only a document's `client_id` is refused this way, and one is a
        # document only when `host_of` reads a host in it
        # (`cimd.is_document_client_id`).
        if exc.transient:
            raise NotRedirectable(
                tr(
                    "this AI app's description could not be read from {host} just now; try "
                    "again in a moment",
                    host=host_of(client_id),
                ),
                error="temporarily_unavailable",
            ) from exc
        raise NotRedirectable(
            tr("this AI app's description could not be read from {host}", host=host_of(client_id)),
            error="invalid_client",
        ) from exc
    if client is None:
        raise NotRedirectable(not_registered(), error="invalid_client")
    redirect_uri = once("redirect_uri")
    if redirect_uri is None:
        if len(client.redirect_uris) != 1:
            raise NotRedirectable(
                tr(
                    "this connection request names no return address, and its AI app "
                    "registered more than one"
                ),
                error="invalid_request",
            )
        redirect_uri = client.redirect_uris[0]
    elif not matches(client.redirect_uris, redirect_uri):
        raise NotRedirectable(
            tr("the address this AI app asked to go back to is not one it registered"),
            error="invalid_redirect_uri",
        )

    # From here on the answer is the app's to hear.
    state = once("state")
    if any(len(values) > 1 for name, values in params.items() if name not in _REPEATABLE):
        raise Redirectable(redirect_uri, None, AUTHZ_REPEATED)  # two states: trust neither
    if once("response_type") != "code":
        raise Redirectable(redirect_uri, state, AUTHZ_RESPONSE_TYPE)
    challenge = once("code_challenge")
    if challenge is None or not valid_challenge(challenge):
        raise Redirectable(redirect_uri, state, AUTHZ_CHALLENGE)
    if once("code_challenge_method") != "S256":
        raise Redirectable(redirect_uri, state, AUTHZ_METHOD)
    if not all(names_resource(value, resource) for value in params.get("resource", [])):
        raise Redirectable(redirect_uri, state, BAD_TARGET)
    return AuthorizationRequest(
        client=client,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        resource=resource,
    )

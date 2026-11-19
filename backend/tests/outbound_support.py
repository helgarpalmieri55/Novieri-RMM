"""Every way out of the test process, routed to one handler (own authentication
phase 4; Task 11, fix round 1).

`route_outbound(monkeypatch, handler)` sends every request the process would
otherwise send to the network to `handler`, which answers it, until the test
ends. Two helpers are built on it, and nothing else should route outbound
requests by hand, so the two cannot drift apart again:

- `install_outbound` (Task 5): an `OutboundRecorder` that answers 599 and
  keeps the list. `outbound.seen == []` is how a test says that nothing left
  the platform -- no identity provider, no anyone.
- `test_auth_phase4_falsifiers.offer_the_key` (Task 11): every request gets
  the JWKS of a key the platform does not know, so a validator that refuses
  an unknown key is handed the key instead.

THE WAYS OUT, each held by a test in `test_user_admin_api.py`
(`test_the_recorder_sees_*`) or by falsifier 11's positive control:

- an `httpx.AsyncClient` or `httpx.Client` built while this is installed, with
  no transport, a network transport of its own (`app/core/egress.py`), a
  transport that is not httpx's, a mount beside an in-process transport, or a
  proxy (which httpx mounts as a network transport);
- a client built BEFORE this was installed (`app/core/vault.py`,
  `app/core/auth/keys.py` and `app/core/ca.py` build theirs at startup): httpx's
  own network transports, async and sync, answer to the handler;
- urllib (`OpenerDirector.open`), which PyJWT's `PyJWKClient` uses. A status
  of 400 or more is raised as the `urllib.error.HTTPError` urllib raises.

Only an in-process transport keeps itself: `httpx.ASGITransport` (the tests'
client, the assistant's, the MCP server's call to itself) or
`httpx.MockTransport`. urllib keeps its non-HTTP schemes (`file:`, `data:`).

The real classes are captured when this module is imported, which is at
collection, before any test patches them: a factory that resolved
`httpx.AsyncClient` by name would recurse into itself.
"""

import email.message
import io
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from typing import Any

import httpx
import pytest

Handler = Callable[[httpx.Request], httpx.Response]

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_SYNC_CLIENT = httpx.Client
_REAL_OPEN = urllib.request.OpenerDirector.open
_IN_PROCESS = (httpx.ASGITransport, httpx.MockTransport)


def route_outbound(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """From now until the test ends, every request out of the process goes to
    `handler` (the module docstring lists the ways out)."""

    def to_handler() -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    def routed(kw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(kw.get("transport"), _IN_PROCESS):
            kw["transport"] = to_handler()
        if kw.get("mounts"):
            # A mount wins over `transport` for the URLs it matches, so an
            # in-process transport does not keep a mount beside it that is not
            # one. `None` is httpx's "this pattern uses `transport`".
            kw["mounts"] = {
                pattern: mounted
                if mounted is None or isinstance(mounted, _IN_PROCESS)
                else to_handler()
                for pattern, mounted in kw["mounts"].items()
            }
        return kw

    def async_client(*args: Any, **kw: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(*args, **routed(kw))

    def sync_client(*args: Any, **kw: Any) -> httpx.Client:
        return _REAL_SYNC_CLIENT(*args, **routed(kw))

    async def network_async(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return handler(request)

    def network_sync(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return handler(request)

    def urllib_open(
        self: urllib.request.OpenerDirector, fullurl: Any, data: Any = None, *args: Any, **kw: Any
    ) -> Any:
        request = (
            fullurl
            if isinstance(fullurl, urllib.request.Request)
            else urllib.request.Request(fullurl)
        )
        if not request.full_url.startswith(("http://", "https://")):
            return _REAL_OPEN(self, fullurl, data, *args, **kw)
        if data is not None:
            request.data = data
        answer = handler(
            httpx.Request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data if isinstance(request.data, bytes) else None,
            )
        )
        headers = email.message.Message()
        for key, value in answer.headers.items():
            headers[key] = value
        body = io.BytesIO(answer.content)
        if answer.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, answer.status_code, answer.reason_phrase, headers, body
            )
        return urllib.response.addinfourl(body, headers, request.full_url, answer.status_code)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", network_sync)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib_open)


class OutboundRecorder:
    """Every request that would have left the test process (`route_outbound`
    says which), kept so that a call to anything outside the platform would
    be SEEN.

    Rule 3 of `test_user_admin_api.py`, and the same rule in
    `test_setup_api.py`, `test_me_locale.py` and `test_mcp_admin_api.py`:
    `.seen == []` holds it. Until own authentication phase 4 this was a fake
    Keycloak that answered its admin API. Nothing is answered now: a 599,
    which no real server sends, so a caller that broke the rule would fail
    visibly as well as be seen.
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(599)


def install_outbound(monkeypatch: pytest.MonkeyPatch) -> OutboundRecorder:
    """From now until the test ends, every request out of the process goes to
    a new `OutboundRecorder` (`route_outbound`; FAIL CLOSED, for the clients
    built before it too). The `test_the_recorder_sees_*` tests in
    `test_user_admin_api.py` hold each way out."""
    recorder = OutboundRecorder()
    route_outbound(monkeypatch, recorder.handler)
    return recorder

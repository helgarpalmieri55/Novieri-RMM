"""The HTTP half: the token exchange and the events fetch.

WHY THIS FILE EXISTS AT ALL, given the client is deliberately thin. "Thin" is
not "zero", and everything here is a fact about a system this project pins and
does not build: the token path, the admin path, the parameter Keycloak filters
on, and the header it wants the token in. Every one of them is a string that is
either right or produces a 404 the sweep reports at 03:00 rather than in CI.

WHAT IT STILL CANNOT SHOW: `httpx.MockTransport` answers whatever these tests
tell it to. It proves the request this code SENDS; it proves nothing about what
a real Keycloak replies, and nothing about whether `view-events` is sufficient
for the endpoint. Those are named as unverified in `docs/modules/console-auth/
spec.md` §7.1 and in `deploy/README.md`.
"""

from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.modules.keycloak_events.client import KeycloakEventsConfig, fetch_events

pytestmark = pytest.mark.asyncio

#: Captured at import, before any test swaps the name. Every factory below
#: builds from THIS, so a fixture that is still active cannot recurse into
#: another fixture's factory.
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakEventsConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-events",
    client_secret="not-a-real-secret",
)


def _transport(events: list[dict[str, Any]], seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        return httpx.Response(200, json=events)

    return httpx.MockTransport(handler)


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the transport under `httpx.AsyncClient` without changing the
    production call sites -- the client builds its own client so that its
    timeout is not something a caller can forget to pass."""
    seen: list[httpx.Request] = []
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}

    def factory(**kw: Any) -> httpx.AsyncClient:
        kwargs.update(kw)
        return _REAL_CLIENT(transport=_transport(events, seen), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return SimpleNamespace(seen=seen, events=events, kwargs=kwargs)


async def test_it_exchanges_client_credentials_then_reads_events(patched: Any) -> None:
    patched.events.append({"type": "LOGIN"})

    result = await fetch_events(CONFIG, since=date(2026, 8, 1))

    assert result == [{"type": "LOGIN"}]
    token_request, events_request = patched.seen
    assert token_request.url.path == "/realms/it-suite/protocol/openid-connect/token"
    assert events_request.url.path == "/admin/realms/it-suite/events"


async def test_the_token_is_presented_as_a_bearer_and_never_in_the_query(
    patched: Any,
) -> None:
    """A credential in a URL is a credential in every proxy log and every
    Keycloak access log between here and there."""
    await fetch_events(CONFIG, since=date(2026, 8, 1))

    _, events_request = patched.seen
    assert events_request.headers["Authorization"] == "Bearer tok-123"
    assert "tok" not in str(events_request.url)
    assert CONFIG.client_secret not in str(events_request.url)


async def test_the_window_is_sent_as_datefrom_which_is_a_date(patched: Any) -> None:
    """DAY-GRANULAR, and this parameter is why. `sweep.py` deduplicates on a
    fingerprint precisely because an hourly sweep cannot express an hourly
    window here."""
    await fetch_events(CONFIG, since=date(2026, 8, 1), limit=42)

    _, events_request = patched.seen
    assert events_request.url.params["dateFrom"] == "2026-08-01"
    assert events_request.url.params["max"] == "42"


async def test_a_refused_credential_raises_rather_than_reporting_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FAILURE THAT MUST NOT BE QUIET. A 401 that returned `[]` would make a
    revoked or mistyped credential indistinguishable from a realm with nothing
    to forward -- and the audit chain would simply stop growing, with every
    status green."""
    def factory(**kw: Any) -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda _r: httpx.Response(401, json={}))
        return _REAL_CLIENT(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_events(CONFIG, since=date(2026, 8, 1))


async def test_a_token_that_works_but_lacks_view_events_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SPECIFIC WAY THIS DEPLOYMENT GOES WRONG, and the all-401 fixture
    above cannot see it: the client-credentials exchange SUCCEEDS -- the client
    exists and its secret is right -- and the events endpoint answers 403
    because nobody assigned `view-events`. Each leg needs its own status check,
    and deleting the events leg's `raise_for_status()` turned no test red until
    this one existed. That deletion is the quiet version of the failure: the
    sweep would report zero events for ever against a credential that is not
    permitted to read them."""

    def factory(**kw: Any) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok-123"})
            return httpx.Response(403, json={"error": "Forbidden"})

        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_events(CONFIG, since=date(2026, 8, 1))

async def test_a_body_that_is_not_a_list_yields_no_events_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keycloak's error bodies are JSON objects, and a 200 carrying one is the
    shape a proxy or a misrouted path produces. Iterating it would hand
    `sweep.py` a stream of single characters."""
    def factory(**kw: Any) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok-123"})
            return httpx.Response(200, json={"error": "not-a-list"})

        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    assert await fetch_events(CONFIG, since=date(2026, 8, 1)) == []


async def test_every_request_carries_a_timeout(patched: Any) -> None:
    """A Keycloak that is slow or wedged must not hold a worker task open until
    Celery's own timeout. Asserted on the client's configuration rather than by
    hanging a fake server, because a test that waits for a timeout to elapse is
    a test that costs that timeout on every run."""
    from app.modules.keycloak_events.client import _TIMEOUT

    await fetch_events(CONFIG, since=date(2026, 8, 1))

    assert _TIMEOUT > 0
    assert patched.kwargs["timeout"] == _TIMEOUT

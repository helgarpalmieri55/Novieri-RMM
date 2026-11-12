"""Own authentication phase 2: falsifier 1 at EVERY route of the API, and a
contact's portal confined to their own tickets inside their own client
(plan 2026-11-06, Task 13, decision 1; design §10; A2-71).

Phase 1 held falsifier 1 at one route, `/me`, before any contact could sign
in. Contacts sign in now, so the claim is held at every route under /api/v1,
of every method: the router is walked (`router_inspect.flatten`, the walk
`test_route_gating.py` makes), and every route but an EXACT list of
exceptions must answer a contact's session 403 `portal_only` -- the door's
refusal, before the route's own validation or queries. The list is checked
both ways: none of it is stale, and the account's own routes that admit a
contact (D26) do not answer `portal_only`. A route added later is in the walk
without anybody listing it; one that should not be has to be argued into
the list here.

Falsifier 8 -- an unknown Microsoft account is refused, and no account and
no link is made -- is
`test_auth_entra_flows.py::test_falsifier_8_an_unknown_account_is_refused_and_nothing_is_made`
(Task 9).
"""

import asyncio
import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.models import Role, Ticket, UserRole
from tests.auth_support import console_ready, contact
from tests.session_support import CONSOLE, cookie_header, refusal_code, sign_in

_PARAM = re.compile(r"\{[^}]+\}")

#: Doors of their own: the portal's, and the sign-in's before any door.
EXCEPT_PREFIXES = ("/api/v1/portal/", "/api/v1/auth/", "/api/v1/setup/")
#: The console's sockets under /api/v1, each driven by the socket falsifier
#: below. A new one fails that test until it is added here and driven there.
CONSOLE_SOCKETS = frozenset({"/api/v1/ws", "/api/v1/remote/sessions/{session_id}/screen"})
#: The person's OWN account routes, which admit a contact (D26, Task 7).
OWN_ACCOUNT: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/me/security"),
        ("PUT", "/api/v1/me/password"),
        ("POST", "/api/v1/me/password"),
        ("DELETE", "/api/v1/me/passkeys/{passkey_id}"),
        ("DELETE", "/api/v1/me/totp"),
        ("POST", "/api/v1/me/recovery-codes"),
        ("DELETE", "/api/v1/me/trusted-devices/{device_id}"),
        ("DELETE", "/api/v1/me/sessions/{session_id}"),
        ("DELETE", "/api/v1/me/microsoft"),
    }
)


def _routes(app: Any) -> list[tuple[str, str, APIRoute]]:
    """Every `(method, path, route)` under /api/v1, HEAD and OPTIONS left out."""
    found: list[tuple[str, str, APIRoute]] = []
    for path, route in flatten(app.router):
        if not isinstance(route, APIRoute) or not path.startswith("/api/v1/"):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            found.append((method, path, route))
    return found


def _excepted(method: str, path: str) -> bool:
    return path.startswith(EXCEPT_PREFIXES) or (method, path) in OWN_ACCOUNT


#: The console's and the account's doors (`app/api/deps.py`), by name.
DOORS = frozenset({"auth_context", "bound_auth_context", "bound_account_context"})


def _calls(dependant: Any) -> set[str]:
    """Every dependency a route resolves, at any depth, by its function's name."""
    found: set[str] = set()
    for sub in dependant.dependencies:
        found.add(getattr(sub.call, "__name__", ""))
        found |= _calls(sub)
    return found


def _filled(path: str) -> str:
    return _PARAM.sub(lambda _match: str(uuid4()), path)


async def _contact_session(env: Any) -> tuple[Any, dict[str, str], str]:
    org = await env.create_org("f1-every-route")
    who = await contact(env, org)
    signed = await sign_in(env, org, who.sub)
    return who, signed.headers, signed.token


async def test_falsifier_1_no_console_route_lets_a_contact_in(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holds `_admit_session`'s contact refusal (Task 4, D42) at every route
    of every method. Without it a contact would reach RBAC, which refuses
    with another code -- or, for a code a contact's role were ever given,
    answers."""
    console_ready(api_env, monkeypatch)
    _who, headers, _token = await _contact_session(api_env)
    routes = _routes(api_env.app)
    walked = [(m, p) for m, p, _r in routes if not _excepted(m, p)]
    assert len(walked) > 300, "the walk found too few routes to mean anything"

    wrong: list[tuple[str, str, int, str | None]] = []
    for method, path in walked:
        answer = await api_env.client.request(method, _filled(path), headers=headers, json={})
        code = refusal_code(answer) if answer.status_code >= 400 else None
        if answer.status_code != 403 or code != "portal_only":
            wrong.append((method, path, answer.status_code, code))
    assert wrong == []


async def test_the_exceptions_are_exact(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both ways (A2-71): every exception is a live route, and the account's
    own routes do admit a contact -- an exception that answers `portal_only`
    hides a route that no longer needs hiding. And every portal code lives
    under /api/v1/portal/."""
    console_ready(api_env, monkeypatch)
    routes = _routes(api_env.app)
    live = {(m, p) for m, p, _r in routes}
    assert OWN_ACCOUNT - live == set()
    for prefix in EXCEPT_PREFIXES:
        assert any(p.startswith(prefix) for _m, p in live), prefix
    # The other direction for the prefixes (f2-3 M9): no route the walk skips
    # stands behind a console or account door. One that did would never be
    # walked; it belongs outside the prefixes, where the walk finds it.
    behind_a_door = sorted(
        (m, p)
        for m, p, route in routes
        if p.startswith(EXCEPT_PREFIXES) and _calls(route.dependant) & DOORS
    )
    assert behind_a_door == []
    misplaced = sorted(
        (m, p)
        for m, p, route in routes
        if (route_permission(route) or "").startswith("portal.")
        and not p.startswith("/api/v1/portal/")
    )
    assert misplaced == []

    _who, headers, _token = await _contact_session(api_env)
    for method, path in sorted(OWN_ACCOUNT):
        answer = await api_env.client.request(method, _filled(path), headers=headers, json={})
        door_refused = answer.status_code >= 400 and refusal_code(answer) == "portal_only"
        assert not door_refused, (method, path)
    portal = await api_env.client.get("/api/v1/portal/me", headers=headers)
    assert portal.status_code == 200, portal.text


async def _socket(env: Any, path: str, cookie: str) -> list[int]:
    """The close codes a WebSocket route sent to a handshake from the
    console's origin with `cookie` (`test_session_sockets.py::_messages`,
    reduced to what this needs)."""
    sent: list[dict[str, Any]] = []
    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "wss",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"itms.test"),
            (b"cookie", cookie.encode()),
            (b"origin", CONSOLE.encode()),
        ],
        "client": ("127.0.0.1", 41234),
        "server": ("127.0.0.1", 443),
        "subprotocols": [],
    }
    try:
        await asyncio.wait_for(env.app(scope, receive, send), timeout=5.0)
    except TimeoutError:
        pass
    return [m.get("code", 1000) for m in sent if m["type"] == "websocket.close"]


async def _grant(env: Any, who: Any, role_name: str) -> None:
    """`role_name` on the contact's OWN client, written past the API: a grant
    no screen offers a contact, which the refusal must outlast ("a contact who
    somehow holds a role is still a contact", `identity.contact_refusal`)."""
    async with AsyncSession(env.admin_engine) as s:
        role = (
            await s.execute(select(Role).where(Role.name == role_name, Role.org_id.is_(None)))
        ).scalar_one()
        s.add(
            UserRole(
                org_id=who.org_id, user_id=who.user_id, role_id=role.id, client_id=who.client_id
            )
        )
        await s.commit()


async def test_falsifier_1_no_console_socket_lets_a_contact_in(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console's live socket and the remote screen's tunnel refuse a
    contact (`identity.contact_refusal`, through `read_session_identity`).

    `test_domain_gate_doors.py` already holds each door's contact refusal
    through its log line. This holds what the BROWSER is answered, and makes
    that answer mean the refusal: the contact holds `remote.control` on their
    own client (the `technician` role), so without the refusal the console's
    socket would accept them and the tunnel would go on to look the session
    up -- 4404 for this random id, which is what a technician holding the same
    grant is answered (the control at the end). Without the grant, the
    tunnel's own 4403 "requires remote.control" would hide a missing refusal.

    Every console socket under /api/v1 is one of the two driven here: the set
    is read from the router, so a new one fails this test until it is added."""
    console_ready(api_env, monkeypatch)
    sockets = {
        path
        for path, route in flatten(api_env.app.router)
        if isinstance(route, APIWebSocketRoute) and path.startswith("/api/v1/")
    }
    assert sockets == CONSOLE_SOCKETS

    org = await api_env.create_org("f1-sockets")
    who = await contact(api_env, org)
    await _grant(api_env, who, "technician")
    signed = await sign_in(api_env, org, who.sub)
    cookie = cookie_header(signed.token)
    screen = f"/api/v1/remote/sessions/{uuid4()}/screen"
    assert await _socket(api_env, "/api/v1/ws", cookie) == [4403]
    assert await _socket(api_env, screen, cookie) == [4403]

    technician = await sign_in(api_env, org, await api_env.create_user(org, "technician"))
    assert await _socket(api_env, screen, cookie_header(technician.token)) == [4404]


async def test_falsifier_1_a_contacts_portal_reads_nobody_elses_ticket(
    api_env: Any, a_ticket_for: Any
) -> None:
    """A contact reads no ticket of another person IN THEIR OWN CLIENT -- the
    requester role's own rule, not only the client scope -- nor anything of
    another organization. Their own ticket, beside it, reads and is listed:
    without that control a portal that served nothing would pass."""
    org = await api_env.create_org("f1-portal")
    other_org = await api_env.create_org("f1-portal-other")
    who = await contact(api_env, org)
    signed = await sign_in(api_env, org, who.sub)
    neighbour = await a_ticket_for(org, "neighbour")
    own = await a_ticket_for(org, "own")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(Ticket).where(Ticket.id == neighbour.id).values(client_id=who.client_id)
        )
        await s.execute(
            update(Ticket)
            .where(Ticket.id == own.id)
            .values(client_id=who.client_id, requester_id=who.user_id)
        )
        await s.commit()
    stranger = await a_ticket_for(other_org, "stranger")

    for ticket in (neighbour, stranger):
        path = f"/api/v1/portal/tickets/{ticket.id}"
        one = await api_env.client.get(path, headers=signed.headers)
        assert one.status_code == 404, one.text
    mine = await api_env.client.get(f"/api/v1/portal/tickets/{own.id}", headers=signed.headers)
    assert mine.status_code == 200, mine.text
    assert mine.json()["id"] == str(own.id)
    listed = await api_env.client.get("/api/v1/portal/tickets", headers=signed.headers)
    assert listed.status_code == 200, listed.text
    assert [ticket["id"] for ticket in listed.json()] == [str(own.id)]

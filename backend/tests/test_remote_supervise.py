"""`remote.supervise` (phase 3 design §5.3, migration 0244).

Stopping or joining ANOTHER technician's remote session needs this code on the
session's client -- its machine's. `orgs.manage` no longer gives that power:
it is organisation settings, and says nothing about remote control. A stop is
audited `remote.session.ended` with the person who stopped the session as its
actor, in the session's client; it named the owner, whoever stopped it.

The rail is one predicate (`app/modules/remote/tunnel.py::owner_or_admin`)
behind two doors, the stop route and the screen tunnel's admission
(`admit_tunnel_session`). The rail tests call the stop route over HTTP and
the admission directly (`authorize_tunnel_session`, through
`tests/test_remote_tunnel.py::_authorize`, with the person's session cookie)
-- the gate the screen WebSocket runs first -- not the predicate alone; they
do not open the WebSocket itself.

Joining is viewing, and the viewer's own acts are audited as theirs (final
review A1): closing the viewer ends the session, and switching control writes
`remote.session.input_enabled` / `input_disabled`. Both named the owner
whoever held the viewer. Two writers end a session when its viewer closes --
the api's end of the leg (`screen_tunnel._finish`) and the gateway's
(`ScreenRelayHub._teardown`) -- and they race, so one test drives the real
WebSocket route and gateway, and one test per writer pins each alone.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Credential, RemotePolicy, RemoteSession
from app.modules.remote import rsp
from app.modules.remote.relay import RelayRegistration, SessionRelay
from app.modules.remote.screen_tunnel import run_screen_tunnel
from app.modules.remote.service import RemoteSessionService, end_session_from_result
from app.modules.remote.tunnel import TunnelAuthError
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.session_support import ws_cookie
from tests.test_client_record_checks import REMOTE, _device, _holder, _remote_session, _user
from tests.test_remote_screen_relay import (
    _auth_agent,
    _closed_with,
    _eventually,
    _free_port,
    _recv_type,
    stack,  # noqa: F401 - the gateway fixture, used by name
)
from tests.test_remote_service import _FakeJobEngine, _FakeRedis, _FakeVault
from tests.test_remote_tunnel import _authorize

NOT_OWNER = "requires the session owner or remote.supervise"


def _cookie(headers: dict[str, str]) -> str:
    """The session token in the `Cookie` header `ApiEnv.headers` built."""
    return headers["Cookie"].split("=", 1)[1]


async def _state(env: ApiEnv, session_id: UUID) -> str:
    async with AsyncSession(env.admin_engine) as s:
        row = await s.get(RemoteSession, session_id)
        assert row is not None
        return row.state


async def _ended(env: ApiEnv, session_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "remote.session.ended",
                        AuditEvent.target_id == str(session_id),
                    )
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def test_orgs_manage_alone_no_longer_stops_or_joins_another_s_session(
    api_env: ApiEnv,
) -> None:
    """An administrator of X who drives sessions there (`remote.control`)
    and holds `orgs.manage` on X, but not `remote.supervise`: the stop is
    the 403 with its sentence and changes nothing; the tunnel door closes
    4403.
    FALSIFIER: `owner_or_admin` asking `orgs.manage` again (`tunnel.py`)."""
    org = await api_env.create_org("sup-orgs")
    x, x_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    session = await _remote_session(api_env, org, await _device(api_env, org, x, x_site), owner)
    _, admin_of_x = await _holder(api_env, org, {x: ("remote.control", "orgs.manage")})

    r = await api_env.client.post(f"{REMOTE}/{session}/stop", headers=admin_of_x)
    assert (r.status_code, r.json()) == (403, {"detail": NOT_OWNER}), r.text
    assert await _state(api_env, session) == "awaiting_consent"
    assert await _ended(api_env, session) == []
    with pytest.raises(TunnelAuthError) as door:
        await _authorize(api_env, _cookie(admin_of_x), session)
    assert door.value.code == 4403


async def test_remote_supervise_on_x_stops_and_joins_x_s_sessions_and_not_y_s(
    api_env: ApiEnv,
) -> None:
    """The person drives sessions on X and on Y (`remote.control` on both)
    and supervises on X only. On Y's machine the rail refuses: the stop is a
    403 and the session is untouched, the door closes 4403. On X's machine
    the door admits them and the stop ends the session.
    FALSIFIER: drop `client_id=` from the `has_permission` call in
    `owner_or_admin` -- a grant on X alone then counts nowhere, and X's door
    closes too."""
    org = await api_env.create_org("sup-xy")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    on_x = await _remote_session(api_env, org, await _device(api_env, org, x, x_site), owner)
    on_y = await _remote_session(api_env, org, await _device(api_env, org, y, y_site), owner)
    supervisor_id, supervisor = await _holder(
        api_env, org, {x: ("remote.control", "remote.supervise"), y: ("remote.control",)}
    )

    refused = await api_env.client.post(f"{REMOTE}/{on_y}/stop", headers=supervisor)
    assert (refused.status_code, refused.json()) == (403, {"detail": NOT_OWNER}), refused.text
    assert await _state(api_env, on_y) == "awaiting_consent"
    with pytest.raises(TunnelAuthError) as door:
        await _authorize(api_env, _cookie(supervisor), on_y)
    assert door.value.code == 4403

    admitted_org, admitted_user, _ = await _authorize(api_env, _cookie(supervisor), on_x)
    assert (admitted_org, admitted_user) == (org, supervisor_id)
    stopped = await api_env.client.post(f"{REMOTE}/{on_x}/stop", headers=supervisor)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "ended"


async def test_a_stop_is_audited_as_the_act_of_whoever_stopped_it(api_env: ApiEnv) -> None:
    """`remote.session.ended` names the person who stopped the session, in
    the session's client: the supervisor when they stop another's session
    (it named the owner before), the owner when they stop their own.
    FALSIFIER: drop `actor_id=ctx.user_id` from the stop route (`api.py`),
    or `client_id=` from `end_session`'s audit (`service.py`)."""
    org = await api_env.create_org("sup-audit")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    owner_id, owner = await _holder(api_env, org, {x: ("remote.control",)})
    supervisor_id, supervisor = await _holder(
        api_env, org, {x: ("remote.control", "remote.supervise")}
    )
    theirs = await _remote_session(api_env, org, device, owner_id)
    own = await _remote_session(api_env, org, device, owner_id)

    r = await api_env.client.post(f"{REMOTE}/{theirs}/stop", headers=supervisor)
    assert r.status_code == 200, r.text
    r = await api_env.client.post(f"{REMOTE}/{own}/stop", headers=owner)
    assert r.status_code == 200, r.text

    (by_supervisor,) = await _ended(api_env, theirs)
    (by_owner,) = await _ended(api_env, own)
    assert (by_supervisor.actor_type, by_supervisor.actor_id, by_supervisor.client_id) == (
        "user", supervisor_id, x
    )
    assert (by_owner.actor_type, by_owner.actor_id, by_owner.client_id) == ("user", owner_id, x)
    assert by_supervisor.meta["reason"] == by_owner.meta["reason"] == "completed"


async def test_the_agent_s_end_of_a_session_is_filed_in_its_client(api_env: ApiEnv) -> None:
    """The other writer of `remote.session.ended`, the job result's
    (`end_session_from_result`): the agent is its actor, and the session's
    client its client.
    FALSIFIER: drop `client_id=` from that function's audit."""
    org = await api_env.create_org("sup-agent-end")
    x, x_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    session = await _remote_session(api_env, org, device, owner)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await end_session_from_result(
            s, session_id=str(session), device_id=device, reason="helper_exited"
        )
    (ended,) = await _ended(api_env, session)
    assert (ended.actor_type, ended.actor_id, ended.client_id) == ("agent", None, x)


# --------------------------------------------------------------------------- #
# The viewer's acts are the viewer's (final review A1)
# --------------------------------------------------------------------------- #


async def _session_trail(env: ApiEnv, org: UUID, session_id: str) -> list[AuditEvent]:
    """Every `remote.session.*` row of one session: those whose target is
    the session, and `requested` and `credential_used`, which name it in
    their metadata."""
    async with AsyncSession(env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org, AuditEvent.action.like("remote.session.%"))
                .order_by(AuditEvent.id)
            )
        ).scalars()
        return [
            r
            for r in rows
            if r.target_id == session_id or (r.meta or {}).get("session_id") == session_id
        ]


def _actor(event: AuditEvent) -> tuple[str, UUID | None, UUID | None]:
    return event.actor_type, event.actor_id, event.client_id


@contextlib.asynccontextmanager
async def _serving(app: Any) -> AsyncIterator[int]:
    """The api on a real port, for the browser's screen WebSocket."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    await _eventually(lambda: asyncio.sleep(0, server.started))
    try:
        yield port
    finally:
        server.should_exit = True
        await task


async def test_a_supervisor_who_joins_then_closes_the_viewer_is_its_actor(stack) -> None:  # noqa: F811
    """Through the real doors: the supervisor opens the screen WebSocket on
    another technician's session, switches it to view-only, then closes it.
    `input_disabled` and `ended` name the supervisor, in the session's
    client; both named the owner. The rest of the trail keeps its own
    actors, and every row of it is filed in the session's client.
    FALSIFIER: `session_screen` (`api.py`) dropping the viewer's id that
    `admit_tunnel_session` returns -- whichever of the two writers wins the
    race then names the owner."""
    api_env = stack.api_env
    enrolled = await stack.device()
    org = enrolled["org_id"]
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        # No recording: this test is about who ended it, not the upload.
        s.add(RemotePolicy(org_id=org, record_sessions=False))
    sid, params = await stack.start(enrolled)
    row = await stack.row(enrolled, sid)
    owner, client = row.technician_id, row.client_id
    supervisor_id, supervisor = await _holder(
        api_env, org, {client: ("remote.control", "remote.supervise")}
    )
    assert supervisor_id != owner

    async with _serving(api_env.app) as port:
        browser = await websockets.connect(
            f"ws://127.0.0.1:{port}/api/v1/remote/sessions/{sid}/screen",
            **ws_cookie(_cookie(supervisor)),
        )
        async with stack.agent(enrolled) as agent:
            await _auth_agent(agent, sid, params["relay_token"])
            await agent.send(rsp.state_frame("consent_granted", 1))
            assert await _recv_type(browser) == rsp.STATE
            # HELLO is forwarded only once consent is recorded, so CONTROL
            # sent after it is not dropped as early.
            await agent.send(rsp.encode(rsp.HELLO, 2, b"{}"))
            assert await _recv_type(browser) == rsp.HELLO
            await browser.send(rsp.encode(rsp.CONTROL, 1, b"\x00"))
            assert await _recv_type(agent) == rsp.CONTROL
            await browser.close()
            assert await _closed_with(agent) == rsp.CLOSE_ENDED

        async def ended() -> bool:
            return (await stack.row(enrolled, sid)).state == "ended"

        await _eventually(ended)

    trail = await _session_trail(api_env, org, sid)
    by_action = {e.action: e for e in trail}
    assert [e.action for e in trail].count("remote.session.ended") == 1
    assert _actor(by_action["remote.session.ended"]) == ("user", supervisor_id, client)
    assert _actor(by_action["remote.session.input_disabled"]) == ("user", supervisor_id, client)
    assert by_action["remote.session.ended"].meta["reason"] == "completed"
    # The owner's own acts stay the owner's; the agent's stay the agent's.
    assert _actor(by_action["remote.session.requested"]) == ("user", owner, client)
    assert _actor(by_action["remote.session.started"]) == ("user", owner, client)
    assert by_action["remote.session.consent_granted"].actor_type == "agent"
    assert {e.client_id for e in trail} == {client}, [(e.action, e.client_id) for e in trail]


async def _viewer_leg(gw: Any, enrolled: dict, *, viewer: str | None) -> tuple[str, UUID]:
    """Run a consented session with the viewer leg dialled STRAIGHT at the
    gateway's internal listener, so no api end races the relay's: switch to
    view-only, then close the viewer. Returns (session id, its owner)."""
    sid, params = await gw.start(enrolled)
    ticket = gw.ticket(sid)
    named = {} if viewer is None else {"viewer": viewer}
    async with gw.viewer() as leg, gw.agent(enrolled) as agent:
        await leg.send(
            rsp.auth_frame(session_id=sid, secret_field="ticket", secret=ticket, **named)
        )
        assert await _recv_type(leg) == rsp.AUTH_OK
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(rsp.state_frame("consent_granted", 1))
        assert await _recv_type(leg) == rsp.STATE
        await agent.send(rsp.encode(rsp.HELLO, 2, b"{}"))
        assert await _recv_type(leg) == rsp.HELLO
        await leg.send(rsp.encode(rsp.CONTROL, 1, b"\x00"))
        assert await _recv_type(agent) == rsp.CONTROL
        await leg.close()
        assert await _closed_with(agent) == rsp.CLOSE_ENDED
    return sid, (await gw.row(enrolled, sid)).technician_id


async def test_the_gateway_names_the_viewer_the_api_put_on_the_leg(stack) -> None:  # noqa: F811
    """The relay's writes of the viewer's acts name the person the api
    admitted, carried in the viewer leg's AUTH (`viewer`: the api->gateway
    leg is server-internal, and no agent frame changes), in the session's
    client.
    FALSIFIER: `ScreenRelayHub._teardown` or the CONTROL branch of
    `_on_viewer_message` (`screen_relay.py`) passing `actor_id=None` again,
    or `handle_viewer` not keeping the AUTH's `viewer` on the join."""
    enrolled = await stack.device()
    org = enrolled["org_id"]
    supervisor = await _user(stack.api_env, org)
    sid, owner = await _viewer_leg(stack, enrolled, viewer=str(supervisor))
    assert supervisor != owner
    client = (await stack.row(enrolled, sid)).client_id

    by_action = {e.action: e for e in await _session_trail(stack.api_env, org, sid)}
    assert _actor(by_action["remote.session.ended"]) == ("user", supervisor, client)
    assert _actor(by_action["remote.session.input_disabled"]) == ("user", supervisor, client)


async def test_a_viewer_leg_naming_no_viewer_is_the_owner_s_as_before(stack) -> None:  # noqa: F811
    """Mixed versions during a rolling deploy: an api too old to name its
    viewer gets what every viewer got before, the session's owner -- never
    an empty actor, never the agent. (A compatibility pin: it held before
    the fix too.)"""
    enrolled = await stack.device()
    org = enrolled["org_id"]
    sid, owner = await _viewer_leg(stack, enrolled, viewer=None)
    client = (await stack.row(enrolled, sid)).client_id

    by_action = {e.action: e for e in await _session_trail(stack.api_env, org, sid)}
    assert _actor(by_action["remote.session.ended"]) == ("user", owner, client)
    assert _actor(by_action["remote.session.input_disabled"]) == ("user", owner, client)


async def test_a_viewer_leg_naming_a_malformed_viewer_is_refused(stack) -> None:  # noqa: F811
    """`viewer` is a user id or absent. Anything else is a malformed AUTH
    (4400), refused before the ticket is looked at.
    FALSIFIER: `handle_viewer` ignoring a `viewer` it cannot read, which
    would end the session in the owner's name."""
    enrolled = await stack.device()
    sid, _ = await stack.start(enrolled)
    ticket = stack.ticket(sid)
    for bad in ("not-a-uuid", 7):
        async with stack.viewer() as leg:
            body = {"session_id": sid, "ticket": ticket, "viewer": bad}
            await leg.send(rsp.encode(rsp.AUTH, 1, json.dumps(body).encode()))
            assert await _closed_with(leg) == rsp.CLOSE_PROTOCOL, bad
    assert (await stack.row(enrolled, sid)).state == "awaiting_consent"


class _Gateway:
    """The gateway's end of the viewer leg as `run_screen_tunnel` dials it:
    admits the AUTH, then closes without ending anything, so the api's own
    end (`_finish`) is the only writer."""

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        self.sent: list[bytes] = []

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_Gateway":
        return self

    async def __aenter__(self) -> "_Gateway":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, raw: bytes) -> None:
        self.sent.append(raw)

    async def recv(self) -> bytes:
        return rsp.auth_ok_frame(str(self.session_id))

    def __aiter__(self) -> "_Gateway":
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


class _NoRegistry:
    async def register(self, reg: RelayRegistration) -> None:
        return None

    async def stop(self, session_id: UUID) -> None:
        return None


async def test_the_api_s_end_of_the_viewer_leg_names_the_viewer(api_env: ApiEnv) -> None:
    """`run_screen_tunnel` is told who the viewer is, names them in the
    viewer leg's AUTH next to the ticket, and its own end of the session
    names them, in the session's client.
    FALSIFIER: `_finish` (`screen_tunnel.py`) calling `end_session` without
    `actor_id=`, or the AUTH frame without `viewer=`."""
    org = await api_env.create_org("sup-viewer-api")
    x, x_site = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    supervisor = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    session = await _remote_session(api_env, org, device, owner)
    service = RemoteSessionService(
        _FakeJobEngine(), SessionRelay(), None, registry=_NoRegistry()  # type: ignore[arg-type]
    )
    await service.dispatch_pending(
        [
            {
                "session_id": session,
                "device_id": device,
                "registration": RelayRegistration(
                    session_id=session,
                    org_id=org,
                    device_id=device,
                    token_hash="t",
                    viewer_ticket_hash="v",
                    connect_by=datetime.now(UTC) + timedelta(seconds=60),
                    consent_mode="attended",
                    allow_input=True,
                    max_duration_s=60,
                    consent_timeout_s=30,
                ),
                "viewer_ticket": "the-ticket",
                "params": {"protocol": "screen", "record": False},
                "envelope": {},
            }
        ],
        _FakeRedis(),  # type: ignore[arg-type]
    )
    gateway = _Gateway(session)

    async def browser_send(_data: bytes) -> None:
        return None

    async def browser_recv() -> bytes | None:
        return None  # the browser closed

    await run_screen_tunnel(
        browser_send=browser_send,
        browser_recv=browser_recv,
        session_id=session,
        org_id=org,
        viewer_id=supervisor,
        service=service,
        store=lambda: None,  # type: ignore[arg-type,return-value]
        app_engine=api_env.app_engine,
        relay_internal_url="ws://gateway.invalid/relay-internal",
        dial=gateway,
    )

    auth = json.loads(rsp.decode(gateway.sent[0]).body)
    assert auth == {"session_id": str(session), "ticket": "the-ticket", "viewer": str(supervisor)}
    (ended,) = await _ended(api_env, session)
    assert _actor(ended) == ("user", supervisor, x)


async def test_every_audit_of_a_session_s_life_is_filed_in_its_client(api_env: ApiEnv) -> None:
    """`requested`, `credential_used`, `started`, a relayed event, `denied`
    and `ended` all carry the session's client. Only `ended` did, so one
    session's trail was half in its client and half outside it.
    FALSIFIER: drop `client_id=` from any of the records in `service.py`
    (`start_session` twice, `_start_screen`, `record_event`, `on_consent`,
    `mark_active`); the screen `requested` is pinned by the supervisor test
    above."""
    org = await api_env.create_org("sup-trail")
    x, x_site = await client_with_site(api_env, org)
    tech = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        s.add(RemotePolicy(org_id=org, allow_unattended=True))
        cred = Credential(org_id=org, name="win", type="windows_admin", vault_ref="v")
        s.add(cred)
        await s.flush()
        cred_id = cred.id
    service = RemoteSessionService(_FakeJobEngine(), SessionRelay(), _FakeVault())
    start: dict[str, Any] = {
        "org_id": org, "technician_id": tech, "device_id": device, "protocol": "rdp"
    }
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        lived = (
            await service.start_session(
                s, **start, requested_mode="unattended", credential_id=cred_id, reason="fix"
            )
        ).id
        refused = (
            await service.start_session(
                s, **start, requested_mode="attended", credential_id=None, reason="no"
            )
        ).id
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await service.on_consent(s, session_id=lived, accepted=True)
        await service.mark_active(s, session_id=lived, audit_ref="ref")
        await service.record_event(
            s,
            session_id=lived,
            action="remote.session.privacy_blank",
            actor_type="agent",
            actor_id=None,
            metadata={"state": "on"},
        )
        await service.end_session(s, session_id=lived, reason="completed")
        await service.on_consent(s, session_id=refused, accepted=False)

    trail = await _session_trail(api_env, org, str(lived)) + await _session_trail(
        api_env, org, str(refused)
    )
    assert sorted((e.action, e.client_id) for e in trail) == sorted(
        [
            ("remote.session.requested", x),
            ("remote.session.credential_used", x),
            ("remote.session.started", x),
            ("remote.session.privacy_blank", x),
            ("remote.session.ended", x),
            ("remote.session.requested", x),
            ("remote.session.denied", x),
        ]
    ), [(e.action, e.client_id) for e in trail]


async def test_a_relay_refusal_names_the_client_its_registration_vouches_for(stack) -> None:  # noqa: F811
    """A refusal on the relay is filed in the session's client when the
    server's own registration says which session it was: a wrong token
    burns that session's registration. Once the registration is gone, the
    session id is only the agent's word, and the refusal names no client.
    FALSIFIER: `_audit` (`screen_relay.py`) naming no client, or naming the
    client of whatever session id the agent sent."""
    enrolled = await stack.device()
    sid, _ = await stack.start(enrolled)
    client = (await stack.row(enrolled, sid)).client_id
    for _attempt in range(2):
        async with stack.agent(enrolled) as agent:
            await agent.send(rsp.auth_frame(session_id=sid, secret_field="token", secret="no"))
            assert await _closed_with(agent) == rsp.CLOSE_REFUSED
    vouched, unvouched = await stack.audits(
        enrolled, action="remote.relay.token_rejected", target_id=sid
    )
    assert (vouched.meta["reason"], vouched.client_id) == ("bad_token", client)
    assert (unvouched.meta["reason"], unvouched.client_id) == ("no_registration", None)

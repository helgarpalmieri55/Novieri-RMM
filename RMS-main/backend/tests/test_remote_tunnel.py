"""The browser WebSocket's admission gate (``authorize_tunnel``).

The screen route (``/sessions/{id}/screen``) admits a browser only with a valid
token, ``remote.control``, and ownership of the session or org-admin. These
tests drive the gate without a socket, then check the route closes a socket
that presents a bad token.
"""

import asyncio
import socket
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, RemoteSession, User
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel

# --------------------------------------------------------------------------- #
# authorize_tunnel — the WS route's auth/ownership gate (no live socket needed)
# --------------------------------------------------------------------------- #


async def _user_id_of(api_env, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _session_row(api_env, org_id, technician_id) -> UUID:
    dev_id = uuid4()
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(Device(id=dev_id, org_id=org_id, hostname="h", os_family="linux"))
        row = RemoteSession(
            org_id=org_id, device_id=dev_id, technician_id=technician_id,
            protocol="screen", consent_mode="attended", state="connecting",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _authorize(api_env, token, sid):
    return await authorize_tunnel(
        token=token, session_id=sid, settings=api_env.settings,
        jwks=api_env.app.state.jwks, admin_engine=api_env.admin_engine,
        app_engine=api_env.app_engine,
    )


async def _token(api_env, sub, org_id) -> str:
    return api_env.idp.token(sub, await api_env.org_slug(org_id))


async def test_authorize_tunnel_owner_ok(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    tech_id = await _user_id_of(api_env, tech)
    sid = await _session_row(api_env, org_id, tech_id)
    got_org, got_user = await _authorize(api_env, await _token(api_env, tech, org_id), sid)
    assert got_org == org_id and got_user == tech_id


async def test_authorize_tunnel_org_admin_non_owner_ok(api_env) -> None:
    org_id = await api_env.create_org("rc")
    owner = await api_env.create_user(org_id, "technician")
    owner_id = await _user_id_of(api_env, owner)
    admin = await api_env.create_user(org_id, "org-admin")
    sid = await _session_row(api_env, org_id, owner_id)
    # org-admin holds orgs.manage, so may drive a session it does not own
    got_org, _ = await _authorize(api_env, await _token(api_env, admin, org_id), sid)
    assert got_org == org_id


async def test_authorize_tunnel_non_owner_forbidden(api_env) -> None:
    org_id = await api_env.create_org("rc")
    owner = await api_env.create_user(org_id, "technician")
    owner_id = await _user_id_of(api_env, owner)
    other = await api_env.create_user(org_id, "technician")  # has remote.control, not owner
    sid = await _session_row(api_env, org_id, owner_id)
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, api_env.idp.token(other, await api_env.org_slug(org_id)), sid)
    assert exc.value.code == 4403


async def test_authorize_tunnel_missing_control_forbidden(api_env) -> None:
    org_id = await api_env.create_org("rc")
    ro = await api_env.create_user(org_id, "read-only")  # lacks remote.control
    ro_id = await _user_id_of(api_env, ro)
    sid = await _session_row(api_env, org_id, ro_id)
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, api_env.idp.token(ro, await api_env.org_slug(org_id)), sid)
    assert exc.value.code == 4403


async def test_authorize_tunnel_unknown_session_404(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, api_env.idp.token(tech, await api_env.org_slug(org_id)), uuid4())
    assert exc.value.code == 4404


async def test_authorize_tunnel_bad_token_401(api_env) -> None:
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, "garbage", uuid4())
    assert exc.value.code == 4401


# --------------------------------------------------------------------------- #
# Thin WS route, over a real socket: the auth gate rejects a bad token by
# closing the connection (mirrors test_ui_ws). The media path beyond the gate
# is covered by test_remote_screen_relay.py.
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def live_api(api_env):
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(api_env.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield port
    server.should_exit = True
    await task


async def test_tunnel_ws_rejects_bad_token(api_env, live_api) -> None:
    import websockets

    port = live_api
    errors = (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    url = f"ws://127.0.0.1:{port}/api/v1/remote/sessions/{uuid4()}/screen"
    with pytest.raises(errors):
        async with websockets.connect(url, subprotocols=["bearer", "garbage"]) as ws:  # type: ignore[list-item]
            await asyncio.wait_for(ws.recv(), timeout=3)


async def test_the_screen_route_reads_the_token_from_the_subprotocol_never_the_url(
    api_env, live_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access logs record URLs verbatim, so a query-string token would be a
    replayable credential in every proxy and application log. The route must
    take the token from `Sec-WebSocket-Protocol` and ignore the URL."""
    import websockets

    from app.modules.remote import api as remote_api
    from app.modules.remote.tunnel import TunnelAuthError

    seen: list[str] = []

    async def record(*, token: str, **_: object) -> tuple[object, object]:
        seen.append(token)
        raise TunnelAuthError(4401, "recorded")

    monkeypatch.setattr(remote_api, "authorize_tunnel", record)
    base = f"ws://127.0.0.1:{live_api}/api/v1/remote/sessions/{uuid4()}/screen"
    errors = (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    for url, protocols in (
        (f"{base}?token=from-the-url", None),
        (base, ["bearer", "from-the-header"]),
    ):
        with pytest.raises(errors):
            async with websockets.connect(url, subprotocols=protocols) as ws:  # type: ignore[arg-type]
                await asyncio.wait_for(ws.recv(), timeout=3)

    assert seen == ["", "from-the-header"]

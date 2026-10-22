"""The domain allowlist and `users.status`, enforced at all three doors.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §3.2,
falsifiers 5 and 6.

THREE DOORS, NOT ONE. `grep -rn "validate_token" app/ --include=*.py` finds
exactly three places that turn a token into a principal:

    app/api/deps.py:52            HTTP API              -> 403
    app/api/v1/ws.py:78           console WebSocket     -> close 4403
    app/modules/remote/tunnel.py  remote-control tunnel -> 4403

An allowlist enforced at one of three doors is not an allowlist, and nothing
in the suite would have shown the gap, so each door gets its own test rather
than trusting that they share a helper. Each door is tested three ways: a
token that passes, a token that fails, and the setting unset.

WHY 403 AND 4403. The token is genuine; the identity is simply not permitted.
A 401 would send the browser round the sign-in loop to fetch another token
that fails the same way. On the WebSocket doors, 4403 keeps "wrong domain"
distinguishable from the existing 4401 "bad token" -- a test that asserted
only "refused" would pass with the wrong code, so these assert the number.

A WORD ON WHAT THE WEBSOCKET CLOSE CODE IS OBSERVABLE THROUGH. Measured, not
assumed: under uvicorn's websockets implementation a close sent BEFORE the
handshake is accepted is delivered to the peer as an HTTP 403 handshake
response, and the numeric close code never reaches the wire. That is true of
the pre-existing 4401 path too -- this workstream did not introduce it and
does not change it. So the console-WebSocket close codes are asserted at the
ASGI boundary, where this code actually sets them, and one live-socket test
below records the end-to-end reality rather than implying the number travels.
"""

import asyncio
import logging
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
import websockets
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ws import AUTH_SUBPROTOCOL
from app.core.identity.policy import OVERRIDE_ENV_VAR
from app.models import Device, RemoteSession, User
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel
from tests.test_email_domain import REFUSAL_MESSAGE

BASE_DOMAIN = "toeshee.com"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _user_id_of(api_env, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _set_status(api_env, sub: str, status: str) -> None:
    """Set `users.status` on the BYPASSRLS admin engine.

    The write path for this column ships in the user-lifecycle workstream; the
    gate under test is the read side, so the fixture writes it directly.
    """
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(User).where(User.external_subject == sub).values(status=status)
        )
        await s.commit()


async def _session_row(api_env, org_id: UUID, technician_id: UUID) -> UUID:
    dev_id = uuid4()
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(Device(id=dev_id, org_id=org_id, hostname="h", os_family="linux"))
        row = RemoteSession(
            org_id=org_id,
            device_id=dev_id,
            technician_id=technician_id,
            protocol="rdp",
            consent_mode="attended",
            state="connecting",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _ws_messages(api_env, *, token: str, disconnect_after: bool = False) -> list[Any]:
    """Drive the console WebSocket route as a raw ASGI app and return every
    message it sent.

    Raw ASGI rather than a live server because the close CODE is the claim
    under test and uvicorn does not put it on the wire (see this module's
    docstring). This is the boundary at which `ws.py` sets it.
    """
    sent: list[Any] = []
    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    if disconnect_after:
        inbound.append({"type": "websocket.disconnect", "code": 1000})

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        await asyncio.sleep(3600)  # never reached in these tests
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/ws",
        "raw_path": b"/api/v1/ws",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"api.test")],
        "client": ("127.0.0.1", 41234),
        "server": ("127.0.0.1", 80),
        "subprotocols": [AUTH_SUBPROTOCOL, token],
    }
    await api_env.app(scope, receive, send)
    return sent


def _closes(messages: list[Any]) -> list[int]:
    return [m.get("code") for m in messages if m.get("type") == "websocket.close"]


# --------------------------------------------------------------------------- #
# Door 1 — the HTTP API (app/api/deps.py)
# --------------------------------------------------------------------------- #


async def test_http_admits_when_the_setting_is_unset(api_env) -> None:
    """THE DEFAULT CHANGES NOTHING. A gate that activates on an unset setting
    is a fleet-wide lockout the first time anyone restarts the api."""
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email="someone@completely-elsewhere.example")

    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


async def test_http_admits_a_matching_domain(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)

    for email in (f"u@{BASE_DOMAIN}", f"u@mail.{BASE_DOMAIN}", f"u@{BASE_DOMAIN.upper()}"):
        token = api_env.idp.token(sub, slug, email=email)
        response = await api_env.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, email


async def test_http_refuses_a_foreign_domain_with_403_not_401(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email="attacker@evil" + BASE_DOMAIN)

    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    # 403, never 401: the token is genuine. A 401 sends the browser back round
    # the sign-in loop to fetch another token that will fail identically.
    assert response.status_code == 403
    # The refusal does not echo the address back.
    assert "attacker" not in response.text


async def test_http_refusal_does_not_create_a_user_row(api_env) -> None:
    """`auth_context` self-heals an unknown `sub` into a `users` row. A refused
    identity must not get one -- otherwise every refused token leaves a
    provisioned account behind, and the preflight's count grows on attack
    traffic."""
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom")
    slug = await api_env.org_slug(org_id)
    sub = f"never-seen-{uuid4().hex[:8]}"
    token = api_env.idp.token(sub, slug, email=f"u@evil{BASE_DOMAIN}")

    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    async with AsyncSession(api_env.admin_engine) as s:
        found = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one_or_none()
    assert found is None


async def test_http_refuses_a_disabled_user(api_env) -> None:
    """Falsifier 6: a disabled user's unexpired token still authorizes.

    Independent of the domain setting, which stays unset here -- disabling
    somebody must work in a deployment that never configured an allowlist.
    """
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    headers = {"Authorization": f"Bearer {api_env.idp.token(sub, slug)}"}

    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 200
    await _set_status(api_env, sub, "disabled")
    # Same token, not reissued and not expired.
    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 403


async def test_http_refuses_a_deleted_user(api_env) -> None:
    org_id = await api_env.create_org("dom")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_status(api_env, sub, "deleted")
    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {api_env.idp.token(sub, slug)}"}
    )
    assert response.status_code == 403


async def test_http_still_401s_a_bad_token(api_env) -> None:
    """The pre-existing vocabulary is untouched: a token that is not genuine is
    still 401, which is what distinguishes it from 'genuine but not admitted'."""
    api_env.settings.console_email_domain = BASE_DOMAIN
    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": "Bearer garbage"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Door 2 — the console WebSocket (app/api/v1/ws.py)
# --------------------------------------------------------------------------- #


async def test_ws_admits_when_the_setting_is_unset(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email="someone@completely-elsewhere.example")

    sent = await _ws_messages(api_env, token=token, disconnect_after=True)
    assert sent[0]["type"] == "websocket.accept"
    assert _closes(sent) == []


async def test_ws_admits_a_matching_domain(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email=f"u@mail.{BASE_DOMAIN}")

    sent = await _ws_messages(api_env, token=token, disconnect_after=True)
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_refuses_a_foreign_domain_with_4403(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email=f"u@evil{BASE_DOMAIN}")

    sent = await _ws_messages(api_env, token=token)
    assert _closes(sent) == [4403]
    assert not any(m["type"] == "websocket.accept" for m in sent)


async def test_ws_refuses_a_disabled_user_with_4403(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_status(api_env, sub, "disabled")
    token = api_env.idp.token(sub, slug)

    sent = await _ws_messages(api_env, token=token)
    assert _closes(sent) == [4403]


async def test_ws_still_closes_4401_for_a_bad_token(api_env) -> None:
    """4403 must stay distinguishable from 4401. A test asserting only
    'refused' passes when the code is wrong, so both numbers are pinned."""
    api_env.settings.console_email_domain = BASE_DOMAIN
    sent = await _ws_messages(api_env, token="garbage")
    assert _closes(sent) == [4401]


async def test_ws_still_closes_4401_for_an_unknown_org(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    token = api_env.idp.token("nobody", "no-such-org", email=f"u@{BASE_DOMAIN}")
    sent = await _ws_messages(api_env, token=token)
    assert _closes(sent) == [4401]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_ws_refusal_is_real_over_a_live_socket(api_env) -> None:
    """The ASGI-level assertions above pin the close code; this pins that the
    refusal actually happens on a real connection.

    It asserts an HTTP 403 handshake response rather than a close frame,
    because that is what uvicorn does with a pre-accept close -- see this
    module's docstring. Recorded as measured behaviour, not as the design.
    """
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-live")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    bad = api_env.idp.token(sub, slug, email=f"u@evil{BASE_DOMAIN}")
    good = api_env.idp.token(sub, slug, email=f"u@{BASE_DOMAIN}")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(api_env.app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        url = f"ws://127.0.0.1:{port}/api/v1/ws"
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            async with websockets.connect(url, subprotocols=[AUTH_SUBPROTOCOL, bad]) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)
        assert exc.value.response.status_code == 403
        # Non-vacuity: the same server admits the same user on a good domain.
        async with websockets.connect(url, subprotocols=[AUTH_SUBPROTOCOL, good]) as ws:
            assert ws.subprotocol == AUTH_SUBPROTOCOL
    finally:
        server.should_exit = True
        await task


# --------------------------------------------------------------------------- #
# Door 3 — the remote-control tunnel (app/modules/remote/tunnel.py)
# --------------------------------------------------------------------------- #


async def _authorize(api_env, token: str, sid: UUID):
    return await authorize_tunnel(
        token=token,
        session_id=sid,
        settings=api_env.settings,
        jwks=api_env.app.state.jwks,
        admin_engine=api_env.admin_engine,
        app_engine=api_env.app_engine,
    )


async def _an_owned_session(api_env) -> tuple[UUID, str, UUID]:
    org_id = await api_env.create_org("dom-rc")
    tech = await api_env.create_user(org_id, "technician")
    tech_id = await _user_id_of(api_env, tech)
    sid = await _session_row(api_env, org_id, tech_id)
    return org_id, tech, sid


async def test_tunnel_admits_when_the_setting_is_unset(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id, tech, sid = await _an_owned_session(api_env)
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(tech, slug, email="someone@completely-elsewhere.example")
    got_org, _ = await _authorize(api_env, token, sid)
    assert got_org == org_id


async def test_tunnel_admits_a_matching_domain(api_env) -> None:
    org_id, tech, sid = await _an_owned_session(api_env)
    api_env.settings.console_email_domain = BASE_DOMAIN
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(tech, slug, email=f"u@mail.{BASE_DOMAIN}")
    got_org, _ = await _authorize(api_env, token, sid)
    assert got_org == org_id


async def test_tunnel_refuses_a_foreign_domain_with_4403(api_env) -> None:
    """The session exists and this user owns it and holds remote.control, so
    4403 here can only be the domain rule -- not ownership, not the code."""
    org_id, tech, sid = await _an_owned_session(api_env)
    api_env.settings.console_email_domain = BASE_DOMAIN
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(tech, slug, email=f"u@evil{BASE_DOMAIN}")
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, token, sid)
    assert exc.value.code == 4403


async def test_tunnel_refuses_a_disabled_user_with_4403(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id, tech, sid = await _an_owned_session(api_env)
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(tech, slug)
    # Non-vacuity: authorized a moment ago on this exact token.
    await _authorize(api_env, token, sid)
    await _set_status(api_env, tech, "disabled")
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, token, sid)
    assert exc.value.code == 4403


async def test_tunnel_still_4401s_a_bad_token(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, "garbage", uuid4())
    assert exc.value.code == 4401


# --------------------------------------------------------------------------- #
# The preflight (spec §3.3) — the only way to learn the cost before paying it
# --------------------------------------------------------------------------- #


def _preflight():
    """Load `scripts/preflight_email_domain.py` by path.

    It lives at the repo root's `scripts/` with the other operator tools and
    is not an importable package, so it is loaded the way an operator runs it:
    as a file.
    """
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "preflight_email_domain.py"
    )
    assert path.exists(), path
    spec = importlib.util.spec_from_file_location("preflight_email_domain", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_partitions_refused_from_merely_already_blocked() -> None:
    """The exit code is about ACTIVE users. A `deleted` row on a foreign domain
    is listed for completeness but loses nothing it still had -- the status
    half of the same gate already refuses it -- and must not be what tells an
    operator the change is unsafe."""
    pf = _preflight()
    rows = [
        ("acme", f"a@{BASE_DOMAIN}", "active"),
        ("acme", f"b@mail.{BASE_DOMAIN}", "active"),
        ("acme", f"c@evil{BASE_DOMAIN}", "active"),
        ("beta", "d@old.example", "deleted"),
    ]
    report = pf.partition(rows, BASE_DOMAIN)
    assert report.examined == 4
    assert [email for _, email, _ in report.refused] == [
        f"c@evil{BASE_DOMAIN}",
        "d@old.example",
    ]
    assert [email for _, email, _ in report.blocking] == [f"c@evil{BASE_DOMAIN}"]


def test_preflight_exit_codes(capsys) -> None:
    pf = _preflight()
    unsafe = pf.render(
        pf.partition([("acme", f"x@evil{BASE_DOMAIN}", "active")], BASE_DOMAIN), BASE_DOMAIN
    )
    assert unsafe == 1
    assert f"x@evil{BASE_DOMAIN}" in capsys.readouterr().out

    safe = pf.render(
        pf.partition([("acme", f"x@mail.{BASE_DOMAIN}", "active")], BASE_DOMAIN), BASE_DOMAIN
    )
    assert safe == 0

    # Nothing to examine is not an error: an empty deployment locks nobody out.
    assert pf.render(pf.partition([], BASE_DOMAIN), BASE_DOMAIN) == 0


def test_preflight_agrees_with_the_gate_on_the_near_miss() -> None:
    """The failure this rules out is a preflight that expresses the rule as its
    own SQL `LIKE '%toeshee.com'`, reports "nobody affected", and locks out the
    `eviltoeshee.com` user the gate refuses.

    THE EXPECTATIONS ARE LITERAL, AND THAT IS THE WHOLE POINT OF THE REWRITE.
    This test used to compute each expected verdict by calling
    `is_allowed_domain` -- the very function `partition` delegates to -- so it
    read `f(x) is f(x)` and passed for ANY delegating implementation, including
    one that delegated to a hand-rolled `LIKE`-equivalent predicate. It could
    not detect the divergence it is named after. Written out by hand, the
    addresses and the verdicts are a claim about the RULE, so a preflight that
    quietly stops agreeing with the doors fails here.
    """
    pf = _preflight()

    def would_be_refused(email: str) -> bool:
        return bool(pf.partition([("acme", email, "active")], "toeshee.com").refused)

    # The two the hand-rolled suffix match gets wrong, in both directions.
    assert would_be_refused("u@eviltoeshee.com") is True  # `LIKE '%toeshee.com'` admits it
    assert would_be_refused("u@toeshee.com.evil.com") is True  # ...and this one
    # And the three it must not start refusing.
    assert would_be_refused("u@toeshee.com") is False
    assert would_be_refused("u@mail.toeshee.com") is False
    assert would_be_refused("u@a.b.toeshee.com") is False
    # Degenerate rows in the projection fail CLOSED: the preflight lists them
    # rather than calling the change safe on a row it could not judge.
    assert would_be_refused("") is True
    assert would_be_refused("u@toeshee.com.au") is True


async def test_preflight_reads_every_org_from_a_real_database(
    pg_cluster, api_env, capsys
) -> None:
    """Run against the real cluster, across two orgs, on the admin engine.

    Cross-tenant by design: the question "who anywhere is about to lose
    access" has no one tenant to scope to, and the application role would see
    one org at a time and under-report -- which for this tool is the dangerous
    direction, since under-reporting reads as "safe to proceed".
    """
    pf = _preflight()
    org_a = await api_env.create_org("pre-a")
    org_b = await api_env.create_org("pre-b")
    slug_a = await api_env.org_slug(org_a)
    slug_b = await api_env.org_slug(org_b)
    lose_a = f"lose-{uuid4().hex[:6]}@evil{BASE_DOMAIN}"
    lose_b = f"lose-{uuid4().hex[:6]}@far.example"

    async with AsyncSession(api_env.admin_engine) as s:
        s.add(User(org_id=org_a, external_subject=f"pf-{uuid4().hex[:8]}", email=lose_a))
        s.add(User(org_id=org_b, external_subject=f"pf-{uuid4().hex[:8]}", email=lose_b))
        await s.commit()

    capsys.readouterr()
    code = await pf.run(pg_cluster.admin_url, BASE_DOMAIN)
    out = capsys.readouterr().out
    assert code == 1  # both seeded rows are active and both fail the rule

    # BOTH orgs appear in one run. A per-tenant read would have found one and
    # reported the other org's user as safe -- under-reporting, which for this
    # tool is the dangerous direction.
    assert slug_a in out and lose_a in out
    assert slug_b in out and lose_b in out


async def test_preflight_lists_org_slug_and_email_for_each_refusal(
    pg_cluster, api_env, capsys
) -> None:
    """The output shape the operator reads: org slug + address, per row.

    An address alone is not actionable in a multi-tenant deployment -- the
    same local part exists in several tenants -- so the slug is what makes a
    listed refusal something the operator can go and fix.
    """
    pf = _preflight()
    org = await api_env.create_org("pre-list")
    slug = await api_env.org_slug(org)
    kept = f"kept-{uuid4().hex[:6]}@mail.{BASE_DOMAIN}"
    lost = f"lost-{uuid4().hex[:6]}@evil{BASE_DOMAIN}"
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(User(org_id=org, external_subject=f"pf-{uuid4().hex[:8]}", email=kept))
        s.add(User(org_id=org, external_subject=f"pf-{uuid4().hex[:8]}", email=lost))
        await s.commit()

    capsys.readouterr()
    await pf.run(pg_cluster.admin_url, BASE_DOMAIN)
    out = capsys.readouterr().out

    assert lost in out
    assert slug in out
    # The user who keeps access is not in the list of people who lose it.
    assert kept not in out


# --------------------------------------------------------------------------- #
# A PADDED CONFIGURED BASE, at a door (review finding 2)
#
# The pure-level cases live in `test_email_domain.py`. This one exists because
# the defect's blast radius was not "one function returns the wrong thing" but
# "every user is refused at every door", and that is only observable here.
# --------------------------------------------------------------------------- #


async def test_http_admits_when_the_configured_base_has_a_trailing_newline(api_env) -> None:
    """`ITS_CONSOLE_EMAIL_DOMAIN=toeshee.com\\n` is what a here-doc or a copied
    `.env` line produces. Before the fix it was non-empty, matched no address,
    and locked out the whole deployment -- the §3.3 lockout, arriving from a
    byte the operator cannot see, with the preflight (which is handed the value
    on the command line, unpadded) reporting the change safe."""
    api_env.settings.console_email_domain = f" {BASE_DOMAIN}\n"
    org_id = await api_env.create_org("dom-pad")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)

    token = api_env.idp.token(sub, slug, email=f"u@mail.{BASE_DOMAIN}")
    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200

    # Non-vacuity: the padded base is still a RESTRICTION, not an accidental
    # "unset". Stripping must not have widened it into admitting everyone.
    foreign = api_env.idp.token(sub, slug, email=f"u@evil{BASE_DOMAIN}")
    refused = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {foreign}"}
    )
    assert refused.status_code == 403


# --------------------------------------------------------------------------- #
# THE REFUSAL IS LOGGED, AT EVERY DOOR (review finding 1)
#
# `log_admission_refusal` had no coverage at all: deleting the function left
# the whole suite green. It is the ONLY server-side diagnostic for a §3.3
# lockout -- the console has stopped admitting the operator, so the console
# cannot tell them why -- which made "delete the log call" the cheapest way to
# satisfy any rule that counts log sites. These tests make that deletion fail.
#
# Every door is asserted separately for the same reason the refusals
# themselves are: the doors do not share a code path, and a diagnostic present
# at two of three is a diagnostic missing exactly when someone is locked out
# through the third.
# --------------------------------------------------------------------------- #


@contextmanager
def _capture_refusals() -> Iterator[list[logging.LogRecord]]:
    """Records emitted by the identity logger, captured on that logger itself.

    Not `caplog`: `create_app` runs `configure_logging`, which rewrites the
    root logger's handlers, so a root-level capture can be detached by wiring
    unrelated to this assertion — and a capture that can be silently
    disconnected is a capture that cannot fail.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("its.identity")
    handler = _Collect(level=logging.DEBUG)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _one_refusal(records: list[logging.LogRecord], *, door: str, sub: str, email: str):
    """Assert exactly one refusal record, and that it carries all four fields.

    All four, because each answers a different question the locked-out
    operator has: WHICH door turned them away (the three fail independently),
    WHO was turned away (`sub` is what survives in Keycloak; `email` is what
    the §3.3 preflight prints, and matching the two is the whole diagnosis),
    and WHY (domain rule or account status — different remedies entirely).
    """
    assert len(records) == 1, [r.getMessage() for r in records]
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == REFUSAL_MESSAGE
    assert record.door == door
    assert record.sub == sub
    assert record.email == email
    assert isinstance(record.reason, str) and record.reason
    return record


async def test_http_logs_the_domain_refusal(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-log")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    email = f"u@evil{BASE_DOMAIN}"
    token = api_env.idp.token(sub, slug, email=email)

    with _capture_refusals() as records:
        response = await api_env.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 403
    record = _one_refusal(records, door="http", sub=sub, email=email)
    assert record.reason == response.json()["detail"]


async def test_http_logs_the_status_refusal(api_env) -> None:
    """The second call site at this door. The domain and status rules refuse
    from different places in `auth_context` — before the org lookup and after
    the user row — so one being logged says nothing about the other."""
    org_id = await api_env.create_org("dom-log")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    email = f"u@{BASE_DOMAIN}"
    await _set_status(api_env, sub, "disabled")

    with _capture_refusals() as records:
        response = await api_env.client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {api_env.idp.token(sub, slug, email=email)}"},
        )
    assert response.status_code == 403
    record = _one_refusal(records, door="http", sub=sub, email=email)
    # The two rules are distinguishable in the log, or the operator cannot
    # tell "fix the allowlist" from "re-enable the account".
    assert record.reason != "email domain not permitted for this console"


async def test_ws_logs_the_domain_refusal(api_env) -> None:
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-log-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    email = f"u@evil{BASE_DOMAIN}"
    token = api_env.idp.token(sub, slug, email=email)

    with _capture_refusals() as records:
        sent = await _ws_messages(api_env, token=token)
    assert _closes(sent) == [4403]
    _one_refusal(records, door="console-ws", sub=sub, email=email)


async def test_ws_logs_the_status_refusal(api_env) -> None:
    """This door is the one where the log matters most: a pre-accept close is
    delivered to the browser as a bare handshake failure (see this module's
    docstring), so the server-side line is the ONLY place the reason exists."""
    org_id = await api_env.create_org("dom-log-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    email = f"u@{BASE_DOMAIN}"
    await _set_status(api_env, sub, "disabled")

    with _capture_refusals() as records:
        sent = await _ws_messages(api_env, token=api_env.idp.token(sub, slug, email=email))
    assert _closes(sent) == [4403]
    _one_refusal(records, door="console-ws", sub=sub, email=email)


async def test_tunnel_logs_the_domain_refusal(api_env) -> None:
    org_id, tech, sid = await _an_owned_session(api_env)
    api_env.settings.console_email_domain = BASE_DOMAIN
    slug = await api_env.org_slug(org_id)
    email = f"u@evil{BASE_DOMAIN}"

    with _capture_refusals() as records:
        with pytest.raises(TunnelAuthError):
            await _authorize(api_env, api_env.idp.token(tech, slug, email=email), sid)
    _one_refusal(records, door="remote-tunnel", sub=tech, email=email)


async def test_tunnel_logs_the_status_refusal(api_env) -> None:
    org_id, tech, sid = await _an_owned_session(api_env)
    slug = await api_env.org_slug(org_id)
    email = f"u@{BASE_DOMAIN}"
    await _set_status(api_env, tech, "disabled")

    with _capture_refusals() as records:
        with pytest.raises(TunnelAuthError):
            await _authorize(api_env, api_env.idp.token(tech, slug, email=email), sid)
    _one_refusal(records, door="remote-tunnel", sub=tech, email=email)


async def test_an_admitted_identity_logs_no_refusal(api_env) -> None:
    """Non-vacuity for all six assertions above: the logger is quiet when
    nobody was turned away, so `_one_refusal` is measuring the refusal and not
    some line every request emits."""
    api_env.settings.console_email_domain = BASE_DOMAIN
    org_id = await api_env.create_org("dom-quiet")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email=f"u@mail.{BASE_DOMAIN}")

    with _capture_refusals() as records:
        response = await api_env.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        sent = await _ws_messages(api_env, token=token, disconnect_after=True)
    assert response.status_code == 200
    assert sent[0]["type"] == "websocket.accept"
    assert records == []


# --------------------------------------------------------------------------- #
# A REFUSAL IN THE HASH-CHAINED AUDIT LOG (review finding 4)
#
# Invariant 4 wants every privileged action in the tamper-evident chain, and a
# refused admission is arguably the entry most worth having there -- a WARNING
# on stdout is rotated, unordered and forgeable, which is precisely what the
# chain is not. The obstacle named in the finding is real but PARTIAL: the
# DOMAIN refusal fires before the org is resolved, so there is no `org_id` to
# write against; the STATUS refusal fires after both the org and the user row
# are in hand, so there is. The identity module's docstring records the
# decision in full; this pins the mechanism it hands the doors.
# --------------------------------------------------------------------------- #


async def test_audit_admission_refusal_writes_a_verifiable_chained_event(api_env) -> None:
    from app.core.audit import audit_service
    from app.core.identity import ADMISSION_REFUSED_ACTION, audit_admission_refusal
    from app.core.tenancy import org_session
    from app.models import AuditEvent

    org_id = await api_env.create_org("dom-audit")
    sub = await api_env.create_user(org_id, "read-only")
    user_id = await _user_id_of(api_env, sub)

    # TENANT-SCOPED, on the application engine: invariant 5 says the filter
    # belongs in the database, and a refusal written through the BYPASSRLS
    # engine would be application-code-only scoping on a security event.
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await audit_admission_refusal(
            session, org_id=org_id, user_id=user_id, door="http", reason="not active"
        )

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
        result = await audit_service.verify(session, org_id)

    assert [e.action for e in events] == [ADMISSION_REFUSED_ACTION]
    event = events[0]
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.target_type == "user"
    assert event.target_id == str(user_id)
    assert event.meta == {"door": "http", "reason": "not active"}
    # The link actually verifies -- a refusal that broke the chain would make
    # every later event unverifiable, which is worse than not recording it.
    assert result.ok is True
    assert result.count == 1


async def test_audit_admission_refusal_chains_onto_what_came_before(api_env) -> None:
    """It is an APPEND, not a special case. Two refusals and an ordinary event
    verify as one chain, so recording refusals cannot fork it."""
    from app.core.audit import audit_service
    from app.core.identity import audit_admission_refusal
    from app.core.tenancy import org_session

    org_id = await api_env.create_org("dom-audit")
    sub = await api_env.create_user(org_id, "read-only")
    user_id = await _user_id_of(api_env, sub)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=user_id,
            action="auth.login",
            target_type="user",
            target_id=str(user_id),
        )
        await audit_admission_refusal(
            session, org_id=org_id, user_id=user_id, door="console-ws", reason="r1"
        )
        await audit_admission_refusal(
            session, org_id=org_id, user_id=user_id, door="remote-tunnel", reason="r2"
        )

    async with AsyncSession(api_env.admin_engine) as session:
        result = await audit_service.verify(session, org_id)
    assert result.ok is True
    assert result.count == 3


# --------------------------------------------------------------------------- #
# The allowlist as an ORG SETTING, at all three doors
#
# `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.2.
# The rule did not change; WHERE THE VALUE COMES FROM did. Every test above
# still sets `settings.console_email_domain` -- that is now the PLATFORM
# DEFAULT for an org with no value of its own, which is what keeps an upgrade
# from silently unrestricting a deployment that had configured the env var.
# These add the two readings that are new, and they are added at each door
# individually for the same reason the file's opening docstring gives: an
# allowlist enforced at one of three doors is not an allowlist, and a shared
# helper is not evidence that all three call it.
#
# The door tests here deliberately leave `settings.console_email_domain`
# UNSET, so a door that ignored the org setting and kept reading the env var
# would admit and the test would go red -- rather than passing on the platform
# default and proving nothing.
# --------------------------------------------------------------------------- #


def _addr(local: str, domain: str) -> str:
    """An address nothing else in the run can also hold.

    `pg_cluster` is SESSION-SCOPED: a row this file's admitted requests write
    (door 1 refreshes `users.email` from the claim) outlives the test and is
    visible to every other suite. A fixed literal therefore collides with the
    same literal elsewhere, and the symptom lands in the OTHER file -- several
    suites look a user up by address with `scalar_one_or_none()`, which raises
    `MultipleResultsFound` on the second copy.
    """
    return f"{local}-{uuid4().hex[:10]}@{domain}"


async def _set_org_domain(api_env, org_id: UUID, value: str) -> None:
    """Write `organizations.settings["console_email_domain"]` directly.

    On the BYPASSRLS admin engine rather than through the PATCH route: the
    route REFUSES a value that would refuse the caller, which is exactly the
    value several of these tests need in place.
    """
    from app.models import Organization

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"console_email_domain": value})
        )
        await s.commit()


async def test_http_honours_the_org_setting_with_no_env_var_at_all(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom-orgset")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)

    bad = api_env.idp.token(sub, slug, email=_addr("u", f"evil{BASE_DOMAIN}"))
    refused = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {bad}"}
    )
    assert refused.status_code == 403
    # The refusal names the RULE and never the address that failed it.
    assert refused.json()["detail"] == "email domain not permitted for this console"
    assert "evil" not in refused.text

    good = api_env.idp.token(sub, slug, email=_addr("u", BASE_DOMAIN))
    admitted = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {good}"}
    )
    assert admitted.status_code == 200


async def test_http_org_setting_refusal_still_creates_no_user_row(api_env) -> None:
    """THE PROPERTY THE MOVE COULD HAVE COST. The refusal used to happen before
    the org was even resolved; reading a per-org setting makes that impossible.
    It now happens as the first statement INSIDE the tenant transaction and
    raises, so the self-heal never runs and nothing commits."""
    org_id = await api_env.create_org("dom-orgset-row")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    sub = f"never-seen-{uuid4().hex[:8]}"

    bad = api_env.idp.token(sub, slug, email=_addr("u", f"evil{BASE_DOMAIN}"))
    response = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {bad}"}
    )
    assert response.status_code == 403
    async with AsyncSession(api_env.admin_engine) as s:
        found = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one_or_none()
    assert found is None


async def test_http_applies_a_changed_setting_without_a_restart(api_env) -> None:
    """The whole point of §2.2: no SSH, no restart. Same app object, same
    token, opposite answer, because the value moved in the database between
    the two requests."""
    org_id = await api_env.create_org("dom-live")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email=_addr("u", BASE_DOMAIN))
    headers = {"Authorization": f"Bearer {token}"}

    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 200
    await _set_org_domain(api_env, org_id, "somewhere-else.example")
    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 403


async def test_ws_honours_the_org_setting(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id = await api_env.create_org("dom-ws-orgset")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)

    # `disconnect_after=True` even though a refusal never reads: if the door
    # WRONGLY ADMITS, the handler accepts and parks on `receive()`, and this
    # test would hang for its whole timeout instead of failing. A test that
    # hangs when the thing it guards breaks is not a guard.
    sent = await _ws_messages(
        api_env,
        token=api_env.idp.token(sub, slug, email=_addr("u", f"evil{BASE_DOMAIN}")),
        disconnect_after=True,
    )
    assert _closes(sent) == [4403]
    assert not any(m["type"] == "websocket.accept" for m in sent)


async def test_tunnel_honours_the_org_setting(api_env) -> None:
    assert api_env.settings.console_email_domain is None
    org_id, tech, sid = await _an_owned_session(api_env)
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)

    token = api_env.idp.token(tech, slug, email=_addr("u", f"evil{BASE_DOMAIN}"))
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, token, sid)
    assert exc.value.code == 4403


# --- the vendor break-glass override, at all three doors -------------------- #


async def test_override_off_disables_the_gate_at_the_http_door(
    api_env, monkeypatch
) -> None:
    org_id = await api_env.create_org("dom-ovr")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    token = api_env.idp.token(sub, slug, email=_addr("u", "elsewhere.example"))
    headers = {"Authorization": f"Bearer {token}"}

    # Non-vacuity: refused a moment ago on this exact token.
    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 403
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "off")
    assert (await api_env.client.get("/api/v1/me", headers=headers)).status_code == 200


async def test_override_off_disables_the_gate_at_the_ws_door(api_env, monkeypatch) -> None:
    org_id = await api_env.create_org("dom-ovr-ws")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    token = api_env.idp.token(sub, slug, email=_addr("u", "elsewhere.example"))

    # See the note above on `disconnect_after`.
    assert _closes(await _ws_messages(api_env, token=token, disconnect_after=True)) == [4403]
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "off")
    sent = await _ws_messages(api_env, token=token, disconnect_after=True)
    assert sent[0]["type"] == "websocket.accept"


async def test_override_off_disables_the_gate_at_the_tunnel_door(
    api_env, monkeypatch
) -> None:
    org_id, tech, sid = await _an_owned_session(api_env)
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    token = api_env.idp.token(tech, slug, email=_addr("u", "elsewhere.example"))

    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, token, sid)
    assert exc.value.code == 4403
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "off")
    got_org, _ = await _authorize(api_env, token, sid)
    assert got_org == org_id


async def test_the_override_ignores_anything_that_is_not_the_word_off(
    api_env, monkeypatch
) -> None:
    """An operator who puts the DOMAIN in the override variable gets the gate,
    not a silently disabled one. The fail-safe direction for a switch whose
    only job is to turn a security control off is `did not turn it off`."""
    org_id = await api_env.create_org("dom-ovr-junk")
    sub = await api_env.create_user(org_id, "read-only")
    slug = await api_env.org_slug(org_id)
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    token = api_env.idp.token(sub, slug, email=_addr("u", "elsewhere.example"))
    headers = {"Authorization": f"Bearer {token}"}

    for value in (BASE_DOMAIN, "", "0", "false", "disabled"):
        monkeypatch.setenv(OVERRIDE_ENV_VAR, value)
        response = await api_env.client.get("/api/v1/me", headers=headers)
        assert response.status_code == 403, value


async def test_the_create_user_form_reads_the_same_org_setting(api_env) -> None:
    """RULE 4 follows the value. A console that refuses an address at sign-in
    but mints it in the create form has not enforced anything -- and the two
    now read the setting from the same place, or they drift the moment an
    operator changes it."""
    from app.core.identity.policy import resolve_console_domain
    from app.core.tenancy import org_session

    org_id = await api_env.create_org("dom-create")
    await _set_org_domain(api_env, org_id, BASE_DOMAIN)
    async with org_session(org_id, engine=api_env.app_engine) as s:
        assert await resolve_console_domain(s, org_id, api_env.settings) == BASE_DOMAIN

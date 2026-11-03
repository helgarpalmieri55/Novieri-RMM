"""`remote.supervise` (phase 3 design §5.3, migration 0244).

Stopping or joining ANOTHER technician's remote session needs this code on the
session's client -- its machine's. `orgs.manage` no longer gives that power:
it is organisation settings, and says nothing about remote control. A stop is
audited `remote.session.ended` with the person who stopped the session as its
actor, in the session's client; it named the owner, whoever stopped it.

The rail is one predicate (`app/modules/remote/tunnel.py::owner_or_admin`)
behind two doors, the stop route and the screen tunnel's admission
(`authorize_tunnel`). Each test goes through both real doors, not through the
predicate alone.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, RemoteSession
from app.modules.remote.service import end_session_from_result
from app.modules.remote.tunnel import TunnelAuthError
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_client_record_checks import REMOTE, _device, _holder, _remote_session, _user
from tests.test_remote_tunnel import _authorize

NOT_OWNER = "requires the session owner or remote.supervise"


def _bearer(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


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
        await _authorize(api_env, _bearer(admin_of_x), session)
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
        await _authorize(api_env, _bearer(supervisor), on_y)
    assert door.value.code == 4403

    admitted_org, admitted_user, _ = await _authorize(api_env, _bearer(supervisor), on_x)
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

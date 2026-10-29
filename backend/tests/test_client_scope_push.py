"""Push install inside the client (plan 2026-10-26 Task 12; CLAUDE.md 2a,
narrowed: the vouched asset, the authorization and the credential are one
client's).

A push credential has exactly one client, and THE CLIENT OF A PUSH IS ITS
CREDENTIAL'S: the address must be a vouched machine of that client, covered by
an authorization of that client, and originated by a site server of that
client. Nothing here widens 2a: every refusal below is a bound that held for
the organisation and now has to hold inside one client as well. No signed
parameter, no envelope and nothing the agent executes changes.
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.global_rows import SCOPE_KEY
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    Credential,
    DeviceGroup,
    DomainEvent,
    EnrollmentToken,
    Job,
    PushRun,
)
from app.modules.agent_push import dispatch as dispatch_service
from app.modules.agent_push import preflight, run_list, runs
from app.modules.agent_push.models import (
    PushAuthorization,
    PushBatchTarget,
    PushCredentialRealm,
    PushCredentialScope,
    PushRunTarget,
)
from app.modules.agent_push.net import parse_target_address
from tests import agent_push_fixtures as F
from tests.client_scope_support import client_with_site, drop_client_keys, everything_on
from tests.conftest import ApiEnv, PgCluster
from tests.push_run_fixtures import (
    CONSOLE,
    CRED_JSON,
    FakeStore,
    FakeVault,
    grant_org_admin,
    job_engine,
    make_dispatcher,
    run_world,
    start_run,
)

P = "/api/v1/agent-push"
CREDENTIALS = "/api/v1/credentials"
ONE_CLIENT = "a push credential belongs to one client"
UPN_SECRET = '{"username": "svc-push@corp.example.com", "secret": "p"}'


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


@pytest.fixture()
def published(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> list[tuple[UUID, Any]]:
    """The run routes' doubles (`tests/test_push_run_api.py`'s, reused)."""
    from app.modules.agent_push import api as push_api

    sent: list[tuple[UUID, Any]] = []

    async def _publish(redis: Any, device_id: UUID, envelope: Any) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(push_api, "publish_job", _publish)
    monkeypatch.setattr(push_api, "_object_store", lambda request: FakeStore())
    monkeypatch.setattr(api_env.app.state, "vault", FakeVault())
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    return sent


WRITTEN = (
    Credential, PushAuthorization, PushCredentialScope, PushCredentialRealm, PushRun,
    PushRunTarget, Job, EnrollmentToken, PushBatchTarget, AuditEvent,
)


async def _written(env: ApiEnv, org: UUID) -> dict[str, int]:
    async with AsyncSession(env.admin_engine) as s:
        return {
            model.__tablename__: (
                await s.execute(select(func.count()).select_from(model).where(model.org_id == org))
            ).scalar_one()
            for model in WRITTEN
        }


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _audits(env: ApiEnv, org: UUID, prefix: str = "push.") -> list[tuple[str, UUID | None]]:
    async with AsyncSession(env.admin_engine) as s:
        return [
            (row.action, row.client_id)
            for row in (
                await s.execute(
                    select(AuditEvent.action, AuditEvent.client_id)
                    .where(AuditEvent.org_id == org, AuditEvent.action.startswith(prefix))
                    .order_by(AuditEvent.id)
                )
            ).all()
        ]


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The brief's five (plan 2026-10-26 Task 12, Step 1)
# --------------------------------------------------------------------------- #


async def test_a_technician_with_both_codes_on_one_client_can_push_there(
    api_env: ApiEnv,
) -> None:
    """research-4 §C: five `has_permission` calls without a client counted
    only every-client grants, so this technician was refused."""
    org = await api_env.create_org("push-own")
    x, x_site = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(world.credential), "targets": [world.address],
              "mode": "probe"},
        headers=x_tech,
    )
    assert r.status_code == 202, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        (job,) = (await s.execute(select(Job).where(Job.org_id == org))).scalars()
    # Through X's own site server, and the job is X's.
    assert (job.target_id, job.client_id) == (world.site_server_device, x)
    assert await _audits(api_env, org) == [("push.dispatched", x)]


async def test_another_client_s_credential_cannot_push_to_x(api_env: ApiEnv) -> None:
    org = await api_env.create_org("push-cred")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    y_credential = await F.seed_credential(api_env, org, client_id=y)
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    before = await _written(api_env, org)
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(y_credential), "targets": [world.address], "mode": "probe"},
        headers=admin,
    )
    # Refused before any job is signed: Y's credential is not X's, so the
    # ladder answers `credential_missing` for X's address (2a(d), narrowed).
    assert r.status_code == 422, r.text
    assert "credential_missing" in r.text
    (target,) = r.json()["detail"]["targets"]
    # Every bound is read in Y's client: X's authorization, X's scope and X's
    # site server answer for X's machine and for nothing of Y's.
    assert target == {
        "address": world.address,
        "reasons": ["credential_missing", "no_authorization", "credential_not_scoped",
                    "no_site_server"],
    }
    after = await _written(api_env, org)
    # Recorded before a signature (2a(d)): one `push.refused`, in Y's client,
    # and nothing else -- no job, no token, no batch row.
    assert {k: after[k] - before[k] for k in after if after[k] != before[k]} == {
        "audit_events": 1
    }
    assert await _audits(api_env, org) == [("push.refused", y)]


async def test_two_clients_may_each_run_a_push_at_once(api_env: ApiEnv) -> None:
    """D3: one active run per CLIENT."""
    org = await api_env.create_org("push-runs")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user(api_env, org)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for client in (x, y):
            credential = await F.seed_credential_in(s, org, client_id=client)
            s.add(PushRun(org_id=org, client_id=client, credential_id=credential,
                          prefix="10.0.0.0/24", list_digest="d" * 64, target_count=0,
                          state="running", created_by=actor))
        await s.flush()
    with pytest.raises(DBAPIError, match="uq_push_run_active"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            credential = await F.seed_credential_in(s, org, client_id=x)
            s.add(PushRun(org_id=org, client_id=x, credential_id=credential,
                          prefix="10.1.0.0/24", list_digest="e" * 64, target_count=0,
                          state="running", created_by=actor))
            await s.flush()


async def test_a_run_takes_its_credential_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("push-run-client")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user(api_env, org)
    with pytest.raises(DBAPIError, match="push run belongs to its credential's client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            credential = await F.seed_credential_in(s, org, client_id=x)
            s.add(PushRun(org_id=org, client_id=y, credential_id=credential,
                          prefix="10.2.0.0/24", list_digest="f" * 64, target_count=0,
                          state="running", created_by=actor))
            await s.flush()


async def _bare_run(env: ApiEnv, org: UUID, client: UUID, *, sealed: bool = True) -> UUID:
    actor = await _user(env, org)
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        credential = await F.seed_credential_in(s, org, client_id=client)
        run = PushRun(org_id=org, client_id=client, credential_id=credential,
                      prefix="10.3.0.0/24", list_digest="a" * 64, target_count=0,
                      state="cancelled", created_by=actor)
        s.add(run)
        await s.flush()
        if sealed:
            run.sealed_at = func.now()
            await s.flush()
        return run.id


async def test_a_run_never_changes_client_even_once_its_credential_is_gone(
    api_env: ApiEnv,
) -> None:
    """`its_push_run_client_guard`'s first clause. With the credential
    deleted (`credential_id` NULL, the one change 0200 allows) the second
    clause has nothing to compare, and only the first keeps the run -- and
    every target, which inherits from it -- in its client."""
    org = await api_env.create_org("push-run-move")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    run_id = await _bare_run(api_env, org, x)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE push_run SET credential_id = NULL WHERE id = :r"), {"r": run_id}
        )
        await s.commit()
    with pytest.raises(DBAPIError, match="a push run never changes client"):
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                text("UPDATE push_run SET client_id = :y WHERE id = :r"), {"y": y, "r": run_id}
            )


@pytest.mark.parametrize("device_client", ["the run's", "another client's"])
async def test_a_target_is_a_machine_of_its_run_s_client(
    api_env: ApiEnv, device_client: str
) -> None:
    """`its_push_run_target_device_guard`: a target inherits its run's client
    (H), and its machine must be of that client. The positive twin is the
    same insert with the run's client's machine."""
    org = await api_env.create_org("push-target-client")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    run_id = await _bare_run(api_env, org, x, sealed=False)
    client, site = (x, x_site) if device_client == "the run's" else (y, y_site)
    device = await F.seed_asset(
        api_env, org, primary_ip="10.3.0.9", client_id=client, site_id=site
    )

    async def _insert() -> None:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(PushRunTarget(org_id=org, run_id=run_id, device_id=device,
                                address="10.3.0.9", position=0, state="waiting"))
            await s.flush()

    if device_client == "the run's":
        await _insert()
        async with AsyncSession(api_env.admin_engine) as s:
            assert (
                await s.execute(
                    select(PushRunTarget.client_id).where(PushRunTarget.run_id == run_id)
                )
            ).scalar_one() == x
    else:
        with pytest.raises(DBAPIError, match="push target device belongs to another client"):
            await _insert()


@pytest.mark.parametrize("credential_type", ["windows_domain", "push_install"])
async def test_a_push_credential_needs_a_client(api_env: ApiEnv, credential_type: str) -> None:
    org = await api_env.create_org("push-cred-client")
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    before = await _written(api_env, org)
    r = await api_env.client.post(
        CREDENTIALS,
        json={"name": "dom", "type": credential_type, "secret": UPN_SECRET},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (422, {"detail": ONE_CLIENT}), r.text
    assert await _written(api_env, org) == before


async def _user(env: ApiEnv, org: UUID) -> UUID:
    from app.models import User

    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        user = User(
            org_id=org, external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"{uuid4().hex[:10]}@test.local",
        )
        s.add(user)
        await s.commit()
        return user.id


# --------------------------------------------------------------------------- #
# A credential's client (`POST /credentials`)
# --------------------------------------------------------------------------- #


async def test_a_push_credential_is_filed_under_the_client_named(api_env: ApiEnv) -> None:
    """The credential, its realm (H) and its audit event are X's; a
    technician limited to X files one there and gets a 404 for Y, with
    nothing written."""
    org = await api_env.create_org("cred-x")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        CREDENTIALS,
        json={"name": "dom", "type": "windows_domain", "secret": UPN_SECRET,
              "client_id": str(x)},
        headers=x_tech,
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(x)
    credential = UUID(r.json()["id"])
    async with AsyncSession(api_env.admin_engine) as s:
        realm = (
            await s.execute(
                select(PushCredentialRealm).where(PushCredentialRealm.credential_id == credential)
            )
        ).scalar_one()
    assert (realm.client_id, realm.realm) == (x, "CORP.EXAMPLE.COM")
    assert await _audits(api_env, org, "credential.") == [("credential.created", x)]

    before = await _written(api_env, org)
    r = await api_env.client.post(
        CREDENTIALS,
        json={"name": "dom", "type": "push_install", "secret": UPN_SECRET, "client_id": str(y)},
        headers=x_tech,
    )
    assert (r.status_code, r.json()) == (404, {"detail": "no such client"}), r.text
    assert await _written(api_env, org) == before


async def test_a_credential_names_a_client_that_may_take_one(api_env: ApiEnv) -> None:
    """404 for a client nobody can see, 409 archived, 422 Unassigned (C10),
    nothing written for any of them."""
    org = await api_env.create_org("cred-refused")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    admin = await _admin(api_env, org)
    before = await _written(api_env, org)
    for client, expected in (
        (uuid4(), (404, "no such client")),
        (archived, (409, "that client is archived")),
        (unassigned, (422, "a credential cannot be filed under Unassigned")),
    ):
        r = await api_env.client.post(
            CREDENTIALS,
            json={"name": "c", "type": "push_install", "secret": "s", "client_id": str(client)},
            headers=admin,
        )
        assert (r.status_code, r.json()["detail"]) == expected, r.text
    assert await _written(api_env, org) == before


async def test_a_group_s_credential_takes_the_group_s_client(api_env: ApiEnv) -> None:
    """With a device group the client is the group's: a body naming another
    is a 422 and writes nothing; naming the same one, or none, files it
    there."""
    org = await api_env.create_org("cred-group")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        group = DeviceGroup(org_id=org, client_id=x, name="g")
        s.add(group)
        await s.commit()
    admin = await _admin(api_env, org)
    before = await _written(api_env, org)
    r = await api_env.client.post(
        CREDENTIALS,
        json={"name": "c", "type": "push_install", "secret": "s",
              "device_group_id": str(group.id), "client_id": str(y)},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (
        422, {"detail": "that device group belongs to another client"}
    ), r.text
    assert await _written(api_env, org) == before
    for body_client in (str(x), None):
        r = await api_env.client.post(
            CREDENTIALS,
            json={"name": "c", "type": "push_install", "secret": "s",
                  "device_group_id": str(group.id), "client_id": body_client},
            headers=admin,
        )
        assert (r.status_code, r.json()["client_id"]) == (201, str(x)), r.text


async def test_a_credential_of_another_kind_may_still_be_global(api_env: ApiEnv) -> None:
    """Only the two push types need a client: any other with none is every
    client's, as before (and only an every-client person may file one)."""
    org = await api_env.create_org("cred-global")
    admin = await _admin(api_env, org)
    r = await api_env.client.post(
        CREDENTIALS, json={"name": "c", "type": "ssh_key", "secret": "s"}, headers=admin
    )
    assert (r.status_code, r.json()["client_id"]) == (201, None), r.text
    assert await _audits(api_env, org, "credential.") == [("credential.created", None)]


# --------------------------------------------------------------------------- #
# Authorizations: one client's network
# --------------------------------------------------------------------------- #


async def test_an_authorization_is_one_client_s_and_two_clients_may_share_a_prefix(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("auth-x")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    made = {}
    for client in (x, y):
        r = await api_env.client.post(
            f"{P}/authorizations",
            json={"prefix": "10.0.0.0/24", "client_id": str(client)},
            headers=admin,
        )
        assert (r.status_code, r.json()["client_id"]) == (201, str(client)), r.text
        made[client] = r.json()["id"]
    again = await api_env.client.post(
        f"{P}/authorizations", json={"prefix": "10.0.0.0/24", "client_id": str(x)}, headers=admin
    )
    assert again.status_code == 409, again.text
    revoked = await api_env.client.delete(f"{P}/authorizations/{made[x]}", headers=admin)
    assert revoked.status_code == 204
    assert await _audits(api_env, org) == [
        ("push.authorization_created", x),
        ("push.authorization_created", y),
        ("push.authorization_revoked", x),
    ]


async def test_an_authorization_names_its_client(api_env: ApiEnv) -> None:
    """Required; 404 invisible, 409 archived, 422 Unassigned; nothing written."""
    org = await api_env.create_org("auth-refused")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    admin = await _admin(api_env, org)
    before = await _written(api_env, org)
    missing = await api_env.client.post(
        f"{P}/authorizations", json={"prefix": "10.3.0.0/24"}, headers=admin
    )
    assert missing.status_code == 422, missing.text
    for client, expected in (
        (uuid4(), (404, "no such client")),
        (archived, (409, "that client is archived")),
        (unassigned, (422, "a push authorization cannot be filed under Unassigned")),
    ):
        r = await api_env.client.post(
            f"{P}/authorizations",
            json={"prefix": "10.3.0.0/24", "client_id": str(client)},
            headers=admin,
        )
        assert (r.status_code, r.json()["detail"]) == expected, r.text
    assert await _written(api_env, org) == before


async def test_a_technician_of_x_authorizes_in_x_and_not_in_y(api_env: ApiEnv) -> None:
    org = await api_env.create_org("auth-tech")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    own = await api_env.client.post(
        f"{P}/authorizations", json={"prefix": "10.4.0.0/24", "client_id": str(x)},
        headers=x_tech,
    )
    assert own.status_code == 201, own.text
    before = await _written(api_env, org)
    other = await api_env.client.post(
        f"{P}/authorizations", json={"prefix": "10.4.0.0/24", "client_id": str(y)},
        headers=x_tech,
    )
    assert (other.status_code, other.json()) == (404, {"detail": "no such client"})
    assert await _written(api_env, org) == before
    listed = await api_env.client.get(f"{P}/authorizations", headers=x_tech)
    assert [a["client_id"] for a in listed.json()] == [str(x)]


# --------------------------------------------------------------------------- #
# Credential scopes: H of the credential
# --------------------------------------------------------------------------- #


async def test_a_credential_with_no_client_cannot_be_bound(api_env: ApiEnv) -> None:
    """A global push credential (planted: the API no longer makes one) is a
    422 with the sentence, before the NOT NULL of its scope would be a 500."""
    org = await api_env.create_org("scope-global")
    credential = await F.seed_credential(api_env, org, client_id=F.NO_CLIENT)
    admin = await _admin(api_env, org)
    before = await _written(api_env, org)
    r = await api_env.client.post(
        f"{P}/credential-scopes",
        json={"credential_id": str(credential), "prefix": "10.5.0.0/24"},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (422, {"detail": ONE_CLIENT}), r.text
    assert await _written(api_env, org) == before


async def test_a_scope_is_its_credential_s_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("scope-x")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    x_credential = await F.seed_credential(api_env, org, client_id=x)
    y_credential = await F.seed_credential(api_env, org, client_id=y)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"{P}/credential-scopes",
        json={"credential_id": str(x_credential), "prefix": "10.6.0.0/24"},
        headers=x_tech,
    )
    assert (r.status_code, r.json()["client_id"]) == (201, str(x)), r.text
    before = await _written(api_env, org)
    other = await api_env.client.post(
        f"{P}/credential-scopes",
        json={"credential_id": str(y_credential), "prefix": "10.6.0.0/24"},
        headers=x_tech,
    )
    assert other.status_code == 404, other.text
    assert await _written(api_env, org) == before
    unbound = await api_env.client.delete(
        f"{P}/credential-scopes/{r.json()['id']}", headers=x_tech
    )
    assert unbound.status_code == 204
    assert await _audits(api_env, org) == [
        ("push.credential_scope_bound", x),
        ("push.credential_scope_unbound", x),
    ]


# --------------------------------------------------------------------------- #
# The ladder, inside the credential's client
# --------------------------------------------------------------------------- #


async def _decide(
    env: ApiEnv, org: UUID, credential: UUID, address: str, *, client: UUID,
    scope: ClientScope | None = None,
) -> preflight.TargetDecision:
    async with org_session(
        org, engine=env.app_engine, clients=scope or ClientScope.all()
    ) as session:
        (decision,) = await preflight.evaluate_targets(
            session, org, addresses=[parse_target_address(address)], credential_id=credential,
            client_id=client,
        )
    return decision


async def test_another_client_s_authorization_authorizes_nothing_here(api_env: ApiEnv) -> None:
    """X's machine, X's credential and scope, X's site server -- and the only
    authorization covering the address is Y's: `no_authorization`."""
    org = await api_env.create_org("lad-auth")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE push_authorization SET client_id = :y WHERE id = :a"),
            {"y": y, "a": world.authorization},
        )
        await s.commit()
    decision = await _decide(api_env, org, world.credential, world.address, client=x)
    assert decision.refusals == ("no_authorization",)
    assert decision.authorization_id is None

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE push_authorization SET client_id = :x WHERE id = :a"),
            {"x": x, "a": world.authorization},
        )
        await s.commit()
    decision = await _decide(api_env, org, world.credential, world.address, client=x)
    assert (decision.refusals, decision.authorization_id) == ((), world.authorization)


async def test_the_credential_chooses_the_machine_when_two_clients_share_an_address(
    api_env: ApiEnv,
) -> None:
    """Two customers on one private range: the address names two machines.
    The ladder never chooses by row id; it is asked for ONE client's -- the
    credential's -- and pushes to that client's machine through that
    client's site server, under every client's scope as under a narrowed
    one (plan 2026-10-26 Task 12, I2). `ambiguous_asset` cannot arise."""
    org = await api_env.create_org("lad-two")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    wx = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    wy = await F.push_world(api_env, org, client_id=y, site_id=y_site)
    for world in (wx, wy):
        for scope in (ClientScope.all(), ClientScope.only([world.client_id])):
            decision = await _decide(
                api_env, org, world.credential, world.address, client=world.client_id,
                scope=scope,
            )
            assert decision.refusals == ()
            assert (decision.device_id, decision.site_server_device_id) == (
                world.device, world.site_server_device
            )
            assert decision.authorization_id == world.authorization


async def test_a_refused_address_names_no_originator(api_env: ApiEnv) -> None:
    """Y's credential at X's machine, inside Y's authorization, scope and
    site server: refused (`credential_missing`), and Y's site server -- which
    covers the address -- is not named as its originator: no machine of the
    push's client is there to carry a credential to."""
    org = await api_env.create_org("lad-origin")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    wx = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    wy = await F.push_world(api_env, org, client_id=y, site_id=y_site, address="10.70.0.18")
    decision = await _decide(api_env, org, wy.credential, wx.address, client=y)
    assert decision.refusals == ("credential_missing",)
    assert (decision.device_id, decision.site_server_id, decision.site_server_device_id) == (
        None, None, None
    )
    assert decision.authorization_id == wy.authorization


async def test_a_narrowed_technician_cannot_name_another_client_s_credential(
    api_env: ApiEnv,
) -> None:
    """X's technician, Y's credential: a 404 (an existence oracle otherwise),
    nothing written and nothing audited."""
    org = await api_env.create_org("lad-narrow")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    await F.push_world(api_env, org, client_id=x, site_id=x_site)
    wy = await F.push_world(api_env, org, client_id=y, site_id=y_site, address="10.70.0.18")
    x_tech = await everything_on(api_env, org, x)
    before = await _written(api_env, org)
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(wy.credential), "targets": [wy.address], "mode": "probe"},
        headers=x_tech,
    )
    assert r.status_code == 404, r.text
    assert await _written(api_env, org) == before


async def _checkout_only_on(env: ApiEnv, org: UUID, client_id: UUID) -> tuple[dict[str, str], UUID]:
    """A person who holds every code but `credentials.checkout` on EVERY
    client, and `credentials.checkout` on `client_id` only: their session
    sees every client, so RLS shows them every client's credential, and only
    the conjunction read in the credential's client refuses the others."""
    from app.models import Permission, Role, RolePermission, User, UserRole

    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        permissions = (await s.execute(select(Permission.id, Permission.code))).all()
        for name, codes, client in (
            ("all-but-checkout", {c for _, c in permissions} - {"credentials.checkout"}, None),
            ("checkout", {"credentials.checkout"}, client_id),
        ):
            role = Role(org_id=org, name=f"{name}-{uuid4().hex[:8]}")
            s.add(role)
            await s.flush()
            for permission_id, code in permissions:
                if code in codes:
                    s.add(RolePermission(role_id=role.id, permission_id=permission_id))
            s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=client))
        await s.commit()
    return await env.headers(org, sub), user_id


async def test_checkout_is_needed_in_the_credential_s_client_not_merely_somewhere(
    api_env: ApiEnv,
) -> None:
    """`credentials.checkout` on X and every other code on every client: a
    push with Y's credential is a 403 -- read in Y, the credential's client --
    and writes nothing; the positive twin pushes with X's credential."""
    org = await api_env.create_org("lad-split")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    wx = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    wy = await F.push_world(api_env, org, client_id=y, site_id=y_site, address="10.70.0.18")
    headers, _user_id = await _checkout_only_on(api_env, org, x)
    before = await _written(api_env, org)
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(wy.credential), "targets": [wy.address], "mode": "probe"},
        headers=headers,
    )
    assert r.status_code == 403, r.text
    assert await _written(api_env, org) == before
    r = await api_env.client.post(
        f"{P}/requests",
        json={"credential_id": str(wx.credential), "targets": [wx.address], "mode": "probe"},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    assert await _audits(api_env, org) == [("push.dispatched", x)]


async def test_a_refused_install_mints_no_token_and_signs_nothing(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """The same as a refused probe, for an install: Y's credential at X's
    machine is refused before the vault, the token and the signer."""
    org = await api_env.create_org("lad-install")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    y_credential = await F.seed_credential(api_env, org, client_id=y)
    vault = FakeVault()
    actor = await _org_admin_id(api_env, org)
    before = await _written(api_env, org)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        result = await dispatch_service.dispatch_push_batch(
            s, job_engine(tmp_path), lambda: FakeStore(),
            vault,  # type: ignore[arg-type]
            org_id=org, actor_id=actor,
            credential_id=y_credential, targets=[world.address], kind="install",
            public_console_url=CONSOLE,
        )
    assert result.refused == {
        world.address: [
            "credential_missing", "no_authorization", "credential_not_scoped", "no_site_server"
        ]
    }
    assert vault.calls == 0
    after = await _written(api_env, org)
    assert {k: after[k] - before[k] for k in after if after[k] != before[k]} == {
        "audit_events": 1
    }
    # Recorded in the push's client: the credential's, Y.
    assert await _audits(api_env, org) == [("push.refused", y)]


async def test_a_pre_dispatch_refusal_is_recorded_in_the_credential_s_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """Past the ladder, `pre_dispatch_checks` refuses an install (no host key
    confirmed, no agent release): `push.refused` in X, the credential's
    client, before the vault and the signer."""
    org = await api_env.create_org("pre-refusal")
    x, x_site = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    vault = FakeVault()
    actor = await _org_admin_id(api_env, org)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        result = await dispatch_service.dispatch_push_batch(
            s, job_engine(tmp_path), lambda: FakeStore(),
            vault,  # type: ignore[arg-type]
            org_id=org, actor_id=actor,
            credential_id=world.credential, targets=[world.address], kind="install",
            public_console_url=CONSOLE,
        )
    assert result.batch_id is None and list(result.refused) == [world.address]
    assert vault.calls == 0
    assert await _audits(api_env, org) == [("push.refused", x)]


async def _org_admin_id(env: ApiEnv, org: UUID) -> UUID:
    from app.models import User

    sub = await env.create_user(org, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(User.id).where(User.external_subject == sub))).scalar_one()


async def test_another_org_s_rows_planted_under_this_client_count_for_nothing(
    api_env: ApiEnv,
) -> None:
    """THE ORG PREDICATES, STILL LOAD-BEARING (lesson 2). Org B's whole
    world for the address -- a vouched machine, an authorization, its site
    server -- planted under ORG A'S internal client, with the keys that make
    such rows impossible dropped, in a transaction that is rolled back. The
    ladder runs on the admin (BYPASSRLS) session, where the client predicate
    passes B's rows and only each lookup's `org_id` keeps them out."""
    a = await api_env.create_org("lad-org-a")
    b = await api_env.create_org("lad-org-b")
    a_credential = await F.seed_credential(api_env, a)
    address, prefix = "10.71.0.17", "10.71.0.0/24"
    b_user = await _user(api_env, b)
    async with AsyncSession(api_env.admin_engine) as s:
        await drop_client_keys(s, "devices", "fk_devices_client_org")
        await drop_client_keys(s, "push_authorization", "fk_push_authorization_client_org")
        await s.execute(text("ALTER TABLE devices DISABLE TRIGGER its_device_client_guard"))
        from datetime import UTC, datetime, timedelta

        from app.models import Agent, Device

        devices = [
            Device(
                org_id=b, client_id=a, site_id=a, hostname=f"{host}-{uuid4().hex[:6]}",
                os_family="linux", architecture="amd64", primary_ip=ip,
                source="discovery", provenance="promoted", review_state="approved",
            )
            for host, ip in (("asset", address), ("ss", "10.71.0.2"))
        ]
        s.add_all(devices)
        await s.flush()
        now = datetime.now(UTC)
        s.add(Agent(
            org_id=b, device_id=devices[1].id, cert_fingerprint=uuid4().hex,
            cert_serial=uuid4().hex[:16], cert_not_after=now + timedelta(days=1),
            enrolled_at=now, status="online", last_seen_at=now,
            capabilities={"agent.push_install.enroll_url": True},
        ))
        await s.flush()
        await s.execute(text(
            "INSERT INTO site_server (id, org_id, device_id, name, segments, roles, enabled,"
            " notes, created_at, updated_at) VALUES (gen_random_uuid(), :b, :d, 'ss',"
            " CAST(:p AS cidr[]), CAST('{wake}' AS text[]), true, '', now(), now())"
        ), {"b": b, "d": devices[1].id, "p": [prefix]})
        await s.execute(text(
            "INSERT INTO push_authorization (id, org_id, client_id, prefix, note, authorized_by,"
            " authorized_at) VALUES (gen_random_uuid(), :b, :a, CAST(:p AS cidr), '', :u, now())"
        ), {"a": a, "b": b, "p": prefix, "u": b_user})
        (decision,) = await preflight.evaluate_targets(
            s, a, addresses=[parse_target_address(address)], credential_id=a_credential,
            client_id=a,
        )
        await s.rollback()
    assert set(decision.refusals) == {
        "no_asset", "no_authorization", "credential_not_scoped", "no_site_server"
    }, decision


async def test_another_org_s_rows_planted_under_this_client_build_no_run(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE RUN LIST'S ORG PREDICATES, load-bearing (lesson 2), as the ladder's
    above: org B's vouched machine and B's unvouched one inside A's subnet,
    and B's authorization of a subnet A never authorized, all planted under
    A's client (keys dropped, rolled back), on the admin session. B's machine
    does not count toward A's list ceiling, B's unvouched one is not counted,
    and B's authorization builds nothing."""
    from app.models import Device
    from app.modules.agent_push.errors import PushValidationError

    world = await run_world(api_env, "list-org-a", octet=210, count=1)
    a = world.org.org_id
    b = await api_env.create_org("list-org-b")
    b_user = await _user(api_env, b)
    other_prefix = "10.212.0.0/24"
    async with AsyncSession(api_env.admin_engine) as s:
        own = await run_list._candidate_addresses(s, a, world.client_id, world.prefix)
        monkeypatch.setattr(run_list, "PUSH_RUN_LIST_MAX", len(own))
        await drop_client_keys(s, "devices", "fk_devices_client_org")
        await drop_client_keys(s, "push_authorization", "fk_push_authorization_client_org")
        await s.execute(text("ALTER TABLE devices DISABLE TRIGGER its_device_client_guard"))
        s.add_all([
            Device(
                org_id=b, client_id=world.client_id, site_id=world.client_id,
                hostname=f"b-{provenance}-{uuid4().hex[:6]}", os_family="linux",
                architecture="amd64", primary_ip=ip, source="discovery",
                provenance=provenance, review_state="approved",
            )
            for provenance, ip in (("promoted", "10.210.0.200"), ("sweep", "10.210.0.201"))
        ])
        await s.execute(text(
            "INSERT INTO push_authorization (id, org_id, client_id, prefix, note, authorized_by,"
            " authorized_at) VALUES (gen_random_uuid(), :b, :a, CAST(:p AS cidr), '', :u, now())"
        ), {"a": world.client_id, "b": b, "p": other_prefix, "u": b_user})
        await s.flush()
        listed = await run_list.expand_prefix(
            s, a, actor_id=world.org.user_id, prefix=world.prefix,
            credential_id=world.credential_id, public_console_url=CONSOLE,
        )
        assert [row.address for row in listed.rows] == list(world.addresses)
        assert (listed.unvouched_count, listed.awaiting_review_count) == (0, 0)
        with pytest.raises(PushValidationError, match="not one of this client's active push"):
            await run_list.expand_prefix(
                s, a, actor_id=world.org.user_id, prefix=other_prefix,
                credential_id=world.credential_id, public_console_url=CONSOLE,
            )
        await s.rollback()


# --------------------------------------------------------------------------- #
# Runs: one client's subnet, one client's credential, one active per client
# --------------------------------------------------------------------------- #


async def _preview(env: ApiEnv, headers: dict[str, str], prefix: str, credential: UUID):
    return await env.client.get(
        f"{P}/runs/preview", params={"prefix": prefix, "credential_id": str(credential)},
        headers=headers,
    )


async def _start(env: ApiEnv, headers: dict[str, str], prefix: str, credential: UUID, digest):
    return await env.client.post(
        f"{P}/runs",
        json={"prefix": prefix, "credential_id": str(credential), "list_digest": digest},
        headers=headers,
    )


async def _client_world(env: ApiEnv, tag: str, *, octet: int, count: int = 2):
    """An org-admin's org with an X world on `10.<octet>.0.0/24`."""
    world = await run_world(env, tag, octet=octet, count=0)
    x, x_site = await client_with_site(env, world.org.org_id)
    xw = await run_world(
        env, tag, octet=octet + 1, count=count, org=world.org, client=(x, x_site)
    )
    return world, xw, x, x_site


async def test_a_run_is_its_credential_s_client_s_and_so_are_its_targets(
    api_env: ApiEnv, published: list[Any]
) -> None:
    _base, xw, x, _x_site = await _client_world(api_env, "run-x", octet=160)
    x_tech = await everything_on(api_env, xw.org.org_id, x)
    preview = await _preview(api_env, x_tech, xw.prefix, xw.credential_id)
    assert preview.status_code == 200, preview.text
    assert (preview.json()["ready_count"], preview.json()["ambiguous_count"]) == (2, 0)
    started = await _start(
        api_env, x_tech, xw.prefix, xw.credential_id, preview.json()["list_digest"]
    )
    assert started.status_code == 201, started.text
    assert started.json()["client_id"] == str(x)
    async with AsyncSession(api_env.admin_engine) as s:
        run = (await s.execute(select(PushRun).where(PushRun.id == UUID(started.json()["id"])))
               ).scalar_one()
        targets = (await s.execute(select(PushRunTarget.client_id).where(
            PushRunTarget.run_id == run.id))).scalars().all()
    assert (run.client_id, set(targets)) == (x, {x})
    # One first (spec §1.4a), through X's site server.
    assert [device for device, _e in published] == [xw.site_server_device_id]
    actions = await _audits(api_env, xw.org.org_id)
    assert actions == [("push.run.started", x), ("push.dispatched", x)]


async def test_a_run_is_built_only_from_its_client_s_authorization_and_machines(
    api_env: ApiEnv, published: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y authorizes the same prefix and has vouched machines in it: X's
    preview lists X's machines only, counts only X's unvouched rows, holds
    only X's machines against the list ceiling, and an X credential cannot
    build a run on Y's authorization."""
    base, xw, x, x_site = await _client_world(api_env, "run-only", octet=162)
    org = xw.org.org_id
    y, y_site = await client_with_site(api_env, org)
    y_credential = await F.seed_credential(api_env, org, client_id=y)
    await F.seed_push_authorization(api_env, xw.org, xw.prefix, client_id=y)
    await F.seed_asset(api_env, org, primary_ip=f"{xw.prefix[:-4]}200", client_id=y,
                       site_id=y_site)
    await F.seed_asset(api_env, org, primary_ip=f"{xw.prefix[:-4]}201", client_id=y,
                       site_id=y_site, source="discovery", provenance="sweep")
    admin = await _admin(api_env, org)
    preview = await _preview(api_env, admin, xw.prefix, xw.credential_id)
    assert preview.status_code == 200, preview.text
    assert [r["address"] for r in preview.json()["rows"]] == list(xw.addresses)
    assert preview.json()["unvouched_count"] == 0
    # The ceiling is X's: at exactly X's own count of vouched machines in the
    # subnet, Y's vouched machine there does not tip the list over it.
    async with AsyncSession(api_env.admin_engine) as s:
        own = (
            await s.execute(
                text(
                    "SELECT count(DISTINCT primary_ip) FROM devices WHERE org_id = :o"
                    " AND client_id = :x AND primary_ip <<= CAST(:p AS cidr)"
                    " AND provenance = ANY(CAST(:v AS text[])) AND review_state = 'approved'"
                ),
                {"o": org, "x": x, "p": xw.prefix, "v": list(preflight.VOUCHED_PROVENANCE)},
            )
        ).scalar_one()
    ceiling = run_list.PUSH_RUN_LIST_MAX
    monkeypatch.setattr(run_list, "PUSH_RUN_LIST_MAX", own)
    at_ceiling = await _preview(api_env, admin, xw.prefix, xw.credential_id)
    assert at_ceiling.status_code == 200, at_ceiling.text
    monkeypatch.setattr(run_list, "PUSH_RUN_LIST_MAX", ceiling)

    # Y's credential on the same prefix: Y's authorization holds, and Y's one
    # vouched machine is listed -- not ready (no scope, no site server), and
    # never X's.
    preview_y = await _preview(api_env, admin, xw.prefix, y_credential)
    assert preview_y.status_code == 200, preview_y.text
    assert [r["address"] for r in preview_y.json()["rows"]] == [f"{xw.prefix[:-4]}200"]
    assert preview_y.json()["unvouched_count"] == 1

    # X's credential on a prefix only Y authorized: refused, as typed.
    other_prefix = f"10.{163 + 50}.0.0/24"
    await F.seed_push_authorization(api_env, xw.org, other_prefix, client_id=y)
    refused = await _preview(api_env, admin, other_prefix, xw.credential_id)
    assert refused.status_code == 400, refused.text
    assert "active push authorizations" in refused.json()["detail"]
    assert base.org == xw.org and x_site is not None


async def test_d3_one_active_run_per_client_through_the_api(
    api_env: ApiEnv, published: list[Any]
) -> None:
    base, xw, x, _x_site = await _client_world(api_env, "run-d3", octet=164, count=1)
    admin = await _admin(api_env, xw.org.org_id)
    for world in (base, xw):
        if world is base:
            # The internal client's world: one machine on its own prefix.
            await F.seed_asset(api_env, base.org.org_id, primary_ip=f"{base.prefix[:-4]}10")
            from tests.agent_push_fixtures import seed_confirmed_host_key

            await seed_confirmed_host_key(api_env, base.org, address=f"{base.prefix[:-4]}10")
        digest = (await _preview(api_env, admin, world.prefix, world.credential_id)).json()[
            "list_digest"
        ]
        r = await _start(api_env, admin, world.prefix, world.credential_id, digest)
        assert r.status_code == 201, r.text
    digest = (await _preview(api_env, admin, xw.prefix, xw.credential_id)).json()["list_digest"]
    second = await _start(api_env, admin, xw.prefix, xw.credential_id, digest)
    assert second.status_code == 409 and "one active run" in second.json()["detail"]
    assert x is not None


async def test_a_confirmation_binds_its_client_and_its_credential(
    api_env: ApiEnv, published: list[Any]
) -> None:
    """2a(f): "bound by a digest of that list". The digest covers the
    credential and its client, so a confirmation shown for one credential
    cannot start a run with another -- not another of the same client, and
    not one of another client -- even over the same machines."""
    _base, xw, x, _x_site = await _client_world(api_env, "run-dig", octet=166)
    org = xw.org.org_id
    second = await F.seed_credential(api_env, org, client_id=x)
    await F.seed_push_credential_scope(api_env, xw.org, second, xw.prefix)
    admin = await _admin(api_env, org)
    shown = (await _preview(api_env, admin, xw.prefix, xw.credential_id)).json()
    other = (await _preview(api_env, admin, xw.prefix, second)).json()
    # The same machines, ready under both.
    assert [r["address"] for r in shown["rows"]] == [r["address"] for r in other["rows"]]
    assert shown["ready_count"] == other["ready_count"] == 2
    assert shown["list_digest"] != other["list_digest"]
    before = await _written(api_env, org)
    replayed = await _start(api_env, admin, xw.prefix, second, shown["list_digest"])
    assert (replayed.status_code, replayed.json()["detail"]["code"]) == (409, "list_changed")
    assert await _written(api_env, org) == before
    assert run_list.compute_list_digest(
        [], client_id=x, credential_id=xw.credential_id
    ) != run_list.compute_list_digest([], client_id=uuid4(), credential_id=xw.credential_id)


async def test_the_subnets_offered_are_the_credential_s_client_s(
    api_env: ApiEnv, published: list[Any]
) -> None:
    """`GET /runs/prefixes?credential_id=`: that credential's client's active
    authorizations, under the same conjunction on that client. Without a
    credential the list needs every client, as before."""
    base, xw, x, _x_site = await _client_world(api_env, "run-pfx", octet=168, count=1)
    org = xw.org.org_id
    x_tech = await everything_on(api_env, org, x)
    own = await api_env.client.get(
        f"{P}/runs/prefixes", params={"credential_id": str(xw.credential_id)}, headers=x_tech
    )
    assert own.status_code == 200, own.text
    assert [(p["prefix"], p["client_id"]) for p in own.json()] == [(xw.prefix, str(x))]
    unfiltered = await api_env.client.get(f"{P}/runs/prefixes", headers=x_tech)
    assert unfiltered.status_code == 403, unfiltered.text
    admin = await _admin(api_env, org)
    everything = await api_env.client.get(f"{P}/runs/prefixes", headers=admin)
    assert sorted(p["prefix"] for p in everything.json()) == sorted([base.prefix, xw.prefix])
    internal = await api_env.client.get(
        f"{P}/runs/prefixes", params={"credential_id": str(base.credential_id)}, headers=admin
    )
    assert [p["prefix"] for p in internal.json()] == [base.prefix]


async def test_a_run_is_built_only_with_checkout_in_the_credential_s_client(
    api_env: ApiEnv, published: list[Any]
) -> None:
    """`credentials.checkout` on X, every other code on every client: Y's
    subnets and Y's list are a 403 -- the conjunction is read in the
    credential's client, not merely somewhere -- and X's are offered."""
    _base, xw, x, _x_site = await _client_world(api_env, "run-split", octet=174, count=1)
    org = xw.org.org_id
    y, y_site = await client_with_site(api_env, org)
    yw = await run_world(api_env, "run-split", octet=176, count=1, org=xw.org, client=(y, y_site))
    headers, _user_id = await _checkout_only_on(api_env, org, x)
    for world, expected in ((yw, 403), (xw, 200)):
        offered = await api_env.client.get(
            f"{P}/runs/prefixes", params={"credential_id": str(world.credential_id)},
            headers=headers,
        )
        assert offered.status_code == expected, offered.text
        preview = await _preview(api_env, headers, world.prefix, world.credential_id)
        assert preview.status_code == expected, preview.text
    assert [p["prefix"] for p in offered.json()] == [xw.prefix]
    assert preview.json()["client_id"] == str(x)


async def test_a_technician_of_x_controls_x_s_run_and_not_y_s(
    api_env: ApiEnv, published: list[Any]
) -> None:
    _base, xw, x, _x_site = await _client_world(api_env, "run-ctl", octet=170, count=1)
    org = xw.org.org_id
    y, y_site = await client_with_site(api_env, org)
    yw = await run_world(api_env, "run-ctl", octet=172, count=1, org=xw.org, client=(y, y_site))
    admin = await _admin(api_env, org)
    run_ids = {}
    for world in (xw, yw):
        digest = (await _preview(api_env, admin, world.prefix, world.credential_id)).json()[
            "list_digest"
        ]
        r = await _start(api_env, admin, world.prefix, world.credential_id, digest)
        assert r.status_code == 201, r.text
        run_ids[world.client_id] = r.json()["id"]
    x_tech = await everything_on(api_env, org, x)
    for verb in ("pause", "resume", "cancel"):
        own = await api_env.client.post(f"{P}/runs/{run_ids[x]}/{verb}", headers=x_tech)
        assert own.status_code == 200, (verb, own.text)
        other = await api_env.client.post(f"{P}/runs/{run_ids[y]}/{verb}", headers=x_tech)
        assert other.status_code == 404, (verb, other.text)
    listed = await api_env.client.get(f"{P}/runs", headers=x_tech)
    assert [r["id"] for r in listed.json()] == [run_ids[x]]
    preview = await _preview(api_env, x_tech, yw.prefix, yw.credential_id)
    assert preview.status_code == 404, preview.text
    controls = [a for a in await _audits(api_env, org) if a[0].startswith("push.run.")]
    assert controls == [
        ("push.run.started", x), ("push.run.started", y),
        ("push.run.paused", x), ("push.run.resumed", x), ("push.run.cancelled", x),
    ]


async def test_the_run_creation_lock_is_per_client(api_env: ApiEnv) -> None:
    """Two confirmations for one client serialize on an advisory lock keyed by
    the client; another client's confirmation does not wait for it."""
    base, xw, x, _x_site = await _client_world(api_env, "run-lock", octet=174, count=1)
    org = xw.org.org_id
    await F.seed_asset(api_env, org, primary_ip=f"{base.prefix[:-4]}10")
    await F.seed_confirmed_host_key(api_env, base.org, address=f"{base.prefix[:-4]}10")

    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('push_run:' || :o || ':' || :c))"),
            {"o": str(org), "c": str(x)},
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        # The internal client's run goes straight through.
        await asyncio.wait_for(start_run(api_env.app_engine, base), timeout=10)
        x_run = asyncio.create_task(start_run(api_env.app_engine, xw))
        waited = await _waits_on_advisory(observer, me)
        await holder.commit()
    assert waited, "X's confirmation never waited on X's run lock"
    await x_run


async def _waits_on_advisory(observer: AsyncSession, holder_pid: int) -> bool:
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(
                text(
                    "SELECT count(*) FROM pg_locks w JOIN pg_locks h"
                    " ON h.locktype = 'advisory' AND h.objid = w.objid AND h.classid = w.classid"
                    " WHERE w.locktype = 'advisory' AND NOT w.granted AND h.granted"
                    " AND h.pid = :me AND w.pid <> :me"
                ),
                {"me": holder_pid},
            )
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False


async def test_an_ambiguous_address_is_counted_and_never_listed(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """research-4 §I4. With the client always named it is 0 (the previews
    above); here the ladder is made to answer `ambiguous_asset`, as it would
    for a caller that could name no client, and the address is counted --
    never listed, never in the digest -- and the count reaches the started
    audit."""
    world = await run_world(api_env, "run-amb", octet=176, count=2)
    real = preflight.evaluate_targets

    async def _ambiguous_first(session: Any, org_id: UUID, **kw: Any) -> Any:
        decisions = await real(session, org_id, **kw)
        first = decisions[0]
        decisions[0] = preflight.TargetDecision(
            address=first.address, client_id=first.client_id, refusals=("ambiguous_asset",)
        )
        return decisions

    monkeypatch.setattr(run_list, "evaluate_targets", _ambiguous_first)
    async with org_session(world.org.org_id, engine=api_env.app_engine,
                           clients=ClientScope.all()) as s:
        listed = await run_list.expand_prefix(
            s, world.org.org_id, actor_id=world.org.user_id, prefix=world.prefix,
            credential_id=world.credential_id, public_console_url=CONSOLE,
        )
        assert (listed.ambiguous_count, [r.address for r in listed.rows]) == (
            1, [world.addresses[1]]
        )
        run = await runs.create_run(
            s, world.org.org_id, actor_id=world.org.user_id, prefix=world.prefix,
            credential_id=world.credential_id, list_digest=listed.list_digest,
            public_console_url=CONSOLE,
        )
        assert run.target_count == 1
    async with AsyncSession(api_env.admin_engine) as s:
        meta = (await s.execute(select(AuditEvent.meta).where(
            AuditEvent.org_id == world.org.org_id, AuditEvent.action == "push.run.started"
        ))).scalar_one()
    assert meta["ambiguous_count"] == 1


# --------------------------------------------------------------------------- #
# The pacer: one run, one client, one transaction
# --------------------------------------------------------------------------- #


def _settings(pg_cluster: PgCluster, tmp_path: Path) -> Any:
    from app.core.config import Settings

    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        public_console_url=CONSOLE,
    )


@pytest.fixture()
def beat(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    from app import worker

    sent: list[tuple[Any, Any]] = []

    async def _publish(redis: Any, device_id: Any, envelope: Any) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(worker, "publish_job", _publish)
    monkeypatch.setattr(worker, "get_vault", lambda settings: FakeVault())
    monkeypatch.setattr(worker, "ObjectStore", lambda settings: FakeStore())
    return sent


async def test_the_beat_continues_each_client_s_run_in_that_client_s_scope(
    api_env: ApiEnv, pg_cluster: PgCluster, tmp_path: Path, beat: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clients' runs in one org, both active (D3), both continued by one
    beat -- each in a session that can see only its own client, so the
    database refuses any row of another (the reviewed every-client entry is
    gone)."""
    from app import worker
    from app.core.config import override_settings

    base, xw, x, _x_site = await _client_world(api_env, "beat-two", octet=178, count=1)
    await F.seed_asset(api_env, base.org.org_id, primary_ip=f"{base.prefix[:-4]}10")
    await F.seed_confirmed_host_key(api_env, base.org, address=f"{base.prefix[:-4]}10")
    base_run = await start_run(api_env.app_engine, base)
    x_run = await start_run(api_env.app_engine, xw)
    scopes: dict[UUID, ClientScope] = {}
    real = runs.advance_run

    async def _spy(session: Any, org_id: UUID, run_id: UUID, **kw: Any) -> Any:
        scopes[run_id] = session.info[SCOPE_KEY]
        return await real(session, org_id, run_id, **kw)

    monkeypatch.setattr(runs, "advance_run", _spy)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.push_run_tick_all_orgs()
    assert scopes[base_run] == ClientScope.only([base.org.org_id])
    assert scopes[x_run] == ClientScope.only([x])
    mine = {d for d, _e in beat if d in (base.site_server_device_id, xw.site_server_device_id)}
    assert mine == {base.site_server_device_id, xw.site_server_device_id}


async def test_finding_the_live_runs_is_one_statement(api_env: ApiEnv) -> None:
    """No fan-out (lesson 3): however many runs of however many clients, the
    beat finds them with one statement, and the triples name each run's own
    client."""
    base, xw, x, _ = await _client_world(api_env, "beat-one", octet=180, count=1)
    await F.seed_asset(api_env, base.org.org_id, primary_ip=f"{base.prefix[:-4]}10")
    await F.seed_confirmed_host_key(api_env, base.org, address=f"{base.prefix[:-4]}10")
    base_run = await start_run(api_env.app_engine, base)
    x_run = await start_run(api_env.app_engine, xw)
    seen: list[str] = []

    def tick(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    engine = api_env.admin_engine.sync_engine
    event.listen(engine, "before_cursor_execute", tick)
    try:
        live = await runs.org_ids_with_live_runs(api_env.admin_engine)
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    assert len([q for q in seen if "push_run" in q]) == 1, seen
    mine = {(o, r, c) for o, r, c in live if o == base.org.org_id}
    assert mine == {(base.org.org_id, base_run, base.org.org_id), (base.org.org_id, x_run, x)}


async def test_the_owner_s_permission_is_read_in_the_run_s_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """`_owner_may_push` with the run's client: a person holding both codes
    on X only continues X's run (research-4 §C, the bug-in-waiting)."""
    _base, xw, x, _ = await _client_world(api_env, "beat-owner", octet=182, count=1)
    org = xw.org.org_id
    x_tech_headers = await everything_on(api_env, org, x)
    me = await api_env.client.get("/api/v1/me", headers=x_tech_headers)
    tech_id = UUID(me.json()["user_id"])
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        listed = await run_list.expand_prefix(
            s, org, actor_id=tech_id, prefix=xw.prefix, credential_id=xw.credential_id,
            public_console_url=CONSOLE,
        )
        run = await runs.create_run(
            s, org, actor_id=tech_id, prefix=xw.prefix, credential_id=xw.credential_id,
            list_digest=listed.list_digest, public_console_url=CONSOLE,
        )
        envelopes = await runs.advance_run(s, org, run.id, dispatcher=make_dispatcher(tmp_path))
    assert [d for d, _e in envelopes] == [xw.site_server_device_id]


async def test_an_owner_holding_the_codes_only_in_another_client_continues_nothing(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """The negative twin: the person who confirmed X's run now holds both
    codes on Y only (their grant moved). Held SOMEWHERE is not held in the
    run's client: X's target is refused `run_owner_not_permitted`."""
    _base, xw, x, _ = await _client_world(api_env, "beat-owner-moved", octet=208, count=1)
    org = xw.org.org_id
    y, _y_site = await client_with_site(api_env, org)
    x_tech_headers = await everything_on(api_env, org, x)
    me = await api_env.client.get("/api/v1/me", headers=x_tech_headers)
    tech_id = UUID(me.json()["user_id"])
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        listed = await run_list.expand_prefix(
            s, org, actor_id=tech_id, prefix=xw.prefix, credential_id=xw.credential_id,
            public_console_url=CONSOLE,
        )
        run = await runs.create_run(
            s, org, actor_id=tech_id, prefix=xw.prefix, credential_id=xw.credential_id,
            list_digest=listed.list_digest, public_console_url=CONSOLE,
        )
        run_id = run.id
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE user_roles SET client_id = :y WHERE user_id = :u"),
            {"y": y, "u": tech_id},
        )
        await s.commit()
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        envelopes = await runs.advance_run(s, org, run_id, dispatcher=make_dispatcher(tmp_path))
    assert envelopes == []
    async with AsyncSession(api_env.admin_engine) as s:
        (target,) = (
            await s.execute(select(PushRunTarget).where(PushRunTarget.run_id == run_id))
        ).scalars()
    assert (target.state, target.job_id) == ("refused", None)
    assert "run_owner_not_permitted" in target.reasons


@pytest.mark.parametrize("scope", ["the run's client", "every client"])
async def test_a_run_whose_credential_left_its_client_pushes_nothing(
    api_env: ApiEnv, tmp_path: Path, scope: str
) -> None:
    """`_run_credential` resolves the credential IN THE RUN'S CLIENT on every
    pass. `its_push_run_client_guard` binds them when the run is written; a
    credential's client can still change afterwards (an O row follows its
    group's client, and nothing on `credentials` looks at `push_run`). Moved
    to Y, it is not X's credential any more: X's machine is refused as
    `credential_missing`, recorded in X, and nothing is signed or minted.

    "every client" is the load-bearing case: a pass in the API's transaction
    runs in its caller's scope, where RLS shows Y's credential and its scope,
    and only the client comparison refuses them. In the beat's scope RLS
    hides both as well."""
    world = await run_world(api_env, "beat-moved", octet=186, count=1)
    run_id = await start_run(api_env.app_engine, world)
    org = world.org.org_id
    y, _y_site = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE credentials SET client_id = :y WHERE id = :c"),
            {"y": y, "c": world.credential_id},
        )
        await s.commit()
    before = await _written(api_env, org)
    clients = ClientScope.only([org]) if scope == "the run's client" else ClientScope.all()
    async with org_session(org, engine=api_env.app_engine, clients=clients) as s:
        envelopes = await runs.advance_run(s, org, run_id, dispatcher=make_dispatcher(tmp_path))
    assert envelopes == []
    async with AsyncSession(api_env.admin_engine) as s:
        (target,) = (
            await s.execute(select(PushRunTarget).where(PushRunTarget.run_id == run_id))
        ).scalars()
    assert (target.state, target.job_id) == ("refused", None)
    assert "credential_missing" in target.reasons
    after = await _written(api_env, org)
    # The refusal and, its only target settled, the run's completion: two
    # audit rows, both X's, and no job, token or batch row.
    assert {k: after[k] - before[k] for k in after if after[k] != before[k]} == {
        "audit_events": 2
    }
    assert [
        a for a in await _audits(api_env, org)
        if a[0] in ("push.refused", "push.dispatched", "push.run.completed")
    ] == [("push.refused", org), ("push.run.completed", org)]


# --------------------------------------------------------------------------- #
# Lock order: sites and clients before the first audit (research-4 §I5)
# --------------------------------------------------------------------------- #

WAITS_ON_A_ROW = text(
    "SELECT count(*) FROM pg_locks t "
    "WHERE t.locktype = 'tuple' AND t.relation = CAST(:rel AS regclass) AND t.pid <> :me "
    "AND (NOT t.granted OR EXISTS ("
    "  SELECT 1 FROM pg_locks w JOIN pg_locks h ON h.transactionid = w.transactionid "
    "  WHERE w.pid = t.pid AND w.locktype = 'transactionid' AND NOT w.granted "
    "  AND h.pid = :me AND h.locktype = 'transactionid' AND h.granted))"
)


async def _wait_until_blocked(observer: AsyncSession, holder_pid: int, relation: str) -> bool:
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(WAITS_ON_A_ROW, {"me": holder_pid, "rel": relation})
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False


async def _two_site_install_world(env: ApiEnv, tag: str) -> tuple[Any, UUID, list[str]]:
    """X with two sites, one vouched machine at each, every bound held for
    an install of both in ONE ticked push."""
    org = await env.create_org(tag)
    x, site_a = await client_with_site(env, org)
    world = await F.push_world(env, org, client_id=x, site_id=site_a)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        from app.models import Site

        site_b = Site(org_id=org, client_id=x, name="Sucursal")
        s.add(site_b)
        await s.commit()
    second = "10.70.0.18"
    await F.seed_asset(env, org, primary_ip=second, client_id=x, site_id=site_b.id)
    await F.seed_agent_release(env, os_name="linux", arch="amd64")
    for address in (world.address, second):
        await F.seed_confirmed_host_key(env, world.org, address=address)
    await grant_org_admin(env, world.org)
    return world, site_b.id, [world.address, second]


async def _install(env: ApiEnv, world: Any, targets: list[str], tmp_path: Path) -> Any:
    async with org_session(
        world.org.org_id, engine=env.app_engine, clients=ClientScope.all()
    ) as s:
        return await dispatch_service.dispatch_push_batch(
            s, job_engine(tmp_path), lambda: FakeStore(),
            FakeVault(CRED_JSON),  # type: ignore[arg-type]
            org_id=world.org.org_id, actor_id=world.org.user_id,
            credential_id=world.credential, targets=targets, kind="install",
            public_console_url=CONSOLE,
        )


@pytest.mark.parametrize("held", ["second site", "client"])
async def test_a_ticked_install_takes_its_sites_and_client_before_the_audit_chain(
    api_env: ApiEnv, held: str, tmp_path: Path
) -> None:
    """THE INVERSION research-4 §I5 names. Minting each target's token took
    its site and client AFTER the previous target's `push.dispatched` took
    the audit chain; `archive_site` holds the site and then wants the chain.
    Held here as an archive holds it, the push waits on it BEFORE its first
    audit; the holder takes the chain and commits; the push then goes
    through -- never a 40P01."""
    world, site_b, targets = await _two_site_install_world(api_env, f"lock-{held[:4]}")
    org = world.org.org_id
    row = site_b if held == "second site" else world.client_id
    relation = "sites" if held == "second site" else "clients"
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text(f"SELECT id FROM {relation} WHERE id = :r FOR NO KEY UPDATE"), {"r": row}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        push = asyncio.create_task(_install(api_env, world, targets, tmp_path))
        if not await _wait_until_blocked(observer, me, relation):
            push.cancel()
            pytest.fail("the push never waited on the held row")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    result = await push
    assert result.refused == {}
    assert sorted(t.address for t in result.dispatched) == sorted(targets)


async def test_the_pacer_takes_its_sites_and_client_before_a_refusal_s_audit(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """The pacer's version: the first waiting target is refused (audited)
    before the second mints a token. With the run's client held as
    `archive_client` holds it, the pass waits before that audit."""
    world = await run_world(api_env, "lock-pacer", octet=184, count=2)
    run_id = await start_run(api_env.app_engine, world)
    # The first target becomes unpushable: its asset is no longer vouched.
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            text("UPDATE devices SET review_state = 'pending' WHERE id = :d"),
            {"d": world.device_ids[0]},
        )
        await s.commit()
    org = world.org.org_id

    async def _advance() -> Any:
        async with org_session(org, engine=api_env.app_engine,
                               clients=ClientScope.only([org])) as s:
            return await runs.advance_run(s, org, run_id, dispatcher=make_dispatcher(tmp_path))

    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :c FOR NO KEY UPDATE"), {"c": org}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(_advance())
        if not await _wait_until_blocked(observer, me, "clients"):
            task.cancel()
            pytest.fail("the pacer never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    envelopes = await task
    assert [d for d, _e in envelopes] == [world.site_server_device_id]
    assert [a for a in await _audits(api_env, org) if a[0] in ("push.refused",
                                                               "push.dispatched")] == [
        ("push.refused", org), ("push.dispatched", org)
    ]


async def _held_client_then_chain(
    env: ApiEnv, org: UUID, client: UUID, work: Any, what: str
) -> Any:
    """Hold `client` as `archive_client` does, start `work`, see it wait on
    that row, then take the audit chain and commit: `work` must have waited
    BEFORE its own first audit, or this is a 40P01."""
    async with AsyncSession(env.admin_engine) as holder, AsyncSession(
        env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :c FOR NO KEY UPDATE"), {"c": client}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(work())
        if not await _wait_until_blocked(observer, me, "clients"):
            task.cancel()
            pytest.fail(f"{what} never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    return await task


async def test_a_confirmation_takes_its_sites_and_client_before_push_run_started(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """`create_run` audits `push.run.started`, and the pacer the API runs in
    the same transaction then mints tokens: the run's sites and client are
    taken before that audit."""
    world = await run_world(api_env, "lock-create", octet=196, count=1)
    org = world.org.org_id

    async def _confirm_and_pace() -> Any:
        async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
            listed = await run_list.expand_prefix(
                s, org, actor_id=world.org.user_id, prefix=world.prefix,
                credential_id=world.credential_id, public_console_url=CONSOLE,
            )
            run = await runs.create_run(
                s, org, actor_id=world.org.user_id, prefix=world.prefix,
                credential_id=world.credential_id, list_digest=listed.list_digest,
                public_console_url=CONSOLE,
            )
            return await runs.advance_run(s, org, run.id, dispatcher=make_dispatcher(tmp_path))

    envelopes = await _held_client_then_chain(
        api_env, org, world.client_id, _confirm_and_pace, "the confirmation"
    )
    assert [d for d, _e in envelopes] == [world.site_server_device_id]


async def test_a_resume_takes_its_sites_and_client_before_push_run_resumed(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """The same for `resume_run`: `push.run.resumed`, then the pacer's mint."""
    world = await run_world(api_env, "lock-resume", octet=198, count=1)
    run_id = await start_run(api_env.app_engine, world)
    org = world.org.org_id
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await runs.pause_run(s, org, run_id=run_id, actor_id=world.org.user_id)

    async def _resume_and_pace() -> Any:
        async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
            await runs.resume_run(s, org, run_id=run_id, actor_id=world.org.user_id)
            return await runs.advance_run(s, org, run_id, dispatcher=make_dispatcher(tmp_path))

    envelopes = await _held_client_then_chain(
        api_env, org, world.client_id, _resume_and_pace, "the resume"
    )
    assert [d for d, _e in envelopes] == [world.site_server_device_id]


async def test_the_lock_scope_takes_nothing_of_another_org(api_env: ApiEnv) -> None:
    """`locks.py`'s org predicates, load-bearing (lesson 2), on the admin
    (BYPASSRLS) session: org B's device names no site for A; B's site, and B's
    client through a site of A planted under it (its trigger disabled, rolled
    back), are not locked -- a FOR NO KEY UPDATE NOWAIT on each goes through."""
    from app.models import Site
    from app.modules.agent_push.locks import lock_target_scope, sites_of

    a = await api_env.create_org("locks-org-a")
    b = await api_env.create_org("locks-org-b")
    b_client, b_site = await client_with_site(api_env, b)
    b_device = await F.seed_asset(
        api_env, b, primary_ip="10.72.0.5", client_id=b_client, site_id=b_site
    )
    async with AsyncSession(api_env.admin_engine) as s, AsyncSession(
        api_env.admin_engine
    ) as probe:
        assert await sites_of(s, a, [b_device]) == set()
        await lock_target_scope(s, a, [b_site])
        for table, row in (("sites", b_site), ("clients", b_client)):
            await probe.execute(
                text(f"SELECT id FROM {table} WHERE id = :r FOR NO KEY UPDATE NOWAIT"), {"r": row}
            )
            await probe.rollback()
        await s.rollback()

        await s.execute(text("ALTER TABLE sites DISABLE TRIGGER its_site_client_check"))
        planted = Site(org_id=a, client_id=b_client, name=f"planted-{uuid4().hex[:6]}")
        s.add(planted)
        await s.flush()
        await lock_target_scope(s, a, [planted.id])
        await probe.execute(
            text("SELECT id FROM clients WHERE id = :r FOR NO KEY UPDATE NOWAIT"), {"r": b_client}
        )
        await probe.rollback()
        await s.rollback()


async def test_another_org_s_active_run_under_this_client_blocks_nothing(
    api_env: ApiEnv,
) -> None:
    """The active-run check's org predicate, load-bearing (lesson 2): org B's
    running run planted under A's client (`fk_push_run_client_org` dropped,
    rolled back), on the admin session where RLS would pass it. A's run is
    still confirmed: one active run per client is per client OF THE ORG."""
    world = await run_world(api_env, "d3-planted", octet=202, count=1)
    a = world.org.org_id
    b = await api_env.create_org("d3-planted-b")
    b_user = await _user(api_env, b)
    async with AsyncSession(api_env.admin_engine) as s:
        await drop_client_keys(s, "push_run", "fk_push_run_client_org")
        s.add(PushRun(org_id=b, client_id=world.client_id, credential_id=None,
                      prefix="10.9.0.0/24", list_digest="b" * 64, target_count=0,
                      state="running", created_by=b_user))
        await s.flush()
        listed = await run_list.expand_prefix(
            s, a, actor_id=world.org.user_id, prefix=world.prefix,
            credential_id=world.credential_id, public_console_url=CONSOLE,
        )
        run = await runs.create_run(
            s, a, actor_id=world.org.user_id, prefix=world.prefix,
            credential_id=world.credential_id, list_digest=listed.list_digest,
            public_console_url=CONSOLE,
        )
        assert (run.org_id, run.client_id, run.state) == (a, world.client_id, "running")
        await s.rollback()


# --------------------------------------------------------------------------- #
# Events and audit: every push event names its client
# --------------------------------------------------------------------------- #

#: Every audit action the push module writes. Each is asserted to carry the
#: client of the push, the run or the row it is about.
AUDITED_TASK_12 = {
    "push.authorization_created", "push.authorization_revoked",
    "push.credential_scope_bound", "push.credential_scope_unbound",
    "push.refused", "push.dispatched", "push.result",
    "push.host_key_observed", "push.host_key_confirmed", "push.winrm_readiness_observed",
    "push.run.started", "push.run.paused", "push.run.resumed", "push.run.cancelled",
    "push.run.stopped", "push.run.dispatch_failed", "push.run.completed",
}


def test_the_audited_set_is_every_push_action_the_module_writes() -> None:
    import re

    root = Path(dispatch_service.__file__).parent
    written = {
        match
        for path in root.glob("*.py")
        for match in re.findall(r'action="(push\.[a-z_.]+)"', path.read_text())
    }
    assert written == AUDITED_TASK_12


async def _finish_job(env: ApiEnv, job_id: UUID, outcome: str) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            text(
                "UPDATE jobs SET status = 'succeeded', result = CAST(:r AS jsonb),"
                " finished_at = now() WHERE id = :j"
            ),
            {"r": json.dumps({"outcome": outcome}), "j": job_id},
        )
        await s.commit()


async def _in_flight_job(env: ApiEnv, run_id: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(PushRunTarget.job_id).where(
                    PushRunTarget.run_id == run_id, PushRunTarget.state == "in_flight"
                )
            )
        ).scalar_one()


async def test_a_completed_and_a_stopped_run_are_published_and_audited_in_its_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """`runs.py`'s two `publish` calls (the `None  # ... Task 12` marker):
    the run's client, and the pacer's own audit events with it -- the
    completion, and the safety stop after three sign-in failures."""
    _base, xw, x, x_site = await _client_world(api_env, "evt", octet=186, count=1)
    org = xw.org.org_id
    dispatcher = make_dispatcher(tmp_path)

    async def advance(run_id: UUID) -> Any:
        async with org_session(org, engine=api_env.app_engine,
                               clients=ClientScope.only([x])) as s:
            return await runs.advance_run(s, org, run_id, dispatcher=dispatcher)

    completed = await start_run(api_env.app_engine, xw)
    assert len(await advance(completed)) == 1
    await _finish_job(api_env, await _in_flight_job(api_env, completed), "installed")
    assert await advance(completed) == []

    stopped_world = await run_world(
        api_env, "evt", octet=188, count=4, org=xw.org, client=(x, x_site)
    )
    stopped = await start_run(api_env.app_engine, stopped_world)
    for _ in range(3):
        assert len(await advance(stopped)) == 1
        await _finish_job(api_env, await _in_flight_job(api_env, stopped), "auth_failed")
    assert await advance(stopped) == []

    async with AsyncSession(api_env.admin_engine) as s:
        published = [
            tuple(row)
            for row in (
                await s.execute(
                    select(DomainEvent.subject_id, DomainEvent.event_type, DomainEvent.client_id)
                    .where(DomainEvent.org_id == org, DomainEvent.subject_type == "push_run")
                    .order_by(DomainEvent.created_at)
                )
            ).all()
        ]
    assert published == [
        (completed, "push.run.completed", x), (stopped, "push.run.stopped", x)
    ]
    pacer = [a for a in await _audits(api_env, org)
             if a[0] in ("push.run.completed", "push.run.stopped")]
    assert pacer == [("push.run.completed", x), ("push.run.stopped", x)]


# --------------------------------------------------------------------------- #
# Wake-on-LAN: the internal client's site servers only (research-4 §I6)
# --------------------------------------------------------------------------- #


async def test_an_install_result_is_audited_in_the_run_s_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """`record_install_result` files `push.result` under the job's client --
    the run's -- read from the job."""
    _base, xw, x, _ = await _client_world(api_env, "res-client", octet=204, count=1)
    run_id = await start_run(api_env.app_engine, xw)
    org = xw.org.org_id
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        envelopes = await runs.advance_run(s, org, run_id, dispatcher=make_dispatcher(tmp_path))
        assert len(envelopes) == 1
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        (target,) = (
            await s.execute(select(PushRunTarget).where(PushRunTarget.run_id == run_id))
        ).scalars()
        job = (await s.execute(select(Job).where(Job.id == target.job_id))).scalar_one()
        await job_engine(tmp_path).complete(
            s, job, state="succeeded",
            result={"target_address": str(target.address), "outcome": "installed",
                    "detail": ""},
        )
    assert [a for a in await _audits(api_env, org) if a[0] == "push.result"] == [
        ("push.result", x)
    ]


async def test_a_failed_dispatch_is_audited_in_the_run_s_client(
    api_env: ApiEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`push.run.dispatch_failed` (the signer failed after the checkout): X's."""
    _base, xw, x, _ = await _client_world(api_env, "fail-client", octet=206, count=1)
    run_id = await start_run(api_env.app_engine, xw)
    org = xw.org.org_id
    dispatcher = make_dispatcher(tmp_path, vault=FakeVault())

    async def _fail(job: Any) -> Any:
        raise RuntimeError("signer unavailable")

    monkeypatch.setattr(dispatcher._job_engine, "signed_envelope", _fail)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.only([x])) as s:
        assert await runs.advance_run(s, org, run_id, dispatcher=dispatcher) == []
    assert [a for a in await _audits(api_env, org) if a[0] == "push.run.dispatch_failed"] == [
        ("push.run.dispatch_failed", x)
    ]


async def test_host_key_events_are_filed_under_the_machine_s_client(api_env: ApiEnv) -> None:
    """Observed, confirmed and WinRM readiness: the row's client -- X's for
    X's machine, none (every client's) for a global row of no device."""
    from app.modules.agent_push import host_key as host_key_service

    org = await api_env.create_org("hk-audit")
    x, x_site = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    actor = await _org_admin_id(api_env, org)
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await host_key_service.record_observation(
            s, org, address=world.address, transport="ssh", fingerprint="SHA256:x",
            key_type="ssh-ed25519", device_id=world.device, job_id=None,
        )
        await host_key_service.confirm_host_key(
            s, org, actor_id=actor, address=world.address, fingerprint="SHA256:x"
        )
        await host_key_service.record_readiness(
            s, org, address=world.address, device_id=world.device, job_id=None,
            readiness={"winrm_https": True, "winrm_http": False, "kdc_reachable": True},
        )
        await host_key_service.record_observation(
            s, org, address="10.70.0.99", transport="ssh", fingerprint="SHA256:g",
            key_type="ssh-ed25519", device_id=None, job_id=None,
        )
    assert [
        a for a in await _audits(api_env, org)
        if a[0] in ("push.host_key_observed", "push.host_key_confirmed",
                    "push.winrm_readiness_observed")
    ] == [
        ("push.host_key_observed", x), ("push.host_key_confirmed", x),
        ("push.winrm_readiness_observed", x), ("push.host_key_observed", None),
    ]


async def test_every_credential_event_is_filed_under_its_client(api_env: ApiEnv) -> None:
    """created, checked out, rotated and deleted: the credential's client,
    read before each write expires it."""
    org = await api_env.create_org("cred-events")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    r = await api_env.client.post(
        CREDENTIALS,
        json={"name": "pi", "type": "push_install", "secret": UPN_SECRET, "client_id": str(x)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    url = f"{CREDENTIALS}/{r.json()['id']}"
    r = await api_env.client.post(f"{url}/checkout", json={"reason": "t"}, headers=admin)
    assert r.status_code == 200, r.text
    r = await api_env.client.put(url, json={"name": "pi", "secret": UPN_SECRET}, headers=admin)
    assert r.status_code == 200, r.text
    r = await api_env.client.delete(url, headers=admin)
    assert r.status_code == 200, r.text
    assert await _audits(api_env, org, "credential.") == [
        ("credential.created", x), ("credential.checked_out", x),
        ("credential.rotated", x), ("credential.deleted", x),
    ]


async def test_a_wake_never_goes_through_another_client_s_site_server(
    admin_engine: Any, app_engine: Any, tmp_path: Path
) -> None:
    """Provisioning is the internal client's (it needs every client), so its
    wake is originated by an internal site server. Another client's, declared
    for the MORE specific segment, is never chosen; without a segment it does
    not make the one internal candidate ambiguous either."""
    from app.core.vault import LocalVaultService
    from app.modules.provisioning.schema import ProvisionWakeRequest
    from tests.ipam_fixtures import seed_agent, seed_device
    from tests.site_server_fixtures import seed_site_server
    from tests.test_site_server_provisioning_wake import (
        _facts,
        _grant,
        _org_user_device,
        _start,
    )

    org, user, device = await _org_user_device(admin_engine)
    await _grant(admin_engine, org, user, "provisioning.run", "site.wake")
    await seed_agent(admin_engine, org, device)
    await seed_site_server(admin_engine, org, device_id=device, segments=["10.20.0.0/16"])
    y, y_site = await F.seed_client(admin_engine, org, "Y")
    y_device = await seed_device(admin_engine, org, client_id=y, site_id=y_site)
    await seed_agent(admin_engine, org, y_device)
    await seed_site_server(admin_engine, org, device_id=y_device, segments=["10.20.7.0/24"])
    vault = LocalVaultService(tmp_path / "root.key")

    await _start(
        app_engine, tmp_path, org_id=org, actor_id=user, vault=vault,
        wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33", segment="10.20.7.5"),
    )
    assert await _facts(admin_engine, org) == (1, 1, "10.20.255.255", str(device))

    await _start(
        app_engine, tmp_path, org_id=org, actor_id=user, vault=vault,
        wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:34"),
    )
    assert await _facts(admin_engine, org) == (2, 2, "10.20.255.255", str(device))


# --------------------------------------------------------------------------- #
# Host keys: two clients may hold one address
# --------------------------------------------------------------------------- #


async def test_a_host_key_names_its_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("hk")
    x, x_site = await client_with_site(api_env, org)
    world = await F.push_world(api_env, org, client_id=x, site_id=x_site)
    await F.seed_confirmed_host_key(api_env, world.org, address=world.address,
                                    device_id=world.device)
    admin = await _admin(api_env, org)
    keys = (await api_env.client.get(f"{P}/host-keys", headers=admin)).json()
    assert [(k["address"], k["client_id"]) for k in keys] == [(world.address, str(x))]

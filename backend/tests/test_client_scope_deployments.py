"""A deployment belongs to its target's client (plan 2026-10-26 Task 9).

`deployments.client_id` is written by the trigger `its_deployment_client`
(migration 0239) from the target -- one device or one group, a group being
one client's -- and never by the application. `create_deployment` resolves
the target in the request's own session first: a target the session cannot
see is a 404, the answer a missing one gets. The change that authorizes a
deployment is the deployment's client's, and that client must use change
management; the gates it meets and the automatic change it gets are that
client's. Dispatch, which the window sweep runs under scope all, reaches
only devices of the deployment's client.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeGate,
    ChangeLink,
    ChangeRequest,
    Deployment,
    Device,
    DeviceGroup,
    Job,
    Package,
)
from app.modules.change_mgmt.gates import authorizing_change, deployment_gate
from app.modules.deploy import api as deploy_api
from app.modules.deploy.dispatch import dispatch_deployment, ingest_deploy_result
from app.modules.deploy.schema import AppDeployResult
from tests.client_scope_support import (
    client_with_site,
    drop_client_keys,
    everything_on,
)
from tests.client_scope_support import (
    wait_until_blocked as _wait_until_blocked,
)
from tests.conftest import ApiEnv
from tests.test_deploy_dispatch import _FakeArtifactSigner, _FakeJobEngine

DEPLOYMENTS = "/api/v1/deploy/deployments"
MODULE_OFF = "this client does not use that module"



async def _device(
    env: ApiEnv, org: UUID, client: UUID, site: UUID, group: UUID | None = None
) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname="pc", os_family="windows",
                   device_group_id=group)
        s.add(d)
        await s.commit()
        return d.id


async def _group(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        g = DeviceGroup(org_id=org, client_id=client, name=f"g-{uuid4().hex[:6]}")
        s.add(g)
        await s.commit()
        return g.id


async def _package(env: ApiEnv, org: UUID) -> UUID:
    """Manager-sourced (winget): it dispatches with no uploaded artifact."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        p = Package(org_id=org, name=f"7zip-{uuid4().hex[:4]}", version="1", os_family="windows",
                    installer_type="winget", package_identifier="7zip.7zip")
        s.add(p)
        await s.commit()
        return p.id


async def _seed_deployment(
    env: ApiEnv, org: UUID, package: UUID, target_type: str, target: UUID
) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Deployment(org_id=org, package_id=package, action="install", target_type=target_type,
                       target_id=target, state="pending")
        s.add(d)
        await s.commit()
        return d.id


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _modules(env: ApiEnv, admin: dict[str, str], client: UUID, *modules: str) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": list(modules)}, headers=admin
    )
    assert r.status_code == 200, r.text


async def _approved_change(env: ApiEnv, admin: dict[str, str], client: UUID) -> UUID:
    r = await env.client.post(
        "/api/v1/changes",
        json={"type": "normal", "title": "Instalar 7-Zip", "description": "d", "risk": "low",
              "affected": [], "rollback_plan": "r", "client_id": str(client)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    change = UUID(r.json()["id"])
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(ChangeRequest).where(ChangeRequest.id == change).values(state="approved")
        )
        await s.commit()
    return change


def _body(package: UUID, target_type: str, target: UUID, **extra: Any) -> dict[str, Any]:
    return {"package_id": str(package), "action": "install", "target_type": target_type,
            "target_id": str(target), **extra}


async def _written(env: ApiEnv, org: UUID) -> tuple[int, int, int]:
    """(deployments, change links, audit events) of `org`: what a refused
    create must leave as it found it."""
    async with AsyncSession(env.admin_engine) as s:
        counts = []
        for model in (Deployment, ChangeLink, AuditEvent):
            counts.append(
                (
                    await s.execute(
                        select(func.count()).select_from(model).where(model.org_id == org)
                    )
                ).scalar_one()
            )
    return counts[0], counts[1], counts[2]


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_a_deployment_takes_its_target_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("d-client")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    device = await _device(api_env, org, x, x_site)
    group = await _group(api_env, org, x)
    package = await _package(api_env, org)
    for target_type, target in (("device", device), ("group", group)):
        r = await api_env.client.post(
            DEPLOYMENTS, json=_body(package, target_type, target), headers=admin
        )
        assert r.status_code == 201, r.text
        assert r.json()["client_id"] == str(x)
    listed = await api_env.client.get(DEPLOYMENTS, headers=admin)
    assert {d["client_id"] for d in listed.json()} == {str(x)}
    # Every client in scope, and still the request's own 404 for a target
    # that does not exist -- not the database's refusal of the row.
    for target_type in ("device", "group"):
        missing = await api_env.client.post(
            DEPLOYMENTS, json=_body(package, target_type, uuid4()), headers=admin
        )
        assert (missing.status_code, missing.json()) == (
            404, {"detail": "deployment target not found"}
        ), missing.text


async def test_a_technician_of_x_cannot_deploy_to_y(api_env: ApiEnv) -> None:
    """research-3 §E5: `create_deployment` never resolved `target_id`, and
    the window sweep dispatched it later under scope all. Y's device and Y's
    group answer exactly like a target that does not exist, and nothing is
    written."""
    org = await api_env.create_org("d-narrow")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    y_device = await _device(api_env, org, y, y_site)
    y_group = await _group(api_env, org, y)
    package = await _package(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    before = await _written(api_env, org)
    missing = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", uuid4()), headers=x_tech
    )
    assert missing.status_code == 404, missing.text
    assert missing.json() == {"detail": "deployment target not found"}
    for target_type, target in (("device", y_device), ("group", y_group)):
        r = await api_env.client.post(
            DEPLOYMENTS, json=_body(package, target_type, target), headers=x_tech
        )
        assert (r.status_code, r.json()) == (404, missing.json()), r.text
    assert await _written(api_env, org) == before


@pytest.mark.parametrize("target_type", ["device", "group"])
async def test_the_database_derives_the_client_and_refuses_a_contradiction(
    api_env: ApiEnv, target_type: str
) -> None:
    org = await api_env.create_org("d-trigger")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    group = await _group(api_env, org, x)
    target = await _device(api_env, org, x, x_site) if target_type == "device" else group
    package = await _package(api_env, org)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        d = Deployment(org_id=org, package_id=package, action="install", target_type=target_type,
                       target_id=target, state="pending")
        s.add(d)
        await s.flush()
        await s.refresh(d)
        assert d.client_id == x
    with pytest.raises(DBAPIError, match="differs from its target"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(Deployment(org_id=org, client_id=y, package_id=package, action="install",
                             target_type=target_type, target_id=target, state="pending"))
            await s.flush()


# --------------------------------------------------------------------------- #
# The trigger, below the application
# --------------------------------------------------------------------------- #


async def test_the_trigger_follows_a_retarget_and_refuses_what_it_cannot_resolve(
    api_env: ApiEnv,
) -> None:
    """A retarget carries the client along (the old one is not a claim), an
    unknown target type and a missing target are refused by name, and the
    same client sent again on an UPDATE is accepted."""
    org = await api_env.create_org("d-retarget")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device, y_device = (await _device(api_env, org, x, x_site),
                          await _device(api_env, org, y, y_site))
    package = await _package(api_env, org)
    deployment = await _seed_deployment(api_env, org, package, "device", x_device)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await s.execute(
            update(Deployment).where(Deployment.id == deployment).values(target_id=y_device)
        )
        moved = (
            await s.execute(select(Deployment.client_id).where(Deployment.id == deployment))
        ).scalar_one()
        assert moved == y
        await s.execute(
            update(Deployment).where(Deployment.id == deployment).values(client_id=y)
        )
    for values, message in (
        ({"target_type": "site"}, "has no client"),
        ({"target_id": uuid4()}, "deployment target is not visible"),
        ({"client_id": x}, "differs from its target"),
    ):
        with pytest.raises(DBAPIError, match=message):
            async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
                await s.execute(
                    update(Deployment).where(Deployment.id == deployment).values(**values)
                )


async def test_the_trigger_is_on_deployments_alone_and_fires_on_the_target(
    admin_engine: Any,
) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname AS tbl, t.tgtype AS type, t.tgfoid::regproc::text AS fn, "
                    "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                    "      WHERE a.attrelid = t.tgrelid "
                    "        AND a.attnum = ANY (t.tgattr::int2[])) AS columns "
                    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname = 'its_deployment_client'"
                )
            )
        ).all()
    assert [(r.tbl, r.fn, r.type) for r in rows] == [
        # ROW | BEFORE | INSERT | UPDATE
        ("deployments", "its_deployment_client", 1 | 2 | 4 | 16)
    ]
    assert set(rows[0].columns) == {"target_type", "target_id", "client_id"}


# --------------------------------------------------------------------------- #
# Reading: RLS
# --------------------------------------------------------------------------- #


async def test_a_technician_of_x_sees_and_runs_only_x_s_deployments(api_env: ApiEnv) -> None:
    org = await api_env.create_org("d-read")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    package = await _package(api_env, org)
    x_dep = await _seed_deployment(
        api_env, org, package, "device", await _device(api_env, org, x, x_site)
    )
    y_dep = await _seed_deployment(
        api_env, org, package, "device", await _device(api_env, org, y, y_site)
    )
    x_tech = await everything_on(api_env, org, x)

    listed = await api_env.client.get(DEPLOYMENTS, headers=x_tech)
    assert [d["id"] for d in listed.json()] == [str(x_dep)]
    assert (await api_env.client.get(f"{DEPLOYMENTS}/{y_dep}", headers=x_tech)).status_code == 404
    assert (await api_env.client.get(f"{DEPLOYMENTS}/{x_dep}", headers=x_tech)).status_code == 200
    run = await api_env.client.post(f"{DEPLOYMENTS}/{y_dep}/execute", headers=x_tech)
    assert (run.status_code, run.json()) == (200, {"job_ids": []}), run.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(Deployment.state).where(Deployment.id == y_dep))
        ).scalar_one() == "pending"
        assert (
            await s.execute(select(func.count()).select_from(Job).where(Job.org_id == org))
        ).scalar_one() == 0


# --------------------------------------------------------------------------- #
# Dispatch: spec §5 test 4, the code's own predicate with no RLS to mask it
# --------------------------------------------------------------------------- #


async def test_dispatch_reaches_only_the_deployment_s_client_and_org(api_env: ApiEnv) -> None:
    """The window sweep dispatches under scope all, so RLS hides nothing from
    `dispatch_deployment`; through the ADMIN engine (no RLS at all) its own
    predicates are all there is. The database makes a stray target
    impossible -- a device and its group share a client
    (`its_device_client_guard`), a deployment and its target too
    (`its_deployment_client`), a row and its client an organisation
    (`fk_devices_client_org`) -- so the strays are planted with those off,
    inside ONE transaction that is rolled back:

    * Y's device in X's group, and a deployment of X naming Y's device: only
      the CLIENT predicate keeps them out;
    * a device of ANOTHER ORGANISATION under X's client, in X's group and
      named by a deployment of X: only the ORG predicate keeps it out
      (lesson 2: the client predicate lets it through).
    """
    org = await api_env.create_org("d-dispatch")
    other = await api_env.create_org("d-dispatch-b")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_group = await _group(api_env, org, x)
    x_device = await _device(api_env, org, x, x_site, x_group)
    y_device = await _device(api_env, org, y, y_site)
    package = await _package(api_env, org)
    group_dep = await _seed_deployment(api_env, org, package, "group", x_group)
    own_dep = await _seed_deployment(api_env, org, package, "device", x_device)

    engine = _FakeJobEngine()

    async def dispatch(s: AsyncSession, deployment: UUID) -> set[UUID]:
        pending = await dispatch_deployment(
            s, engine, org_id=org, deployment_id=deployment, now=datetime.now(UTC),
            artifact_urls=_FakeArtifactSigner(),
        )
        return {device for _, device, _ in pending}

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await s.execute(text("ALTER TABLE devices DISABLE TRIGGER its_device_client_guard"))
        await s.execute(text("ALTER TABLE deployments DISABLE TRIGGER its_deployment_client"))
        await drop_client_keys(s, "devices", "fk_devices_client_org")
        await s.execute(
            update(Device).where(Device.id == y_device).values(device_group_id=x_group)
        )
        foreign = Device(org_id=other, client_id=x, site_id=x_site, hostname="b",
                         os_family="windows", device_group_id=x_group)
        s.add(foreign)
        await s.flush()
        strays = [
            Deployment(org_id=org, client_id=x, package_id=package, action="install",
                       target_type="device", target_id=target, state="pending")
            for target in (y_device, foreign.id)
        ]
        s.add_all(strays)
        await s.flush()
        reached = {
            "group": await dispatch(s, group_dep),
            "own": await dispatch(s, own_dep),
            "y": await dispatch(s, strays[0].id),
            "foreign": await dispatch(s, strays[1].id),
        }
        await s.rollback()
    assert reached == {"group": {x_device}, "own": {x_device}, "y": set(), "foreign": set()}


# --------------------------------------------------------------------------- #
# The authorizing change (Task 7 review carry-over)
# --------------------------------------------------------------------------- #


async def test_another_client_s_change_cannot_authorize_a_deployment(api_env: ApiEnv) -> None:
    """X's deployment named Y's approved change and was linked to it -- a
    false authorization of X's gate, visible to anyone holding both
    clients. Y's change answers exactly like one that does not exist, and
    nothing is written; X's own is linked."""
    org = await api_env.create_org("d-auth")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    await _modules(api_env, admin, y, "change")
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    x_change, y_change = (await _approved_change(api_env, admin, x),
                          await _approved_change(api_env, admin, y))

    before = await _written(api_env, org)
    missing = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", device, change_id=str(uuid4())), headers=admin
    )
    assert missing.status_code == 404, missing.text
    theirs = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", device, change_id=str(y_change)), headers=admin
    )
    assert (theirs.status_code, theirs.json()) == (404, missing.json()), theirs.text
    assert await _written(api_env, org) == before

    ours = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", device, change_id=str(x_change)), headers=admin
    )
    assert ours.status_code == 201, ours.text
    async with AsyncSession(api_env.admin_engine) as s:
        links = (await s.execute(select(ChangeLink.change_id, ChangeLink.client_id).where(
            ChangeLink.org_id == org))).all()
    assert [tuple(link) for link in links] == [(x_change, x)]


async def test_a_change_of_a_client_without_the_module_cannot_authorize(api_env: ApiEnv) -> None:
    """X's approved change, from before X switched change management off: a
    link on it is a write on a module X no longer has. The module's 404,
    before anything is written."""
    org = await api_env.create_org("d-auth-off")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _approved_change(api_env, admin, x)
    await _modules(api_env, admin, x)
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    before = await _written(api_env, org)
    r = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", device, change_id=str(change)), headers=admin
    )
    assert (r.status_code, r.json()) == (404, {"detail": MODULE_OFF}), r.text
    assert await _written(api_env, org) == before


async def test_the_create_takes_the_client_before_the_audit_chain(
    api_env: ApiEnv,
) -> None:
    """The create reads its target's client FOR SHARE as its FIRST lock --
    the new row's client (`client_for_new_rows`), which the authorizing
    change's module guard then takes again: an archive (or a module switch)
    holds the client and takes the audit chain's lock next, so a create that
    audited first and read the client after would deadlock with it (40P01).
    With the client held, the create waits BEFORE the audit lock; the holder
    takes that lock and commits; the create then goes through."""
    org = await api_env.create_org("d-lock")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _approved_change(api_env, admin, x)
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    body = _body(package, "device", device, change_id=str(change))

    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        create = asyncio.create_task(api_env.client.post(DEPLOYMENTS, json=body, headers=admin))
        if not await _wait_until_blocked(observer, me):
            create.cancel()
            pytest.fail("the create never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    r = await create
    assert r.status_code == 201, r.text


async def test_the_authorizing_change_holds_its_client_until_commit(api_env: ApiEnv) -> None:
    """`authorizing_change` itself, not only the create around it: the
    module guard it runs reads the client FOR SHARE and holds it, so the
    "module on" it answered cannot go stale under a switch-off before the
    link is written. Task 14's patch path will rely on exactly this."""
    org = await api_env.create_org("d-lock-unit")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _approved_change(api_env, admin, x)

    async def authorize() -> UUID:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            return (await authorizing_change(s, org, change, client_id=x)).id

    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(authorize())
        if not await _wait_until_blocked(observer, me):
            task.cancel()
            pytest.fail("authorizing_change never waited on the client lock")
        await holder.commit()
    assert await task == change


async def test_an_archived_client_s_target_takes_no_new_deployment(api_env: ApiEnv) -> None:
    """A deployment is a new row of its target's client, and an archived
    client takes none (`client_for_new_rows`): a 409 with its sentence. Nor
    does Unassigned (C10): a 422 with its sentence, as every other family's
    create answers. Nothing is written for either."""
    org = await api_env.create_org("d-archived")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    group = await _group(api_env, org, archived)
    admin = await _admin(api_env, org)
    package = await _package(api_env, org)
    before = await _written(api_env, org)
    r = await api_env.client.post(DEPLOYMENTS, json=_body(package, "group", group),
                                  headers=admin)
    assert (r.status_code, r.json()) == (409, {"detail": "that client is archived"}), r.text
    assert await _written(api_env, org) == before

    # "Sin asignar" can hold no device -- it can have no site (0230), and a
    # device's site must be its client's (0231) -- so its only possible
    # target is a group that can never have members. Refused like any other
    # family's row under it.
    async with AsyncSession(api_env.admin_engine) as s:
        unassigned = (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()
    empty = await _group(api_env, org, unassigned)
    before = await _written(api_env, org)
    r = await api_env.client.post(DEPLOYMENTS, json=_body(package, "group", empty),
                                  headers=admin)
    assert (r.status_code, r.json()) == (
        422, {"detail": "a deployment cannot be filed under Unassigned"}
    ), r.text
    assert await _written(api_env, org) == before


async def test_every_deployment_output_names_its_client(api_env: ApiEnv) -> None:
    """Plan: every output of a client's row carries `client_id` -- the list
    and the create (`DeploymentOut`), the detail, a device's history and the
    fleet report."""
    org = await api_env.create_org("d-outputs")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    created = await api_env.client.post(DEPLOYMENTS, json=_body(package, "device", device),
                                        headers=admin)
    assert created.status_code == 201, created.text
    deployment = created.json()["id"]
    run = await api_env.client.post(f"{DEPLOYMENTS}/{deployment}/execute", headers=admin)
    assert run.status_code == 200, run.text

    detail = await api_env.client.get(f"{DEPLOYMENTS}/{deployment}", headers=admin)
    history = await api_env.client.get(f"/api/v1/deploy/devices/{device}/deployments",
                                       headers=admin)
    fleet = await api_env.client.get("/api/v1/deploy/report", headers=admin)
    assert detail.json()["client_id"] == str(x), detail.text
    assert [h["client_id"] for h in history.json()] == [str(x)], history.text
    assert [d["client_id"] for d in fleet.json()["deployments"]] == [str(x)], fleet.text


# --------------------------------------------------------------------------- #
# Gates and the automatic change are the deployment's client's
# --------------------------------------------------------------------------- #


async def test_only_the_deployment_s_client_s_change_authorizes_its_dispatch(
    api_env: ApiEnv,
) -> None:
    """0237 filed every existing change, and every link to one, under the
    internal client, so a deployment of X made before 0239 can carry a link
    to an approved change that is not X's. At dispatch that link must not
    pass X's gate; X's own approved change still does. Planted under scope
    all, which is where such a row is reachable."""
    org = await api_env.create_org("d-authorized")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    package = await _package(api_env, org)
    deployment = await _seed_deployment(
        api_env, org, package, "device", await _device(api_env, org, x, x_site)
    )
    theirs = await _approved_change(api_env, admin, org)  # the internal client's
    ours = await _approved_change(api_env, admin, x)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                         applies_to_deployments=True))
        await s.commit()

    async def plant(change: UUID) -> None:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            linked_by = (
                await s.execute(
                    select(ChangeRequest.created_by).where(ChangeRequest.id == change)
                )
            ).scalar_one()
            s.add(ChangeLink(org_id=org, change_id=change, action_type="deployment",
                             action_id=deployment, linked_by=linked_by))

    await plant(theirs)
    refused = await api_env.client.post(f"{DEPLOYMENTS}/{deployment}/execute", headers=admin)
    assert refused.status_code == 409, refused.text
    await plant(ours)
    ran = await api_env.client.post(f"{DEPLOYMENTS}/{deployment}/execute", headers=admin)
    assert ran.status_code == 200, ran.text
    assert len(ran.json()["job_ids"]) == 1


async def test_the_gate_is_the_deployment_s_client_s(api_env: ApiEnv) -> None:
    """`deployment_gate` asks the client it is given -- the deployment's own
    column, which the trigger keeps equal to the target's -- and no longer
    derives one from the target. So the contract is pinned by asking for a
    client other than the target's: X uses change management and meets the
    global gate, Y does not."""
    org = await api_env.create_org("d-gate")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    x_group = await _group(api_env, org, x)
    x_device = await _device(api_env, org, x, x_site, x_group)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                         applies_to_deployments=True))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for target_type, target in (("device", x_device), ("group", x_group)):
            assert await deployment_gate(s, org, target_type, target, client_id=x) is not None
            assert await deployment_gate(s, org, target_type, target, client_id=y) is None


async def test_a_client_without_change_management_meets_no_deployment_gate(
    api_env: ApiEnv,
) -> None:
    """D4 through both doors: under a global deployment gate, Y (no change
    management) is created and executed freely, X (with it) is refused at
    both. The internal client uses change management and meets the gate, so
    a door that asked the internal client's gates instead of the
    deployment's would refuse Y too."""
    org = await api_env.create_org("d-d4")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    package = await _package(api_env, org)
    x_dep = await _seed_deployment(
        api_env, org, package, "device", await _device(api_env, org, x, x_site)
    )
    y_device = await _device(api_env, org, y, y_site)
    y_dep = await _seed_deployment(api_env, org, package, "device", y_device)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                         applies_to_deployments=True))
        await s.commit()

    created = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", y_device), headers=admin
    )
    assert created.status_code == 201, created.text
    ran = await api_env.client.post(f"{DEPLOYMENTS}/{y_dep}/execute", headers=admin)
    assert ran.status_code == 200, ran.text
    assert len(ran.json()["job_ids"]) == 1

    async with AsyncSession(api_env.admin_engine) as s:
        x_device = (
            await s.execute(select(Deployment.target_id).where(Deployment.id == x_dep))
        ).scalar_one()
    refused = await api_env.client.post(
        DEPLOYMENTS, json=_body(package, "device", x_device), headers=admin
    )
    assert refused.status_code == 409, refused.text
    held = await api_env.client.post(f"{DEPLOYMENTS}/{x_dep}/execute", headers=admin)
    assert held.status_code == 409, held.text


async def test_the_automatic_change_is_the_deployment_s_client_s(api_env: ApiEnv) -> None:
    """A deployment made without a change gets an automatic one of ITS
    client -- not the internal client's -- and a client without change
    management gets none at all (D4), though the internal client has it."""
    org = await api_env.create_org("d-auto")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    package = await _package(api_env, org)
    for client, site in ((x, x_site), (y, y_site)):
        r = await api_env.client.post(
            DEPLOYMENTS,
            json=_body(package, "device", await _device(api_env, org, client, site)),
            headers=admin,
        )
        assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        autos = (await s.execute(select(ChangeRequest.client_id).where(
            ChangeRequest.org_id == org, ChangeRequest.origin == "auto"))).scalars().all()
    assert autos == [x]


async def test_a_deployment_whose_target_is_gone_is_still_its_client_s(api_env: ApiEnv) -> None:
    """The client is the deployment's column, not a fresh read of the
    target: a deployment whose group was removed is still X's -- it links to
    X's change and not Y's, and it still meets X's every-device gate at
    dispatch (it reached no gate before, having no target to ask)."""
    org = await api_env.create_org("d-gone")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    await _modules(api_env, admin, y, "change")
    group = await _group(api_env, org, x)
    package = await _package(api_env, org)
    deployment = await _seed_deployment(api_env, org, package, "group", group)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("DELETE FROM device_groups WHERE id = :g"), {"g": group})
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                         applies_to_deployments=True))
        await s.commit()
    x_change, y_change = (await _approved_change(api_env, admin, x),
                          await _approved_change(api_env, admin, y))

    async def link(change: UUID) -> Any:
        return await api_env.client.post(
            f"/api/v1/changes/{change}/links",
            json={"action_type": "deployment", "action_id": str(deployment)},
            headers=admin,
        )

    assert (await link(y_change)).status_code == 400
    assert (await link(x_change)).status_code == 201

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(ChangeRequest).where(ChangeRequest.id == x_change).values(state="cancelled")
        )
        await s.commit()
    r = await api_env.client.post(f"{DEPLOYMENTS}/{deployment}/execute", headers=admin)
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# Audit: every deployment event carries the deployment's client
# --------------------------------------------------------------------------- #


async def test_every_deployment_audit_event_carries_its_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("d-audit")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    device = await _device(api_env, org, x, x_site)
    package = await _package(api_env, org)
    body = _body(package, "device", device)

    # Created with an automatic change (no gate yet), dispatched, reported.
    first = await api_env.client.post(DEPLOYMENTS, json=body, headers=admin)
    assert first.status_code == 201, first.text
    run = await api_env.client.post(f"{DEPLOYMENTS}/{first.json()['id']}/execute", headers=admin)
    assert run.status_code == 200, run.text
    [job_id] = run.json()["job_ids"]
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        job = (await s.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one()
        await ingest_deploy_result(s, job, AppDeployResult.model_validate(
            {"outcomes": [{"identifier": "7zip.7zip", "state": "installed", "exit_code": 0}]}
        ))

    # Gated: refused at creation, and the first one refused at execution.
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                         applies_to_deployments=True))
        await s.commit()
    refused = await api_env.client.post(DEPLOYMENTS, json=body, headers=admin)
    assert refused.status_code == 409, refused.text
    again = await api_env.client.post(f"{DEPLOYMENTS}/{first.json()['id']}/execute",
                                      headers=admin)
    assert again.status_code == 409, again.text

    # Authorized by X's change.
    change = await _approved_change(api_env, admin, x)
    linked = await api_env.client.post(
        DEPLOYMENTS, json={**body, "change_id": str(change)}, headers=admin
    )
    assert linked.status_code == 201, linked.text

    actions = {
        "deploy.deployment_created", "change.auto_recorded", "change.linked",
        "deploy.dispatched", "deploy.result", "change.gate_refused",
    }
    async with AsyncSession(api_env.admin_engine) as s:
        events = (await s.execute(select(AuditEvent.action, AuditEvent.target_type,
                                         AuditEvent.client_id).where(
            AuditEvent.org_id == org, AuditEvent.action.in_(actions)))).all()
    assert {e.action for e in events} == actions
    assert {(e.action, e.target_type) for e in events if e.action == "change.gate_refused"} == {
        ("change.gate_refused", "device"), ("change.gate_refused", "deployment")
    }
    assert {e.client_id for e in events} == {x}, events


# --------------------------------------------------------------------------- #
# A write the trigger refuses is a 409, never a 500
# --------------------------------------------------------------------------- #


async def test_a_target_the_trigger_cannot_resolve_is_a_409(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request reads the target, then inserts; the trigger reads it
    again. Should the two disagree -- a target gone in between -- the
    database refuses the row, and the person gets a 409 with a sentence,
    not a 500. Simulated by a read that answers for a device that does not
    exist."""
    org = await api_env.create_org("d-race")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    package = await _package(api_env, org)

    async def stale(*_: Any) -> tuple[UUID, str]:
        return x, "approved"

    monkeypatch.setattr(deploy_api, "_deployment_target", stale)
    before = await _written(api_env, org)
    r = await api_env.client.post(DEPLOYMENTS, json=_body(package, "device", uuid4()),
                                  headers=admin)
    assert r.status_code == 409, r.text
    assert r.json() == {
        "detail": "the deployment target changed while it was being created; try again"
    }
    assert await _written(api_env, org) == before

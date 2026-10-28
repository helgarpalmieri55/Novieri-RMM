"""Optional modules per client (spec §1.4, §3.2; plan 2026-10-26 Task 7 and
D4): a write in budget, BCP or change management for a client without the
module is a 404; a client without change management meets no change gate and
gets no automatic change. Change requests and BCP plans belong to a client,
and what they point at is of that client."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.router_inspect import flatten
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeExternalRef,
    ChangeGate,
    ChangeRequest,
    Client,
    Deployment,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    DocArticle,
    Job,
    OsImage,
    Package,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchRun,
    ProvisionProfile,
    ProvisionRun,
    Ticket,
    User,
)
from app.modules.change_mgmt import gates as gates_module
from app.modules.change_mgmt.gates import (
    deployment_gate,
    gates_for,
    patch_approval_gate,
    record_auto_change,
)
from app.modules.patch import apply as apply_module
from app.modules.patch.apply import dispatch_apply
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv
from tests.test_patch_reboot import _FakeJobEngine

MODULE_OFF = "this client does not use that module"


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _modules(env: ApiEnv, admin: dict[str, str], client: UUID, *modules: str) -> None:
    r = await env.client.patch(
        f"/api/v1/clients/{client}", json={"enabled_modules": list(modules)}, headers=admin
    )
    assert r.status_code == 200, r.text


def _change_body(client: UUID, **extra: Any) -> dict[str, Any]:
    return {"type": "normal", "title": "Cambiar firewall", "description": "d",
            "risk": "low", "affected": [], "rollback_plan": "r", "client_id": str(client),
            **extra}


def _plan_body(client: UUID, **extra: Any) -> dict[str, Any]:
    return {"name": "Caída del ERP", "client_id": str(client), "scope": [],
            "rto_minutes": 60, **extra}


async def _new_change(env: ApiEnv, headers: dict[str, str], client: UUID) -> str:
    r = await env.client.post("/api/v1/changes", json=_change_body(client), headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _new_plan(env: ApiEnv, headers: dict[str, str], client: UUID, **extra: Any) -> str:
    r = await env.client.post("/api/v1/bcp/plans", json=_plan_body(client, **extra),
                              headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def _user_id(env: ApiEnv, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _seed(env: ApiEnv, *rows: Any) -> list[UUID]:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()
    return [row.id for row in rows]


async def _device(
    env: ApiEnv, org: UUID, client: UUID, site: UUID, group: UUID | None = None
) -> UUID:
    [device] = await _seed(env, Device(
        org_id=org, client_id=client, site_id=site, hostname="h", os_family="windows",
        device_group_id=group,
    ))
    return device


async def _group(env: ApiEnv, org: UUID, client: UUID, name: str = "Servidores") -> UUID:
    [group] = await _seed(env, DeviceGroup(org_id=org, client_id=client, name=name))
    return group


# --------------------------------------------------------------------------- #
# The brief's tests (bodies are the real schemas: `change_mgmt/schema.py`,
# `bcp/schema.py`)
# --------------------------------------------------------------------------- #


async def test_a_change_for_a_client_without_the_module_is_a_404(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-change")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    body = _change_body(x)
    off = await api_env.client.post("/api/v1/changes", json=body, headers=admin)
    assert off.status_code == 404, off.text
    assert off.json()["detail"] == MODULE_OFF
    await _modules(api_env, admin, x, "change")
    on = await api_env.client.post("/api/v1/changes", json=body, headers=admin)
    assert on.status_code == 201, on.text
    assert on.json()["client_id"] == str(x)


async def test_the_module_s_404_speaks_the_person_s_language(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-es")
    x, _ = await client_with_site(api_env, org)
    admin = {**await _admin(api_env, org), "Accept-Language": "es"}
    r = await api_env.client.post("/api/v1/changes", json=_change_body(x), headers=admin)
    assert (r.status_code, r.json()["detail"]) == (404, "este cliente no usa ese módulo")


async def test_a_bcp_plan_for_a_client_without_the_module_is_a_404(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-bcp")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    body = _plan_body(x)
    off = await api_env.client.post("/api/v1/bcp/plans", json=body, headers=admin)
    assert off.status_code == 404, off.text
    assert off.json()["detail"] == MODULE_OFF
    await _modules(api_env, admin, x, "bcp")
    created = await api_env.client.post("/api/v1/bcp/plans", json=body, headers=admin)
    assert created.status_code == 201, created.text
    assert created.json()["client_id"] == str(x)


async def test_the_internal_client_has_every_optional_module(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-internal")
    admin = await _admin(api_env, org)
    r = await api_env.client.post("/api/v1/changes", json=_change_body(org), headers=admin)
    assert r.status_code == 201, r.text
    p = await api_env.client.post("/api/v1/bcp/plans", json=_plan_body(org), headers=admin)
    assert p.status_code == 201, p.text


async def test_turning_a_module_off_refuses_every_write_on_its_rows(api_env: ApiEnv) -> None:
    """Every write route of the change and BCP routers, on rows that exist
    from before the module was turned off. The route list is read from the
    app, so a new write route without a case here fails. Each case was run
    once with the module ON and answers something other than 404 there."""
    org = await api_env.create_org("m-off")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change", "bcp")
    change = await _new_change(api_env, admin, x)
    [approval, other_approval] = await _global_approvals(api_env, org, 2)
    link = await api_env.client.post(
        f"/api/v1/changes/{change}/links",
        json={"action_type": "patch_approval", "action_id": str(approval)},
        headers=admin,
    )
    assert link.status_code == 201, link.text
    plan = await _new_plan(api_env, admin, x)
    test = (await api_env.client.post(
        f"/api/v1/bcp/plans/{plan}/tests", json={"type": "tabletop"}, headers=admin
    )).json()["id"]
    finding = (await api_env.client.post(
        f"/api/v1/bcp/tests/{test}/findings", json={"description": "s", "severity": "low"},
        headers=admin,
    )).json()["id"]
    await _modules(api_env, admin, x)

    cases: dict[tuple[str, str], tuple[str, dict[str, Any] | None]] = {
        ("POST", "/api/v1/changes"): ("/api/v1/changes", _change_body(x)),
        ("PATCH", "/api/v1/changes/{change_id}"): (f"/api/v1/changes/{change}", {"title": "u"}),
        ("POST", "/api/v1/changes/{change_id}/submit"): (f"/api/v1/changes/{change}/submit", None),
        ("POST", "/api/v1/changes/{change_id}/approve"): (f"/api/v1/changes/{change}/approve", {}),
        ("POST", "/api/v1/changes/{change_id}/reject"): (
            f"/api/v1/changes/{change}/reject", {"comment": "r"}),
        ("POST", "/api/v1/changes/{change_id}/transition"): (
            f"/api/v1/changes/{change}/transition", {"action": "cancel"}),
        ("POST", "/api/v1/changes/{change_id}/links"): (
            f"/api/v1/changes/{change}/links",
            {"action_type": "patch_approval", "action_id": str(other_approval)}),
        ("DELETE", "/api/v1/changes/{change_id}/links/{link_id}"): (
            f"/api/v1/changes/{change}/links/{link.json()['id']}", None),
        ("POST", "/api/v1/bcp/plans"): ("/api/v1/bcp/plans", _plan_body(x)),
        ("PATCH", "/api/v1/bcp/plans/{plan_id}"): (f"/api/v1/bcp/plans/{plan}", {"name": "m"}),
        ("POST", "/api/v1/bcp/plans/{plan_id}/review"): (f"/api/v1/bcp/plans/{plan}/review", {}),
        ("POST", "/api/v1/bcp/plans/{plan_id}/tests"): (
            f"/api/v1/bcp/plans/{plan}/tests", {"type": "tabletop"}),
        ("PATCH", "/api/v1/bcp/tests/{test_id}"): (
            f"/api/v1/bcp/tests/{test}", {"outcome": "pass", "notes": "n"}),
        ("PUT", "/api/v1/bcp/tests/{test_id}/steps"): (
            f"/api/v1/bcp/tests/{test}/steps", {"ordinal": 1, "description": "d"}),
        ("POST", "/api/v1/bcp/tests/{test_id}/findings"): (
            f"/api/v1/bcp/tests/{test}/findings", {"description": "s", "severity": "low"}),
        ("PATCH", "/api/v1/bcp/findings/{finding_id}"): (
            f"/api/v1/bcp/findings/{finding}", {"description": "t"}),
    }
    # Gates are not per-module writes: a gate is configuration on a group
    # (or global), handled by D4 at evaluation time.
    exempt = {("PUT", "/api/v1/changes/gates"), ("DELETE", "/api/v1/changes/gates/{gate_id}")}
    routes = {
        (method, path)
        for path, route in flatten(api_env.app.router)
        for method in getattr(route, "methods", None) or set()
        if method in {"POST", "PUT", "PATCH", "DELETE"}
        and (path.startswith("/api/v1/changes") or path.startswith("/api/v1/bcp"))
    }
    assert routes - exempt == set(cases), sorted((routes - exempt) ^ set(cases))
    for (method, _), (url, body) in cases.items():
        r = await api_env.client.request(method, url, json=body, headers=admin)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        assert r.json()["detail"] == MODULE_OFF, (method, url, r.text)
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(
                select(ChangeRequest.title, ChangeRequest.state).where(
                    ChangeRequest.id == UUID(change)
                )
            )
        ).one() == ("Cambiar firewall", "draft"), "a refused write changed the change"


async def test_a_client_without_change_management_meets_no_gate(api_env: ApiEnv) -> None:
    """D4: a global "every device" gate does not stop a client that does not
    use change management -- it could never create the change it would need."""
    org = await api_env.create_org("m-gate")
    x, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=True,
                         applies_to_deployments=True))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        for_x = await gates_for(s, org, "deployments", client_id=x)
        for_internal = await gates_for(s, org, "deployments", client_id=org)
    assert not for_x.any
    assert for_internal.everywhere


async def test_a_client_gate_without_a_group_reaches_only_that_client(api_env: ApiEnv) -> None:
    """research-5 §D: `everywhere` was any NULL group -- a client's own
    group-less gate would have gated every client."""
    org = await api_env.create_org("m-gate-client")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _modules(api_env, admin, client, "change")
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(ChangeGate(org_id=org, client_id=x, device_group_id=None,
                         applies_to_patches=True, applies_to_deployments=True))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert (await gates_for(s, org, "deployments", client_id=x)).everywhere
        assert not (await gates_for(s, org, "deployments", client_id=y)).any


# --------------------------------------------------------------------------- #
# Creating, and who is answered what
# --------------------------------------------------------------------------- #


async def test_a_change_or_plan_names_a_client_it_may_file_under(api_env: ApiEnv) -> None:
    """The create rules every new row's client follows (`client_for_new_rows`):
    none named 422, invisible or another organisation's 404, archived 409,
    "Sin asignar" 422 (C10)."""
    org = await api_env.create_org("m-create")
    other = await api_env.create_org("m-create-other")
    admin = await _admin(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    for path, body in (("/api/v1/changes", _change_body), ("/api/v1/bcp/plans", _plan_body)):
        missing = body(org)
        del missing["client_id"]
        r = await api_env.client.post(path, json=missing, headers=admin)
        assert r.status_code == 422 and "client_id" in r.text, (path, r.text)
        for client, expected in ((other, 404), (uuid4(), 404), (archived, 409),
                                 (unassigned, 422)):
            r = await api_env.client.post(path, json=body(client), headers=admin)
            assert r.status_code == expected, (path, client, r.status_code, r.text)
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(ChangeRequest.id).where(ChangeRequest.org_id == org))
        ).first() is None


async def test_rbac_answers_before_the_module_does(api_env: ApiEnv) -> None:
    """A person without the permission is told so (403) whether or not the
    client uses the module: the module lookup runs after RBAC, in the
    service."""
    org = await api_env.create_org("m-rbac")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change", "bcp")
    change = await _new_change(api_env, admin, x)
    plan = await _new_plan(api_env, admin, x)
    await _modules(api_env, admin, x)
    reader = await api_env.headers(org, await api_env.create_user(org, "read-only"))
    for method, url, body in (
        ("POST", "/api/v1/changes", _change_body(x)),
        ("PATCH", f"/api/v1/changes/{change}", {"title": "t"}),
        ("POST", "/api/v1/bcp/plans", _plan_body(x)),
        ("PATCH", f"/api/v1/bcp/plans/{plan}", {"name": "n"}),
    ):
        r = await api_env.client.request(method, url, json=body, headers=reader)
        assert r.status_code == 403, (method, url, r.status_code, r.text)


async def test_a_technician_of_one_client_submits_that_client_s_change(api_env: ApiEnv) -> None:
    """Task 2's 500: submit filed the change's ticket under the INTERNAL client,
    which a technician of X alone cannot see. The ticket now goes to the
    change's client; another client's change is a 404, and so is X's own when
    X does not use change management. Never a 500."""
    org = await api_env.create_org("m-narrow")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    x_tech = await everything_on(api_env, org, x)

    mine = await _new_change(api_env, x_tech, x)
    submitted = await api_env.client.post(f"/api/v1/changes/{mine}/submit", headers=x_tech)
    assert submitted.status_code == 200, submitted.text
    async with AsyncSession(api_env.admin_engine) as s:
        ref = (
            await s.execute(
                select(ChangeExternalRef.external_id).where(
                    ChangeExternalRef.change_id == UUID(mine)
                )
            )
        ).scalar_one()
        ticket_client = (
            await s.execute(select(Ticket.client_id).where(Ticket.id == UUID(ref)))
        ).scalar_one()
    assert ticket_client == x

    internal = await _new_change(api_env, admin, org)
    r = await api_env.client.post(f"/api/v1/changes/{internal}/submit", headers=x_tech)
    assert r.status_code == 404, r.text
    r = await api_env.client.post("/api/v1/changes", json=_change_body(org), headers=x_tech)
    assert r.status_code == 404, r.text

    another = await _new_change(api_env, x_tech, x)
    await _modules(api_env, admin, x)
    r = await api_env.client.post(f"/api/v1/changes/{another}/submit", headers=x_tech)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == MODULE_OFF


async def test_submitting_a_change_of_an_archived_client_is_a_409(api_env: ApiEnv) -> None:
    """Submit files a ticket under the change's client, and an archived
    client takes no new rows (`client_for_new_rows`): a 409 with the
    sentence, never the ticket core's error escaping as a 500."""
    org = await api_env.create_org("m-archived")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _new_change(api_env, admin, x)
    archive = await api_env.client.post(f"/api/v1/clients/{x}/archive", headers=admin)
    assert archive.status_code == 200, archive.text
    r = await api_env.client.post(f"/api/v1/changes/{change}/submit", headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "that client is archived"


async def test_change_submit_takes_the_client_before_the_audit_chain(api_env: ApiEnv) -> None:
    """Task 2's concern 1. Submit audited `change.submitted` -- the org's
    audit advisory lock -- before `create_ticket` read the client FOR SHARE;
    an archive takes the client first and the audit lock next, so the two
    could deadlock (40P01). The module guard now reads the client FOR SHARE
    before anything is written: with an archive holding the client, submit
    waits BEFORE the audit lock, the archive takes that lock and commits, and
    submit then goes through."""
    org = await api_env.create_org("m-lock")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _new_change(api_env, admin, x)

    async with AsyncSession(api_env.admin_engine) as archive, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await archive.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await archive.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        submit = asyncio.create_task(
            api_env.client.post(f"/api/v1/changes/{change}/submit", headers=admin)
        )
        for _ in range(400):
            await asyncio.sleep(0.025)
            waiting = (
                await observer.execute(
                    # `pg_locks`, not `pg_stat_activity`: another role's
                    # wait events are hidden from a non-superuser.
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted AND pid <> :me"),
                    {"me": me},
                )
            ).scalar_one()
            await observer.rollback()
            if waiting:
                break
        else:
            submit.cancel()
            pytest.fail("submit never waited on the archive's client lock")
        # The archive's next lock. Held by submit, this is the deadlock.
        await archive.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await archive.commit()
    r = await submit
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# What a change and a plan point at is of their client
# --------------------------------------------------------------------------- #


async def _global_approvals(
    env: ApiEnv, org: UUID, n: int, client: UUID | None = None
) -> list[UUID]:
    """`n` patch approvals of the ring "broad": global, or of `client`."""
    rows: list[Any] = []
    for i in range(n):
        patch = Patch(id=uuid4(), org_id=org, os_family="windows", source="windows_update",
                      identifier=f"KB{i}-{uuid4().hex[:4]}", title="t", severity="critical")
        rows += [patch, PatchApproval(org_id=org, client_id=client, patch_id=patch.id,
                                      ring_name="broad")]
    ids = await _seed(env, *rows)
    return ids[1::2]


async def _patch_run(env: ApiEnv, org: UUID, device: UUID) -> UUID:
    now = datetime.now(UTC)
    job = Job(id=uuid4(), org_id=org, type="patch.apply", target_type="device", target_id=device,
              params={}, nonce=uuid4().hex, issued_at=now, expires_at=now, signature="s")
    [_, run] = await _seed(env, job, PatchRun(org_id=org, job_id=job.id, device_id=device))
    return run


async def _deployment(env: ApiEnv, org: UUID, target_type: str, target: UUID) -> UUID:
    package = Package(id=uuid4(), org_id=org, name=f"p-{uuid4().hex[:4]}", version="1",
                      os_family="windows", installer_type="msi")
    [_, deployment] = await _seed(env, package, Deployment(
        org_id=org, package_id=package.id, action="install", target_type=target_type,
        target_id=target, state="pending",
    ))
    return deployment


async def _provision_run(env: ApiEnv, org: UUID, device: UUID | None) -> UUID:
    image = OsImage(id=uuid4(), org_id=org, os_family="windows", name="w", kind="image")
    profile = ProvisionProfile(id=uuid4(), org_id=org, name=f"p-{uuid4().hex[:4]}",
                               image_id=image.id)
    [_, _, run] = await _seed(env, image, profile, ProvisionRun(
        org_id=org, profile_id=profile.id, device_id=device, state="pending",
        actor_type="system",
    ))
    return run


async def test_a_link_target_of_another_client_is_refused_like_a_missing_one(
    api_env: ApiEnv,
) -> None:
    """research-5 §E6: the resolvers checked only the organisation. A target
    of another client gets the answer a missing one gets, word for word; an O
    target with no client (a global patch approval, a provisioning run with
    no device yet) is every client's."""
    org = await api_env.create_org("m-links")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    change = await _new_change(api_env, admin, x)
    x_device, y_device = (await _device(api_env, org, x, x_site),
                          await _device(api_env, org, y, y_site))
    x_group, y_group = await _group(api_env, org, x), await _group(api_env, org, y)
    [y_approval] = await _global_approvals(api_env, org, 1, client=y)
    [x_approval, global_approval] = (
        await _global_approvals(api_env, org, 1, client=x)
        + await _global_approvals(api_env, org, 1)
    )

    async def link(action_type: str, action_id: UUID) -> Any:
        return await api_env.client.post(
            f"/api/v1/changes/{change}/links",
            json={"action_type": action_type, "action_id": str(action_id)},
            headers=admin,
        )

    missing = await link("patch_run", uuid4())
    assert missing.status_code == 400
    for action_type, theirs in (
        ("patch_run", await _patch_run(api_env, org, y_device)),
        ("deployment", await _deployment(api_env, org, "device", y_device)),
        ("deployment", await _deployment(api_env, org, "group", y_group)),
        ("provision_run", await _provision_run(api_env, org, y_device)),
        ("patch_approval", y_approval),
    ):
        r = await link(action_type, theirs)
        assert (r.status_code, r.json()) == (400, missing.json()), (action_type, r.text)
    for action_type, ours in (
        ("patch_run", await _patch_run(api_env, org, x_device)),
        ("deployment", await _deployment(api_env, org, "device", x_device)),
        ("deployment", await _deployment(api_env, org, "group", x_group)),
        ("provision_run", await _provision_run(api_env, org, x_device)),
        ("provision_run", await _provision_run(api_env, org, None)),
        ("patch_approval", x_approval),
        ("patch_approval", global_approval),
    ):
        r = await link(action_type, ours)
        assert r.status_code == 201, (action_type, r.text)


async def test_a_plan_s_scope_procedures_and_changes_are_its_client_s(api_env: ApiEnv) -> None:
    """research-5 §E5: `_validate_scope`, `_validate_procedure_refs` and a
    finding's `change_ref` checked only the organisation. Another client's
    device, group, article or change is a 422; a global article is every
    client's; a missing one stays the 400 it was."""
    org = await api_env.create_org("m-bcp-refs")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "bcp", "change")
    x_device, y_device = (await _device(api_env, org, x, x_site),
                          await _device(api_env, org, y, y_site))
    x_group, y_group = await _group(api_env, org, x), await _group(api_env, org, y)
    x_article, y_article, global_article = await _seed(
        api_env,
        DocArticle(org_id=org, client_id=x, title="x"),
        DocArticle(org_id=org, client_id=y, title="y"),
        DocArticle(org_id=org, client_id=None, title="g"),
    )
    actor = await _user_id(api_env, await api_env.create_user(org))
    x_change, y_change = await _seed(
        api_env,
        *(ChangeRequest(org_id=org, client_id=c, type="normal", title="t", description="d",
                        risk="low", affected=[], state="draft", created_by=actor)
          for c in (x, y)),
    )

    for extra in (
        {"scope": [{"kind": "device", "id": str(y_device)}]},
        {"scope": [{"kind": "group", "id": str(y_group)}]},
        {"procedure_refs": [str(y_article)]},
    ):
        r = await api_env.client.post("/api/v1/bcp/plans", json=_plan_body(x, **extra),
                                      headers=admin)
        assert r.status_code == 422, (extra, r.text)
    missing = await api_env.client.post(
        "/api/v1/bcp/plans",
        json=_plan_body(x, scope=[{"kind": "device", "id": str(uuid4())}]),
        headers=admin,
    )
    assert missing.status_code == 400, missing.text

    plan = await _new_plan(
        api_env, admin, x,
        scope=[{"kind": "device", "id": str(x_device)}, {"kind": "group", "id": str(x_group)}],
        procedure_refs=[str(x_article), str(global_article)],
    )
    for patch in ({"scope": [{"kind": "device", "id": str(y_device)}]},
                  {"procedure_refs": [str(y_article)]}):
        r = await api_env.client.patch(f"/api/v1/bcp/plans/{plan}", json=patch, headers=admin)
        assert r.status_code == 422, (patch, r.text)

    test = (await api_env.client.post(
        f"/api/v1/bcp/plans/{plan}/tests", json={"type": "tabletop"}, headers=admin
    )).json()["id"]
    finding = (await api_env.client.post(
        f"/api/v1/bcp/tests/{test}/findings", json={"description": "s", "severity": "low"},
        headers=admin,
    )).json()["id"]
    for change_ref, expected in ((y_change, 422), (uuid4(), 400), (x_change, 200)):
        r = await api_env.client.patch(
            f"/api/v1/bcp/findings/{finding}", json={"change_ref": str(change_ref)},
            headers=admin,
        )
        assert r.status_code == expected, (change_ref, r.text)


# --------------------------------------------------------------------------- #
# Audit carries the record's client
# --------------------------------------------------------------------------- #


async def test_every_change_and_bcp_audit_event_carries_its_client(api_env: ApiEnv) -> None:
    """Every action of the two modules whose subject is a change, a plan, a
    test or a finding: each one this test makes, and nothing else, is filed
    under X. The period report is org-wide and carries none."""
    org = await api_env.create_org("m-audit")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    approver = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change", "bcp")

    def ok(r: Any, code: int = 200) -> Any:
        assert r.status_code == code, r.text
        return r.json()

    post, patch, put, delete, get = (api_env.client.post, api_env.client.patch,
                                     api_env.client.put, api_env.client.delete,
                                     api_env.client.get)
    change = await _new_change(api_env, admin, x)
    ok(await patch(f"/api/v1/changes/{change}", json={"title": "u"}, headers=admin))
    ok(await post(f"/api/v1/changes/{change}/submit", headers=admin))
    ok(await post(f"/api/v1/changes/{change}/approve", json={}, headers=approver))
    ok(await post(f"/api/v1/changes/{change}/transition", json={"action": "begin"},
                  headers=admin))
    rejected = await _new_change(api_env, admin, x)
    ok(await post(f"/api/v1/changes/{rejected}/submit", headers=admin))
    ok(await post(f"/api/v1/changes/{rejected}/reject", json={}, headers=approver))
    [approval] = await _global_approvals(api_env, org, 1)
    link = ok(await post(f"/api/v1/changes/{change}/links",
                         json={"action_type": "patch_approval", "action_id": str(approval)},
                         headers=admin), 201)
    gone = await delete(f"/api/v1/changes/{change}/links/{link['id']}", headers=admin)
    assert gone.status_code == 204, gone.text
    ok(await get(f"/api/v1/changes/{change}/evidence", headers=admin))
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        auto = await record_auto_change(
            s, org_id=org, client_id=x, actor_id=await _user_id_of(api_env, admin),
            title="auto", description="d", affected=[], action_type="patch_approval",
            action_ids=[approval],
        )
        assert auto is not None

    plan = await _new_plan(api_env, admin, x)
    ok(await patch(f"/api/v1/bcp/plans/{plan}", json={"name": "m"}, headers=admin))
    ok(await post(f"/api/v1/bcp/plans/{plan}/review", json={}, headers=admin))
    test = ok(await post(f"/api/v1/bcp/plans/{plan}/tests", json={"type": "tabletop"},
                         headers=admin), 201)["id"]
    ok(await patch(f"/api/v1/bcp/tests/{test}", json={"outcome": "pass"}, headers=admin))
    ok(await put(f"/api/v1/bcp/tests/{test}/steps", json={"ordinal": 1, "description": "d"},
                 headers=admin))
    finding = ok(await post(f"/api/v1/bcp/tests/{test}/findings",
                            json={"description": "s", "severity": "low"}, headers=admin),
                 201)["id"]
    ok(await patch(f"/api/v1/bcp/findings/{finding}", json={"change_ref": change},
                   headers=admin))
    ok(await get(f"/api/v1/bcp/tests/{test}/report", headers=admin))
    ok(await get("/api/v1/bcp/report/period",
                 params={"from": "2020-01-01T00:00:00Z", "to": "2100-01-01T00:00:00Z"},
                 headers=admin))

    expected = {
        "change.created", "change.updated", "change.submitted", "change.approved",
        "change.transitioned", "change.rejected", "change.linked", "change.unlinked",
        "change.evidence_exported", "change.auto_recorded",
        "bcp.plan_created", "bcp.plan_updated", "bcp.plan_reviewed", "bcp.test_created",
        "bcp.test_recorded", "bcp.test_step_recorded", "bcp.finding_created",
        "bcp.finding_updated", "bcp.report_exported",
    }
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.target_type, AuditEvent.client_id).where(
                    AuditEvent.org_id == org,
                    AuditEvent.action.like("change.%") | AuditEvent.action.like("bcp.%"),
                )
            )
        ).all()
        ticket = (
            await s.execute(
                select(AuditEvent.client_id).where(
                    AuditEvent.org_id == org, AuditEvent.action == "ticket.created"
                )
            )
        ).scalars().all()
    assert {action for action, _, _ in rows} == expected, sorted(
        {action for action, _, _ in rows} ^ expected
    )
    period = [(a, c) for a, t, c in rows if t == "bcp_report"]
    assert period == [("bcp.report_exported", None)]
    stray = [(a, t, c) for a, t, c in rows if t != "bcp_report" and c != x]
    assert stray == [], stray
    assert ticket and set(ticket) == {x}


async def _user_id_of(env: ApiEnv, headers: dict[str, str]) -> UUID:
    me = await env.client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200, me.text
    return UUID(me.json()["user_id"])


# --------------------------------------------------------------------------- #
# Gates and automatic changes, per client (D4)
# --------------------------------------------------------------------------- #


class _Counting:
    """`gates_for` with a call log: one entry per client asked about."""

    def __init__(self) -> None:
        self.clients: list[UUID] = []

    def __call__(self, real: Any) -> Any:
        async def counted(session: Any, org_id: UUID, kind: Any, *, client_id: UUID) -> Any:
            self.clients.append(client_id)
            return await real(session, org_id, kind, client_id=client_id)

        return counted


async def _ring(env: ApiEnv, org: UUID, client: UUID, site: UUID, n: int) -> list[UUID]:
    group = await _group(env, org, client, name=f"g-{uuid4().hex[:4]}")
    await _seed(env, PatchPolicy(org_id=org, device_group_id=group, ring_name="broad"))
    return [await _device(env, org, client, site, group) for _ in range(n)]


async def test_a_ring_s_approval_is_gated_per_client_once_each(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ring spans clients. Its approval is gated when ANY client it reaches
    meets a gate -- evaluated once per client, never per device."""
    org = await api_env.create_org("m-ring")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    x_devices = await _ring(api_env, org, x, x_site, 3)
    y_devices = await _ring(api_env, org, y, y_site, 2)
    await _seed(api_env, ChangeGate(org_id=org, device_group_id=None, applies_to_patches=True,
                                    applies_to_deployments=False))
    counting = _Counting()
    monkeypatch.setattr(gates_module, "gates_for", counting(gates_module.gates_for))
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        both = await patch_approval_gate(s, org, [*x_devices, *y_devices], "broad")
        assert sorted(map(str, counting.clients)) == sorted([str(x), str(y)])
        only_y = await patch_approval_gate(s, org, y_devices, "broad")
    assert both is not None and "every device" in str(both)
    assert only_y is None


async def test_dispatch_gates_each_device_by_its_client_once_per_client(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window sweep: X uses change management and a global gate covers
    it, so X's approved patch waits for a change; Y does not, so Y's goes.
    `gates_for` once per client of the sweep, not once per device."""
    org = await api_env.create_org("m-apply")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    x_devices = await _ring(api_env, org, x, x_site, 2)
    y_devices = await _ring(api_env, org, y, y_site, 2)
    [approval] = await _global_approvals(api_env, org, 1)
    async with AsyncSession(api_env.admin_engine) as s:
        patch_id = (
            await s.execute(select(PatchApproval.patch_id).where(PatchApproval.id == approval))
        ).scalar_one()
        s.add_all([
            DevicePatchStatus(org_id=org, device_id=d, patch_id=patch_id, state="missing")
            for d in (*x_devices, *y_devices)
        ])
        s.add(ChangeGate(org_id=org, device_group_id=None, applies_to_patches=True,
                         applies_to_deployments=False))
        await s.commit()
    counting = _Counting()
    monkeypatch.setattr(apply_module, "gates_for", counting(apply_module.gates_for))
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        pending = await dispatch_apply(s, _FakeJobEngine(), org_id=org, now=datetime.now(UTC))
    assert {device for _, device, _ in pending} == set(y_devices)
    assert sorted(map(str, counting.clients)) == sorted([str(x), str(y)])


async def test_a_deployment_meets_its_target_s_client_s_gates(api_env: ApiEnv) -> None:
    """The gates are the deployment's client's -- its own column since plan
    2026-10-26 Task 9, which the trigger keeps equal to the target's, so each
    call passes the target's client. X uses change management and meets the
    global gate; Y does not, and the internal client (which does) must not
    stand in for either."""
    org = await api_env.create_org("m-deploy")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    x_group, y_group = await _group(api_env, org, x), await _group(api_env, org, y)
    x_device = await _device(api_env, org, x, x_site, x_group)
    y_device = await _device(api_env, org, y, y_site, y_group)
    await _seed(api_env, ChangeGate(org_id=org, device_group_id=None, applies_to_patches=False,
                                    applies_to_deployments=True))
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await deployment_gate(s, org, "device", x_device, client_id=x) is not None
        assert await deployment_gate(s, org, "group", x_group, client_id=x) is not None
        assert await deployment_gate(s, org, "device", y_device, client_id=y) is None
        assert await deployment_gate(s, org, "group", y_group, client_id=y) is None


async def test_no_automatic_change_for_a_client_without_the_module(api_env: ApiEnv) -> None:
    org = await api_env.create_org("m-auto")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id_of(api_env, admin)
    [approval] = await _global_approvals(api_env, org, 1)
    kw: dict[str, Any] = {"actor_id": actor, "title": "auto", "description": "d",
                          "affected": [], "action_type": "patch_approval",
                          "action_ids": [approval]}
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await record_auto_change(s, org_id=org, client_id=x, **kw) is None
        internal = await record_auto_change(s, org_id=org, client_id=org, **kw)
        assert internal is not None and internal.client_id == org
    async with AsyncSession(api_env.admin_engine) as s:
        assert set(
            (await s.execute(select(ChangeRequest.client_id).where(ChangeRequest.org_id == org)))
            .scalars()
        ) == {org}


async def test_a_global_approval_s_automatic_change_is_the_internal_client_s(
    api_env: ApiEnv,
) -> None:
    """C8: a global patch approval has no client; Novieri decided it."""
    org = await api_env.create_org("m-auto-patch")
    admin = await _admin(api_env, org)
    [patch] = await _seed(api_env, Patch(org_id=org, os_family="windows",
                                         source="windows_update", identifier="KB9",
                                         title="t", severity="critical"))
    r = await api_env.client.post(
        "/api/v1/patch/approvals", json={"patch_id": str(patch), "ring_name": "broad"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(ChangeRequest.client_id).where(ChangeRequest.org_id == org))
        ).scalars().all() == [org]


async def test_a_group_s_gate_needs_change_management_in_the_group_s_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("m-set-gate")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _modules(api_env, admin, x, "change")
    for group, expected in ((await _group(api_env, org, y), 404),
                            (await _group(api_env, org, x), 200), (None, 200)):
        r = await api_env.client.put(
            "/api/v1/changes/gates",
            json={"device_group_id": str(group) if group else None},
            headers=admin,
        )
        assert r.status_code == expected, (group, r.text)

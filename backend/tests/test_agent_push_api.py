"""The push-install API surface and its RBAC completeness
(spec §8, §14; `CLAUDE.md` invariant 2a (e), (f); falsifiers 23 and 24).

**RBAC COMPLETENESS IS DERIVED FROM THE ROUTER**, not from a list somebody has
to remember to update: every route is walked, `route_permission` is called on
each, and a route registered without `require_permission` returns `None` there
and fails BY EXISTING. The route table is pinned by EQUALITY and by an exact
COUNT -- a `>=` hides a missing route and a subset hides one that was never
mounted.

**THE PERMISSION SPLIT IS ASSERTED AS BEHAVIOUR, AGAINST ONE FIXTURE.** A
principal holding `agent.push.run` and not `agent.push.scope.manage` is
refused the authorization write; a principal holding
`agent.push.scope.manage` and not `credentials.checkout` is refused the push.
Collapsed into one code, anyone who could push could also decide where pushes
may go, which is the whole control.

**THE `credentials.checkout` CONJUNCTION CANNOT BE ASSERTED BELOW THE ROUTER**
in the form that matters: the route dependency gates `agent.push.run`, and the
second half is a service check. Only a request carrying a token whose roles
grant one and not the other can show that the conjunction is enforced at the
HTTP boundary, which is where an attacker meets it.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **The cross-org tests use a REAL id from a REAL second org.** A random UUID
  404s against any implementation, including one with no tenancy at all.
* **Every role is issued against the SAME org and the SAME rows**, so a 403 is
  a statement about the role rather than about what the fixture contained.
* **The route-count assertion is a number, not a `>=`.**
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import PERMISSIONS, route_permission
from app.models import Permission, Role, RolePermission
from tests.agent_push_fixtures import (
    seed_asset,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)

PUSH = "/api/v1/agent-push"

RUN = "agent.push.run"
SCOPE = "agent.push.scope.manage"

EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{PUSH}/authorizations"): SCOPE,
    ("POST", f"{PUSH}/authorizations"): SCOPE,
    ("DELETE", f"{PUSH}/authorizations/{{authorization_id}}"): SCOPE,
    ("GET", f"{PUSH}/credential-scopes"): SCOPE,
    ("POST", f"{PUSH}/credential-scopes"): SCOPE,
    ("DELETE", f"{PUSH}/credential-scopes/{{scope_id}}"): SCOPE,
    ("POST", f"{PUSH}/requests"): RUN,
    # Cycle B2: the host-key crux. Probing/confirming/pushing is running a push
    # (§4.4), so these are gated on `agent.push.run`, not `scope.manage`.
    ("GET", f"{PUSH}/host-keys"): RUN,
    ("POST", f"{PUSH}/host-keys/confirm"): RUN,
    # Push runs (design 2026-09-30 §1.7): "the existing push codes. Nothing new
    # is needed, because confirming a run *is* a push."
    ("GET", f"{PUSH}/runs/prefixes"): RUN,
    ("GET", f"{PUSH}/runs/preview"): RUN,
    ("POST", f"{PUSH}/runs"): RUN,
    ("GET", f"{PUSH}/runs"): RUN,
    ("GET", f"{PUSH}/runs/{{run_id}}"): RUN,
    ("GET", f"{PUSH}/runs/{{run_id}}/targets"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/pause"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/resume"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/cancel"): RUN,
}
EXPECTED_ROUTE_COUNT = 18


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper rather
    than flattening. Copied from `tests/test_ipam_api.py`, and the same in
    every module that derives completeness from the router.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(
                _flatten(original, prefix + (getattr(context, "prefix", "") or ""))
            )
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _push_routes(app: Any) -> list[tuple[str, Any]]:
    return [(path, route) for path, route in _flatten(app.router) if path.startswith(PUSH)]


# --------------------------------------------------------------------------- #
# The surface
# --------------------------------------------------------------------------- #


async def test_the_route_table_is_exactly_what_this_cycle_mounts(api_env) -> None:
    found = {
        (method, path)
        for path, route in _push_routes(api_env.app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    assert found == set(EXPECTED_ROUTES)
    assert len(found) == EXPECTED_ROUTE_COUNT


async def test_every_route_carries_a_permission_and_it_is_the_right_one(
    api_env,
) -> None:
    """COMPLETENESS DERIVED FROM THE ROUTER (invariant #6).

    Two assertions, both needed. The first: a route with no
    `require_permission` returns `None` here and fails by existing. The second
    pins WHICH code -- a `/authorizations` POST gated on `agent.push.run` is
    fully gated and completely wrong, and the first assertion cannot see it.

    GUARD OBSERVABILITY: delete `Depends(require_permission(...))` from any one
    route in `api.py` and this fails naming that exact (method, path).
    """
    found = {
        (method, path): route_permission(route)
        for path, route in _push_routes(api_env.app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    ungated = sorted(key for key, code in found.items() if code is None)
    assert ungated == [], f"routes with no permission dependency: {ungated}"
    assert found == EXPECTED_ROUTES


def test_every_permission_this_module_gates_on_is_a_registered_code() -> None:
    """A typo'd code is not a weaker gate, it is a 500 -- `has_permission`
    raises `ValueError` for an unknown code."""
    unknown = sorted({c for c in EXPECTED_ROUTES.values() if c not in PERMISSIONS})
    assert unknown == []


def test_the_run_code_gates_the_push_and_the_host_key_crux_and_nothing_else() -> None:
    """`agent.push.run` is the code that causes an unenrolled machine to execute
    this platform's installer, plus the host-key steps that make a push
    defensible (§4.4). Stated as a set equality so a future route that quietly
    reuses it -- "it is a push thing anyway" -- fails here rather than widening
    the grant. The scope-management routes stay on `scope.manage`."""
    running = {k for k, code in EXPECTED_ROUTES.items() if code == RUN}
    assert running == {
        ("POST", f"{PUSH}/requests"),
        ("GET", f"{PUSH}/host-keys"),
        ("POST", f"{PUSH}/host-keys/confirm"),
        ("GET", f"{PUSH}/runs/prefixes"),
        ("GET", f"{PUSH}/runs/preview"),
        ("POST", f"{PUSH}/runs"),
        ("GET", f"{PUSH}/runs"),
        ("GET", f"{PUSH}/runs/{{run_id}}"),
        ("GET", f"{PUSH}/runs/{{run_id}}/targets"),
        ("POST", f"{PUSH}/runs/{{run_id}}/pause"),
        ("POST", f"{PUSH}/runs/{{run_id}}/resume"),
        ("POST", f"{PUSH}/runs/{{run_id}}/cancel"),
    }


# --------------------------------------------------------------------------- #
# Behaviour: the split, and the conjunction
# --------------------------------------------------------------------------- #


async def _role_with(api_env, org_id: UUID, name: str, codes: list[str]) -> str:
    """An org-scoped role holding exactly `codes`, and a user in it.

    Built here rather than reusing a seeded role because the point of every
    test below is a principal holding SOME of the codes: `technician` holds
    neither and `org-admin` holds both, so neither can show a conjunction
    failing.
    """
    from app.models import User, UserRole

    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        role = Role(org_id=org_id, name=f"{name}-{uuid4().hex[:6]}")
        session.add(role)
        await session.flush()
        for code in codes:
            permission = (
                await session.execute(select(Permission).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        # The `UserRole` row is written here rather than through
        # `api_env.create_user`, which resolves roles by name among the GLOBAL
        # ones (`Role.org_id IS NULL`). The whole point of this helper is a
        # role holding SOME of the codes, and no global role does.
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        session.add(user)
        await session.flush()
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return sub


async def test_a_push_runner_may_not_authorize_a_prefix(api_env) -> None:
    """**THE WHOLE CONTROL, IN ONE TEST, AGAINST ONE FIXTURE.**

    *If the two codes were collapsed*, the second call observes 201 -- and
    anyone who can push can also decide where pushes may go.
    """
    org_id = await api_env.create_org("push-split")
    sub = await _role_with(
        api_env, org_id, "runner", [RUN, "credentials.checkout"]
    )
    headers = await api_env.headers(org_id, sub)

    created = await api_env.client.post(
        f"{PUSH}/authorizations", json={"prefix": "10.80.0.0/24"}, headers=headers
    )
    assert created.status_code == 403

    listed = await api_env.client.get(f"{PUSH}/authorizations", headers=headers)
    assert listed.status_code == 403


async def test_a_scope_manager_may_not_run_a_push(api_env) -> None:
    """The other direction. A principal who decides where pushes may go does
    not thereby get to make one."""
    org_id = await api_env.create_org("push-split2")
    sub = await _role_with(api_env, org_id, "scoper", [SCOPE])
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "install", "credential_id": str(uuid4()), "targets": ["10.80.0.5"]},
        headers=headers,
    )
    assert response.status_code == 403


async def test_the_run_code_alone_is_refused_without_credentials_checkout(
    api_env,
) -> None:
    """Spec §2.5, §8: a push USES a stored secret, so the permission that
    governs decrypting one is not optional.

    ASSERTED AT THE ROUTER, because that is the only place the conjunction can
    be shown to hold for a real request: the dependency gates `agent.push.run`
    and the service checks the second half.

    *If the service check were deleted*, this observes a 422 or a 501 -- the
    ladder answering a caller who may not use a stored secret at all.
    """
    org_id = await api_env.create_org("push-conj")
    sub = await _role_with(api_env, org_id, "runner-only", [RUN])
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "install", "credential_id": str(uuid4()), "targets": ["10.80.0.5"]},
        headers=headers,
    )
    assert response.status_code == 403
    assert "credentials.checkout" in response.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{PUSH}/authorizations"),
        ("POST", f"{PUSH}/authorizations"),
        ("GET", f"{PUSH}/credential-scopes"),
        ("POST", f"{PUSH}/credential-scopes"),
        ("POST", f"{PUSH}/requests"),
    ],
)
async def test_an_unauthenticated_request_is_refused(
    api_env, method: str, path: str
) -> None:
    response = await api_env.client.request(method, path, json={})
    assert response.status_code in (401, 403)


async def test_a_technician_holds_neither_code(api_env) -> None:
    """Spec §8, D8: admin tier deliberately. The seeded `technician` role holds
    `ipam.sweep.run`, `deploy.script.run` and `provisioning.run` -- and must
    hold nothing here."""
    org_id = await api_env.create_org("push-tech")
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)

    for path in (f"{PUSH}/authorizations", f"{PUSH}/credential-scopes"):
        assert (await api_env.client.get(path, headers=headers)).status_code == 403
    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "install", "credential_id": str(uuid4()), "targets": ["10.80.0.5"]},
        headers=headers,
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Behaviour: the ladder, through HTTP
# --------------------------------------------------------------------------- #


async def _authorized_world(api_env, tag: str):
    """Every bound satisfied, built through the admin engine."""
    org = await seed_org(api_env, tag)
    sub = await api_env.create_user(org.org_id, "org-admin")
    headers = await api_env.headers(org.org_id, sub)
    credential_id = await seed_credential(api_env, org)
    await seed_asset(api_env, org.org_id, primary_ip="10.90.0.17")
    await seed_push_authorization(api_env, org, "10.90.0.0/24")
    await seed_push_credential_scope(api_env, org, credential_id, "10.90.0.0/24")
    await seed_push_site_server(api_env, org.org_id, segments=["10.90.0.0/24"])
    return org, headers, credential_id


async def test_a_fully_authorized_probe_dispatches_a_signed_job(
    api_env,
) -> None:
    """**SPEC §14, THROUGH THE ROUTE, IN CYCLE B2.** Everything holds; the
    platform signs and dispatches an `agent.push_probe` job through the site
    server and answers 202 with the batch and its job.

    A PROBE rather than an install, because a probe carries no credential and no
    artefact (§4.4) -- so it exercises the signing and site-server dispatch path
    without needing a decrypted secret or a presigned URL, and it is the step an
    operator must do first anyway.

    The job is real: it targets the SITE SERVER'S device (never the machine
    being probed, which has no `devices` row a job could target), it is signed,
    and `push.dispatched` is on the audit chain.
    """
    org, headers, credential_id = await _authorized_world(api_env, "push-probe")
    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "probe", "credential_id": str(credential_id), "targets": ["10.90.0.17"]},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == "probe"
    assert [t["address"] for t in body["targets"]] == ["10.90.0.17"]

    async with AsyncSession(api_env.admin_engine) as session:
        from app.models import AuditEvent, Job

        jobs = (
            (await session.execute(select(Job).where(Job.org_id == org.org_id)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        job = jobs[0]
        assert job.type == "agent.push_probe"
        # Signed over canonical bytes, like every other job (invariant #1).
        assert job.signature
        # Targets the site server's device, not the probed address.
        assert str(job.target_id) != "10.90.0.17"
        # No credential of any kind in a probe's params (§4.4).
        assert "credential" not in job.params
        assert "enroll_token" not in job.params

        dispatched = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "push.dispatched",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.meta["target_address"] for e in dispatched] == ["10.90.0.17"]
    assert dispatched[0].meta["kind"] == "probe"


async def test_a_refused_push_names_every_address_and_every_reason(api_env) -> None:
    """Spec §9: a refusal that does not say which is a refusal nobody can act
    on. THE REFUSAL ALSO HAS TO SURVIVE THE TRANSACTION -- asserted here by
    reading `push.refused` back after the response, because `org_session` rolls
    back on any exception leaving it and a service that raised from inside
    would answer 422 having recorded nothing.
    """
    org, headers, credential_id = await _authorized_world(api_env, "push-422")
    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={
            "mode": "probe",
            "credential_id": str(credential_id),
            "targets": ["10.90.0.17", "10.90.0.99"],
        },
        headers=headers,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [t["address"] for t in detail["targets"]] == ["10.90.0.99"]
    assert detail["targets"][0]["reasons"] == ["no_asset"]
    assert "no_asset" in detail["reason_help"]

    async with AsyncSession(api_env.admin_engine) as session:
        from app.models import AuditEvent

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "push.refused",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.meta["target_address"] for e in events] == ["10.90.0.99"], (
        "the refusal was rolled back with the exception that reported it"
    )


async def test_a_batch_over_the_ceiling_is_a_400_and_names_the_ceiling(
    api_env,
) -> None:
    _, headers, credential_id = await _authorized_world(api_env, "push-ceil")
    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={
            "mode": "probe",
            "credential_id": str(credential_id),
            "targets": [f"10.90.0.{i}" for i in range(1, 27)],
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "25" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"credential_id": "00000000-0000-0000-0000-000000000001", "group_id": "x"},
        {
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "targets": ["10.90.0.17"],
            "install_args": "/PASSWORD=hunter2",
        },
        {
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "targets": ["10.90.0.17"],
            "script": "curl x | sh",
        },
        {
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "targets": ["10.90.0.17"],
            "release_url": "https://evil.example/agent",
        },
    ],
)
async def test_the_request_model_forbids_every_extra_field(api_env, body: dict) -> None:
    """Invariant 2a clauses (b) and (f), at the edge of the system.

    `extra="forbid"` is what makes "no command line, no script, no artefact
    URL, no group" a property of the MODEL rather than of whoever reviews the
    next pull request. Each body here is a different smuggling shape and each
    must be a 422 from validation, never a 201, a 501 or a silently ignored
    field.
    """
    org_id = await api_env.create_org("push-forbid")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    response = await api_env.client.post(f"{PUSH}/requests", json=body, headers=headers)
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Tenancy through the route (falsifier 23)
# --------------------------------------------------------------------------- #


async def test_another_orgs_authorization_is_a_404_not_a_403(api_env) -> None:
    """A REAL id from a REAL second org. A 403 would confirm the id exists,
    which is a cross-tenant existence oracle."""
    a = await api_env.create_org("push-x-a")
    b_org = await seed_org(api_env, "push-x-b")
    b_row = await seed_push_authorization(api_env, b_org, "10.91.0.0/24")

    sub = await api_env.create_user(a, "org-admin")
    headers = await api_env.headers(a, sub)
    response = await api_env.client.delete(
        f"{PUSH}/authorizations/{b_row}", headers=headers
    )
    assert response.status_code == 404


async def test_another_orgs_credential_is_a_404_not_a_403(api_env) -> None:
    """Over `credentials` an existence oracle is an oracle on the vault's own
    key space."""
    a = await api_env.create_org("push-xc-a")
    b_org = await seed_org(api_env, "push-xc-b")
    b_credential = await seed_credential(api_env, b_org)

    sub = await api_env.create_user(a, "org-admin")
    headers = await api_env.headers(a, sub)
    response = await api_env.client.post(
        f"{PUSH}/credential-scopes",
        json={"credential_id": str(b_credential), "prefix": "10.92.0.0/24"},
        headers=headers,
    )
    assert response.status_code == 404


async def test_a_list_never_shows_another_orgs_rows(api_env) -> None:
    a_org = await seed_org(api_env, "push-xl-a")
    b_org = await seed_org(api_env, "push-xl-b")
    await seed_push_authorization(api_env, a_org, "10.93.0.0/24")
    await seed_push_authorization(api_env, b_org, "10.94.0.0/24")

    sub = await api_env.create_user(a_org.org_id, "org-admin")
    headers = await api_env.headers(a_org.org_id, sub)
    response = await api_env.client.get(f"{PUSH}/authorizations", headers=headers)
    assert response.status_code == 200
    assert [row["prefix"] for row in response.json()] == ["10.93.0.0/24"]


async def test_no_response_body_carries_credential_material(api_env) -> None:
    """Invariant #3, at the API boundary. No route here returns a secret, a
    ciphertext, a wrapped DEK or a vault reference -- and none has a field
    that could."""
    org, headers, credential_id = await _authorized_world(api_env, "push-nosecret")
    bodies = [
        (await api_env.client.get(f"{PUSH}/authorizations", headers=headers)).text,
        (await api_env.client.get(f"{PUSH}/credential-scopes", headers=headers)).text,
        (
            await api_env.client.post(
                f"{PUSH}/requests",
                json={"credential_id": str(credential_id), "targets": ["10.90.0.17"]},
                headers=headers,
            )
        ).text,
    ]
    for body in bodies:
        lowered = body.lower()
        for forbidden in ("secret", "ciphertext", "wrapped_dek", "vault_ref", "envelope:"):
            assert forbidden not in lowered, body


async def test_the_push_route_hands_dispatch_the_configured_console_url(
    api_env, monkeypatch
) -> None:
    """W2 superseded: the enroll URL a pushed computer is given comes from
    ITS_PUBLIC_CONSOLE_URL, and the route is what reads it. A route that passed
    nothing would have every Windows install refused -- and nothing may reach
    for the presigned release URL's origin instead."""
    from app.modules.agent_push import dispatch as dispatch_service

    captured: dict[str, Any] = {}

    async def _fake_dispatch(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return dispatch_service.PushDispatchResult(
            batch_id=None, kind=kwargs["kind"], refused={"10.80.0.9": ["no_asset"]}
        )

    monkeypatch.setattr(dispatch_service, "dispatch_push_batch", _fake_dispatch)
    monkeypatch.setattr(
        api_env.app.state.settings, "public_console_url", "https://itms.example.com"
    )
    org_id = await api_env.create_org("push-console-url")
    sub = await _role_with(api_env, org_id, "runner", [RUN, "credentials.checkout"])
    headers = await api_env.headers(org_id, sub)

    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "install", "credential_id": str(uuid4()), "targets": ["10.80.0.9"]},
        headers=headers,
    )
    assert response.status_code == 422
    assert captured["public_console_url"] == "https://itms.example.com"


async def test_a_dispatch_refusal_is_committed_to_the_audit_chain_and_explained(
    api_env, monkeypatch
) -> None:
    """Review Minor 2, through the route: a refusal dispatch finds (here, no
    console address) is a 422 naming the reason with its plain-language help,
    and its `push.refused` row survives -- it is not an exception that rolls
    the record back."""
    from app.models import AuditEvent

    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", None)
    org, headers, credential_id = await _authorized_world(api_env, "push-refused-audit")
    response = await api_env.client.post(
        f"{PUSH}/requests",
        json={"mode": "install", "credential_id": str(credential_id), "targets": ["10.90.0.17"]},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    (target,) = detail["targets"]
    assert target["address"] == "10.90.0.17"
    assert "no_console_url" in target["reasons"]
    assert "ITS_PUBLIC_CONSOLE_URL" in detail["reason_help"]["no_console_url"]
    async with AsyncSession(api_env.admin_engine) as session:
        refused = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id, AuditEvent.action == "push.refused"
                )
            )
        ).scalars().all()
    assert any("no_console_url" in e.meta["reasons"] for e in refused)

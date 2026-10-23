"""The site-server API surface and its RBAC (spec §11, §10.2, §14).

**ROUTE DISCOVERY IS FROM THE APP, NOT FROM THE MODULE ROUTER.** In FastAPI
0.139.2 `include_router` appends a single lazy `_IncludedRouter` wrapper whose
own `.path` is `None`, so filtering `app.routes` by a path prefix yields an
empty list forever — and an RBAC completeness test over an empty list passes
gloriously while gating nothing. `_flatten` descends through
`_IncludedRouter.original_router` and re-applies `include_context.prefix`; it is
copied from `tests/test_email_intake_api.py`, which found this the hard way.

`test_the_routes_are_mounted_and_counted` pins the flattener itself against an
empty result, so a future FastAPI that changes the wrapper's shape fails loudly
here instead of silently passing every assertion over nothing.

**THE `site.manage` / `site.wake` SPLIT IS TESTED IN BOTH DIRECTIONS.** One
direction alone passes against a router that gates every route on a single code.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.rbac import route_permission
from tests.ipam_fixtures import seed_agent, seed_device, seed_org
from tests.site_server_fixtures import seed_site_server, site_server_rows

PREFIX = "/api/v1/site-servers"

#: Every route spec §11 tabulates, fully prefixed, with its permission.
#: Method+path, because GET/PATCH/DELETE share one path and each needs its own
#: gate.
EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", PREFIX): "site.read",
    ("POST", PREFIX): "site.manage",
    ("GET", f"{PREFIX}/{{site_server_id}}"): "site.read",
    ("PATCH", f"{PREFIX}/{{site_server_id}}"): "site.manage",
    ("DELETE", f"{PREFIX}/{{site_server_id}}"): "site.manage",
    ("GET", f"{PREFIX}/{{site_server_id}}/segment-check"): "site.read",
    ("POST", f"{PREFIX}/{{site_server_id}}/wake"): "site.wake",
}


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _app_routes(app: Any) -> list[tuple[str, Any]]:
    return [(path, route) for path, route in _flatten(app.router) if path.startswith(PREFIX)]


def _method_paths(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in _app_routes(app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }


# --------------------------------------------------------------------------- #
# Route completeness and RBAC derivation
# --------------------------------------------------------------------------- #


async def test_the_routes_are_mounted_and_counted(api_env: Any) -> None:
    """**AN EXACT SET, NOT A SUPERSET.** A `>=` assertion hides a missing route,
    and an empty result would make every other assertion in this file vacuous —
    which is exactly what filtering `app.routes` by prefix would produce."""
    assert _method_paths(api_env.app) == set(EXPECTED_ROUTES)
    assert len(_app_routes(api_env.app)) == 7


async def test_every_route_is_gated_on_the_permission_spec_section_eleven_names(
    api_env: Any,
) -> None:
    """RBAC completeness DERIVED FROM THE ROUTER (invariant #6), never from a
    hand-maintained list — a route registered without `require_permission`
    returns `None` from `route_permission` and fails here BY EXISTING.

    *If `require_permission` were removed from exactly one route* the observed
    map carries `None` for that method+path, and the assertion names it. That
    mutation was run; see the commit message.
    """
    observed = {
        (method, path): route_permission(route)
        for path, route in _app_routes(api_env.app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    assert observed == EXPECTED_ROUTES


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


async def _org_with_site_server(
    api_env: Any, *, segments: list[str] | None = None
) -> tuple[UUID, UUID, UUID]:
    """`(org_id, device_id, site_server_id)` — an org whose device has an agent."""
    org = await seed_org(api_env.admin_engine, "ssa")
    await seed_agent(api_env.admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        api_env.admin_engine,
        org.org_id,
        device_id=org.device_id,
        segments=segments or ["10.20.7.0/24"],
    )
    return org.org_id, org.device_id, site_server_id


async def _headers(api_env: Any, org_id: UUID, *roles: str) -> dict[str, str]:
    sub = await api_env.create_user(org_id, *roles)
    return await api_env.headers(org_id, sub)


async def _jobs(engine: AsyncEngine, org_id: UUID) -> list[Any]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    text("SELECT id, type, target_id, params FROM jobs WHERE org_id = :org"),
                    {"org": org_id},
                )
            ).all()
        )


# --------------------------------------------------------------------------- #
# Happy paths, one per route
# --------------------------------------------------------------------------- #


async def test_promote_list_get_patch_and_demote(api_env: Any) -> None:
    org = await seed_org(api_env.admin_engine, "ssa-crud")
    await seed_agent(api_env.admin_engine, org.org_id, org.device_id)
    headers = await _headers(api_env, org.org_id, "org-admin")

    created = await api_env.client.post(
        PREFIX,
        json={"device_id": str(org.device_id), "segments": ["10.20.0.0/16"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    site_server_id = body["id"]
    assert (body["segments"], body["roles"], body["agent_status"]) == (
        ["10.20.0.0/16"],
        ["wake"],
        "online",
    )

    listed = await api_env.client.get(PREFIX, headers=headers)
    assert [row["id"] for row in listed.json()] == [site_server_id]

    fetched = await api_env.client.get(f"{PREFIX}/{site_server_id}", headers=headers)
    assert fetched.json()["device_id"] == str(org.device_id)

    patched = await api_env.client.patch(
        f"{PREFIX}/{site_server_id}",
        json={"segments": ["10.21.0.0/16"], "enabled": False},
        headers=headers,
    )
    assert (patched.json()["segments"], patched.json()["enabled"]) == (
        ["10.21.0.0/16"],
        False,
    )

    removed = await api_env.client.delete(f"{PREFIX}/{site_server_id}", headers=headers)
    assert removed.status_code == 204
    assert await site_server_rows(api_env.admin_engine, org.org_id) == []


async def test_the_list_segment_filter_is_containment(api_env: Any) -> None:
    """Asking who serves `10.20.7.0/24` must find the branch server declared for
    `10.20.0.0/16`. The fixture holds a second, non-matching server so the
    assertion observes WHICH row came back."""
    org = await seed_org(api_env.admin_engine, "ssa-filter")
    branch = await seed_device(api_env.admin_engine, org.org_id, hostname="branch")
    other = await seed_device(api_env.admin_engine, org.org_id, hostname="other")
    branch_ss = await seed_site_server(
        api_env.admin_engine, org.org_id, device_id=branch, segments=["10.20.0.0/16"]
    )
    await seed_site_server(
        api_env.admin_engine, org.org_id, device_id=other, segments=["10.90.0.0/16"]
    )
    headers = await _headers(api_env, org.org_id, "org-admin")

    response = await api_env.client.get(
        PREFIX, params={"segment": "10.20.7.0/24"}, headers=headers
    )
    assert [row["id"] for row in response.json()] == [str(branch_ss)]


async def test_wake_creates_a_job_and_returns_the_computed_broadcast(
    api_env: Any,
) -> None:
    org_id, device_id, site_server_id = await _org_with_site_server(
        api_env, segments=["10.20.0.0/16", "10.20.7.0/24"]
    )
    headers = await _headers(api_env, org_id, "org-admin")

    response = await api_env.client.post(
        f"{PREFIX}/{site_server_id}/wake",
        json={"target_mac": "A4-BB-6D-11-22-33", "segment": "10.20.7.31"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    # SERVER-COMPUTED, from the MORE SPECIFIC declared segment. The caller never
    # supplies a broadcast address and cannot influence it beyond naming which
    # wire the machine is on.
    assert (body["broadcast"], body["target_mac"], body["device_id"]) == (
        "10.20.7.255",
        "a4:bb:6d:11:22:33",
        str(device_id),
    )

    rows = await _jobs(api_env.admin_engine, org_id)
    assert [(r.type, r.target_id, r.params["broadcast"]) for r in rows] == [
        ("site.wake", device_id, "10.20.7.255")
    ]


async def test_a_wake_whose_target_is_on_no_declared_segment_is_a_400_creating_no_job(
    api_env: Any,
) -> None:
    """ASSERT THE JOB COUNT as well as the status. A 400 that had already
    created and signed the job would satisfy a status assertion alone."""
    org_id, _device_id, site_server_id = await _org_with_site_server(
        api_env, segments=["10.20.0.0/16"]
    )
    headers = await _headers(api_env, org_id, "org-admin")

    response = await api_env.client.post(
        f"{PREFIX}/{site_server_id}/wake",
        json={"target_mac": "a4:bb:6d:11:22:33", "segment": "10.90.1.1"},
        headers=headers,
    )
    assert (response.status_code, await _jobs(api_env.admin_engine, org_id)) == (400, [])


async def test_the_segment_check_route_answers_with_an_evidence_marker(
    api_env: Any,
) -> None:
    org_id, _device_id, site_server_id = await _org_with_site_server(api_env)
    headers = await _headers(api_env, org_id, "org-admin")

    response = await api_env.client.get(
        f"{PREFIX}/{site_server_id}/segment-check", headers=headers
    )
    body = response.json()
    assert response.status_code == 200
    # IPAM IS installed in the test schema, so the evidence set is real and
    # empty rather than unavailable — two different answers, and the marker is
    # what distinguishes them.
    assert (body["evidence"], body["declared_unobserved"], body["matched"]) == (
        "ipam",
        ["10.20.7.0/24"],
        [],
    )


# --------------------------------------------------------------------------- #
# RBAC behaviour — the split, in BOTH directions
# --------------------------------------------------------------------------- #


async def test_a_wake_only_principal_cannot_promote(api_env: Any) -> None:
    """DIRECTION ONE. A technician holds `site.read` + `site.wake` and NOT
    `site.manage` (migration 0050's grant), so they may wake a machine inside an
    existing configuration and may not decide what a site server is."""
    org = await seed_org(api_env.admin_engine, "ssa-split1")
    await seed_agent(api_env.admin_engine, org.org_id, org.device_id)
    headers = await _headers(api_env, org.org_id, "technician")

    response = await api_env.client.post(
        PREFIX,
        json={"device_id": str(org.device_id), "segments": ["10.20.0.0/16"]},
        headers=headers,
    )
    assert (response.status_code, await site_server_rows(api_env.admin_engine, org.org_id)) == (
        403,
        [],
    )


async def test_the_same_principal_may_wake(api_env: Any) -> None:
    """DIRECTION ONE's other half: the 403 above must be about the PERMISSION,
    not about the principal being broken. Same role, different route, 202."""
    org_id, _device_id, site_server_id = await _org_with_site_server(api_env)
    headers = await _headers(api_env, org_id, "technician")

    response = await api_env.client.post(
        f"{PREFIX}/{site_server_id}/wake",
        json={"target_mac": "a4:bb:6d:11:22:33"},
        headers=headers,
    )
    assert response.status_code == 202, response.text


async def test_a_manage_only_principal_cannot_wake(api_env: Any) -> None:
    """DIRECTION TWO, and it needs a role that does not exist in the seed — so
    it is granted directly.

    Without this direction the suite passes against a router that gates
    everything on `site.manage`: every seeded role that holds `site.manage` also
    holds `site.wake`, so no seeded role can tell the two apart.
    """
    org_id, _device_id, site_server_id = await _org_with_site_server(api_env)
    sub = await api_env.create_user(org_id)
    await _grant(api_env.admin_engine, org_id, sub, "site.read", "site.manage")
    headers = await api_env.headers(org_id, sub)

    waking = await api_env.client.post(
        f"{PREFIX}/{site_server_id}/wake",
        json={"target_mac": "a4:bb:6d:11:22:33"},
        headers=headers,
    )
    reading = await api_env.client.get(f"{PREFIX}/{site_server_id}", headers=headers)
    patching = await api_env.client.patch(
        f"{PREFIX}/{site_server_id}", json={"notes": "ok"}, headers=headers
    )

    assert (waking.status_code, reading.status_code, patching.status_code) == (403, 200, 200)
    assert await _jobs(api_env.admin_engine, org_id) == []


async def _grant(engine: AsyncEngine, org_id: UUID, sub: str, *codes: str) -> None:
    """A bespoke org role holding exactly `codes`.

    Needed because no SEEDED role holds `site.manage` without also holding
    `site.wake` — which is precisely why direction two cannot be tested with a
    seeded role, and precisely why it must be tested at all.
    """
    async with AsyncSession(engine) as session:
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE external_subject = :sub"), {"sub": sub}
            )
        ).scalar_one()
        role_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO roles (id, org_id, name, description)"
                " VALUES (:id, :org, :name, '')"
            ),
            {"id": role_id, "org": org_id, "name": f"manage-only-{uuid4().hex[:8]}"},
        )
        for code in codes:
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id)"
                    " SELECT :role, id FROM permissions WHERE code = :code"
                ),
                {"role": role_id, "code": code},
            )
        await session.execute(
            text(
                "INSERT INTO user_roles (id, org_id, user_id, role_id)"
                " VALUES (gen_random_uuid(), :org, :user, :role)"
            ),
            {"org": org_id, "user": user_id, "role": role_id},
        )
        await session.commit()


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("get", "", None),
        ("post", "", {"device_id": str(uuid4()), "segments": ["10.20.0.0/16"]}),
    ],
)
async def test_a_principal_with_no_site_permission_is_refused(
    api_env: Any, method: str, path_suffix: str, body: Any
) -> None:
    org = await seed_org(api_env.admin_engine, "ssa-none")
    sub = await api_env.create_user(org.org_id)
    headers = await api_env.headers(org.org_id, sub)
    call = getattr(api_env.client, method)
    response = await (
        call(f"{PREFIX}{path_suffix}", headers=headers)
        if body is None
        else call(f"{PREFIX}{path_suffix}", json=body, headers=headers)
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Cross-tenant: 404, never 403
# --------------------------------------------------------------------------- #


async def test_another_orgs_site_server_is_a_404_on_every_route(api_env: Any) -> None:
    """404 AND NEVER 403: a 403 confirms the id exists, which is a cross-tenant
    existence oracle. Asserted over every route that takes an id, as a tuple, so
    one route answering differently is named."""
    _org_b, _device_b, foreign = await _org_with_site_server(api_env)
    org_a = await seed_org(api_env.admin_engine, "ssa-x")
    headers = await _headers(api_env, org_a.org_id, "org-admin")

    statuses = (
        (await api_env.client.get(f"{PREFIX}/{foreign}", headers=headers)).status_code,
        (
            await api_env.client.patch(
                f"{PREFIX}/{foreign}", json={"notes": "x"}, headers=headers
            )
        ).status_code,
        (await api_env.client.delete(f"{PREFIX}/{foreign}", headers=headers)).status_code,
        (
            await api_env.client.get(f"{PREFIX}/{foreign}/segment-check", headers=headers)
        ).status_code,
        (
            await api_env.client.post(
                f"{PREFIX}/{foreign}/wake",
                json={"target_mac": "a4:bb:6d:11:22:33"},
                headers=headers,
            )
        ).status_code,
    )
    assert statuses == (404, 404, 404, 404, 404)


async def test_promoting_another_orgs_device_is_a_404(api_env: Any) -> None:
    org_b = await seed_org(api_env.admin_engine, "ssa-y")
    await seed_agent(api_env.admin_engine, org_b.org_id, org_b.device_id)
    org_a = await seed_org(api_env.admin_engine, "ssa-z")
    headers = await _headers(api_env, org_a.org_id, "org-admin")

    response = await api_env.client.post(
        PREFIX,
        json={"device_id": str(org_b.device_id), "segments": ["10.20.0.0/16"]},
        headers=headers,
    )
    assert (
        response.status_code,
        await site_server_rows(api_env.admin_engine, org_a.org_id),
    ) == (404, [])


# --------------------------------------------------------------------------- #
# Request-model strictness and the publish boundary
# --------------------------------------------------------------------------- #


async def test_an_unknown_body_key_is_refused(api_env: Any) -> None:
    org = await seed_org(api_env.admin_engine, "ssa-extra")
    await seed_agent(api_env.admin_engine, org.org_id, org.device_id)
    headers = await _headers(api_env, org.org_id, "org-admin")
    response = await api_env.client.post(
        PREFIX,
        json={
            "device_id": str(org.device_id),
            "segments": ["10.20.0.0/16"],
            "broadcast": "10.20.255.255",
        },
        headers=headers,
    )
    assert (response.status_code, await site_server_rows(api_env.admin_engine, org.org_id)) == (
        422,
        [],
    )


async def test_a_cache_role_from_the_api_is_a_400(api_env: Any) -> None:
    org = await seed_org(api_env.admin_engine, "ssa-cache")
    await seed_agent(api_env.admin_engine, org.org_id, org.device_id)
    headers = await _headers(api_env, org.org_id, "org-admin")
    response = await api_env.client.post(
        PREFIX,
        json={
            "device_id": str(org.device_id),
            "segments": ["10.20.0.0/16"],
            "roles": ["wake", "cache"],
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "§4.4" in response.json()["detail"]


def test_no_route_publishes_inside_its_tenant_transaction() -> None:
    """A job published before the transaction commits could be handed to an
    agent describing state that rolled back — and this module's job puts a
    datagram on a LAN.

    Asserted from the AST of `api.py`: every `async with org_session(...)` block
    is searched for a `publish_job` call in its BODY. The positive control below
    is what makes the empty result evidence rather than an artefact of a walker
    that finds nothing.
    """
    import ast
    from pathlib import Path

    from app.modules.site_server import api as api_module

    def _publishing_org_session_blocks(source: str) -> list[Any]:
        return [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "org_session"
                for item in node.items
            )
            and "publish_job"
            in ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        ]

    assert _publishing_org_session_blocks(Path(api_module.__file__).read_text()) == []

    # POSITIVE CONTROL: a handler that DOES publish inside the block must be
    # found, or the assertion above is satisfied by a walker that never matches.
    bad = (
        "async def bad(ctx, request):\n"
        "    async with org_session(ctx.org_id, engine=e) as session:\n"
        "        job, envelope = await wake_via_site_server(session, je)\n"
        "        await publish_job(request.app.state.redis, d, envelope)\n"
    )
    assert len(_publishing_org_session_blocks(bad)) == 1

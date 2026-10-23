"""Managed distribution -- GPO/Intune.

`docs/modules/agent-distribution/spec-managed-distribution.md` §6.

THE ONE PROPERTY EVERYTHING ELSE HANGS OFF is that the deployment package
carries no credential. A GPO software-distribution point is read by every
machine account in the domain by design, so a token that reached the package
would be a permanent fleet-wide enrolment credential sitting on a share --
exactly what `agent/packaging/README.md` forbids ("A token inside a `.deb` is
copied to file servers, baked into golden images and cached by apt. One image
build and the whole fleet shares one token").

That is why the artefact and the credential are TWO ROUTES under TWO
PERMISSIONS, and why the first test below asserts the negative from both sides:
the response has no token in it, AND the call created no `enrollment_tokens`
row. A response-only assertion passes for a route that mints a token and
forgets to return it.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.client_scope import ClientScope
from app.core.rbac import route_permission
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken
from tests.conftest import ApiEnv

_RELEASE = {"version": "873d666", "os": "windows", "arch": "amd64", "signed": False, "notes": ""}


class _StubStore(ObjectStore):
    """Stands in for `ObjectStore` so no bucket is needed to build a package.

    SUBCLASSED, not duck-typed, for the reason the same stub in
    `test_agent_release_api.py` gives: `_object_store` asserts the cached
    object really is an `ObjectStore`, which is a production guard against
    something else being parked on `app.state` and not one to weaken for a
    test. `__init__` is deliberately not called -- constructing the real thing
    reaches for a bucket.
    """

    def __init__(self) -> None:  # noqa: D107 - see above
        pass

    def presign_agent_release_get(
        self, version: str, os_name: str, arch: str, *, expires_in: int
    ) -> str:
        return f"https://minio.test/agent-releases/{version}/its-agent-{os_name}-{arch}?sig=x"


async def _published(
    api_env: ApiEnv, org_id: UUID, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    """A release WITH an artefact, since a package for zero bytes is a 409."""
    from app.models.core import AgentRelease

    version = overrides.pop("version", f"v{uuid4().hex[:10]}")
    resp = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": version, **overrides},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    created: dict[str, Any] = resp.json()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        release = await session.get(AgentRelease, UUID(created["id"]))
        assert release is not None
        release.sha256 = "b" * 64
        release.size_bytes = 21_331_968
        await session.commit()
    api_env.app.state.object_store = _StubStore()
    return created



def _agent_route_permissions(app: Any) -> list[tuple[str, str | None]]:
    """`(full path, permission code)` for every route under `/api/v1/agent/`.

    `include_router` in this FastAPI appends a wrapper rather than flattening,
    so `app.routes` is not the list it looks like. Duplicated from
    `test_agent_release_api.py` rather than shared, following that file's own
    convention for these harness helpers -- and its note about the trailing
    slash applies here too: `/api/v1/agent` is also a prefix of the kernel's
    `/api/v1/agents/{device_id}/revoke`.
    """

    def flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
        found: list[tuple[str, Any]] = []
        for route in getattr(router, "routes", []):
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                found.extend(
                    flatten(original, prefix + (getattr(context, "prefix", "") or ""))
                )
            else:
                found.append((prefix + getattr(route, "path", ""), route))
        return found

    return [
        (path, route_permission(route))
        for path, route in flatten(app.router)
        if path.startswith("/api/v1/agent/")
    ]

# --------------------------------------------------------------------------
# Falsifier 1 -- the package carries a credential.
# --------------------------------------------------------------------------


async def test_the_deployment_package_carries_no_credential(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)

    resp = await api_env.client.get(
        f"/api/v1/agent/releases/{created['id']}/deployment-package?channel=gpo",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # No field that could be one, whatever it is called.
    assert "token" not in body
    assert "enroll_token" not in body
    # And nothing token-shaped smuggled into the rendered text. An enrollment
    # token is "<uuid>.<urlsafe-secret>"; the strings below legitimately talk
    # ABOUT the token, so this asserts on the environment-variable idiom being
    # the only way one ever appears.
    rendered = resp.text
    assert "ITS_AGENT_ENROLL_TOKEN=<" not in rendered

    # The half a response-only check cannot see: nothing was minted.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list((await session.execute(select(EnrollmentToken))).scalars())
    assert rows == [], "the package route minted an enrollment token"


async def test_the_package_route_does_not_require_enrollment_manage(
    api_env: ApiEnv,
) -> None:
    """The two halves are two permissions, and this is the half that proves it.

    `agent.release.read` is seeded to `org-admin` and the package is inert, so
    an operator who may not mint credentials may still get the artefact and the
    staging procedure. If this route were gated on `enrollment.manage` the
    split in §1.1 would be cosmetic.
    """
    routes = dict(_agent_route_permissions(api_env.app))
    package = routes["/api/v1/agent/releases/{release_id}/deployment-package"]
    token = routes["/api/v1/agent/releases/{release_id}/deployment-token"]
    assert package == "agent.release.read"
    assert token == "enrollment.manage"


async def test_both_new_routes_refuse_an_unauthenticated_caller(
    api_env: ApiEnv,
) -> None:
    release_id = uuid4()
    for method, path in (
        ("GET", f"/api/v1/agent/releases/{release_id}/deployment-package"),
        ("POST", f"/api/v1/agent/releases/{release_id}/deployment-token"),
    ):
        resp = await api_env.client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


# --------------------------------------------------------------------------
# Falsifiers 2 and 3 -- the credential's bounds.
# --------------------------------------------------------------------------


async def test_a_deployment_token_cannot_be_minted_unbounded(api_env: ApiEnv) -> None:
    """§3.2. `EnrollmentToken.max_uses` is NULLABLE and null means unlimited,
    and `POST /enrollment/tokens` defaults it to null -- so "forgot the
    argument" is the unbounded case everywhere else on this platform. This
    route cannot express it: both numbers are required and both are capped."""
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)
    path = f"/api/v1/agent/releases/{created['id']}/deployment-token"

    for body in (
        {},  # neither
        {"expires_in_hours": 24},  # no max_uses
        {"max_uses": 50},  # no expiry
        {"max_uses": None, "expires_in_hours": 24},  # null is not "unlimited"
        {"max_uses": 0, "expires_in_hours": 24},
        {"max_uses": 1001, "expires_in_hours": 24},  # over the ceiling
        {"max_uses": 50, "expires_in_hours": 337},  # over 14 days
        {"max_uses": 50, "expires_in_hours": 0},
    ):
        resp = await api_env.client.post(path, json=body, headers=headers)
        assert resp.status_code == 422, f"{body} -> {resp.status_code} {resp.text}"


async def test_a_minted_deployment_token_is_bounded_in_the_database(
    api_env: ApiEnv,
) -> None:
    """The stored row, not the response. A route that echoed the bounds back
    while writing null would pass every assertion made on its own output."""
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)

    resp = await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/deployment-token",
        json={"max_uses": 300, "expires_in_hours": 72},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_uses"] == 300
    assert body["token"]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list((await session.execute(select(EnrollmentToken))).scalars())
    assert len(rows) == 1
    assert rows[0].max_uses == 300, "max_uses is null: this token is unlimited"
    assert rows[0].expires_at is not None


async def test_minting_a_deployment_token_requires_enrollment_manage(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    reader = await api_env.create_user(org_id, "read-only")
    created = await _published(api_env, org_id, await api_env.headers(org_id, admin))

    resp = await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/deployment-token",
        json={"max_uses": 10, "expires_in_hours": 24},
        headers=await api_env.headers(org_id, reader),
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------
# Falsifier 4 -- the token in argv.
# --------------------------------------------------------------------------


async def test_no_generated_invocation_puts_the_token_in_argv(
    api_env: ApiEnv,
) -> None:
    """`spec.md` §6.4's rule, applied to the two new strings.

    The managed path multiplies the stakes: an install invocation carrying
    `-Token <value>` would be run by every machine in the domain, and Windows
    records process command lines in Security event 4688 where command-line
    auditing is on.
    """
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)

    for os_name, channel in (("windows", "gpo"), ("windows", "intune"), ("linux", "manual")):
        created = await _published(api_env, org_id, headers, os=os_name)
        resp = await api_env.client.get(
            f"/api/v1/agent/releases/{created['id']}/deployment-package?channel={channel}",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        install, enrol = body["install_command"], body["enrol_command"]
        for text in (install, enrol):
            assert "-Token " not in text
            assert "--token " not in text
            assert "--token=" not in text
        # Positively, and this is the half that stops the assertions above
        # passing over a string that simply never mentions a token: the enrol
        # step must use one of the three idioms the shipped helpers accept, all
        # of which keep the value out of argv.
        #
        #   ITS_AGENT_ENROLL_TOKEN=  the environment (both platforms)
        #   -TokenFromStdin          PowerShell's stdin form
        #   --token-stdin            the Linux helper's stdin form
        #
        # `--token-stdin` is a switch and carries no value, which is exactly why
        # `"--token "` and `"--token="` above do not match it.
        assert any(
            idiom in enrol
            for idiom in ("ITS_AGENT_ENROLL_TOKEN", "TokenFromStdin", "--token-stdin")
        ), enrol
        # And the INSTALL step never mentions the token, because installing and
        # enrolling are two steps on this path (§1).
        assert "ITS_AGENT_ENROLL_TOKEN" not in install


# --------------------------------------------------------------------------
# Falsifier 6 -- tenant independence.
# --------------------------------------------------------------------------


async def test_the_deployment_package_is_identical_for_two_orgs(
    api_env: ApiEnv,
) -> None:
    """§1.1 and `spec.md` §6.3. The artefact is one catalogue for every tenant;
    a package that differed by org would mean a per-org artefact, which is the
    shape a baked-in token has."""
    org_a = await api_env.create_org("managed-a")
    org_b = await api_env.create_org("managed-b")
    admin_a = await api_env.create_user(org_a, "super-admin")
    admin_b = await api_env.create_user(org_b, "super-admin")
    headers_a = await api_env.headers(org_a, admin_a)
    headers_b = await api_env.headers(org_b, admin_b)

    created = await _published(api_env, org_a, headers_a)
    path = f"/api/v1/agent/releases/{created['id']}/deployment-package?channel=gpo"

    a = (await api_env.client.get(path, headers=headers_a)).json()
    b = (await api_env.client.get(path, headers=headers_b)).json()

    assert a["sha256"] == b["sha256"]
    assert a["install_command"] == b["install_command"]
    assert a["enrol_command"] == b["enrol_command"]
    for org_id in (org_a, org_b):
        assert str(org_id) not in a["install_command"]
        assert str(org_id) not in a["download_url"]


# --------------------------------------------------------------------------
# Falsifier 8 -- the integrity claim.
# --------------------------------------------------------------------------


async def test_the_gpo_package_does_not_claim_install_time_verification(
    api_env: ApiEnv,
) -> None:
    """§4. GPO copies a file from a share and runs it; it verifies no digest.
    The console must say the endpoint-side check does NOT happen here, and it
    is asserted on the server-rendered text so the console cannot drift from
    it. The manual channel, where a human runs the command, keeps it."""
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)
    base = f"/api/v1/agent/releases/{created['id']}/deployment-package"

    gpo = (await api_env.client.get(f"{base}?channel=gpo", headers=headers)).json()
    assert gpo["integrity"]["verified_at_install"] is False
    joined = " ".join(gpo["integrity"]["notes"])
    # The staging comparison exists and is named...
    assert "Get-FileHash" in gpo["integrity"]["verify_command"]
    # ...and the missing one is named too, rather than left to be assumed.
    assert "distribution point" in joined
    assert "unsigned" in joined.lower()

    # Non-vacuity: a channel where a human runs the command DOES verify, so the
    # assertion above is about the channel and not about the field always being
    # false.
    manual = (await api_env.client.get(f"{base}?channel=manual", headers=headers)).json()
    assert manual["integrity"]["verified_at_install"] is True


async def test_a_deployment_package_is_refused_before_the_bytes_exist(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    resp = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": f"v{uuid4().hex[:10]}"},
        headers=headers,
    )
    created = resp.json()

    got = await api_env.client.get(
        f"/api/v1/agent/releases/{created['id']}/deployment-package?channel=gpo",
        headers=headers,
    )
    assert got.status_code == 409, got.text


async def test_an_unknown_channel_is_refused(api_env: ApiEnv) -> None:
    """Not accepted-and-ignored. The channel decides the staging steps and the
    integrity statement, so a typo that fell back to a default would hand an
    Intune administrator the GPO integrity accounting."""
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)

    resp = await api_env.client.get(
        f"/api/v1/agent/releases/{created['id']}/deployment-package?channel=sccm",
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# Invariant 4.
# --------------------------------------------------------------------------


async def test_both_acts_are_audited(api_env: ApiEnv) -> None:
    """The pairing `spec.md` §5 requires: an auditor must be able to answer
    "who was handed the ability to enrol devices, for which artefact, and how
    many" -- which for a fleet credential is a question with a number in it."""
    org_id = await api_env.create_org("managed")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _published(api_env, org_id, headers)

    await api_env.client.get(
        f"/api/v1/agent/releases/{created['id']}/deployment-package?channel=intune",
        headers=headers,
    )
    await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/deployment-token",
        json={"max_uses": 25, "expires_in_hours": 48},
        headers=headers,
    )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent.action, AuditEvent.meta).order_by(
                        AuditEvent.id
                    )
                )
            ).all()
        )
    actions = [action for action, _ in rows]
    assert "agent.deployment_package_generated" in actions
    assert "agent.deployment_token_generated" in actions
    assert "enrollment.token_created" in actions

    minted = next(meta for action, meta in rows if action == "agent.deployment_token_generated")
    assert minted["max_uses"] == 25
    assert minted["token_id"]
    # The token itself is never in the log.
    assert "token" not in minted

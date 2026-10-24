"""Agent release routes (docs/modules/agent-distribution/spec.md §4.3, §6).

THE AUTHORIZATION TESTS COME FIRST, and that ordering is the point. Every other
table in this schema carries `org_id` and is covered by an RLS policy, so a
forgotten application-level check still fails closed at the database. This table
carries neither, deliberately -- so the `require_permission` on each route is the
ONLY control between a caller and the bytes every endpoint in the fleet will
execute. There is no second line here.

AND SINCE MIGRATION 0070, THE CALLER WHO PUBLISHES IS THE PLATFORM OPERATOR,
NOT A TENANT'S OWN ADMINISTRATOR. `agent_releases` is one global catalogue
(`tests/test_tenant_column_sweep.py`'s `GLOBAL_BY_NATURE`), so `agent.release.
manage` writes the bytes EVERY tenant's endpoints will execute -- 0060 seeded it
to `org-admin`, which on a multi-tenant deployment let any customer's
administrator publish the binary every other customer's fleet runs. 0070 moves
it to `super-admin` and leaves `agent.release.read` where it is, which is why
almost every test below now publishes as `super-admin` and reads as either.

The other tests are the spec's §6 falsifiers, one apiece.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select

from app.api.deps import AuthContext, require_permission
from app.core.client_scope import ClientScope
from app.core.rbac import route_permission
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent
from tests.conftest import ApiEnv

_RELEASE = {
    "version": "873d666",
    "os": "linux",
    "arch": "amd64",
    "signed": False,
    "notes": "first build",
}


async def _publish(
    api_env: ApiEnv, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    """Publish a release with a version unique to this call.

    A FIXED version here made these tests pass alone and fail together, and the
    reason is a real property rather than a harness quirk: `agent_releases` is
    global, so the unique (version, os, arch) constraint is shared by every
    test -- and, in a deployment, by every ORG. See
    `test_the_catalogue_is_shared_across_orgs_by_design`.
    """
    version = overrides.pop("version", f"v{uuid4().hex[:10]}")
    resp = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": version, **overrides},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


# --------------------------------------------------------------------------
# Authorization. Nothing underneath these.
# --------------------------------------------------------------------------


async def test_every_route_refuses_an_unauthenticated_caller(api_env: ApiEnv) -> None:
    release_id = uuid4()
    for method, path in (
        ("GET", "/api/v1/agent/releases"),
        ("POST", "/api/v1/agent/releases"),
        ("POST", f"/api/v1/agent/releases/{release_id}/artifact"),
        ("DELETE", f"/api/v1/agent/releases/{release_id}"),
        ("POST", f"/api/v1/agent/releases/{release_id}/install-command"),
    ):
        resp = await api_env.client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


async def test_publishing_requires_manage_and_reading_does_not(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    reader = await api_env.create_user(org_id, "read-only")

    # read-only holds neither agent.release.read nor .manage today, so it is
    # refused on both -- the assertion that matters is that the WRITE is
    # refused for a principal who does not hold `agent.release.manage`.
    resp = await api_env.client.post(
        "/api/v1/agent/releases",
        json=_RELEASE,
        headers=await api_env.headers(org_id, reader),
    )
    assert resp.status_code == 403, resp.text

    ok = await api_env.client.post(
        "/api/v1/agent/releases",
        json=_RELEASE,
        headers=await api_env.headers(org_id, admin),
    )
    assert ok.status_code == 201, ok.text


async def test_deleting_requires_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    reader = await api_env.create_user(org_id, "read-only")
    created = await _publish(api_env, await api_env.headers(org_id, admin))

    denied = await api_env.client.delete(
        f"/api/v1/agent/releases/{created['id']}",
        headers=await api_env.headers(org_id, reader),
    )
    assert denied.status_code == 403, denied.text


async def test_an_org_admin_cannot_publish_and_a_super_admin_can(
    api_env: ApiEnv,
) -> None:
    """THE REGRESSION TEST FOR MIGRATION 0070, ASSERTED FROM BOTH SIDES.

    One side alone would be worthless. "org-admin gets 403" passes just as
    happily if the write routes were deleted, gated on a code nobody holds, or
    simply broken; "super-admin gets 201" passes just as happily if the grant
    had been left on `org-admin` too. Together they say the boundary MOVED
    rather than closed: the tenant's administrator is out, the platform
    operator is in.

    All three write routes, because all three take `agent.release.manage` and
    all three change what the fleet will execute -- registering a version,
    putting bytes behind it, and unpublishing one that endpoints are still
    being handed.

    WHY THIS IS A BOUNDARY AT ALL. `agent_releases` carries no `org_id`: it is
    one catalogue every tenant shares (spec §4.2). So on a multi-tenant
    deployment 0060's grant meant any customer's own administrator could
    publish, replace or withdraw the binary every OTHER customer's endpoints
    run -- code execution across the tenant boundary, on every managed machine,
    from a role that ships to every tenant by default.
    """
    org_id = await api_env.create_org("agentrel-boundary")
    tenant_admin = await api_env.headers(
        org_id, await api_env.create_user(org_id, "org-admin")
    )
    operator = await api_env.headers(
        org_id, await api_env.create_user(org_id, "super-admin")
    )

    refused = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": f"v{uuid4().hex[:10]}"},
        headers=tenant_admin,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "requires agent.release.manage"

    created = await _publish(api_env, operator)

    refused_upload = await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/artifact",
        content=b"not the fleet's bytes",
        headers=tenant_admin,
    )
    assert refused_upload.status_code == 403, refused_upload.text
    assert refused_upload.json()["detail"] == "requires agent.release.manage"

    refused_delete = await api_env.client.delete(
        f"/api/v1/agent/releases/{created['id']}", headers=tenant_admin
    )
    assert refused_delete.status_code == 403, refused_delete.text
    assert refused_delete.json()["detail"] == "requires agent.release.manage"

    # And the operator really can withdraw it, so the 403s above are about WHO
    # is asking rather than about a route that refuses everybody.
    allowed_delete = await api_env.client.delete(
        f"/api/v1/agent/releases/{created['id']}", headers=operator
    )
    assert allowed_delete.status_code == 204, allowed_delete.text


async def test_an_org_admin_can_still_read_the_release_catalogue(
    api_env: ApiEnv,
) -> None:
    """THE OTHER HALF OF 0070, AND IT IS DELIBERATE RATHER THAN AN OVERSIGHT.

    `agent.release.read` stays with `org-admin`. 0060's docstring is explicit
    about why: reading the catalogue is what anyone who INSTALLS an agent does,
    because generating an install command is a read of the catalogue plus a
    token mint. Taking read away with manage would leave every tenant unable to
    onboard a machine without the platform operator, which closes nothing --
    the bytes are published by the operator either way.

    Asserted positively, and against a release the operator published, so this
    cannot pass on an empty list.
    """
    org_id = await api_env.create_org("agentrel-read")
    operator = await api_env.headers(
        org_id, await api_env.create_user(org_id, "super-admin")
    )
    tenant_admin = await api_env.headers(
        org_id, await api_env.create_user(org_id, "org-admin")
    )

    published = await _publish(api_env, operator)

    listed = await api_env.client.get("/api/v1/agent/releases", headers=tenant_admin)
    assert listed.status_code == 200, listed.text
    assert published["id"] in [row["id"] for row in listed.json()]


# --------------------------------------------------------------------------
# Spec §6 falsifiers
# --------------------------------------------------------------------------


async def test_the_server_computes_the_digest_and_will_not_take_one(
    api_env: ApiEnv,
) -> None:
    """§6.2. A digest supplied with the bytes by the same party verifies nothing.

    The body model is `extra="forbid"`, so an attempt to set it is a 422 rather
    than a silently ignored field -- an operator who thinks they pinned a digest
    and did not is worse off than one who got an error.
    """
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)

    rejected = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "sha256": "0" * 64},
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text


async def test_a_version_that_could_escape_the_prefix_is_a_422_not_a_500(
    api_env: ApiEnv,
) -> None:
    """§6 (traversal). `version` is operator-supplied and this table has no
    tenant scope, so a `..` version would address another module's prefix.
    It is refused where the operator can see and fix it."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    resp = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": "../../backups/other-org"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422, resp.text


async def test_an_install_command_is_refused_before_the_bytes_exist(
    api_env: ApiEnv,
) -> None:
    """A token and a URL for an object nobody uploaded fails at the far end of a
    copy/paste, on the endpoint, where it is most expensive to diagnose."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    created = await _publish(api_env, headers)

    resp = await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/install-command",
        json={"expires_in_hours": 4, "site_id": str(org_id)},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text


async def test_a_release_reports_whether_its_artifact_exists(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    created = await _publish(api_env, await api_env.headers(org_id, admin))
    assert created["has_artifact"] is False
    assert created["sha256"] == ""


async def test_publishing_writes_an_audit_event(api_env: ApiEnv) -> None:
    """Invariant #4. Publishing is the act that decides what the fleet runs."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    await _publish(api_env, await api_env.headers(org_id, admin))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        actions = [
            row
            for row in (
                await session.execute(select(AuditEvent.action).order_by(AuditEvent.id))
            ).scalars()
        ]
    assert "agent.release_published" in actions


async def test_two_artefacts_for_one_version_os_arch_are_refused(
    api_env: ApiEnv,
) -> None:
    """The uniqueness constraint, through the route.

    An install command names exactly one object; with two rows nothing in the
    data chooses between them.
    """
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    first = await _publish(api_env, headers)

    duplicate = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": first["version"]},
        headers=headers,
    )
    # 409, not a 500. The constraint fired as an unhandled IntegrityError
    # before this test existed -- a database traceback where an operator
    # re-running a publish script needs a sentence.
    assert duplicate.status_code == 409, duplicate.text


async def test_a_release_for_a_different_arch_is_allowed(api_env: ApiEnv) -> None:
    """Non-vacuity for the test above: the constraint is on the TRIPLE."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    headers = await api_env.headers(org_id, admin)
    first = await _publish(api_env, headers)
    other = await _publish(api_env, headers, version=first["version"], arch="arm64")
    assert other["arch"] == "arm64"
    assert other["version"] == first["version"]


async def test_the_catalogue_is_shared_across_orgs_by_design(api_env: ApiEnv) -> None:
    """One org's release is visible to another, and blocks the same triple.

    This is the consequence of `agent_releases` having no `org_id`
    (spec §4.2), and it is recorded here rather than discovered later. The
    artefact bytes are identical for every tenant, so a shared catalogue is the
    point -- but two things follow that a single-org deployment will never
    notice:

      * every tenant READS one catalogue, so a release published for anyone is
        listed for everyone;
      * the uniqueness constraint is global, so a version X for linux/amd64
        published once cannot be published again under that name for a
        different tenant's build.

    THE THIRD CONSEQUENCE USED TO BE THE DANGEROUS ONE, and it is gone. This
    docstring read "an org-admin in ONE tenant can publish and unpublish
    artefacts every other tenant's endpoints will run" -- written as an
    acknowledged residual, which is exactly what it was: `agent.release.manage`
    sat with `org-admin` from 0060 until 0070 moved it to `super-admin`. Only
    the platform operator writes this catalogue now, which is the shape a
    shared, tenantless table needed all along.
    """
    org_a = await api_env.create_org("agentrel-a")
    org_b = await api_env.create_org("agentrel-b")
    operator_a = await api_env.create_user(org_a, "super-admin")
    admin_b = await api_env.create_user(org_b, "org-admin")
    operator_b = await api_env.create_user(org_b, "super-admin")

    published = await _publish(api_env, await api_env.headers(org_a, operator_a))

    # Read is still a tenant's own code (0060, and 0070 left it there), so
    # org B's OWN administrator sees org A's release.
    listed = await api_env.client.get(
        "/api/v1/agent/releases", headers=await api_env.headers(org_b, admin_b)
    )
    assert listed.status_code == 200, listed.text
    assert published["id"] in [row["id"] for row in listed.json()]

    # The collision is asserted through a caller who is ALLOWED to publish --
    # otherwise the 403 that org B's administrator now gets would satisfy this
    # assertion without the constraint ever being reached.
    collision = await api_env.client.post(
        "/api/v1/agent/releases",
        json={**_RELEASE, "version": published["version"]},
        headers=await api_env.headers(org_b, operator_b),
    )
    assert collision.status_code == 409, collision.text


# --------------------------------------------------------------------------
# The install command itself (spec §6.4 and §6.5).
#
# WHY THESE DO NOT UPLOAD BYTES. The install-command route needs a release with
# a digest and a presigned URL, and this suite has no object store -- there is
# no MinIO behind `api_env`, which is why every other test here stops at the
# catalogue. So the digest is written straight onto the row and the presign is
# stubbed. What that buys is the route's OWN behaviour: the token it mints and
# the string it returns. What it does not touch is the presign, which
# `test_objectstore_agent_release.py` covers separately.
# --------------------------------------------------------------------------


class _StubStore(ObjectStore):
    """Stands in for `ObjectStore` so no bucket is needed to mint a command.

    SUBCLASSED, not duck-typed, for two reasons. `_object_store` asserts the
    cached object really is an `ObjectStore` -- a production guard against
    something else being parked on `app.state`, and not one to weaken for a
    test. And an override keeps the type checker comparing this signature
    against the real one, so a stub that quietly stops matching the method it
    stands in for goes red here rather than passing over a shape that no longer
    exists.

    `__init__` is deliberately not called: constructing the real thing reaches
    for a bucket.
    """

    def __init__(self) -> None:  # noqa: D107 - see above
        pass

    def presign_agent_release_get(
        self, version: str, os_name: str, arch: str, *, expires_in: int
    ) -> str:
        return f"https://minio.test/agent-releases/{version}/its-agent-{os_name}-{arch}?sig=x"


async def _publish_with_artifact(
    api_env: ApiEnv, org_id: UUID, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    """Publish, then fake the upload by writing the digest the server would."""
    from app.models.core import AgentRelease

    created = await _publish(api_env, headers, **overrides)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        release = await session.get(AgentRelease, UUID(created["id"]))
        assert release is not None
        release.sha256 = "a" * 64
        release.size_bytes = 18_874_368
        await session.commit()
    api_env.app.state.object_store = _StubStore()
    return created


async def _mint(
    api_env: ApiEnv, org_id: UUID, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    created = await _publish_with_artifact(api_env, org_id, headers, **overrides)
    resp = await api_env.client.post(
        f"/api/v1/agent/releases/{created['id']}/install-command",
        json={"expires_in_hours": 4, "site_id": str(org_id)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_an_install_command_mints_a_single_use_token(api_env: ApiEnv) -> None:
    """§6.5. The token is a literal in a command an operator pastes, so it
    reaches their shell history. Single-use and short-lived is what makes that
    a residual rather than a standing credential -- and `max_uses` is NULLABLE
    on this table, meaning "unlimited", so a forgotten argument is unlimited
    rather than a crash."""
    from app.models import EnrollmentToken

    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(api_env, org_id, await api_env.headers(org_id, admin))

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (await session.execute(select(EnrollmentToken))).scalars()
        )
    assert len(rows) == 1, rows
    assert rows[0].max_uses == 1
    assert rows[0].expires_at is not None
    # And it is served exactly once, in this response, like every other
    # enrollment token here.
    assert body["token"]
    assert body["token_expires_at"]


async def test_the_install_command_never_puts_the_token_in_argv(
    api_env: ApiEnv,
) -> None:
    """§6.4. The shipped install scripts refuse `--token` precisely so the
    token cannot be read out of a process listing or a shell's argv. A
    generated command that reintroduced it would defeat them from the other
    side, and the operator pasting it would have no way to know."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(api_env, org_id, await api_env.headers(org_id, admin))

    command, token = body["command"], body["token"]
    # The token value is never an OPTION VALUE. `--token-stdin` is a flag with
    # no argument; what is forbidden is the token appearing after `--token`,
    # `--url` or `--binary`.
    assert f"--token {token}" not in command
    assert f"--token={token}" not in command
    assert f"--url {token}" not in command
    assert f"--binary {token}" not in command
    # Positively: the token reaches the installer on stdin, which is exactly the
    # form install.sh accepts and the form that never lands in argv.
    assert f"printf '%s' '{token}' | sudo bash" in command
    assert "--token-stdin" in command


async def test_the_linux_install_command_installs_and_starts_a_service(
    api_env: ApiEnv,
) -> None:
    """The defect this cycle fixes: the old command placed a bare binary and
    never registered a systemd service. The new one runs the reviewed
    install.sh -- which writes the hardened unit and enables and starts it --
    and carries the unit alongside so install.sh finds it. It must NOT be the
    old shape that leaves an un-registered service."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(api_env, org_id, await api_env.headers(org_id, admin))

    command = body["command"]
    # Runs the real installer with the binary it just downloaded.
    assert 'sudo bash "$d/install.sh"' in command
    assert '--binary "$d/its-agent"' in command
    # Carries the unit the installer needs, next to the script.
    assert '> "$d/its-agent.service"' in command
    # The control-plane URL the agent enrols against -- absent from the old
    # command, so the token had nowhere to be spent.
    assert "--url 'http://api.test'" in command
    # And NOT the old shape: no bare binary placement standing in for an install.
    assert "install -m755 /tmp/its-agent /usr/local/bin/its-agent" not in command


async def test_the_command_carries_the_exact_reviewed_installer(
    api_env: ApiEnv,
) -> None:
    """The command must run the SAME install.sh that packaging_test.go checks,
    not a re-derivation of it. Assert the base64 it carries decodes to the
    bundled installer bytes, which `test_installer_scripts.py` pins to
    `agent/packaging/`."""
    import base64

    from app.modules.agent_distribution.service import _installer_bytes

    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(api_env, org_id, await api_env.headers(org_id, admin))

    sh_b64 = base64.b64encode(_installer_bytes("linux/install.sh")).decode("ascii")
    unit_b64 = base64.b64encode(
        _installer_bytes("linux/its-agent.service")
    ).decode("ascii")
    assert sh_b64 in body["command"]
    assert unit_b64 in body["command"]


async def test_a_windows_install_command_also_keeps_the_token_out_of_argv(
    api_env: ApiEnv,
) -> None:
    """The Windows branch is a different string built by different code, so the
    Linux assertion above says nothing about it. The token reaches the installer
    through the environment variable install.ps1 reads (never argv), and the
    command spawns the real install.ps1 rather than assuming it is already on
    the machine."""
    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(
        api_env, org_id, await api_env.headers(org_id, admin), os="windows"
    )

    command, token = body["command"], body["token"]
    # Token is not an argument, on either spelling.
    assert "-Token" not in command
    assert "--token" not in command
    # It reaches install.ps1 through the environment it inherits.
    assert f"$env:ITS_AGENT_ENROLL_TOKEN='{token}'" in command
    assert token in command  # non-vacuity: the command really does carry it
    # The hash check no longer depends on Get-FileHash, which the operator hit
    # as "not recognized" and which is absent on older shells.
    assert "Get-FileHash" not in command
    assert "Security.Cryptography.SHA256" in command
    # It runs the real installer, with -Binary and -Url as (non-secret)
    # arguments -- not the old bare `.\install.ps1` that was never downloaded.
    assert "-File $ps1 -Binary $exe -Url 'http://api.test'" in command
    assert ".\\install.ps1" not in command


async def test_a_windows_command_carries_the_exact_reviewed_installer(
    api_env: ApiEnv,
) -> None:
    """The Windows counterpart of the Linux integrity test: the base64 the
    command carries decodes to the reviewed install.ps1, so the ACL and
    New-Service steps are the ones packaging_test.go checks, not a re-derivation
    this CI cannot even parse."""
    import base64

    from app.modules.agent_distribution.service import _installer_bytes

    org_id = await api_env.create_org("agentrel")
    admin = await api_env.create_user(org_id, "super-admin")
    body = await _mint(
        api_env, org_id, await api_env.headers(org_id, admin), os="windows"
    )
    ps1_b64 = base64.b64encode(_installer_bytes("windows/install.ps1")).decode("ascii")
    assert ps1_b64 in body["command"]


# --------------------------------------------------------------------------
# Invariant #6 completeness, for THIS module.
#
# Found by planting a dropped `require_permission` on the publish route: the
# test above went red, and every existing completeness sweep stayed green --
# 25 passed. Those sweeps are per-module (`test_every_notification_route_...`,
# the site-server one, the SLA one), so a NEW module is covered by none of them
# until it brings its own. A module whose table has no RLS underneath it is the
# worst one to leave uncovered, so this is that sweep.
# --------------------------------------------------------------------------

AGENT_PREFIX = "/api/v1/agent"


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper rather
    than flattening; descend through `.original_router` and the prefix it was
    mounted under. Duplicated from `tests/test_sla_api.py` rather than shared,
    following that file's own convention for these harness helpers.
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


def _agent_routes(app: Any) -> list[tuple[str, Any]]:
    return [
        (path, route)
        for path, route in _flatten(app.router)
        # The trailing slash matters: `/api/v1/agent` is also a prefix of the
        # kernel's `/api/v1/agents/{device_id}/revoke`, which belongs to
        # enrollment, not this module. The equality assertion below is what
        # surfaced that -- a `>=` would have quietly counted six.
        if path.startswith(f"{AGENT_PREFIX}/")
    ]


async def test_every_agent_release_route_carries_a_permission(api_env: ApiEnv) -> None:
    """Invariant #6, derived from the router rather than a hand-kept list."""
    routes = _agent_routes(api_env.app)
    # Exactly the seven this module registers -- an equality, not a `>=`, which
    # cannot fail when a route goes missing. Five for the catalogue, plus the
    # two managed-distribution routes
    # (`docs/modules/agent-distribution/spec-managed-distribution.md` §2), whose
    # own permissions are pinned individually in
    # `test_agent_managed_deployment.py` because the SPLIT between them is the
    # design: the package route must NOT take `enrollment.manage` and the token
    # route must. Plus fourteen for self-update (spec-self-update.md §8.3):
    # twelve rollout routes and the two catalogue writes (promote, signature).
    # Plus one: remove a whole version at once (2026-09-24).
    assert len(routes) == 22, f"found {len(routes)} agent routes: {[p for p, _ in routes]}"
    for path, route in routes:
        assert route_permission(route) is not None, f"{path} has no permission"


async def test_the_completeness_check_would_fail_for_an_ungated_route() -> None:
    """The mutation proof, in-repo rather than only in a commit message.

    Built as a throwaway app rather than by editing the shipped router, and it
    asserts the INVERSE too -- a gated route on the same app is not flagged --
    because "returns None for everything" is exactly how this reflection fails
    when `require_permission`'s closure shape changes.
    """
    mutant = APIRouter()

    @mutant.get("/agent/ungated")
    async def _ungated() -> dict[str, str]:  # pragma: no cover -- never called
        return {}

    @mutant.get("/agent/gated")
    async def _gated(
        ctx: AuthContext = Depends(require_permission("agent.release.read")),  # noqa: B008
    ) -> dict[str, str]:  # pragma: no cover -- never called
        return {}

    app = FastAPI()
    app.include_router(mutant, prefix="/api/v1")

    found = {path: route_permission(route) for path, route in _agent_routes(app)}
    assert found == {
        f"{AGENT_PREFIX}/ungated": None,
        f"{AGENT_PREFIX}/gated": "agent.release.read",
    }

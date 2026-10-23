"""`GET`/`PATCH /api/v1/orgs/current` -- the only write path to `organizations`.

The invariants under test are #4 (audit), #5 (tenancy, at the DATABASE) and #6
(server-side RBAC), plus the JSONB allowlist that keeps an operator-writable
blob from becoming an unbounded store or a namespace a future reader trusts.

TWO SEPARATE TENANCY TESTS, AND THEY ARE NOT REDUNDANT. `organizations` carries
an RLS policy (migration 0002, `USING (id = current_org)`), so a cross-org test
run through `org_session` on the app engine passes EVEN IF the route's own
`WHERE id = ctx.org_id` predicate is deleted -- HANDOFF.md §4 ("RLS masks
missing application predicates"). So:

  * `test_the_route_predicate_scopes_the_org_without_rls_to_mask_it` repoints
    `app.state.app_engine` at the ADMIN (BYPASSRLS) engine and drives the REAL
    routes through it. Nothing but the application predicate is left standing.
  * `test_rls_refuses_a_cross_org_read_and_write_at_the_database` goes the
    other way: no route at all, an explicit `WHERE id = <other org>` through a
    tenant session, so the application predicate cannot be what refuses it.

Between them, deleting either guard turns one of the two red.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.v1.orgs import (
    MAX_SETTINGS_BYTES,
    NEVER_ORG_SETTINGS,
    SECRET_SHAPED_FRAGMENTS,
    SETTINGS_ALLOWLIST,
)
from app.core.client_scope import ClientScope
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.core.tenancy import org_session
from app.models import Organization


async def _org_row(engine: AsyncEngine, org_id: UUID) -> Organization:
    """Read an org through the ADMIN engine -- the assertions about what a
    request did NOT touch must not themselves be filtered by RLS."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


async def test_read_returns_the_callers_own_org_and_not_another(api_env: Any) -> None:
    mine = await api_env.create_org("orgs-mine")
    theirs = await api_env.create_org("orgs-theirs")
    reader = await api_env.create_user(mine, "read-only")
    headers = await api_env.headers(mine, reader)

    response = await api_env.client.get("/api/v1/orgs/current", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(mine)
    assert body["id"] != str(theirs)
    assert body["slug"] == await api_env.org_slug(mine)
    assert body["status"] == "active"
    assert body["settings"] == {}


async def test_read_requires_authentication(api_env: Any) -> None:
    assert (await api_env.client.get("/api/v1/orgs/current")).status_code == 401


# --------------------------------------------------------------------------- #
# RBAC (invariant #6)
# --------------------------------------------------------------------------- #


async def test_patch_without_orgs_manage_is_403_and_changes_nothing(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-rbac")
    before = (await _org_row(api_env.admin_engine, org_id)).name

    for role in ("read-only", "technician", "auditor"):
        user = await api_env.create_user(org_id, role)
        headers = await api_env.headers(org_id, user)
        response = await api_env.client.patch(
            "/api/v1/orgs/current", json={"name": f"escalated via {role}"}, headers=headers
        )
        assert response.status_code == 403, f"{role}: {response.status_code} {response.text}"

    # a user with no roles at all
    nobody = await api_env.create_user(org_id)
    refused = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"name": "escalated"},
        headers=await api_env.headers(org_id, nobody),
    )
    assert refused.status_code == 403

    # The gate held, not just the status code: the row is untouched.
    assert (await _org_row(api_env.admin_engine, org_id)).name == before

    # And the same body from an org-admin DOES land -- otherwise every
    # assertion above is consistent with a route that refuses everyone.
    admin = await api_env.create_user(org_id, "org-admin")
    allowed = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"name": "Renamed by the admin"},
        headers=await api_env.headers(org_id, admin),
    )
    assert allowed.status_code == 200, allowed.text
    assert (await _org_row(api_env.admin_engine, org_id)).name == "Renamed by the admin"


# --------------------------------------------------------------------------- #
# Tenancy (invariant #5)
# --------------------------------------------------------------------------- #


async def test_patch_cannot_touch_another_org_even_with_an_id_in_the_body(
    api_env: Any,
) -> None:
    mine = await api_env.create_org("orgs-attacker")
    theirs = await api_env.create_org("orgs-victim")
    theirs_before = await _org_row(api_env.admin_engine, theirs)
    admin = await api_env.create_user(mine, "org-admin")
    headers = await api_env.headers(mine, admin)

    for key in ("id", "org_id", "organization_id"):
        response = await api_env.client.patch(
            "/api/v1/orgs/current",
            json={key: str(theirs), "name": "Pwned", "settings": {"timezone": "Etc/UTC"}},
            headers=headers,
        )
        # Refused outright rather than silently ignored: a 200 for a body that
        # named a target and did not hit it reads like success.
        assert response.status_code == 422, f"{key}: {response.status_code} {response.text}"

    # The victim is byte-identical.
    theirs_after = await _org_row(api_env.admin_engine, theirs)
    assert (theirs_after.name, theirs_after.settings) == (
        theirs_before.name,
        theirs_before.settings,
    )

    # NOT VACUOUS: the same request minus the id field takes effect on the
    # caller's own org, so "nothing happened to the victim" is not merely
    # "nothing happened".
    ok = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"name": "Mine", "settings": {"timezone": "Etc/UTC"}},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert (await _org_row(api_env.admin_engine, mine)).name == "Mine"
    still_theirs = await _org_row(api_env.admin_engine, theirs)
    assert still_theirs.name == theirs_before.name
    assert still_theirs.settings == theirs_before.settings


def test_no_route_takes_an_organization_id(api_env: Any) -> None:
    """The structural half of the tenancy guarantee: there is no `/orgs/{id}`
    for a future authorization check to get wrong.

    ROUTE DISCOVERY GOES THROUGH `flatten`, NOT `app.routes`. In this FastAPI,
    `include_router` appends a single lazy `_IncludedRouter` whose own `.path`
    is `None`, so a naive prefix filter over `app.routes` yields an empty list
    FOREVER and this assertion would pass over nothing -- which is exactly what
    it did on its first run here, before the flattener went in.
    """
    all_paths = [path for path, _ in flatten(api_env.app.router)]
    # Non-vacuity: the flattener really is returning routes.
    assert "/api/v1/me" in all_paths

    org_paths = sorted({path for path in all_paths if path.startswith("/api/v1/orgs")})
    # `console-domain-preview` was added 2026-08-19 (invisible-identity-layer
    # design §2.2). It hangs off `/current` and takes no id, so the claim this
    # test makes is unchanged -- the literal below is the inventory, and the
    # assertion under it is the rule.
    assert org_paths == [
        "/api/v1/orgs/current",
        "/api/v1/orgs/current/console-domain-preview",
    ]
    assert not any("{" in path for path in org_paths)


def test_the_two_org_routes_carry_the_intended_gates(api_env: Any) -> None:
    """`GET` is authenticated-only by design; `PATCH` is `orgs.manage`.

    Read off the live dependency tree (`route_permission` walks it), so a
    handler that lost its `Depends(require_permission(...))` is caught here as
    well as by the 403 test above.
    """
    gates = {
        (method, path): route_permission(route)
        for path, route in flatten(api_env.app.router)
        if path == "/api/v1/orgs/current"
        for method in sorted(getattr(route, "methods", set()))
    }
    assert gates == {
        ("GET", "/api/v1/orgs/current"): None,
        ("PATCH", "/api/v1/orgs/current"): "orgs.manage",
    }


async def test_the_route_predicate_scopes_the_org_without_rls_to_mask_it(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """Drive the REAL routes with `app.state.app_engine` repointed at the
    BYPASSRLS admin engine, so the application's own `WHERE id = ctx.org_id`
    is the only thing scoping the query (HANDOFF.md §4).

    Deleting that predicate from either handler turns this red: with several
    orgs in the shared test database, an unpredicated `select(Organization)`
    fails `scalar_one_or_none()` outright.
    """
    victim = await api_env.create_org("orgs-norls-victim")
    mine = await api_env.create_org("orgs-norls-mine")
    victim_before = await _org_row(admin_engine, victim)
    admin = await api_env.create_user(mine, "org-admin")
    headers = await api_env.headers(mine, admin)

    original_engine = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        read = await api_env.client.get("/api/v1/orgs/current", headers=headers)
        assert read.status_code == 200, read.text
        assert read.json()["id"] == str(mine)

        write = await api_env.client.patch(
            "/api/v1/orgs/current",
            json={"name": "Renamed with RLS out of the way"},
            headers=headers,
        )
        assert write.status_code == 200, write.text
        assert write.json()["id"] == str(mine)
    finally:
        api_env.app.state.app_engine = original_engine

    assert (await _org_row(admin_engine, mine)).name == "Renamed with RLS out of the way"
    victim_after = await _org_row(admin_engine, victim)
    assert victim_after.name == victim_before.name
    assert victim_after.settings == victim_before.settings


async def test_rls_refuses_a_cross_org_read_and_write_at_the_database(
    api_env: Any, app_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    """No route, no service: a tenant session pinned to org A, naming org B's
    row EXPLICITLY. Only the database can refuse this."""
    a = await api_env.create_org("orgs-rls-a")
    b = await api_env.create_org("orgs-rls-b")

    async with org_session(a, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            await session.execute(select(Organization.id).where(Organization.id == b))
        ).scalar_one_or_none()
        assert seen is None, "a tenant session read another org's row"

        # `UPDATE ... WHERE id = <other org>` names the victim; the only thing
        # that can make it a no-op is the RLS policy.
        result = await session.execute(
            text("UPDATE organizations SET name = 'rls breach' WHERE id = :id"),
            {"id": str(b)},
        )
        assert result.rowcount == 0

        # NOT VACUOUS: the same statement against the session's OWN org does
        # affect a row, so rowcount 0 above is the policy and not a broken
        # statement.
        own = await session.execute(
            text("UPDATE organizations SET name = 'own row' WHERE id = :id"),
            {"id": str(a)},
        )
        assert own.rowcount == 1

    assert (await _org_row(admin_engine, b)).name != "rls breach"
    assert (await _org_row(admin_engine, a)).name == "own row"


# --------------------------------------------------------------------------- #
# The settings allowlist
# --------------------------------------------------------------------------- #


async def test_an_unknown_settings_key_is_422_and_writes_nothing(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-unknown")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"timezone": "Etc/UTC", "job_signing_key": "hunter2"}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert "unknown settings key" in response.text

    # Refused as a whole: the valid key alongside it did not land either.
    assert (await _org_row(api_env.admin_engine, org_id)).settings == {}

    # And the same body without the unknown key is accepted, so the 422 is
    # about the key and not about the route.
    ok = await api_env.client.patch(
        "/api/v1/orgs/current", json={"settings": {"timezone": "Etc/UTC"}}, headers=headers
    )
    assert ok.status_code == 200, ok.text
    assert (await _org_row(api_env.admin_engine, org_id)).settings == {"timezone": "Etc/UTC"}


async def test_an_oversized_settings_blob_is_422(api_env: Any) -> None:
    """The SIZE gate specifically -- the key is allowlisted, so this cannot be
    the unknown-key path answering, and the message is asserted to prove it."""
    org_id = await api_env.create_org("orgs-oversize")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    payload = "x" * (MAX_SETTINGS_BYTES * 4)
    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"console_banner": payload}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "bytes" in detail and str(MAX_SETTINGS_BYTES) in detail
    assert "unknown settings key" not in detail
    assert (await _org_row(api_env.admin_engine, org_id)).settings == {}


async def test_settings_values_are_type_and_bound_checked(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-types")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    bad_bodies: list[dict[str, Any]] = [
        {"timezone": 42},  # wrong type
        {"show_device_serials": "yes"},  # wrong type
        {"default_page_size": True},  # bool is not an int here
        {"default_page_size": 5000},  # out of bounds
        {"date_format": "swatch-beats"},  # not a choice
        {"console_banner_severity": "apocalyptic"},  # not a choice
        {"brand_primary_color": "expression(alert(1))"},  # not #RRGGBB
        {"support_email": "e" * 500},  # over the per-key length
    ]
    for settings in bad_bodies:
        response = await api_env.client.patch(
            "/api/v1/orgs/current", json={"settings": settings}, headers=headers
        )
        assert response.status_code == 422, f"{settings}: {response.status_code}"

    assert (await _org_row(api_env.admin_engine, org_id)).settings == {}

    good = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={
            "settings": {
                "timezone": "Europe/London",
                "show_device_serials": False,
                "default_page_size": 50,
                "date_format": "iso",
                "console_banner_severity": "warning",
                "brand_primary_color": "#0A7E3F",
                "support_email": "help@example.test",
            }
        },
        headers=headers,
    )
    assert good.status_code == 200, good.text
    assert (await _org_row(api_env.admin_engine, org_id)).settings == {
        "timezone": "Europe/London",
        "show_device_serials": False,
        "default_page_size": 50,
        "date_format": "iso",
        "console_banner_severity": "warning",
        "brand_primary_color": "#0A7E3F",
        "support_email": "help@example.test",
    }


async def test_patch_merges_and_null_clears(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-merge")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"timezone": "Etc/UTC", "locale": "en-GB"}},
        headers=headers,
    )
    merged = await api_env.client.patch(
        "/api/v1/orgs/current", json={"settings": {"locale": "fr-FR"}}, headers=headers
    )
    assert merged.json()["settings"] == {"timezone": "Etc/UTC", "locale": "fr-FR"}

    cleared = await api_env.client.patch(
        "/api/v1/orgs/current", json={"settings": {"locale": None}}, headers=headers
    )
    assert cleared.json()["settings"] == {"timezone": "Etc/UTC"}

    # A name-only PATCH leaves settings alone.
    renamed = await api_env.client.patch(
        "/api/v1/orgs/current", json={"name": "Still configured"}, headers=headers
    )
    assert renamed.json()["settings"] == {"timezone": "Etc/UTC"}
    assert renamed.json()["name"] == "Still configured"


async def test_slug_and_status_are_not_writable(api_env: Any) -> None:
    """The org's slug is its identity in the OIDC claim; `status` is a
    platform lifecycle field. Neither may move through this route."""
    org_id = await api_env.create_org("orgs-immutable")
    before = await _org_row(api_env.admin_engine, org_id)
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    for field, value in (("slug", "hijacked"), ("status", "suspended")):
        response = await api_env.client.patch(
            "/api/v1/orgs/current", json={field: value}, headers=headers
        )
        assert response.status_code == 422, f"{field}: {response.status_code}"

    after = await _org_row(api_env.admin_engine, org_id)
    assert (after.slug, after.status) == (before.slug, before.status)


async def test_an_empty_patch_is_refused(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-empty")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    response = await api_env.client.patch("/api/v1/orgs/current", json={}, headers=headers)
    assert response.status_code == 422
    assert "no fields supplied" in response.text


def test_the_allowlist_holds_nothing_that_belongs_in_the_environment_or_vault() -> None:
    """Invariant #3, enforced against a FUTURE edit rather than against today's
    list: a key that shadows an environment- or vault-owned secret would make
    `orgs.manage` -- an org-admin grant -- a fleet-level privilege."""
    assert set(SETTINGS_ALLOWLIST) & NEVER_ORG_SETTINGS == set()
    offenders = {
        key
        for key in SETTINGS_ALLOWLIST
        if any(fragment in key.lower() for fragment in SECRET_SHAPED_FRAGMENTS)
    }
    assert offenders == set(), f"secret-shaped settings key(s): {sorted(offenders)}"

    # The guard is not vacuous: it does fire on a name of the shape it exists
    # to catch.
    assert any(fragment in "job_signing_key" for fragment in SECRET_SHAPED_FRAGMENTS)
    assert "openbao_token" in NEVER_ORG_SETTINGS


# --------------------------------------------------------------------------- #
# Audit (invariant #4)
# --------------------------------------------------------------------------- #


async def test_a_write_appears_in_the_audit_log_with_the_chain_intact(api_env: Any) -> None:
    org_id = await api_env.create_org("orgs-audit")
    admin = await api_env.create_user(org_id, "org-admin", "auditor")
    headers = await api_env.headers(org_id, admin)

    before = await api_env.client.get("/api/v1/audit?action=org.updated", headers=headers)
    assert before.json()["total"] == 0

    response = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={
            "name": "Audited Ltd",
            "settings": {"timezone": "Etc/UTC", "support_phone": "+44 20 7946 0000"},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text

    events = await api_env.client.get("/api/v1/audit?action=org.updated", headers=headers)
    assert events.json()["total"] == 1
    event = events.json()["items"][0]
    assert event["actor_type"] == "user"
    assert event["target_type"] == "organization"
    assert event["target_id"] == str(org_id)
    meta = event["metadata"]
    assert meta["fields"] == ["name", "settings"]
    assert meta["name_from"] != meta["name_to"] == "Audited Ltd"
    assert meta["settings_set"] == ["support_phone", "timezone"]
    assert meta["settings_cleared"] == []

    # VALUES ARE NOT IN THE LOG. The audit log is append-only, so a value
    # written here can never be redacted -- keys answer "who changed what".
    serialized = str(meta)
    assert "+44 20 7946 0000" not in serialized
    assert "Etc/UTC" not in serialized

    verify = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True
    assert verify.json()["broken_at"] is None


async def test_a_refused_write_is_not_audited(api_env: Any) -> None:
    """A rejected PATCH must not leave an `org.updated` row behind claiming a
    change that never happened."""
    org_id = await api_env.create_org("orgs-audit-refused")
    admin = await api_env.create_user(org_id, "org-admin", "auditor")
    headers = await api_env.headers(org_id, admin)

    refused = await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"settings": {"not_a_real_key": 1}},
        headers=headers,
    )
    assert refused.status_code == 422

    events = await api_env.client.get("/api/v1/audit?action=org.updated", headers=headers)
    assert events.json()["total"] == 0


async def test_an_audited_write_is_scoped_to_the_writers_org(api_env: Any) -> None:
    mine = await api_env.create_org("orgs-audit-mine")
    theirs = await api_env.create_org("orgs-audit-theirs")
    admin = await api_env.create_user(mine, "org-admin")
    await api_env.client.patch(
        "/api/v1/orgs/current",
        json={"name": "Mine only"},
        headers=await api_env.headers(mine, admin),
    )

    other_auditor = await api_env.create_user(theirs, "auditor")
    events = await api_env.client.get(
        "/api/v1/audit?action=org.updated",
        headers=await api_env.headers(theirs, other_auditor),
    )
    assert events.json()["total"] == 0


async def test_a_patch_for_a_deleted_org_is_404_not_a_crash(api_env: Any) -> None:
    """Belt and braces on the `scalar_one_or_none()` path: a token whose org
    no longer exists is refused at authentication, so the handler's own 404 is
    unreachable in practice -- assert the observable, not the branch."""
    slug = f"gone-{uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {api_env.idp.token('nobody', slug)}"}
    response = await api_env.client.get("/api/v1/orgs/current", headers=headers)
    assert response.status_code == 401

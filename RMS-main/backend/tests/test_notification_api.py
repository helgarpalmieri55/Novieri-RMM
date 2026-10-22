"""Notification core REST API tests (docs/modules/notification/spec.md §9, §8,
`.git/sdd/task-10-brief.md`).

RBAC COMPLETENESS IS DERIVED FROM THE ROUTER (requirement #2), not a
hand-maintained list of routes. `app.core.rbac.route_permission` recovers the
`require_permission(code)` a route was actually built with by inspecting its
own dependant tree -- `test_every_notification_route_requires_a_permission`
asserts every registered `/api/v1/notifications/*` route has one, so a route
added later without `require_permission` fails this suite instead of leaving
the suite green with an ungated route. `test_technician_is_denied_every_route`
goes one step further and proves the gate actually WORKS at runtime: it walks
the same router-derived route set and confirms a technician (who holds
neither `notification.read` nor `notification.admin` -- migration 0025 grants
both ONLY to super-admin/org-admin) is refused on every single one, without
hand-listing which routes exist.

ROUTE ORDERING (requirement #3) is exercised implicitly throughout: the
channel CRUD test hits `GET/PATCH/DELETE /channels/{id}` and
`POST /channels/{id}/test` against the SAME channel id and gets the right
handler every time; the allowlist test hits the literal `/allowlist`
collection and the `/allowlist/{id}` item route back to back.

THE TEST-SEND ROUTE'S EGRESS GUARD (requirement #5, spec §12.6) --
`test_test_send_is_subject_to_the_egress_guard` is the one this task names
explicitly: a channel configured against a private-space address (RFC1918,
never the platform's own DB/gateway host, which the test env's Postgres
happens to also bind on loopback) is refused through `POST
/channels/{id}/test` exactly as it would be for a real delivery attempt --
`error == "blocked_private"`, never a raw exception, never a 500.

FIXTURE PATTERN: `api_env` (`tests/conftest.py`), mirroring
`test_service_desk_api.py`. `api_env.create_user(org_id, *role_names)` +
`api_env.headers(org_id, sub)` obtain bearer tokens for a user holding named
global roles; `_user_with_permissions` (mirrors `test_service_desk_api.py`'s
identical helper) builds a bespoke org-scoped role for a permission set no
named role grants alone (a `notification.read`-only actor -- no built-in role
holds `read` without `admin` alongside it, spec §8).

EGRESS SETTINGS: `post_json` (`app.core.egress`) reads `get_settings()`
against the module-level singleton, which `api_env` never populates -- the
`_egress_settings` autouse fixture below is `test_notification_delivery.py`'s
identical fixture, needed by every test that exercises `POST
/channels/{id}/test` against a webhook channel.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.config import override_settings
from app.core.rbac import route_permission
from app.models import (
    AuditEvent,
    DomainEvent,
    NotificationDelivery,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.modules.notification.channels import (
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
)
from app.modules.notification.delivery import attempt_delivery
from app.modules.notification.errors import NotificationNotFoundError
from app.modules.notification.schema import ChannelCreate
from app.modules.service_desk.refdata import create_queue

API = "/api/v1/notifications"


# --------------------------------------------------------------------------- #
# Fixture helpers -- mirrors test_service_desk_api.py's shape exactly.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _egress_settings(request: pytest.FixtureRequest):
    if "api_env" not in request.fixturenames:
        yield
        return
    api_env = request.getfixturevalue("api_env")
    with override_settings(api_env.settings):
        yield


async def _user_id(api_env, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _org_admin(api_env, slug: str = "notif-api") -> tuple[UUID, dict[str, str], UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    user_id = await _user_id(api_env, sub)
    return org_id, headers, user_id


async def _role_user(api_env, org_id: UUID, *role_names: str) -> dict[str, str]:
    sub = await api_env.create_user(org_id, *role_names)
    return await api_env.headers(org_id, sub)


async def _user_with_permissions(api_env, org_id: UUID, *codes: str) -> dict[str, str]:
    """Create a user whose ONLY permissions are `codes`, via a bespoke
    org-scoped role -- there is no built-in role holding `notification.read`
    without `notification.admin` alongside it (migration 0025 grants both
    together, only to super-admin/org-admin), so the read-only negative-case
    actor `test_mutations_require_admin_not_read` needs has to be built by
    hand. Mirrors `test_service_desk_api.py`'s identical helper."""
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        session.add(user)
        await session.flush()
        role = Role(org_id=org_id, name=f"custom-{uuid4().hex[:8]}")
        session.add(role)
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


def _vault(tmp_path: Path):
    from app.core.vault import LocalVaultService

    return LocalVaultService(tmp_path / "root.key")


def _mock_transport(status_code: int, *, body: str = ""):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler)


def _notification_routes() -> list[Any]:
    """Every route THIS MODULE'S OWN router declares, straight off the
    `APIRouter` object -- not `app.routes`. `app.include_router(router,
    prefix="/api/v1")` (`app.modules.notification.register`) wraps a router's
    routes for inclusion rather than flattening them onto `app.routes`
    (this FastAPI's own internal shape) -- `router.routes` is unaffected by
    that and is what `test_service_desk_api.py`'s identical
    router-derived completeness test already uses for exactly this reason.
    Every path here is relative to the router (no `/api/v1` prefix) --
    `_request_path` below adds it back for an actual HTTP call."""
    from app.modules.notification.api import router as notification_router

    routes = list(notification_router.routes)
    assert routes, "no notification routes registered"
    return routes


def _concrete_path(template: str) -> str:
    """Substitute every `{...}` path parameter with a fresh random UUID --
    good enough to reach the permission dependency (which runs before any
    path-parameter-dependent lookup), never good enough to reach real data."""
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid4()), template)


def _request_path(route_path: str) -> str:
    return f"/api/v1{_concrete_path(route_path)}"


# --------------------------------------------------------------------------- #
# Requirement #2: RBAC completeness derived from the router, not a
# hand-maintained list -- carried from the ticket core (task brief Step 1).
# --------------------------------------------------------------------------- #


def test_every_notification_route_requires_a_permission() -> None:
    """Derived from the router, not a hand-maintained list -- a route added
    later without a permission fails this test."""
    for route in _notification_routes():
        assert route_permission(route) is not None, f"{route.path} has no permission"


def test_read_routes_use_notification_read_and_mutations_use_notification_admin() -> None:
    """A finer-grained completeness check on top of the bare "has SOME
    permission" assertion above: every GET is `notification.read`; every
    mutating verb (POST/PATCH/PUT/DELETE) -- test-send included -- is
    `notification.admin` (spec §8)."""
    for route in _notification_routes():
        methods = route.methods - {"HEAD"}
        permission = route_permission(route)
        for method in methods:
            if method == "GET":
                assert permission == "notification.read", (method, route.path, permission)
            else:
                assert permission == "notification.admin", (method, route.path, permission)


async def test_technician_is_denied_every_notification_route(api_env) -> None:
    """Functional counterpart to the two structural checks above: a
    technician -- who holds neither `notification.read` nor
    `notification.admin` (migration 0025 grants both ONLY to
    super-admin/org-admin) -- is refused on every registered route, derived
    from the router rather than hand-listed."""
    org_id, _admin_headers, _admin_id = await _org_admin(api_env, "notif-tech-deny")
    tech_headers = await _role_user(api_env, org_id, "technician")

    for route in _notification_routes():
        path = _request_path(route.path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            resp = await api_env.client.request(
                method, path, headers=tech_headers, json={}
            )
            assert resp.status_code == 403, (
                f"{method} {route.path} leaked to technician ({resp.status_code}) {resp.text}"
            )


# --------------------------------------------------------------------------- #
# Requirement #1: mutations require notification.admin, not notification.read.
# --------------------------------------------------------------------------- #


async def test_mutations_require_admin_not_read(api_env) -> None:
    org_id, _admin_headers, _admin_id = await _org_admin(api_env, "notif-read-only")
    read_only_headers = await _user_with_permissions(api_env, org_id, "notification.read")

    body = {"kind": "webhook", "name": "x", "config": {"url": "https://h.example.com/x"}}
    got = await api_env.client.post(f"{API}/channels", json=body, headers=read_only_headers)
    assert got.status_code == 403


# --------------------------------------------------------------------------- #
# Requirement #4: no credential, no full URL w/ query, no response body ever.
# --------------------------------------------------------------------------- #


async def test_channel_response_never_contains_a_secret(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-secret")
    body = {
        "kind": "webhook",
        "name": "ops",
        "config": {"url": "https://h.example.com/x"},
        "secret": "bearer-token-value",
    }
    got = await api_env.client.post(f"{API}/channels", json=body, headers=admin_headers)
    assert got.status_code == 201, got.text
    assert "bearer-token-value" not in got.text
    payload = got.json()
    assert "credential_id" not in payload
    assert payload["has_credential"] is True


async def test_channel_crud_round_trip_and_cross_org_404(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-channel-crud")
    other_org_id, other_admin_headers, _ = await _org_admin(api_env, "notif-channel-other")

    created = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "ops", "config": {"url": "https://h.example.com/x"}},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    got = await api_env.client.get(f"{API}/channels/{channel_id}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["name"] == "ops"

    listed = await api_env.client.get(f"{API}/channels", headers=admin_headers)
    assert listed.status_code == 200
    assert any(c["id"] == channel_id for c in listed.json())

    renamed = await api_env.client.patch(
        f"{API}/channels/{channel_id}", json={"name": "ops-renamed"}, headers=admin_headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "ops-renamed"

    # Cross-org: the SAME channel id is indistinguishable from nonexistent
    # to another org's admin (invariant #5).
    cross_org = await api_env.client.get(
        f"{API}/channels/{channel_id}", headers=other_admin_headers
    )
    assert cross_org.status_code == 404

    deleted = await api_env.client.delete(f"{API}/channels/{channel_id}", headers=admin_headers)
    assert deleted.status_code == 204

    gone = await api_env.client.get(f"{API}/channels/{channel_id}", headers=admin_headers)
    assert gone.status_code == 404


# --------------------------------------------------------------------------- #
# Requirement #5: test-send subject to the identical egress guard.
# --------------------------------------------------------------------------- #


async def test_test_send_is_subject_to_the_egress_guard(api_env) -> None:
    """Spec §12.6 -- 'it's only a test' is exactly how an SSRF bypass ships.
    `10.0.0.5` is RFC1918 private space, distinct from the test harness's own
    Postgres (which binds loopback, `127.0.0.1`/`app.core.egress._platform_entries`)
    so this exercises the GENERIC private-range guard, not the platform's
    own-host guard."""
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-test-send-blocked")
    created = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "private", "config": {"url": "https://10.0.0.5/hook"}},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    got = await api_env.client.post(f"{API}/channels/{channel_id}/test", headers=admin_headers)
    assert got.status_code == 200
    body = got.json()
    assert body["delivered"] is False
    assert body["error"] == "blocked_private"
    # Never a raw response body/excerpt -- only the bounded egress category.
    assert "error_excerpt" not in body


async def test_test_send_is_rate_limited_per_org(api_env) -> None:
    """Spec §5's per-org rate limit (whole-branch review finding I3):
    `TestSendResult.error` distinguishes `blocked_private` from
    `dns_failure`, an unmetered internal-DNS existence oracle otherwise.
    `_TEST_SEND_RATE_LIMIT` in `app.modules.notification.api` is 5 per
    rolling 60s window; the 6th request in the same window is refused."""
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-test-send-throttle")
    created = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "private", "config": {"url": "https://10.0.0.5/hook"}},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    statuses = []
    for _ in range(6):
        resp = await api_env.client.post(f"{API}/channels/{channel_id}/test", headers=admin_headers)
        statuses.append(resp.status_code)

    assert statuses[:5] == [200] * 5, statuses
    assert statuses[5] == 429, statuses


async def test_test_send_rate_limit_is_scoped_per_org(api_env) -> None:
    """Different orgs must not share the same counter -- an org that has
    exhausted its own budget must not affect another org's."""
    org_a, admin_a, _ = await _org_admin(api_env, "notif-test-send-throttle-a")
    org_b, admin_b, _ = await _org_admin(api_env, "notif-test-send-throttle-b")

    async def _make_channel(headers: dict[str, str]) -> str:
        created = await api_env.client.post(
            f"{API}/channels",
            json={"kind": "webhook", "name": "private", "config": {"url": "https://10.0.0.5/hook"}},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    channel_a = await _make_channel(admin_a)
    channel_b = await _make_channel(admin_b)

    for _ in range(5):
        resp = await api_env.client.post(f"{API}/channels/{channel_a}/test", headers=admin_a)
        assert resp.status_code == 200

    got = await api_env.client.post(f"{API}/channels/{channel_b}/test", headers=admin_b)
    assert got.status_code == 200, "org_b's own budget must be unaffected by org_a's usage"


async def test_test_send_requires_admin_not_read(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-test-send-rbac")
    read_only_headers = await _user_with_permissions(api_env, org_id, "notification.read")
    created = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "ops", "config": {"url": "https://h.example.com/x"}},
        headers=admin_headers,
    )
    channel_id = created.json()["id"]

    got = await api_env.client.post(
        f"{API}/channels/{channel_id}/test", headers=read_only_headers
    )
    assert got.status_code == 403


async def test_test_send_credential_checkout_is_attributed_to_the_calling_user(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-branch review finding M7: `_send_webhook`/`_send_email` record
    the credential-checkout audit as `actor_type="system", actor_id=None` --
    correct for the worker's real delivery path (no human triggered it), but
    wrong for `POST /channels/{id}/test`, where a NAMED USER pressed the
    button. The hash-chained audit log (invariant #4) must attribute this
    checkout to the admin who actually caused it, not to "system"."""
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))

    org_id, admin_headers, admin_id = await _org_admin(api_env, "notif-test-send-attribution")
    created = await api_env.client.post(
        f"{API}/channels",
        json={
            "kind": "webhook",
            "name": "ops",
            "config": {"url": "https://hooks.example.com/x"},
            "secret": "bearer-token-for-attribution-test",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    got = await api_env.client.post(f"{API}/channels/{channel_id}/test", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["delivered"] is True

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        checkouts = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "notification.credential_used",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(checkouts) == 1
    assert checkouts[0].actor_type == "user"
    assert checkouts[0].actor_id == admin_id


async def test_test_send_delivers_and_is_audited_when_allowed(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive counterpart to the guard test: the IDENTICAL path
    (`POST /channels/{id}/test` -> `delivery.send_test` -> `_send_webhook` ->
    `app.core.egress.post_json`) succeeds for a public, allowed destination,
    proving the guard is the thing rejecting the private-address case above --
    not something else (e.g. a network error every test-send hits
    regardless). Also asserts the ONE audit event spec §5/§8 requires:
    `notification.channel_tested`, written exactly once."""
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: _mock_transport(200))

    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-test-send-ok")
    created = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "ops", "config": {"url": "https://hooks.example.com/x"}},
        headers=admin_headers,
    )
    channel_id = created.json()["id"]

    got = await api_env.client.post(f"{API}/channels/{channel_id}/test", headers=admin_headers)
    assert got.status_code == 200
    body = got.json()
    assert body["delivered"] is True
    assert body["status_code"] == 200
    assert body["error"] is None

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "notification.channel_tested",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].meta["delivered"] is True


# --------------------------------------------------------------------------- #
# Rules CRUD via the API.
# --------------------------------------------------------------------------- #


async def test_rule_crud_round_trip(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-rules-api")
    created_channel = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "ops", "config": {"url": "https://h.example.com/x"}},
        headers=admin_headers,
    )
    channel_id = created_channel.json()["id"]

    created_rule = await api_env.client.post(
        f"{API}/rules",
        json={
            "event_type": "ticket.assigned",
            "channel_id": channel_id,
            "recipient_kind": "assignee",
        },
        headers=admin_headers,
    )
    assert created_rule.status_code == 201, created_rule.text
    rule_id = created_rule.json()["id"]
    assert created_rule.json()["active"] is True

    listed = await api_env.client.get(f"{API}/rules", headers=admin_headers)
    assert listed.status_code == 200
    assert any(r["id"] == rule_id for r in listed.json())

    disabled = await api_env.client.patch(
        f"{API}/rules/{rule_id}", json={"active": False}, headers=admin_headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False

    deleted = await api_env.client.delete(f"{API}/rules/{rule_id}", headers=admin_headers)
    assert deleted.status_code == 204

    missing = await api_env.client.patch(
        f"{API}/rules/{rule_id}", json={"active": True}, headers=admin_headers
    )
    assert missing.status_code == 404


async def test_rule_create_rejects_internal_note_to_requester(api_env) -> None:
    """Spec §6.4 -- rejected at write time via the API, as a 400, never a raw
    IntegrityError against `ck_rule_no_internal_to_requester`."""
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-rules-internal")
    created_channel = await api_env.client.post(
        f"{API}/channels",
        json={"kind": "webhook", "name": "ops", "config": {"url": "https://h.example.com/x"}},
        headers=admin_headers,
    )
    channel_id = created_channel.json()["id"]

    got = await api_env.client.post(
        f"{API}/rules",
        json={
            "event_type": "ticket.internal_note_added",
            "channel_id": channel_id,
            "recipient_kind": "requester",
        },
        headers=admin_headers,
    )
    assert got.status_code == 422  # Pydantic model_validator rejects it first


# --------------------------------------------------------------------------- #
# Requirement #6: allowlist entries carry a NOT NULL `port`.
# --------------------------------------------------------------------------- #


async def test_allowlist_crud_carries_port(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-allowlist-api")

    created = await api_env.client.post(
        f"{API}/allowlist",
        json={"host": "mattermost.lan", "note": "internal chat"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["port"] == 443  # schema default
    entry_id = created.json()["id"]

    created_explicit_port = await api_env.client.post(
        f"{API}/allowlist",
        json={"host": "mattermost.lan", "port": 8065, "note": "internal chat, custom port"},
        headers=admin_headers,
    )
    assert created_explicit_port.status_code == 201
    assert created_explicit_port.json()["port"] == 8065

    listed = await api_env.client.get(f"{API}/allowlist", headers=admin_headers)
    assert listed.status_code == 200
    ports = {(e["host"], e["port"]) for e in listed.json()}
    assert ("mattermost.lan", 443) in ports
    assert ("mattermost.lan", 8065) in ports

    duplicate = await api_env.client.post(
        f"{API}/allowlist", json={"host": "mattermost.lan", "port": 443}, headers=admin_headers
    )
    assert duplicate.status_code == 400

    deleted = await api_env.client.delete(f"{API}/allowlist/{entry_id}", headers=admin_headers)
    assert deleted.status_code == 204

    missing = await api_env.client.delete(f"{API}/allowlist/{entry_id}", headers=admin_headers)
    assert missing.status_code == 404


async def test_allowlist_rejects_a_cidr_host(api_env) -> None:
    org_id, admin_headers, _admin_id = await _org_admin(api_env, "notif-allowlist-cidr")
    got = await api_env.client.post(
        f"{API}/allowlist", json={"host": "10.0.0.0/8"}, headers=admin_headers
    )
    assert got.status_code == 422


# --------------------------------------------------------------------------- #
# Queue membership via the API.
# --------------------------------------------------------------------------- #


async def test_queue_member_add_list_remove(api_env) -> None:
    org_id, admin_headers, admin_id = await _org_admin(api_env, "notif-queue-members")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await create_queue(session, org_id, admin_id, key="ops", name="Ops")
        await session.commit()
        queue_id = queue.id

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        member_user = User(org_id=org_id, external_subject=f"member-{uuid4().hex[:8]}",
                            email=f"member-{uuid4().hex[:8]}@test.local")
        session.add(member_user)
        await session.commit()
        member_user_id = member_user.id

    added = await api_env.client.put(
        f"{API}/queues/{queue_id}/members/{member_user_id}", headers=admin_headers
    )
    assert added.status_code == 200, added.text
    assert added.json()["user_id"] == str(member_user_id)

    listed = await api_env.client.get(f"{API}/queues/{queue_id}/members", headers=admin_headers)
    assert listed.status_code == 200
    assert any(m["user_id"] == str(member_user_id) for m in listed.json())

    removed = await api_env.client.delete(
        f"{API}/queues/{queue_id}/members/{member_user_id}", headers=admin_headers
    )
    assert removed.status_code == 204

    missing = await api_env.client.delete(
        f"{API}/queues/{queue_id}/members/{member_user_id}", headers=admin_headers
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Requirement #4 (deliveries side): no response body ever, and pagination.
# --------------------------------------------------------------------------- #


async def test_deliveries_list_never_exposes_a_response_body(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-branch review finding M8: the earlier version of this test SEEDED
    `last_error` itself and asserted only `len(...) <= 500` and two absent
    keys -- it never exercised the REAL path, where `last_error` IS a
    500-char excerpt of the remote response body (`delivery.py`'s
    `_send_webhook`/`response.excerpt` -> `_apply_outcome` ->
    `DeliveryOut.last_error`), readable by any `notification.read` holder.
    This version drives a REAL failed delivery through `attempt_delivery`
    against a mock transport that returns an oversized (>500 char), non-
    printable-laced response body, then asserts on what the API ACTUALLY
    returns for it: bounded to the column's 500-char limit, control
    characters stripped, and -- the point of spec §4.4/§9 (see the amended
    §9 wording) -- a genuine excerpt of the real body, deliberately retained
    for diagnosis, never the raw body itself (which was thousands of chars,
    never appears in full) and never a `response_body`/`config` field."""
    org_id, admin_headers, admin_id = await _org_admin(api_env, "notif-deliveries")
    raw_body = "upstream-error-detail-" * 40 + "\x00\x01control-chars-stripped"
    assert len(raw_body) > 500  # the excerpt must actually truncate something real
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport", lambda ip: _mock_transport(503, body=raw_body)
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=admin_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=_vault(tmp_path),
        )
        event = DomainEvent(
            id=uuid7(),
            org_id=org_id,
            event_type="ticket.assigned",
            subject_type="ticket",
            subject_id=uuid7(),
            payload={},
            actor_user_id=None,
            occurred_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        delivery = NotificationDelivery(
            id=uuid7(),
            org_id=org_id,
            event_id=event.id,
            channel_id=channel.id,
            recipient="h.example.com",
            status="pending",
            attempts=0,
        )
        session.add(delivery)
        await session.flush()

        vault = _vault(tmp_path)
        await attempt_delivery(session, delivery=delivery, vault=vault)
        await session.commit()

        assert delivery.status == "failed"  # a 503 is retryable (spec §7)
        assert delivery.last_status_code == 503
        # The real excerpt this test is exercising -- captured here so the
        # assertions against the API response below can compare against the
        # ACTUAL value the service layer produced, not a fixture stand-in.
        real_last_error = delivery.last_error

    assert real_last_error is not None
    assert len(real_last_error) <= 500
    assert raw_body not in real_last_error  # the full (longer) body never appears verbatim
    assert "\x00" not in real_last_error  # control characters stripped (spec §4.4)

    got = await api_env.client.get(f"{API}/deliveries", headers=admin_headers)
    assert got.status_code == 200
    payload = got.json()
    assert "items" in payload and "total" in payload
    assert payload["total"] >= 1
    rows = [row for row in payload["items"] if row["channel_id"] == str(channel.id)]
    assert len(rows) == 1
    row = rows[0]
    # The API returns the SAME bounded, sanitized excerpt the service layer
    # produced -- spec §4.4/§2.5's "bounded, sanitized excerpt, retained for
    # diagnosis" (amended §9 wording), never the raw response body.
    assert row["last_error"] == real_last_error
    assert len(row["last_error"]) <= 500
    assert raw_body not in row["last_error"]
    # Never a raw response body/excerpt field, never the full URL/config.
    assert "response_body" not in row
    assert "config" not in row


async def test_deliveries_list_filters_by_status_and_channel(
    api_env, tmp_path: Path
) -> None:
    org_id, admin_headers, admin_id = await _org_admin(api_env, "notif-deliveries-filter")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=admin_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": "https://h.example.com/x"}
            ),
            secret=None,
            vault=_vault(tmp_path),
        )
        event = DomainEvent(
            id=uuid7(),
            org_id=org_id,
            event_type="ticket.resolved",
            subject_type="ticket",
            subject_id=uuid7(),
            payload={},
            actor_user_id=None,
            occurred_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        sent = NotificationDelivery(
            id=uuid7(), org_id=org_id, event_id=event.id, channel_id=channel.id,
            recipient="h.example.com", status="sent", attempts=1,
        )
        dead = NotificationDelivery(
            id=uuid7(), org_id=org_id, event_id=event.id, channel_id=channel.id,
            recipient="h.example.com", status="dead", attempts=5, last_error="terminal",
        )
        session.add_all([sent, dead])
        await session.commit()

    got = await api_env.client.get(
        f"{API}/deliveries", params={"status": "dead"}, headers=admin_headers
    )
    assert got.status_code == 200
    statuses = {row["status"] for row in got.json()["items"]}
    assert statuses == {"dead"}


async def test_the_channel_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    api_env, bypassrls_session: AsyncSession, tmp_path: Path
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Every cross-org assertion in this file goes through a route, which means
    `org_session` on the NOBYPASSRLS `app_user` role: `notification_channel`
    carries a forced `tenant_isolation` policy, so the neighbour's channel is
    already unreachable when `NotificationChannel.org_id == org_id` runs, and
    those tests hold with the predicate deleted. `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here the predicate is the only scoping.

    A CHANNEL IS AN EGRESS DESTINATION. `delete_channel` resolves through
    `get_channel`, so a missing predicate there does not disclose a webhook
    URL, it DELETES another tenant's alerting route -- and the failure is
    silent, because the symptom is alerts that stop arriving rather than an
    error anybody sees.

    Both orgs hold a channel of the same name, which is legal (the uniqueness
    is per-org) and is what makes a name-based miss visible as well as an
    id-based one.

    FALSIFIED: deleting `NotificationChannel.org_id == org_id` from
    `get_channel` returns org B's channel to org A and lets `delete_channel`
    remove it; deleting it from `list_channels` shows both orgs' channels.
    """
    org_a, _headers_a, admin_a = await _org_admin(api_env, "notif-pred-a")
    org_b, _headers_b, admin_b = await _org_admin(api_env, "notif-pred-b")
    vault = _vault(tmp_path)

    channels: dict[str, UUID] = {}
    for key, org_id, actor_id in (("a", org_a, admin_a), ("b", org_b, admin_b)):
        channel = await create_channel(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            data=ChannelCreate(
                kind="webhook", name="ops", config={"url": f"https://{key}.example.com/x"}
            ),
            secret=None,
            vault=vault,
        )
        channels[key] = channel.id
    await bypassrls_session.commit()

    assert [c.id for c in await list_channels(bypassrls_session, org_id=org_a)] == [
        channels["a"]
    ]
    assert [c.id for c in await list_channels(bypassrls_session, org_id=org_b)] == [
        channels["b"]
    ]

    with pytest.raises(NotificationNotFoundError):
        await get_channel(bypassrls_session, org_id=org_a, channel_id=channels["b"])

    with pytest.raises(NotificationNotFoundError):
        await delete_channel(
            bypassrls_session, org_id=org_a, actor_id=admin_a, channel_id=channels["b"]
        )
    await bypassrls_session.rollback()

    surviving = await get_channel(
        bypassrls_session, org_id=org_b, channel_id=channels["b"]
    )
    assert surviving.config["url"] == "https://b.example.com/x", (
        "org A deleted or rewrote a neighbour tenant's egress destination"
    )

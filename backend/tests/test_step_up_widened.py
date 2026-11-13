"""A recent second factor for the organization's sign-in settings and for a
technician's access (design §2.5, widened by the operator on 2026-11-08,
S1-S4, and on 2026-11-09, S5; plan 2026-11-06, Task 14).

Each act in `ACTS` needs the caller's own second factor confirmed in the last
ten minutes. Without one -- none in this session, one eleven minutes old, or a
session a remembered device started (design §2.6) -- it is the vault's 403
`step_up_required`: nothing changes and no audit row is written. With one it
goes through. Pinned as needing none: what only takes access away, the test
mail and a contact's acts (S4), renaming a role (S5), a client edit that
leaves its Microsoft tenants as they were, and switching AI access off. Over
MCP, an act gated as a whole is not a tool, and a tool gated in part refuses
that part.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp import exclusions
from app.mcp.catalog import build_catalog
from app.models import (
    AuditEvent,
    Client,
    Organization,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.models.auth import (
    AccountMailSettings,
    AuthActionToken,
    AuthEntraSettings,
    AuthOrgSettings,
    AuthSession,
)
from tests.auth_support import capture_mail, console_ready
from tests.entra_support import APP, TENANT
from tests.mcp_support import (
    MCP_BASE,
    marked_client,
    mcp_org,
    mcp_session,
    mcp_token,
    running_mcp,
    tool_error_text,
)
from tests.session_support import refusal_code, remembered_device, sign_in, user_id_of
from tests.tool_surface import build_router_only_app

#: What `deps.require_step_up` says, in the body and in an MCP tool's error.
CONFIRM_AGAIN = "confirm your second factor again to do this"
#: What `deps.require_all_clients` says to a holder of the code on fewer than
#: every client (`tests/test_global_writes.py::EVERY_CLIENT`).
EVERY_CLIENT = "only a technician with access to every client can do this"
#: The confirmations the refusals are asked of (design §2.5, §2.6).
REFUSED = ("none", "stale", "remembered")

EMAIL = {
    "host": "smtp.new.internal",
    "port": 465,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "",
    "encryption": "ssl",
    "username": "itms",
    "password": "s3cr3t-typed-once",
}
SESSIONS = {
    "staff_idle_minutes": 45,
    "staff_max_hours": 12,
    "remember_device_enabled": True,
    "remember_device_days": 15,
    "remembered_max_hours": 168,
    "contact_idle_minutes": 60,
    "contact_max_days": 7,
}
MICROSOFT = {"enabled": False, "app_client_id": str(APP), "staff_tenant_id": str(TENANT)}

Send = Callable[[dict[str, str]], Awaitable[httpx.Response]]
Read = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class Act:
    """One act design §2.5 covers since 2026-11-08. `prepare` builds what it
    acts on, with an administrator's fresh headers, and answers how to send
    the act and how to read what it would change."""

    name: str
    succeeds_with: int
    prepare: Callable[[Any, UUID, dict[str, str]], Awaitable[tuple[Send, Read]]]


@pytest.fixture()
def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`api_env` with the console's address and the account mail captured."""
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


@pytest.fixture()
def mcp_env(api_env: Any) -> Any:
    """`api_env` as a deployment whose MCP resource is `MCP_AUD`."""
    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def _admin(env: Any) -> tuple[UUID, str, dict[str, str]]:
    org = await env.create_org("step-up-widened")
    sub = await env.create_user(org, "org-admin")
    return org, sub, await env.headers(org, sub)


async def _confirmed(env: Any, org: UUID, sub: str, state: str) -> dict[str, str]:
    """A new session of `sub` whose last second factor is `state`: `fresh`
    (just now, as a sign-in with the factor leaves it), `none`, `stale`
    (eleven minutes ago) or `remembered` (a remembered device stood in for
    it, which confirms nothing: design §2.6)."""
    if state == "remembered":
        device = await remembered_device(env, org, sub)
        signed = await sign_in(env, org, sub, trusted_device_id=device)
    else:
        signed = await sign_in(env, org, sub)
    if state in ("none", "stale"):
        at = None if state == "none" else datetime.now(UTC) - timedelta(minutes=11)
        async with AsyncSession(env.admin_engine) as s:
            await s.execute(
                update(AuthSession).where(AuthSession.id == signed.session_id).values(step_up_at=at)
            )
            await s.commit()
    async with AsyncSession(env.admin_engine) as s:
        at = await s.scalar(
            select(AuthSession.step_up_at).where(AuthSession.id == signed.session_id)
        )
    recent = at is not None and datetime.now(UTC) - at < timedelta(minutes=10)
    assert recent == (state == "fresh"), (state, at)
    return signed.headers


async def _audit_rows(env: Any, org: UUID) -> int:
    async with AsyncSession(env.admin_engine) as s:
        rows = await s.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.org_id == org)
        )
    return int(rows or 0)


async def _role(env: Any, name: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        role: UUID = (
            await s.execute(select(Role.id).where(Role.name == name, Role.org_id.is_(None)))
        ).scalar_one()
    return role


async def _invited(env: Any, admin: dict[str, str]) -> UUID:
    made = await env.client.post(
        "/api/v1/users/invite",
        headers=admin,
        json={
            "email": f"new-{uuid4().hex[:8]}@toeshee.com",
            "display_name": "Ana",
            "grants": [{"role_id": str(await _role(env, "technician"))}],
        },
    )
    assert made.status_code == 201, made.text
    return UUID(made.json()["user_id"])


async def _technician(env: Any, org: UUID) -> UUID:
    return await user_id_of(env, await env.create_user(org, "technician"))


async def _grants(env: Any, user_id: UUID) -> list[UUID]:
    async with AsyncSession(env.admin_engine) as s:
        return sorted(
            (await s.execute(select(UserRole.id).where(UserRole.user_id == user_id))).scalars(),
            key=str,
        )


async def _own_role(env: Any, admin: dict[str, str]) -> UUID:
    """A role of the organization's own, with no codes and nobody holding it,
    made with the administrator's fresh headers."""
    made = await env.client.post(
        "/api/v1/roles",
        headers=admin,
        json={"name": f"role-{uuid4().hex[:8]}", "description": "", "codes": []},
    )
    assert made.status_code == 201, made.text
    return UUID(made.json()["id"])


async def _role_codes(env: Any, role_id: UUID) -> list[str]:
    async with AsyncSession(env.admin_engine) as s:
        return sorted(
            (
                await s.execute(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id == role_id)
                )
            ).scalars()
        )


async def _customer(env: Any, admin: dict[str, str]) -> str:
    made = await env.client.post("/api/v1/clients", headers=admin, json={"name": "Acme"})
    assert made.status_code == 201, made.text
    client_id: str = made.json()["id"]
    return client_id


async def _tenants(env: Any, client_id: str) -> list[UUID]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            await s.scalar(select(Client.microsoft_tenants).where(Client.id == UUID(client_id)))
            or []
        )


async def _org_settings(env: Any, org: UUID) -> dict[str, Any]:
    async with AsyncSession(env.admin_engine) as s:
        return dict(
            await s.scalar(select(Organization.settings).where(Organization.id == org)) or {}
        )


async def _entra(env: Any, org: UUID) -> tuple[object, ...] | None:
    async with AsyncSession(env.admin_engine) as s:
        row = await s.get(AuthEntraSettings, org)
    if row is None:
        return None
    return (row.app_client_id, row.certificate_thumbprint, row.next_certificate_thumbprint)


# --------------------------------------------------------------------------- #
# The acts (S1, S3, S5)
# --------------------------------------------------------------------------- #


async def _email(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.put("/api/v1/settings/email", headers=headers, json=EMAIL)

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            return await s.get(AccountMailSettings, org) is not None

    return send, read


async def _sessions(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.put("/api/v1/settings/sessions", headers=headers, json=SESSIONS)

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            row = await s.get(AuthOrgSettings, org)
        return None if row is None else row.staff_idle_minutes

    return send, read


def _microsoft_save(
    body: dict[str, Any], *, before: tuple[dict[str, Any], ...] = ()
) -> Callable[[Any, UUID, dict[str, str]], Awaitable[tuple[Send, Read]]]:
    """`PUT /settings/microsoft-sign-in` with `body`, after the saves `before`
    (each with the administrator's fresh headers)."""

    async def prepare(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
        for earlier in before:
            saved = await env.client.put(
                "/api/v1/settings/microsoft-sign-in", headers=admin, json=earlier
            )
            assert saved.status_code == 200, saved.text

        async def send(headers: dict[str, str]) -> httpx.Response:
            return await env.client.put(
                "/api/v1/settings/microsoft-sign-in", headers=headers, json=body
            )

        async def read() -> object:
            return await _entra(env, org)

        return send, read

    return prepare


async def _tenant_change(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    client_id = await _customer(env, admin)

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.patch(
            f"/api/v1/clients/{client_id}",
            headers=headers,
            json={"microsoft_tenants": [str(TENANT)]},
        )

    async def read() -> object:
        return await _tenants(env, client_id)

    return send, read


async def _invite(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    email = f"new-{uuid4().hex[:8]}@toeshee.com"
    technician = await _role(env, "technician")

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.post(
            "/api/v1/users/invite",
            headers=headers,
            json={"email": email, "display_name": "Ana", "grants": [{"role_id": str(technician)}]},
        )

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            return await s.scalar(
                select(func.count())
                .select_from(User)
                .where(User.org_id == org, User.email == email)
            )

    return send, read


async def _resend(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    user_id = await _invited(env, admin)

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.post(f"/api/v1/users/{user_id}/invite/resend", headers=headers)

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            rows = await s.execute(
                select(AuthActionToken.id, AuthActionToken.used_at).where(
                    AuthActionToken.user_id == user_id
                )
            )
        return sorted((str(row.id), row.used_at) for row in rows)

    return send, read


async def _assign(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    target = await _technician(env, org)
    reader = await _role(env, "read-only")

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.post(
            f"/api/v1/users/{target}/roles", headers=headers, json={"role_id": str(reader)}
        )

    async def read() -> object:
        return await _grants(env, target)

    return send, read


async def _remove(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    target = await _technician(env, org)
    [assignment] = await _grants(env, target)

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.delete(
            f"/api/v1/users/{target}/roles/{assignment}", headers=headers
        )

    async def read() -> object:
        return await _grants(env, target)

    return send, read


async def _create_role(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    name = f"role-{uuid4().hex[:8]}"

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.post(
            "/api/v1/roles",
            headers=headers,
            json={"name": name, "description": "", "codes": ["devices.read"]},
        )

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            return await s.scalar(
                select(func.count()).select_from(Role).where(Role.org_id == org, Role.name == name)
            )

    return send, read


async def _role_permissions(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    role = await _own_role(env, admin)

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.put(
            f"/api/v1/roles/{role}/permissions", headers=headers, json={"codes": ["devices.read"]}
        )

    async def read() -> object:
        return await _role_codes(env, role)

    return send, read


async def _delete_role(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    role = await _own_role(env, admin)

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.delete(f"/api/v1/roles/{role}", headers=headers)

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            return await s.get(Role, role) is not None

    return send, read


async def _enable(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    target = await _technician(env, org)
    disabled = await env.client.post(f"/api/v1/users/{target}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text

    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.post(f"/api/v1/users/{target}/enable", headers=headers)

    async def read() -> object:
        async with AsyncSession(env.admin_engine) as s:
            return await s.scalar(select(User.status).where(User.id == target))

    return send, read


async def _ai_on(env: Any, org: UUID, admin: dict[str, str]) -> tuple[Send, Read]:
    async def send(headers: dict[str, str]) -> httpx.Response:
        return await env.client.patch(
            "/api/v1/orgs/current", headers=headers, json={"settings": {"mcp_enabled": True}}
        )

    async def read() -> object:
        return await _org_settings(env, org)

    return send, read


ACTS = (
    Act("email settings", 200, _email),
    Act("session settings", 200, _sessions),
    Act("microsoft settings", 200, _microsoft_save(MICROSOFT)),
    Act(
        "microsoft new certificate",
        200,
        _microsoft_save(
            {**MICROSOFT, "new_certificate": True},
            before=({**MICROSOFT, "new_certificate": True},),
        ),
    ),
    Act(
        "microsoft activate certificate",
        200,
        _microsoft_save(
            {**MICROSOFT, "activate_certificate": True},
            before=({**MICROSOFT, "new_certificate": True},) * 2,
        ),
    ),
    Act("a client's microsoft tenants", 200, _tenant_change),
    Act("invite a technician", 201, _invite),
    Act("resend an invitation", 200, _resend),
    Act("assign a role", 201, _assign),
    Act("remove a role", 204, _remove),
    Act("create a role", 201, _create_role),
    Act("change a role's permissions", 200, _role_permissions),
    Act("delete a role", 204, _delete_role),
    Act("re-enable a person", 200, _enable),
    Act("switch ai access on", 200, _ai_on),
)


@pytest.mark.parametrize("state", REFUSED)
@pytest.mark.parametrize("act", ACTS, ids=[act.name for act in ACTS])
async def test_without_a_recent_second_factor_nothing_happens(
    env: Any, act: Act, state: str
) -> None:
    """S1, S3; design §2.6 for `remembered`. Delete the gate of any one act
    and its three cases go red: it answers its success, and `read` moves."""
    org, sub, admin = await _admin(env)
    send, read = await act.prepare(env, org, admin)
    headers = await _confirmed(env, org, sub, state)
    before, rows = await read(), await _audit_rows(env, org)

    refused = await send(headers)

    assert refused.status_code == 403, refused.text
    assert refusal_code(refused) == "step_up_required"
    assert refused.json()["detail"] == CONFIRM_AGAIN
    assert await read() == before
    assert await _audit_rows(env, org) == rows


@pytest.mark.parametrize("act", ACTS, ids=[act.name for act in ACTS])
async def test_with_a_recent_second_factor_it_goes_through(env: Any, act: Act) -> None:
    org, sub, admin = await _admin(env)
    send, read = await act.prepare(env, org, admin)
    headers = await _confirmed(env, org, sub, "fresh")
    before, rows = await read(), await _audit_rows(env, org)

    done = await send(headers)

    assert done.status_code == act.succeeds_with, done.text
    assert await read() != before
    assert await _audit_rows(env, org) > rows


# --------------------------------------------------------------------------- #
# The scope comes first
# --------------------------------------------------------------------------- #


async def _on_one_client(env: Any, org: UUID) -> str:
    """A person holding the platform's `org-admin` -- `orgs.manage`,
    `users.manage` and `roles.manage` among its codes -- on ONE client only."""
    sub = await env.create_user(org)
    user_id = await user_id_of(env, sub)
    org_admin = await _role(env, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        client = Client(org_id=org, name="Acme", kind="customer")
        s.add(client)
        await s.flush()
        s.add(UserRole(org_id=org, user_id=user_id, role_id=org_admin, client_id=client.id))
        await s.commit()
    return sub


#: One write behind the dependency per code it wraps: Settings → Email
#: (`orgs.manage`), and a `_MANAGE_CONFIRMED` route of `user_admin/api.py`
#: (`users.manage`) and of `roles.py` (`roles.manage`).
SCOPE_FIRST = {
    "orgs.manage": ("PUT", "/api/v1/settings/email"),
    "users.manage": ("POST", "/api/v1/users/invite"),
    "roles.manage": ("POST", "/api/v1/roles"),
}


@pytest.mark.parametrize("code", sorted(SCOPE_FIRST))
async def test_on_one_client_the_scope_is_refused_and_no_factor_asked(env: Any, code: str) -> None:
    """`deps.require_permission_on_every_client_with_step_up`: the scope
    first. A person who holds `code` on one client only, with a confirmation
    eleven minutes old, is told the act is not theirs -- the every-client 403
    -- and is never asked for a factor that could not make it so. With the
    two checks the other way round this answer is `step_up_required`. The
    every-client detail also shows the person holds `code` (else: "requires")."""
    org = await env.create_org("step-up-scope")
    sub = await _on_one_client(env, org)
    stale = await _confirmed(env, org, sub, "stale")
    bodies: dict[str, dict[str, Any]] = {
        "orgs.manage": EMAIL,
        "users.manage": {
            "email": f"new-{uuid4().hex[:8]}@toeshee.com",
            "display_name": "Ana",
            "grants": [{"role_id": str(await _role(env, "technician"))}],
        },
        "roles.manage": {"name": f"role-{uuid4().hex[:8]}", "description": "", "codes": []},
    }
    method, url = SCOPE_FIRST[code]
    rows = await _audit_rows(env, org)

    refused = await env.client.request(method, url, headers=stale, json=bodies[code])

    assert refused.status_code == 403, refused.text
    assert refusal_code(refused) != "step_up_required", refused.text
    assert refused.json()["detail"] == EVERY_CLIENT, refused.text
    assert await _audit_rows(env, org) == rows


# --------------------------------------------------------------------------- #
# Only the change itself asks
# --------------------------------------------------------------------------- #


async def test_a_client_edit_that_leaves_its_tenants_alone_needs_none(env: Any) -> None:
    """S1: the tenant list is gated only when it changes. The same tenants in
    another order, repeated, or a field beside them, are not a change."""
    org, sub, admin = await _admin(env)
    client_id = await _customer(env, admin)
    one, two = sorted([uuid4(), uuid4()], key=str)
    url = f"/api/v1/clients/{client_id}"
    set_up = await env.client.patch(
        url, headers=admin, json={"microsoft_tenants": [str(one), str(two)]}
    )
    assert set_up.status_code == 200, set_up.text
    stale = await _confirmed(env, org, sub, "stale")

    renamed = await env.client.patch(url, headers=stale, json={"name": "Acme Logistics"})
    same = await env.client.patch(
        url,
        headers=stale,
        json={"notes": "two offices", "microsoft_tenants": [str(two), str(one), str(two)]},
    )
    changed = await env.client.patch(url, headers=stale, json={"microsoft_tenants": [str(one)]})

    assert renamed.status_code == 200, renamed.text
    assert same.status_code == 200, same.text
    assert same.json()["notes"] == "two offices"
    assert (changed.status_code, refusal_code(changed)) == (403, "step_up_required")
    assert await _tenants(env, client_id) == [one, two]


async def test_only_switching_ai_access_on_asks(env: Any) -> None:
    """S3 and S4: off to on asks; on to off, on again while on, and every
    other key do not."""
    org, sub, admin = await _admin(env)
    stale = await _confirmed(env, org, sub, "stale")

    def patch(headers: dict[str, str], settings: dict[str, Any]) -> Awaitable[httpx.Response]:
        return env.client.patch(
            "/api/v1/orgs/current", headers=headers, json={"settings": settings}
        )

    other = await patch(stale, {"timezone": "UTC"})
    refused = await patch(stale, {"mcp_enabled": True})
    switched = await patch(admin, {"mcp_enabled": True})
    again = await patch(stale, {"mcp_enabled": True})
    off = await patch(stale, {"mcp_enabled": False})

    assert other.status_code == 200, other.text
    assert (refused.status_code, refusal_code(refused)) == (403, "step_up_required")
    assert switched.status_code == 200, switched.text
    assert again.status_code == 200, again.text
    assert off.status_code == 200, off.text
    assert (await _org_settings(env, org)).get("mcp_enabled") is False


async def _connected_ai_app(env: Any, org: UUID, sub: str, admin: dict[str, str]) -> str:
    """An AI app `sub` really connected: AI access switched on with the
    administrator's fresh headers and one tool called over MCP as `claude`,
    so `GET /mcp/connections` lists it. The console becomes the MCP
    resource's (`MCP_BASE`), as `mcp_env` does."""
    env.settings.public_console_url = MCP_BASE
    on = await env.client.patch(
        "/api/v1/orgs/current", headers=admin, json={"settings": {"mcp_enabled": True}}
    )
    assert on.status_code == 200, on.text
    token = await mcp_token(env, await env.org_slug(org), sub)
    async with running_mcp(env), mcp_session(env, token) as session:
        called = await session.call_tool("me_get", {})
    assert called.is_error is False, called
    return "claude"


async def test_the_acts_left_without_a_confirmation_need_none(env: Any) -> None:
    """S4, pinned: cancelling an invitation, disabling a person, signing them
    out, turning AI access off and revoking an AI app (one this person really
    connected) protect, and must never wait for a factor; the test mail
    changes nothing; a contact's acts are unchanged (inviting to the portal,
    cancelling it, deactivating). S5, pinned: renaming or re-describing a role
    changes nobody's access, and disabling stays as it was while enabling
    again asks. Each with an eleven-minute-old confirmation. Deleting a
    person and archiving a client only take access away too (final review of
    phase 2, item 5), and are pinned with the rest."""
    org, sub, admin = await _admin(env)
    ai_app = await _connected_ai_app(env, org, sub, admin)
    invited = await _invited(env, admin)
    disabled, signed_out = await _technician(env, org), await _technician(env, org)
    deleted = await _technician(env, org)
    role = await _own_role(env, admin)
    client_id = await _customer(env, admin)
    to_archive = await env.client.post("/api/v1/clients", headers=admin, json={"name": "Globex"})
    assert to_archive.status_code == 201, to_archive.text
    archived = to_archive.json()["id"]
    made = await env.client.post(
        f"/api/v1/clients/{client_id}/contacts",
        headers=admin,
        json={"email": "ana@acme.example", "display_name": "Ana"},
    )
    assert made.status_code == 201, made.text
    contact = made.json()["id"]
    contact_invite = f"/api/v1/clients/{client_id}/contacts/{contact}/invite"
    stale = await _confirmed(env, org, sub, "stale")
    client = env.client

    answers = {
        "cancel an invitation": await client.delete(
            f"/api/v1/users/{invited}/invite", headers=stale
        ),
        "disable a person": await client.post(f"/api/v1/users/{disabled}/disable", headers=stale),
        "sign a person out": await client.delete(
            f"/api/v1/users/{signed_out}/sessions", headers=stale
        ),
        "switch ai access off": await client.patch(
            "/api/v1/orgs/current", headers=stale, json={"settings": {"mcp_enabled": False}}
        ),
        "revoke an ai app": await client.post(
            f"/api/v1/mcp/connections/{ai_app}/revoke", headers=stale, json={"scope": "me"}
        ),
        "send the test mail": await client.post(
            "/api/v1/settings/email/test", headers=stale, json={}
        ),
        "rename a role": await client.patch(
            f"/api/v1/roles/{role}",
            headers=stale,
            json={"name": "night-shift", "description": "Nights only"},
        ),
        "invite a contact": await client.post(contact_invite, headers=stale),
        "cancel a contact's invitation": await client.delete(contact_invite, headers=stale),
        "deactivate a contact": await client.patch(
            f"/api/v1/contacts/{contact}", headers=stale, json={"status": "disabled"}
        ),
        "delete a person": await client.delete(f"/api/v1/users/{deleted}", headers=stale),
        "archive a client": await client.post(f"/api/v1/clients/{archived}/archive", headers=stale),
    }

    asked = {
        name: answer.status_code
        for name, answer in answers.items()
        if answer.status_code == 403 or answer.status_code >= 500
    }
    assert asked == {}
    assert answers["cancel an invitation"].status_code == 204
    assert answers["disable a person"].status_code == 200
    assert answers["sign a person out"].status_code == 204
    assert answers["switch ai access off"].status_code == 200
    assert (await _org_settings(env, org)).get("mcp_enabled") is False
    assert answers["revoke an ai app"].status_code == 200
    assert answers["revoke an ai app"].json()["revoked"] is True
    # In force: the app this person connected is listed as revoked for them.
    listed = await client.get("/api/v1/mcp/connections", headers=await env.headers(org, sub))
    assert listed.status_code == 200, listed.text
    [connection] = listed.json()["apps"]
    assert (connection["client_id"], connection["revoked_for_me"]) == (ai_app, True)
    # Nothing saved to send through: past the gate, refused by the route.
    assert answers["send the test mail"].status_code == 409
    assert answers["rename a role"].status_code == 200
    assert answers["invite a contact"].status_code == 200
    assert answers["cancel a contact's invitation"].status_code == 204
    assert answers["deactivate a contact"].status_code == 200
    assert answers["delete a person"].status_code == 204
    assert answers["archive a client"].status_code == 200
    assert answers["archive a client"].json()["status"] == "archived"


# --------------------------------------------------------------------------- #
# MCP (invariant 1c(e): the API's own bounds still hold)
# --------------------------------------------------------------------------- #

#: Gated as a whole: never a tool, refused twice on the channel.
WHOLE = {
    ("PUT", "/api/v1/settings/email"): "settings_email_set",
    ("PUT", "/api/v1/settings/sessions"): "settings_sessions_set",
    ("PUT", "/api/v1/settings/microsoft-sign-in"): "settings_microsoft_sign_in_set",
    ("POST", "/api/v1/users/invite"): "users_invite_create",
    ("POST", "/api/v1/users/{user_id}/invite/resend"): "users_invite_resend",
    ("POST", "/api/v1/users/{user_id}/roles"): "users_roles_create",
    ("DELETE", "/api/v1/users/{user_id}/roles/{assignment_id}"): "users_roles_delete",
    ("POST", "/api/v1/roles"): "roles_create",
    ("PUT", "/api/v1/roles/{role_id}/permissions"): "roles_permissions_set",
    ("DELETE", "/api/v1/roles/{role_id}"): "roles_delete",
    ("POST", "/api/v1/users/{user_id}/enable"): "users_enable",
}
#: Gated in part: a tool whose description says which part needs the console.
PART = {
    ("PATCH", "/api/v1/clients/{client_id}"): "clients_update",
    ("PATCH", "/api/v1/orgs/current"): "orgs_current_update",
}


def test_the_acts_gated_as_a_whole_are_not_tools() -> None:
    catalog = build_catalog(build_router_only_app())
    tools = {(tool.method, tool.path): tool for tool in catalog.values()}
    assert set(WHOLE) <= exclusions.NEVER_EXPOSED
    assert not set(WHOLE) & set(tools)
    for key, name in PART.items():
        assert tools[key].description and name in catalog, key
        assert "needs a second factor confirmed in the console" in tools[key].description, key


async def test_over_mcp_a_tenant_change_is_refused_and_the_rest_of_the_edit_is_not(
    mcp_env: Any,
) -> None:
    org, slug = await mcp_org(mcp_env)
    sub = await mcp_env.create_user(org, "org-admin")
    async with AsyncSession(mcp_env.admin_engine, expire_on_commit=False) as s:
        acme = Client(org_id=org, name="Acme", kind="customer")
        s.add(acme)
        await s.commit()
    token = await mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_session(mcp_env, token) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        tenants = await session.call_tool(
            "clients_update",
            {"client_id": str(acme.id), "body": {"microsoft_tenants": [str(TENANT)]}},
        )
        notes = await session.call_tool(
            "clients_update", {"client_id": str(acme.id), "body": {"notes": "over mcp"}}
        )
    assert {"clients_update", "orgs_current_update"} <= names
    assert not set(WHOLE.values()) & names
    assert tool_error_text(tenants) == CONFIRM_AGAIN
    assert notes.is_error is False, notes
    # Read back: the notes the second call sent are stored, the tenants the
    # first one sent are not.
    async with AsyncSession(mcp_env.admin_engine) as s:
        stored = await s.get(Client, acme.id)
    assert stored is not None
    assert stored.notes == "over mcp"
    assert list(stored.microsoft_tenants or []) == []


async def test_on_the_mcp_channel_switching_ai_access_on_is_refused(mcp_env: Any) -> None:
    """Behind the MCP server's own refusal of an organization whose switch is
    off (`app/mcp/server.py`): the API refuses it too, as it refuses any act
    that asks for a second factor, because the channel never carries one."""
    org = await mcp_env.create_org("mcp-off")
    sub = await mcp_env.create_user(org, "org-admin")
    token = await mcp_token(mcp_env, await mcp_env.org_slug(org), sub)
    bearer = {"Authorization": f"Bearer {token}"}
    async with marked_client(mcp_env) as client:
        on = await client.patch(
            "/api/v1/orgs/current", headers=bearer, json={"settings": {"mcp_enabled": True}}
        )
        other = await client.patch(
            "/api/v1/orgs/current", headers=bearer, json={"settings": {"timezone": "UTC"}}
        )
    assert (on.status_code, refusal_code(on)) == (403, "step_up_required")
    assert other.status_code == 200, other.text
    assert (await _org_settings(mcp_env, org)).get("mcp_enabled") is None

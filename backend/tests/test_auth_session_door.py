"""Door 1 admits the console's session cookie (P2 design §6.5; coordinator D21, D14).

`app/api/deps.py::admit` admits off the MCP channel by the session and by
nothing else since Task 13, which removed the console's Keycloak bearer path
(D21). These tests hold what the session path must do the way the bearer path
did -- and the things only it does: a session without its second factor is
not signed in, and the acts of design §2.5 ask for a recent second factor.

FALSIFIER 1 (design §10): "Una sesión de una organización no lee nada de otra;
una sesión de contacto no pasa la puerta de la consola" --
`test_a_session_reads_only_its_own_organisation` and
`test_a_contact_session_never_passes_the_console_door`. FALSIFIER 5, the vault
half: `test_the_vault_asks_a_remembered_device_for_the_second_factor`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import end, mark_step_up
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Client, Device, User
from app.models.auth import AuthSession
from tests.mcp_support import marked_client
from tests.session_support import console_headers, remembered_device, sign_in, use_console


@pytest.fixture(autouse=True)
def _console(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    use_console(monkeypatch, api_env)


async def _me(env: Any, headers: dict[str, str]) -> Any:
    return await env.client.get("/api/v1/me", headers=headers)


async def test_a_session_admits_its_person(api_env: Any) -> None:
    org_id = await api_env.create_org("door")
    sub = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    response = await _me(api_env, signed.headers)
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == str(signed.user_id)


async def test_a_session_without_its_second_factor_is_not_signed_in(api_env: Any) -> None:
    """Protects `if not principal.factor_done` in `_admit_session`: the
    password alone opens the sign-in routes (Task 7), nothing else."""
    org_id = await api_env.create_org("door")
    sub = await api_env.create_user(org_id, "read-only")
    half = await sign_in(api_env, org_id, sub, factor_done=False)
    response = await _me(api_env, half.headers)
    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"


async def test_no_credential_is_a_401_the_console_recognises(api_env: Any) -> None:
    response = await _me(api_env, {})
    assert response.status_code == 401
    assert response.json() == {"detail": "sign in to continue", "code": "auth_required"}


async def test_an_ended_session_is_not_admitted(api_env: Any) -> None:
    org_id = await api_env.create_org("door")
    sub = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await end(s, signed.session_id, "signed_out")
    response = await _me(api_env, signed.headers)
    assert response.status_code == 401


async def test_the_session_wins_over_a_bearer_token(api_env: Any) -> None:
    """Cookie first (interfaces, Task 6): a request that carries both is the
    session's person. Bob's console token is minted here: `api_env.headers`
    is another session cookie since Task 13, which would win by coming last."""
    org_id = await api_env.create_org("door")
    alice = await api_env.create_user(org_id, "read-only")
    bob = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, alice)
    bob_token = api_env.idp.token(bob, await api_env.org_slug(org_id), email=f"{bob}@test.local")
    response = await _me(api_env, {**signed.headers, "Authorization": f"Bearer {bob_token}"})
    assert response.json()["user_id"] == str(signed.user_id)


async def test_the_mcp_channel_never_reads_the_session_cookie(api_env: Any) -> None:
    """Invariant 1c(a): on the MCP channel only the OAuth token the person
    gave the AI app is a credential. Protects `if call is None` in `admit`: a
    cookie riding on an in-process MCP call admits nobody, and the bearer path
    answers 401."""
    org_id = await api_env.create_org("door-mcp")
    sub = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    async with marked_client(api_env) as client:
        response = await client.get("/api/v1/me", headers=signed.headers)
    assert response.status_code == 401, response.text


async def _device(env: Any, org_id: UUID, hostname: str) -> None:
    async with AsyncSession(env.admin_engine) as s:
        s.add(
            Device(
                id=uuid4(),
                org_id=org_id,
                hostname=hostname,
                os_family="linux",
                client_id=org_id,
                site_id=org_id,
            )
        )
        await s.commit()


async def test_a_session_reads_only_its_own_organisation(api_env: Any) -> None:
    """FALSIFIER 1, first half. The organisation comes from the session row
    (`its_auth_session`), never from the request, and every read after it is
    RLS-pinned to it."""
    mine = await api_env.create_org("door-a")
    theirs = await api_env.create_org("door-b")
    await _device(api_env, mine, "ours")
    await _device(api_env, theirs, "not-ours")
    sub = await api_env.create_user(mine, "read-only")
    signed = await sign_in(api_env, mine, sub)

    response = await api_env.client.get("/api/v1/devices", headers=signed.headers)
    assert response.status_code == 200, response.text
    assert [d["hostname"] for d in response.json()["items"]] == ["ours"]


async def test_a_contact_session_never_passes_the_console_door(api_env: Any) -> None:
    """FALSIFIER 1, second half. Protects `contact_refusal` in
    `_admit_session`: a contact's session is refused by the console (403),
    whatever it holds; only the portal admits it (`admit(contacts=True)`)."""
    org_id = await api_env.create_org("door-contact")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org_id, name="Acme", kind="customer")
        s.add(client)
        await s.flush()
        contact = User(
            org_id=org_id,
            external_subject=f"contact-pending:{uuid4()}",
            email=f"{uuid4().hex[:8]}@acme.example",
            kind="contact",
            client_id=client.id,
        )
        s.add(contact)
        await s.commit()
    signed = await sign_in(api_env, org_id, contact.external_subject)
    response = await _me(api_env, signed.headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "this account can only use the portal"
    assert response.json()["code"] == "portal_only"


async def test_a_contacts_keep_alive_is_refused_and_still_renews_their_session(
    api_env: Any,
) -> None:
    """Final review of phase 2, item 6, pinned. A contact's console
    keep-alive is `GET /me`: it answers 403 `portal_only` and yet renews
    their session, because the door resolves the session -- `sessions.
    resolve(touch=True)`, committed in its own transaction -- before
    `_admit_session` refuses the contact. That call order is what keeps an
    open portal page alive; moved after the refusal, the touch would never
    run for a contact, and their session would idle out under the page."""
    org_id = await api_env.create_org("door-keep-alive")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org_id, name="Acme", kind="customer")
        s.add(client)
        await s.flush()
        contact = User(
            org_id=org_id,
            external_subject=f"contact-pending:{uuid4()}",
            email=f"{uuid4().hex[:8]}@acme.example",
            kind="contact",
            client_id=client.id,
        )
        s.add(contact)
        await s.commit()
    signed = await sign_in(api_env, org_id, contact.external_subject)
    # Last seen five minutes ago: inside the contact's idle limit (an hour),
    # past `TOUCH_EVERY` (a minute), so a touch is due.
    before = datetime.now(UTC) - timedelta(minutes=5)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(last_seen_at=before)
        )
        await s.commit()

    response = await _me(api_env, signed.headers)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "portal_only"
    async with AsyncSession(api_env.admin_engine) as s:
        seen = await s.scalar(
            select(AuthSession.last_seen_at).where(AuthSession.id == signed.session_id)
        )
    assert seen is not None and seen - before >= timedelta(minutes=4), (before, seen)


async def test_the_console_domain_rule_holds_for_a_session(api_env: Any) -> None:
    """Protects `domain_refusal` in `_admit_session`, read against the row's
    address (`{sub}@test.local`)."""
    org_id = await api_env.create_org("door-domain")
    sub = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    api_env.settings.console_email_domain = "allowed.example"
    response = await _me(api_env, signed.headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "email domain not permitted for this console"


async def test_a_disabled_person_is_signed_out(api_env: Any) -> None:
    org_id = await api_env.create_org("door-disabled")
    sub = await api_env.create_user(org_id, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(update(User).where(User.id == signed.user_id).values(status="disabled"))
        await s.commit()
    response = await _me(api_env, signed.headers)
    assert response.status_code == 401


async def test_the_consoles_address_is_the_sessions(api_env: Any) -> None:
    """D14: `ctx.email` (here, the preview's "would this refuse you") reads the
    session's person when there is no token to read it from."""
    org_id = await api_env.create_org("door-email")
    sub = await api_env.create_user(org_id, "org-admin")
    signed = await sign_in(api_env, org_id, sub)
    url = "/api/v1/orgs/current/console-domain-preview"
    ours = await api_env.client.get(url, params={"domain": "test.local"}, headers=signed.headers)
    other = await api_env.client.get(
        url, params={"domain": "elsewhere.example"}, headers=signed.headers
    )
    assert ours.status_code == 200, ours.text
    assert ours.json()["would_refuse_you"] is False
    assert other.json()["would_refuse_you"] is True


# --- recent confirmation (design §2.5) -------------------------------------


async def _credential(env: Any, headers: dict[str, str]) -> str:
    response = await env.client.post(
        "/api/v1/credentials",
        json={"name": "dc-admin", "type": "windows_admin", "secret": "P@ssw0rd!"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    credential_id: str = response.json()["id"]
    return credential_id


async def _checkout(env: Any, credential_id: str, headers: dict[str, str]) -> Any:
    return await env.client.post(
        f"/api/v1/credentials/{credential_id}/checkout",
        json={"reason": "ticket 4821"},
        headers=headers,
    )


async def test_the_vault_asks_a_remembered_device_for_the_second_factor(api_env: Any) -> None:
    """FALSIFIER 5, the vault half (design §2.6, §10). Protects
    `require_permission_with_step_up` on the checkout route and `require_step_up`:
    a session signed in on a remembered device verified no second factor, so
    the vault refuses until one is (`mark_step_up`, Task 7's step-up routes)."""
    org_id = await api_env.create_org("door-vault")
    sub = await api_env.create_user(org_id, "org-admin")
    remembered = await sign_in(
        api_env, org_id, sub, trusted_device_id=await remembered_device(api_env, org_id, sub)
    )
    credential_id = await _credential(api_env, remembered.headers)

    refused = await _checkout(api_env, credential_id, remembered.headers)
    assert refused.status_code == 403
    assert refused.json()["code"] == "step_up_required"
    assert "secret" not in refused.json()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await mark_step_up(s, remembered.session_id)
    allowed = await _checkout(api_env, credential_id, remembered.headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["secret"] == "P@ssw0rd!"


async def test_a_second_factor_from_sign_in_counts_for_ten_minutes(api_env: Any) -> None:
    org_id = await api_env.create_org("door-vault")
    sub = await api_env.create_user(org_id, "org-admin")
    fresh = await sign_in(api_env, org_id, sub)
    credential_id = await _credential(api_env, fresh.headers)
    assert (await _checkout(api_env, credential_id, fresh.headers)).status_code == 200

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == fresh.session_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await s.commit()
    stale = await _checkout(api_env, credential_id, fresh.headers)
    assert stale.status_code == 403 and stale.json()["code"] == "step_up_required"


async def test_a_console_bearer_is_no_way_into_the_vault(api_env: Any) -> None:
    """Was `test_a_bearer_token_is_not_asked_for_a_session_it_does_not_have`
    (D21: until Task 13 the console's bearer checked out as before). Since
    Task 13 the console's token is no credential at all: the checkout it asks
    for is a 401 `auth_required`, never the secret."""
    org_id = await api_env.create_org("door-vault")
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    credential_id = await _credential(api_env, headers)
    slug = await api_env.org_slug(org_id)
    token = api_env.idp.token(sub, slug, email=f"{sub}@test.local")
    refused = await _checkout(
        api_env, credential_id, {"Authorization": f"Bearer {token}", **console_headers()}
    )
    assert refused.status_code == 401
    assert refused.json()["code"] == "auth_required"
    assert "secret" not in refused.json()

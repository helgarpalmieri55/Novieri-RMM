"""My security for a client's contact (plan 2026-11-06, Task 7; design §3,
D26, D27; operator decision C5): the account's own routes through the
door that admits contacts, the contact's rules for its ways in, the
recovery codes that follow the app, and the first password -- with the
gates shown to refuse, not only to admit."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pyotp
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import account
from app.core.auth.refusals import AuthRefusal
from app.core.client_scope import ClientScope
from app.models.auth import (
    ENTRA,
    AuthExternalIdentity,
    AuthPasskey,
    AuthPassword,
    AuthRecoveryCode,
    AuthSession,
    AuthTotp,
)
from tests.auth_support import (
    CONTACT_PASSWORD,
    add_passkey,
    capture_mail,
    console_ready,
    contact,
    login,
    point_at,
)
from tests.session_support import http_request, principal_for, refusal_code, sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _signed(env: Any, **kwargs: Any) -> tuple[Any, dict[str, str]]:
    """A contact and the headers of a fresh, complete session of theirs."""
    org = await env.create_org("contact-sec")
    who = await contact(env, org, **kwargs)
    await point_at(env, org)
    signed = await sign_in(env, org, who.sub, step_up=True)
    return who, {**who.network, **signed.headers}


async def test_a_contact_reads_their_own_security_and_still_not_the_console(
    auth_env: Any,
) -> None:
    who, headers = await _signed(auth_env)
    mine = await auth_env.client.get("/api/v1/me/security", headers=headers)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert (body["kind"], body["has_password"], body["has_totp"], body["microsoft"]) == (
        "contact",
        True,
        False,
        None,
    )
    console = await auth_env.client.get("/api/v1/me", headers=headers)
    assert console.status_code == 403
    assert console.json()["code"] == "portal_only"


async def test_a_contact_keeps_one_way_in(auth_env: Any) -> None:
    who, headers = await _signed(auth_env, password=None)
    only = await add_passkey(auth_env, who)
    refused = await auth_env.client.delete(f"/api/v1/me/passkeys/{only}", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_method"
    # The refusal comes after the delete, inside the transaction: the key is
    # still there because the block rolled back (fix round 1, Minor 2).
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPasskey, only) is not None

    with_password, headers2 = await _signed(auth_env)
    key = await add_passkey(auth_env, with_password)
    allowed = await auth_env.client.delete(f"/api/v1/me/passkeys/{key}", headers=headers2)
    assert allowed.status_code == 204, allowed.text


async def _stale(env: Any, who: Any) -> None:
    """The recent confirmation of every session of `who`, 30 minutes old."""
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == who.user_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=30))
        )
        await s.commit()


async def test_a_contacts_authenticator_app_is_optional_and_takes_its_codes_with_it(
    auth_env: Any,
) -> None:
    """D27 and B2-06: the app goes whenever the contact likes, and the
    recovery codes made for it go with it."""
    who, headers = await _signed(auth_env)
    none_yet = await auth_env.client.post("/api/v1/me/recovery-codes", headers=headers)
    assert none_yet.status_code == 409
    assert none_yet.json()["code"] == "method_unavailable"
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=headers)
    assert start.status_code == 200, start.text
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirmed = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )
    assert confirmed.status_code == 204, confirmed.text
    codes = await auth_env.client.post("/api/v1/me/recovery-codes", headers=headers)
    assert codes.status_code == 200, codes.text
    removed = await auth_env.client.delete("/api/v1/me/totp", headers=headers)
    assert removed.status_code == 204, removed.text
    async with AsyncSession(auth_env.admin_engine) as s:
        left = await s.scalar(select(AuthTotp.user_id).where(AuthTotp.user_id == who.user_id))
        kept = await s.scalar(
            select(func.count())
            .select_from(AuthRecoveryCode)
            .where(AuthRecoveryCode.user_id == who.user_id)
        )
    assert left is None
    assert kept == 0


async def test_adding_a_password_needs_a_recent_confirmation(auth_env: Any) -> None:
    """A2-37: the gate refuses, not only admits."""
    who, headers = await _signed(auth_env, password=None)
    await add_passkey(auth_env, who)
    await _stale(auth_env, who)
    refused = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert refused.status_code == 403
    assert refusal_code(refused) == "step_up_required"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None


async def test_a_session_ended_while_waiting_for_the_lock_adds_no_password(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2-38: a reset that ends the session after the door admitted it and
    before the person's lock is taken leaves no password behind -- the check
    under the lock sees the session gone. (Ending it before the request
    would only show the door refusing, which the phase-1 tests already do.)"""
    who, headers = await _signed(auth_env, password=None)
    await add_passkey(auth_env, who)
    real_lock = account.lock_person

    async def reset_lands_first(session: AsyncSession, user_id: UUID) -> None:
        async with AsyncSession(auth_env.admin_engine) as s:
            await s.execute(
                update(AuthSession)
                .where(AuthSession.user_id == who.user_id)
                .values(ended_at=datetime.now(UTC), ended_reason="factors_reset")
            )
            await s.commit()
        await real_lock(session, user_id)

    monkeypatch.setattr(account, "lock_person", reset_lands_first)
    refused = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert refused.status_code == 401
    assert refusal_code(refused) == "auth_required"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None


async def test_a_contact_without_a_password_adds_one_with_the_contact_rule(
    auth_env: Any,
) -> None:
    who, headers = await _signed(auth_env, password=None)
    await add_passkey(auth_env, who)
    # Another session of the same contact, which adding a password must not end.
    await sign_in(auth_env, who.org_id, who.sub)
    short = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": "Fourteen-chars"}
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"
    added = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert added.status_code == 204, added.text
    again = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "password_exists"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is not None
        alive = await s.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None))
        )
    # B2-27: the other sessions stay, and the §2.7 notice goes -- once.
    assert alive == 2
    assert [m.to for m in auth_env.mail.sent] == [who.email]
    who.password = CONTACT_PASSWORD
    assert (await login(auth_env, who)).status_code == 200


async def test_a_contacts_password_change_holds_the_contact_rule(auth_env: Any) -> None:
    who, headers = await _signed(auth_env)
    short = await auth_env.client.put(
        "/api/v1/me/password",
        headers=headers,
        json={"current": CONTACT_PASSWORD, "new": "Twelve-chars"},
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"


async def _link_microsoft(
    env: Any, who: Any, *, used: datetime | None = None
) -> AuthExternalIdentity:
    """A Microsoft account linked to `who` (Task 9 makes these; seeded here)."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        link = AuthExternalIdentity(
            org_id=who.org_id,
            user_id=who.user_id,
            provider=ENTRA,
            tenant_id=uuid4(),
            subject=uuid4(),
            last_used_at=used,
        )
        s.add(link)
        await s.commit()
        await s.refresh(link)
        return link


async def test_my_security_shows_the_linked_microsoft_account_and_not_its_id(
    auth_env: Any,
) -> None:
    """`MicrosoftLinkOut`: the tenant and the two moments, never the account's
    own id (`oid`, `subject`)."""
    who, headers = await _signed(auth_env)
    used = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    link = await _link_microsoft(auth_env, who, used=used)
    mine = await auth_env.client.get("/api/v1/me/security", headers=headers)
    assert mine.status_code == 200, mine.text
    shown = mine.json()["microsoft"]
    assert set(shown) == {"tenant_id", "created_at", "last_used_at"}
    assert UUID(shown["tenant_id"]) == link.tenant_id
    assert datetime.fromisoformat(shown["created_at"]) == link.created_at
    assert datetime.fromisoformat(shown["last_used_at"]) == used
    assert str(link.subject) not in mine.text


async def test_a_contact_with_microsoft_may_remove_their_last_passkey(auth_env: Any) -> None:
    """Decision 2 (B2-28): a linked Microsoft account is a way in, so the last
    passkey may go."""
    who, headers = await _signed(auth_env, password=None)
    only = await add_passkey(auth_env, who)
    await _link_microsoft(auth_env, who)
    removed = await auth_env.client.delete(f"/api/v1/me/passkeys/{only}", headers=headers)
    assert removed.status_code == 204, removed.text
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPasskey, only) is None


async def test_a_contacts_own_routes_cannot_reach_another_clients_rows(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 5a (coordinator decision, Task 7 fix round 1): a contact's
    `/me/*` session runs in their client's scope, so the DATABASE confines it,
    not only the `user_id` filter in `account.py`. A probe runs in the very
    session each of the eight routes opens -- `account.signed_user`, which
    every one of them calls first -- and looks up and touches another
    client's contact BY ID, the query the filter would otherwise be the only
    thing against: it finds nothing and changes nothing, while the caller's
    own row is there."""
    who, headers = await _signed(auth_env)
    other = await contact(auth_env, who.org_id)
    assert other.client_id != who.client_id
    seen: list[tuple[int, int, int]] = []
    real = account.signed_user

    async def probing(session: AsyncSession, principal: Any) -> Any:
        theirs = await session.scalar(
            select(func.count())
            .select_from(AuthPassword)
            .where(AuthPassword.user_id == other.user_id)
        )
        touched = await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == other.user_id)
            .values(changed_at=AuthPassword.changed_at)
            .execution_options(synchronize_session=False)
        )
        mine = await session.scalar(
            select(func.count())
            .select_from(AuthPassword)
            .where(AuthPassword.user_id == who.user_id)
        )
        seen.append((theirs or 0, touched.rowcount, mine or 0))
        return await real(session, principal)

    monkeypatch.setattr(account, "signed_user", probing)
    routes: list[tuple[str, str, dict[str, str] | None]] = [
        ("GET", "/api/v1/me/security", None),
        ("PUT", "/api/v1/me/password", {"current": CONTACT_PASSWORD, "new": "Twelve-chars"}),
        ("POST", "/api/v1/me/password", {"new": CONTACT_PASSWORD}),
        ("DELETE", f"/api/v1/me/passkeys/{uuid4()}", None),
        ("DELETE", "/api/v1/me/totp", None),
        ("POST", "/api/v1/me/recovery-codes", None),
        ("DELETE", f"/api/v1/me/trusted-devices/{uuid4()}", None),
        ("DELETE", f"/api/v1/me/sessions/{uuid4()}", None),
    ]
    for method, path, body in routes:
        await auth_env.client.request(method, path, headers=headers, json=body)
    # Per route: (another client's rows seen, changed; the caller's own row seen).
    assert seen == [(0, 0, 1)] * len(routes)


async def test_a_contacts_own_session_is_never_every_client(auth_env: Any) -> None:
    """`account._own_session` fails closed: handed every client for a contact
    -- which the door never does, it pins a contact to their own client --
    it refuses instead of reopening invariant 5a; handed the contact's own
    client, it answers."""
    org = await auth_env.create_org("contact-sec-all")
    who = await contact(auth_env, org)
    signed = await sign_in(auth_env, org, who.sub, step_up=True)
    principal = await principal_for(auth_env, signed.session_id)
    assert who.client_id is not None
    with pytest.raises(AuthRefusal) as refused:
        await account.my_security(http_request(auth_env), principal, clients=ClientScope.all())
    assert refused.value.status_code == 401
    assert refused.value.code == "auth_required"
    mine = await account.my_security(
        http_request(auth_env), principal, clients=ClientScope.only([who.client_id])
    )
    assert mine.kind == "contact"

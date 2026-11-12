"""A contact's way into the portal (plan 2026-11-06, Task 8; design §3, §7;
decisions D39, D40, D43; B2-11, B2-29, B2-30) -- every gate shown to refuse,
not only to admit (A2-37), and every test one that fails without the code it
names (A2-43)."""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pyotp
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens, flows
from app.core.auth.methods import has_sign_in_method, with_sign_in_method
from app.core.auth.passkeys import PasskeyRejected
from app.core.auth.sessions import SESSION_COOKIE
from app.core.client_scope import ClientScope
from app.core.mail import MailSettingsInput, read_settings, save_settings
from app.core.tenancy import org_session
from app.models import AuditEvent, Role, User, UserRole
from app.models.auth import (
    ENTRA,
    AuthActionToken,
    AuthExternalIdentity,
    AuthPasskey,
    AuthPassword,
    AuthRecoveryCode,
    AuthSession,
    AuthTotp,
    AuthTrustedDevice,
)
from tests.auth_support import (
    CONTACT_PASSWORD,
    add_passkey,
    browser,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    login,
    point_at,
)
from tests.session_support import refusal_code, remembered_device, sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _world(env: Any, *, password: str | None = None) -> tuple[Any, dict[str, str]]:
    """A contact and an org-admin's headers. A contact with no password has
    not accepted an invitation yet, so holds no portal role either (A2-43:
    `contact()` grants it unless told not to)."""
    org = await env.create_org("contact-life")
    who = await contact(env, org, password=password, role=password is not None)
    await point_at(env, org)
    admin = await env.create_user(org, "org-admin")
    return who, await env.headers(org, admin)


def _invite_url(who: Any) -> str:
    return f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite"


def _reset_url(who: Any) -> str:
    return f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/access/reset"


async def _invite(env: Any, who: Any, headers: dict[str, str]) -> str:
    sent = await env.client.post(_invite_url(who), headers=headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["invite_link"] is None
    assert sent.json()["contact"]["access"] == "invited"
    return link_token(env.mail.sent[-1].text)


async def _grants(env: Any, who: Any) -> list[UUID]:
    async with AsyncSession(env.admin_engine) as s:
        rows = await s.execute(
            select(UserRole.client_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == who.user_id, Role.name == "requester")
        )
        return list(rows.scalars())


async def _metas(env: Any, who: Any, action: str) -> list[dict[str, Any]]:
    """The metadata of every `action` row about `who`, oldest first."""
    async with AsyncSession(env.admin_engine) as s:
        rows = await s.execute(
            select(AuditEvent.meta)
            .where(AuditEvent.action == action, AuditEvent.target_id == str(who.user_id))
            .order_by(AuditEvent.ts, AuditEvent.id)
        )
        return [dict(meta) for meta in rows.scalars()]


async def _stale_admin(env: Any, org_id: UUID) -> dict[str, str]:
    """An org-admin whose recent confirmation is 30 minutes old."""
    admin = await env.create_user(org_id, "org-admin")
    signed = await sign_in(env, org_id, admin, step_up=True)
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=30))
        )
        await s.commit()
    return signed.headers


async def test_an_invited_contact_accepts_with_a_password_and_gets_the_portal_in_their_client(
    auth_env: Any,
) -> None:
    who, headers = await _world(auth_env)
    assert await _grants(auth_env, who) == []
    token = await _invite(auth_env, who, headers)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.status_code == 200, details.text
    assert details.json() == {
        "email": who.email,
        "display_name": "",
        "kind": "contact",
        "password_minimum": 15,
        "passkey": True,
        "microsoft": False,
    }
    short = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Fourteen-chars"},
    )
    assert short.status_code == 422
    assert await _grants(auth_env, who) == []
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    session = cookie(accepted, SESSION_COOKIE)
    me = await auth_env.client.get(
        "/api/v1/portal/me", headers={**browser(), "Cookie": f"{SESSION_COOKIE}={session}"}
    )
    assert me.status_code == 200 and me.json()["kind"] == "contact"
    async with AsyncSession(auth_env.admin_engine) as s:
        accepted_events = (
            (
                await s.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == who.org_id,
                        AuditEvent.action == "contact.invite_accepted",
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one grant, on the contact's own client, made by the acceptance.
    assert await _grants(auth_env, who) == [who.client_id]
    assert list(accepted_events) == [{"client_id": str(who.client_id), "method": "password"}]
    again = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert again.status_code == 410


async def test_without_mail_the_link_is_shown_once(auth_env: Any) -> None:
    who, headers = await _world(auth_env)
    auth_env.mail.configured = False
    sent = await auth_env.client.post(_invite_url(who), headers=headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["invite_link"].startswith("https://itms.test/invite#")


async def test_the_answers_that_can_carry_a_link_are_never_cached(auth_env: Any) -> None:
    """Phase 3's cross-phase re-review, X2: an invitation and a reset may
    answer a one-time link, so both are `Cache-Control: no-store`
    (`security_headers.NO_STORE_ROUTES`), as `POST /users/invite` is."""
    auth_env.mail.configured = False
    who, headers = await _world(auth_env)
    invited = await auth_env.client.post(_invite_url(who), headers=headers)
    assert invited.status_code == 200, invited.text
    assert invited.json()["invite_link"] is not None
    assert invited.headers["Cache-Control"] == "no-store"
    assert invited.headers.get("Pragma") == "no-cache"
    active, admin = await _world(auth_env, password=CONTACT_PASSWORD)
    reset = await auth_env.client.post(_reset_url(active), headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["invite_link"] is not None
    assert reset.headers["Cache-Control"] == "no-store"
    assert reset.headers.get("Pragma") == "no-cache"
    # Fix round 1: the audit says the link was SHOWN, both times.
    assert [m["link_shown"] for m in await _metas(auth_env, who, "contact.invited")] == [True]
    assert [m["link_shown"] for m in await _metas(auth_env, active, "contact.access_reset")] == [
        True
    ]


async def test_a_contact_accepts_with_a_passkey_and_a_refused_one_leaves_the_link_alive(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    options = await auth_env.client.post(
        "/api/v1/auth/invite/passkey/options", headers=browser(), json={"token": token}
    )
    assert options.status_code == 200, options.text
    assert options.json()["authenticatorSelection"]["residentKey"] == "required"

    async def refusing(*_args: Any, **_kwargs: Any) -> AuthPasskey:
        raise PasskeyRejected(reason="verification")

    monkeypatch.setattr(flows, "verify_registration", refusing)
    refused = await auth_env.client.post(
        "/api/v1/auth/invite/passkey",
        headers=browser(),
        json={"token": token, "credential": {"id": "x"}, "name": "Phone"},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "passkey_rejected"
    still = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert still.status_code == 200

    async def accepting(session: Any, user: Any, credential: Any, **_kwargs: Any) -> AuthPasskey:
        key = AuthPasskey(
            org_id=user.org_id,
            user_id=user.id,
            credential_id=secrets.token_bytes(32),
            public_key=secrets.token_bytes(77),
            sign_count=0,
            transports=["internal"],
            backup_eligible=True,
            backup_state=True,
            name="Phone",
        )
        session.add(key)
        await session.flush()
        return key

    monkeypatch.setattr(flows, "verify_registration", accepting)
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/passkey",
        headers=browser(),
        json={"token": token, "credential": {"id": "x"}, "name": "Phone"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    async with AsyncSession(auth_env.admin_engine) as s:
        added = (
            (
                await s.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == who.org_id,
                        AuditEvent.action == "auth.factor_added",
                        AuditEvent.target_id == str(who.user_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    # A2-47: the passkey is audited as every other passkey is.
    assert list(added) == [{"factor": "passkey", "name": "Phone"}]


async def test_a_technicians_invitation_is_never_accepted_with_a_passkey(auth_env: Any) -> None:
    """f2-3 M8: accepting with a passkey is a contact's (D40). A technician's
    invitation is refused by both routes with 409, and its link stays live."""
    _who, headers = await _world(auth_env)
    async with AsyncSession(auth_env.admin_engine) as s:
        role = await s.scalar(
            select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
        )
    email = f"tech-{uuid4().hex[:6]}@toeshee.test"
    invited = await auth_env.client.post(
        "/api/v1/users/invite",
        headers=headers,
        json={"email": email, "display_name": "Ana", "grants": [{"role_id": str(role)}]},
    )
    assert invited.status_code == 201, invited.text
    token = link_token([m for m in auth_env.mail.sent if m.to == email][-1].text)
    options = await auth_env.client.post(
        "/api/v1/auth/invite/passkey/options", headers=browser(), json={"token": token}
    )
    assert options.status_code == 409
    assert options.json()["code"] == "method_unavailable"
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/passkey",
        headers=browser(),
        json={"token": token, "credential": {"id": "x"}, "name": "Phone"},
    )
    assert accepted.status_code == 409
    assert accepted.json()["code"] == "method_unavailable"
    still = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert still.status_code == 200


async def _everything(env: Any, who: Any) -> None:
    """Every way in a contact can hold and everything they can have open, for
    a reset to take away (A2-48): a passkey, an authenticator app and its
    recovery codes, a Microsoft link, a remembered browser and a reset link
    (the password `_world` gave them)."""
    await login(env, who)
    await add_passkey(env, who)
    start = await env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirmed = await env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirmed.status_code == 204, confirmed.text
    codes = await env.client.post("/api/v1/me/recovery-codes", headers=who.headers())
    assert codes.status_code == 200, codes.text
    await remembered_device(env, who.org_id, who.sub)
    async with AsyncSession(env.admin_engine) as s:
        s.add(
            AuthExternalIdentity(
                org_id=who.org_id,
                user_id=who.user_id,
                provider=ENTRA,
                tenant_id=uuid4(),
                subject=uuid4(),
            )
        )
        await s.commit()
    async with org_session(who.org_id, engine=env.app_engine, clients=ClientScope.all()) as s:
        await action_tokens.issue(
            s,
            org_id=who.org_id,
            user_id=who.user_id,
            purpose=action_tokens.RESET,
            ttl=action_tokens.RESET_TTL,
            created_by=None,
        )


async def _count(s: AsyncSession, column: Any, who: Any, *extra: Any) -> int:
    """How many rows of `column`'s table are `who`'s (the FROM comes from
    the WHERE)."""
    found = await s.scalar(select(func.count()).where(column == who.user_id, *extra))
    return int(found or 0)


async def test_an_active_contact_is_not_invited_again_and_a_reset_cuts_everything(
    auth_env: Any,
) -> None:
    who, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await _everything(auth_env, who)
    old = who.headers()
    again = await auth_env.client.post(_invite_url(who), headers=headers)
    assert again.status_code == 409

    reset = await auth_env.client.post(_reset_url(who), headers=headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["contact"]["access"] == "invited"
    assert (await auth_env.client.get("/api/v1/portal/me", headers=old)).status_code == 401
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None
        left = {
            "passkeys": await _count(s, AuthPasskey.user_id, who),
            "totp": await _count(s, AuthTotp.user_id, who),
            "codes": await _count(s, AuthRecoveryCode.user_id, who),
            "microsoft": await _count(s, AuthExternalIdentity.user_id, who),
            "devices": await _count(
                s, AuthTrustedDevice.user_id, who, AuthTrustedDevice.revoked_at.is_(None)
            ),
            "sessions": await _count(s, AuthSession.user_id, who, AuthSession.ended_at.is_(None)),
            "reset_links": await _count(
                s,
                AuthActionToken.user_id,
                who,
                AuthActionToken.purpose == action_tokens.RESET,
                AuthActionToken.used_at.is_(None),
            ),
        }
    assert left == dict.fromkeys(left, 0)
    assert "reset" in auth_env.mail.sent[-1].subject.lower()


async def test_resetting_a_contacts_access_needs_the_technicians_recent_confirmation(
    auth_env: Any,
) -> None:
    """A2-37, design §2.5: restoring another person's way in is a confirmed act."""
    who, _headers = await _world(auth_env, password=CONTACT_PASSWORD)
    stale = await _stale_admin(auth_env, who.org_id)
    refused = await auth_env.client.post(_reset_url(who), headers=stale)
    assert refused.status_code == 403
    assert refusal_code(refused) == "step_up_required"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is not None


async def test_reading_a_client_is_not_managing_its_contacts_access(auth_env: Any) -> None:
    """A2-37: `technician` holds `clients.read` and not `clients.manage`
    (migration 0232); the three acts are each a 403, and nothing moved."""
    who, headers = await _world(auth_env)
    await _invite(auth_env, who, headers)
    reader = await auth_env.create_user(who.org_id, "technician")
    reading = await auth_env.headers(who.org_id, reader)
    listed = await auth_env.client.get(f"/api/v1/clients/{who.client_id}/contacts", headers=reading)
    assert listed.status_code == 200, listed.text
    for method, url in (
        ("POST", _invite_url(who)),
        ("DELETE", _invite_url(who)),
        ("POST", _reset_url(who)),
    ):
        # Well-formed JSON: the door answers before the body (Task 13, Step 1's note).
        answer = await auth_env.client.request(method, url, headers=reading, json={})
        assert answer.status_code == 403, (method, url, answer.text)
    assert len(auth_env.mail.sent) == 1


async def test_cancelling_kills_the_link_and_keeps_the_contact(auth_env: Any) -> None:
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    cancelled = await auth_env.client.delete(_invite_url(who), headers=headers)
    assert cancelled.status_code == 204, cancelled.text
    dead = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert dead.status_code == 410
    listed = await auth_env.client.get(f"/api/v1/clients/{who.client_id}/contacts", headers=headers)
    [row] = listed.json()
    assert (row["id"], row["access"]) == (str(who.user_id), "none")


async def test_switched_off_and_on_again_nothing_from_before_works(
    auth_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """D43, A2-43. The contact is switched back ON before the checks: with
    the contact active the door would admit a session nobody ended, and
    `invitee` would honour a link nobody revoked -- so only `_cut_access`
    makes these pass. While off, the door's "a live session belongs to a
    person who is not active" defect line must not appear: the session was
    ended, not merely refused."""
    who, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await login(auth_env, who)
    signed = who.headers()
    invited = await contact(auth_env, who.org_id, password=None, role=False)
    token = await _invite(auth_env, invited, headers)
    for person in (who, invited):
        off = await auth_env.client.patch(
            f"/api/v1/contacts/{person.user_id}", headers=headers, json={"status": "disabled"}
        )
        assert off.status_code == 200, off.text
        assert off.json()["access"] == "none"
    with caplog.at_level(logging.WARNING, logger="its.auth.sessions"):
        assert (await auth_env.client.get("/api/v1/portal/me", headers=signed)).status_code == 401
    # `sessions.resolve`'s defect line (`test_auth_sessions.py:251` reads it the same way).
    assert [r for r in caplog.records if r.name == "its.auth.sessions"] == []
    for person in (who, invited):
        on = await auth_env.client.patch(
            f"/api/v1/contacts/{person.user_id}", headers=headers, json={"status": "active"}
        )
        assert on.status_code == 200, on.text
    assert (await auth_env.client.get("/api/v1/portal/me", headers=signed)).status_code == 401
    dead = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert dead.status_code == 410
    async with AsyncSession(auth_env.admin_engine) as s:
        reasons = (
            (
                await s.execute(
                    select(AuthSession.ended_reason).where(AuthSession.user_id == who.user_id)
                )
            )
            .scalars()
            .all()
        )
    assert list(reasons) == ["disabled"]


async def test_archiving_the_client_cuts_its_contacts_access(auth_env: Any) -> None:
    """D43, A2-43: the archive path (`archive_client`, which cuts its active
    contacts' access right after locking them, before any audit write), which
    no other test reaches. An archived client is not restored, so the rows
    say it."""
    who, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await login(auth_env, who)
    async with org_session(who.org_id, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        await action_tokens.issue(
            s,
            org_id=who.org_id,
            user_id=who.user_id,
            purpose=action_tokens.RESET,
            ttl=action_tokens.RESET_TTL,
            created_by=None,
        )
    archived = await auth_env.client.post(
        f"/api/v1/clients/{who.client_id}/archive", headers=headers
    )
    assert archived.status_code == 200, archived.text
    async with AsyncSession(auth_env.admin_engine) as s:
        live = await _count(s, AuthSession.user_id, who, AuthSession.ended_at.is_(None))
        links = await _count(s, AuthActionToken.user_id, who, AuthActionToken.used_at.is_(None))
    assert (live, links) == (0, 0)


async def test_a_new_address_kills_the_links_sent_to_the_old_one(auth_env: Any) -> None:
    """A2-44: an invitation sent to a mistyped address must not stay
    acceptable by whoever reads that mailbox once the address is fixed."""
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    moved = await auth_env.client.patch(
        f"/api/v1/contacts/{who.user_id}", headers=headers, json={"email": "ana@acme.test"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["access"] == "none"
    dead = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert dead.status_code == 410
    async with AsyncSession(auth_env.admin_engine) as s:
        meta = await s.scalar(
            select(AuditEvent.meta)
            .where(AuditEvent.action == "contact.updated", AuditEvent.target_id == str(who.user_id))
            .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
            .limit(1)
        )
    assert meta is not None and meta["links_revoked"] == 1


async def test_the_contacts_list_reads_access_in_bulk_and_as_the_single_check_does(
    auth_env: Any,
) -> None:
    """A2-46: `with_sign_in_method` is `has_sign_in_method` for many at once."""
    who, _headers = await _world(auth_env, password=CONTACT_PASSWORD)
    keyed = await contact(auth_env, who.org_id, password=None)
    await add_passkey(auth_env, keyed)
    bare = await contact(auth_env, who.org_id, password=None, role=False)
    # Fix round 1: a Microsoft link alone is a way in too (the third table).
    linked = await contact(auth_env, who.org_id, password=None)
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            AuthExternalIdentity(
                org_id=linked.org_id,
                user_id=linked.user_id,
                provider=ENTRA,
                tenant_id=uuid4(),
                subject=uuid4(),
            )
        )
        await s.commit()
    ids = [who.user_id, keyed.user_id, bare.user_id, linked.user_id]
    async with org_session(who.org_id, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        bulk = await with_sign_in_method(s, ids)
        single = {i for i in ids if await has_sign_in_method(s, i)}
    assert bulk == single == {who.user_id, keyed.user_id, linked.user_id}


async def test_a_technician_of_another_client_cannot_reach_this_contact(auth_env: Any) -> None:
    """Decision 5: an org-admin grant on another client only; RLS hides this
    client's contact, and the answer is the 404 of an unknown one."""
    who, _headers = await _world(auth_env)
    other = await contact(auth_env, who.org_id)
    sub = await auth_env.create_user(who.org_id)
    async with AsyncSession(auth_env.admin_engine) as s:
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "org-admin", Role.org_id.is_(None))
        )
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
        s.add(
            UserRole(org_id=who.org_id, user_id=user_id, role_id=role_id, client_id=other.client_id)
        )
        await s.commit()
    limited = await auth_env.headers(who.org_id, sub)
    refused = await auth_env.client.post(_invite_url(who), headers=limited)
    assert refused.status_code == 404


async def test_a_technician_of_this_client_alone_manages_its_contacts_access(
    auth_env: Any,
) -> None:
    """Decision 5, the admitting side of the test above: an org-admin grant on
    the contact's client ONLY. Every act runs in that scope (`ctx.clients`,
    never every client), and RLS lets it write what each act writes -- the
    link, the wiped credentials, the ended sessions -- because they are that
    client's rows (migration 0231); and each audit row names the client,
    the acceptance's included."""
    who, _headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await _everything(auth_env, who)
    sub = await auth_env.create_user(who.org_id)
    async with AsyncSession(auth_env.admin_engine) as s:
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "org-admin", Role.org_id.is_(None))
        )
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
        s.add(
            UserRole(org_id=who.org_id, user_id=user_id, role_id=role_id, client_id=who.client_id)
        )
        await s.commit()
    limited = await auth_env.headers(who.org_id, sub)
    reset = await auth_env.client.post(_reset_url(who), headers=limited)
    assert reset.status_code == 200, reset.text
    assert reset.json()["contact"]["access"] == "invited"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None
        assert await _count(s, AuthPasskey.user_id, who) == 0
        assert await _count(s, AuthSession.user_id, who, AuthSession.ended_at.is_(None)) == 0
    cancelled = await auth_env.client.delete(_invite_url(who), headers=limited)
    assert cancelled.status_code == 204, cancelled.text
    invited = await auth_env.client.post(_invite_url(who), headers=limited)
    assert invited.status_code == 200, invited.text
    assert invited.json()["contact"]["access"] == "invited"
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": link_token(auth_env.mail.sent[-1].text), "password": CONTACT_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    async with AsyncSession(auth_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id)
                .where(
                    AuditEvent.target_id == str(who.user_id),
                    AuditEvent.action.in_(
                        (
                            "contact.access_reset",
                            "contact.invite_cancelled",
                            "contact.invited",
                            "contact.invite_accepted",
                        )
                    ),
                )
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("contact.access_reset", who.client_id),
        ("contact.invite_cancelled", who.client_id),
        ("contact.invited", who.client_id),
        ("contact.invite_accepted", who.client_id),
    ]


async def test_an_expired_invitation_is_no_invitation(auth_env: Any) -> None:
    """Decision 6 (B2-29): an invitation that ran out reads `none`, as its
    link answers 410; a cancel finds nothing pending, and a new one may go."""
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthActionToken)
            .where(AuthActionToken.user_id == who.user_id)
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=73),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()
    listed = await auth_env.client.get(f"/api/v1/clients/{who.client_id}/contacts", headers=headers)
    [row] = listed.json()
    assert (row["access"], row["invite_expires_at"]) == ("none", None)
    dead = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert dead.status_code == 410
    cancelled = await auth_env.client.delete(_invite_url(who), headers=headers)
    assert cancelled.status_code == 409
    await _invite(auth_env, who, headers)


async def test_an_invitation_admits_nobody_who_already_has_a_way_in(auth_env: Any) -> None:
    """`flows.invitee`: a contact with a passkey and no password has a way in
    (`methods.has_sign_in_method`), so a link that names them -- however it
    came to exist -- is dead, and sets no password."""
    who, _headers = await _world(auth_env)
    await add_passkey(auth_env, who)
    async with org_session(who.org_id, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        token = await action_tokens.issue(
            s,
            org_id=who.org_id,
            user_id=who.user_id,
            purpose=action_tokens.INVITE,
            ttl=action_tokens.INVITE_TTL,
            created_by=None,
        )
    for path, body in (
        ("details", {"token": token}),
        ("accept", {"token": token, "password": CONTACT_PASSWORD}),
        ("passkey/options", {"token": token}),
    ):
        answer = await auth_env.client.post(
            f"/api/v1/auth/invite/{path}", headers=browser(), json=body
        )
        assert answer.status_code == 410, (path, answer.text)
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None
    assert await _grants(auth_env, who) == []


# --------------------------------------------------------------------------- #
# Fix round 1
# --------------------------------------------------------------------------- #


async def test_a_link_that_had_run_out_is_not_counted_as_revoked(auth_env: Any) -> None:
    """Fix round 1, item 3: `contact.updated` says `links_revoked` only for
    links that could still have been used. An invitation that ran out
    unused is spent with the rest, and not counted; a live reset link is."""
    who, headers = await _world(auth_env)

    async def _links(*purposes: str) -> None:
        async with org_session(
            who.org_id, engine=auth_env.app_engine, clients=ClientScope.all()
        ) as s:
            for purpose in purposes:
                await action_tokens.issue(
                    s,
                    org_id=who.org_id,
                    user_id=who.user_id,
                    purpose=purpose,
                    ttl=action_tokens.INVITE_TTL,
                    created_by=None,
                )
        async with AsyncSession(auth_env.admin_engine) as s:
            await s.execute(
                update(AuthActionToken)
                .where(
                    AuthActionToken.user_id == who.user_id,
                    AuthActionToken.purpose == action_tokens.INVITE,
                    AuthActionToken.used_at.is_(None),
                )
                .values(
                    created_at=datetime.now(UTC) - timedelta(hours=73),
                    expires_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )
            await s.commit()

    await _links(action_tokens.INVITE, action_tokens.RESET)
    first = await auth_env.client.patch(
        f"/api/v1/contacts/{who.user_id}", headers=headers, json={"email": "ana@acme.test"}
    )
    assert first.status_code == 200, first.text
    await _links(action_tokens.INVITE)
    second = await auth_env.client.patch(
        f"/api/v1/contacts/{who.user_id}", headers=headers, json={"email": "bea@acme.test"}
    )
    assert second.status_code == 200, second.text
    metas = await _metas(auth_env, who, "contact.updated")
    assert [m.get("links_revoked") for m in metas] == [1, None]
    async with AsyncSession(auth_env.admin_engine) as s:
        # Spent all the same: no unused row is left behind.
        unused = await _count(s, AuthActionToken.user_id, who, AuthActionToken.used_at.is_(None))
    assert unused == 0


async def test_a_switched_off_contact_or_one_of_an_archived_client_is_neither_invited_nor_reset(
    auth_env: Any,
) -> None:
    """Fix round 1, item 4: `contact_access._contact`'s two 409s, for each
    act that issues a link. Nothing is mailed and no link is left."""
    off, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    switched = await auth_env.client.patch(
        f"/api/v1/contacts/{off.user_id}", headers=headers, json={"status": "disabled"}
    )
    assert switched.status_code == 200, switched.text
    gone, admin = await _world(auth_env, password=CONTACT_PASSWORD)
    archived = await auth_env.client.post(
        f"/api/v1/clients/{gone.client_id}/archive", headers=admin
    )
    assert archived.status_code == 200, archived.text
    for who, auth, detail in (
        (off, headers, "switch the contact on first"),
        (gone, admin, "that client is archived"),
    ):
        for url in (_invite_url(who), _reset_url(who)):
            refused = await auth_env.client.post(url, headers=auth)
            assert refused.status_code == 409, (url, refused.text)
            assert refused.json()["detail"] == detail, url
        async with AsyncSession(auth_env.admin_engine) as s:
            assert await s.get(AuthPassword, who.user_id) is not None
            assert await _count(s, AuthActionToken.user_id, who) == 0
    assert auth_env.mail.sent == []


async def test_the_audit_says_whether_the_link_was_shown_resent_and_what_ended(
    auth_env: Any,
) -> None:
    """Fix round 1, item 4: `contact.invited` records `resent` and
    `link_shown` (False when a mail carried the link, True when the page
    showed it); `contact.access_reset` records `link_shown` and how many
    sessions it ended."""
    who, headers = await _world(auth_env)
    await _invite(auth_env, who, headers)
    auth_env.mail.configured = False
    again = await auth_env.client.post(_invite_url(who), headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["invite_link"] is not None
    assert await _metas(auth_env, who, "contact.invited") == [
        {"client_id": str(who.client_id), "email": who.email, "resent": False, "link_shown": False},
        {"client_id": str(who.client_id), "email": who.email, "resent": True, "link_shown": True},
    ]
    auth_env.mail.configured = True
    active, admin = await _world(auth_env, password=CONTACT_PASSWORD)
    for _ in range(2):
        await sign_in(auth_env, active.org_id, active.sub)
    reset = await auth_env.client.post(_reset_url(active), headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["invite_link"] is None
    assert await _metas(auth_env, active, "contact.access_reset") == [
        {
            "client_id": str(active.client_id),
            "email": active.email,
            "sessions_ended": 2,
            "link_shown": False,
        }
    ]


class _Relay:
    """`smtplib.SMTP`, recording: what the account mail signed in with and
    sent. Nothing opens a socket."""

    logins: list[tuple[str, str]] = []
    sent: list[str] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host, self.port = host, port

    def __enter__(self) -> "_Relay":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        _Relay.logins.append((username, password))

    def send_message(self, message: Any) -> None:
        _Relay.sent.append(str(message["To"]))


async def test_a_technician_of_one_client_mails_the_invitation_through_the_relay(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, item 5. The relay's sign-in is a client-less
    `credentials` row (migration 0231: a per-client scope may read it, never
    change it), and every contact act runs in the technician's own scope. So
    the checkout must work from a scope of ONE client: read directly with
    `vault.checkout`, as `mail._sign_in` does, and end to end -- the
    invitation of a technician who holds `clients.manage` on that client
    alone goes through the real `send_account_mail`, signed in, audited, and
    the link is not shown. No mail is captured here."""
    console_ready(api_env, monkeypatch)
    monkeypatch.setattr("smtplib.SMTP", _Relay)
    _Relay.logins, _Relay.sent = [], []
    org = await api_env.create_org("contact-relay")
    who = await contact(api_env, org, password=None, role=False)
    vault = api_env.app.state.vault
    async with org_session(org, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        await save_settings(
            s,
            vault,
            org_id=org,
            actor_id=who.user_id,
            values=MailSettingsInput(
                host="smtp.internal",
                port=587,
                from_address="itms@toeshee.com",
                from_display_name="ITMS",
                reply_to="",
                encryption="starttls",
                username="itms",
                auth_type="password",
                token_url="",
                token_scope="",
                token_client_id="",
            ),
            password="relay-pass-SENTINEL",
            client_secret=None,
        )
    one = ClientScope.only([who.client_id])
    async with org_session(org, engine=api_env.app_engine, clients=one) as s:
        settings = await read_settings(s, org)
        assert settings is not None and settings.credential_id is not None
        secret = await vault.checkout(s, org_id=org, credential_id=settings.credential_id)
    assert json.loads(secret) == {"password": "relay-pass-SENTINEL"}

    sub = await api_env.create_user(org)
    async with AsyncSession(api_env.admin_engine) as s:
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "org-admin", Role.org_id.is_(None))
        )
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role_id, client_id=who.client_id))
        await s.commit()
    sent = await api_env.client.post(_invite_url(who), headers=await api_env.headers(org, sub))
    assert sent.status_code == 200, sent.text
    assert sent.json()["invite_link"] is None
    assert _Relay.logins == [("itms", "relay-pass-SENTINEL")]
    assert _Relay.sent == [who.email]
    async with AsyncSession(api_env.admin_engine) as s:
        used = (
            (
                await s.execute(
                    select(AuditEvent.actor_id).where(
                        AuditEvent.org_id == org, AuditEvent.action == "email.credential_used"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert list(used) == [user_id]
    assert [m["link_shown"] for m in await _metas(api_env, who, "contact.invited")] == [False]

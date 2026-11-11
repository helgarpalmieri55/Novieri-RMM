"""My security for a client's contact (plan 2026-11-06, Task 7; design §3,
D26, D27; operator decision C5): the account's own routes through the
door that admits contacts, the contact's rules for its ways in, the
recovery codes that follow the app, and the first password -- with the
gates shown to refuse, not only to admit."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pyotp
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import account
from app.models.auth import AuthPassword, AuthRecoveryCode, AuthSession, AuthTotp
from tests.auth_support import (
    CONTACT_PASSWORD,
    add_passkey,
    capture_mail,
    console_ready,
    contact,
    login,
    point_at,
)
from tests.session_support import refusal_code, sign_in


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
    assert console.json()["code"] == "portal_only"


async def test_a_contact_keeps_one_way_in(auth_env: Any) -> None:
    who, headers = await _signed(auth_env, password=None)
    only = await add_passkey(auth_env, who)
    refused = await auth_env.client.delete(f"/api/v1/me/passkeys/{only}", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_method"

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
    assert alive == 1
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

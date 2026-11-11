"""A client's contact signs in with the platform's own sign-in (own
authentication phase 2, Task 4; design §3).

- One route for both kinds (plan decision D25): `POST /auth/login` signs a
  contact in; with no second factor that is the whole sign-in (D27), and the
  session is a contact's -- the portal's door admits it (`GET /portal/me`)
  and the console's answers 403 `portal_only` (falsifier 1, D42).
- A contact who set up an authenticator app is asked for it, as a
  technician is; a contact's passkey is another way in, not a second factor
  of the password (operator decision C5, plan decision 3).
- A wrong password is the one generic 401, counted per account (§2.3).
- The reset link holds the contact's rule: 15 characters; the other sessions
  end; the mail does not promise a second factor the contact does not have;
  it gives a passkey-only contact a password that signs in alone; it is sent
  neither to a contact who never accepted an invitation nor to one who signs
  in only with Microsoft (operator decision C6, plan decision 4).
- A contact without a second factor may confirm with the password (D29); a
  contact who turned one on may not, nor a contact without a password, nor a
  technician (design §2.6, plan decision 6).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pyotp
import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import SESSION_COOKIE
from app.core.auth.totp import step_of
from app.models import AuditEvent
from app.models.auth import (
    ENTRA,
    AuthExternalIdentity,
    AuthFailure,
    AuthPasskey,
    AuthPassword,
    AuthSession,
    AuthTotp,
)
from tests.auth_support import (
    CONTACT_PASSWORD,
    PASSWORD,
    add_passkey,
    browser,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    login,
    point_at,
    signed_in,
)
from tests.session_support import sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _signed_in_contact(env: Any, **kwargs: Any) -> Any:
    org = await env.create_org("contact")
    who = await contact(env, org, **kwargs)
    await point_at(env, org)
    return who


async def _with_totp(env: Any, who: Any) -> str:
    """Sign `who` in and turn their authenticator app on, from the fresh
    confirmation a contact's sign-in leaves; the app's secret."""
    await login(env, who)
    start = await env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirm = await env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirm.status_code == 204, confirm.text
    return secret


async def _linked_to_microsoft(env: Any, who: Any) -> None:
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


async def test_a_contact_signs_in_with_the_password_alone_and_only_the_portal_lets_it_in(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    answer = await login(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}
    assert who.session is not None

    portal = await auth_env.client.get("/api/v1/portal/me", headers=who.headers())
    assert portal.status_code == 200, portal.text
    assert portal.json() == {
        "user_id": str(who.user_id),
        "kind": "contact",
        "email": who.email,
        "display_name": "",
        "timezone": None,
        "permissions": ["portal.ticket.read", "portal.ticket.write"],
    }
    console = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert console.status_code == 403
    assert console.json()["code"] == "portal_only"

    async with AsyncSession(auth_env.admin_engine) as s:
        kinds = set(
            (
                await s.execute(
                    select(AuthSession.kind).where(
                        AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
                    )
                )
            ).scalars()
        )
        audited = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == who.org_id, AuditEvent.action == "auth.signed_in"
                )
            )
        ).scalars().all()
    assert kinds == {"contact"}
    assert list(audited) == [{"method": "password", "trusted_device": False}]


async def test_a_contacts_passkey_is_another_way_in_not_a_second_factor(
    auth_env: Any,
) -> None:
    """Operator decision C5: with a password and a passkey, the password
    alone still signs a contact in."""
    who = await _signed_in_contact(auth_env)
    await add_passkey(auth_env, who)
    answer = await login(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}


async def test_a_contact_with_an_authenticator_app_is_asked_for_it(auth_env: Any) -> None:
    who = await _signed_in_contact(auth_env)
    secret = await _with_totp(auth_env, who)
    await auth_env.client.post("/api/v1/auth/logout", headers=who.headers())
    who.session = None

    again = await login(auth_env, who)
    assert again.json() == {
        "state": "factor_required",
        "methods": ["totp"],
        "remember_device_days": 15,
    }
    # The code above was accepted for this step: the next one is a step on.
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthTotp)
            .where(AuthTotp.user_id == who.user_id)
            .values(last_step=step_of(datetime.now(UTC)) - 2)
        )
        await s.commit()
    factor = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now(), "remember_device": False},
    )
    assert factor.status_code == 200, factor.text
    assert factor.json() == {"state": "signed_in"}


async def test_the_factor_step_does_not_offer_a_contacts_passkey(auth_env: Any) -> None:
    """Operator decision C5 (plan decision 3): a contact with a passkey AND an
    authenticator app is asked for the app alone. The passkey is another way
    in, never offered as a second factor of the password."""
    who = await _signed_in_contact(auth_env)
    await add_passkey(auth_env, who)
    await _with_totp(auth_env, who)
    await auth_env.client.post("/api/v1/auth/logout", headers=who.headers())
    who.session = None

    again = await login(auth_env, who)
    assert again.status_code == 200, again.text
    assert again.json()["state"] == "factor_required"
    assert again.json()["methods"] == ["totp"]


async def test_a_contacts_wrong_password_is_the_generic_answer_and_is_counted(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    answer = await login(auth_env, who, password="Not-The-Password-41-Harbor")
    assert answer.status_code == 401
    assert answer.json()["code"] == "invalid_credentials"
    assert cookie(answer, SESSION_COOKIE) is None
    async with AsyncSession(auth_env.admin_engine) as s:
        consecutive = await s.scalar(
            select(AuthFailure.consecutive).where(
                AuthFailure.user_id == who.user_id, AuthFailure.factor == "password"
            )
        )
    assert consecutive == 1


async def test_a_contacts_reset_link_holds_the_contact_rule_and_ends_the_other_sessions(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    await login(auth_env, who)
    old_session = who.session

    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.headers(), json={"email": who.email}
    )
    assert sent.status_code == 202
    [mail] = auth_env.mail.sent
    assert mail.to == who.email
    assert "second factor" not in mail.text
    token = link_token(mail.text)

    short = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": token, "password": "Twelve-chars-x"},
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"
    # Fourteen characters: refused for its length, not by the blocklist -- so
    # a regression to the technicians' 12 cannot pass this line unseen.
    assert short.json()["detail"] == "Use at least 15 characters."

    done = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": token, "password": "Another-Harbor-Kettle-8842"},
    )
    assert done.status_code == 204, done.text
    gone = await auth_env.client.get(
        "/api/v1/portal/me", headers={**who.network, "Cookie": f"{SESSION_COOKIE}={old_session}"}
    )
    assert gone.status_code == 401
    who.session = None
    assert (await login(auth_env, who, password="Another-Harbor-Kettle-8842")).json() == {
        "state": "signed_in"
    }


async def test_a_contact_who_never_accepted_is_sent_nothing(auth_env: Any) -> None:
    who = await _signed_in_contact(auth_env, password=None)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    assert auth_env.mail.sent == []


async def test_a_passkey_only_contact_gets_a_password_that_signs_in_alone(
    auth_env: Any,
) -> None:
    """Operator decisions C5 and C6: the link goes to a contact whose only
    way in is a passkey, promises no second factor, and the password it sets
    signs them in by itself."""
    who = await _signed_in_contact(auth_env, password=None)
    await add_passkey(auth_env, who)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    [mail] = auth_env.mail.sent
    assert "second factor" not in mail.text
    done = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": link_token(mail.text), "password": "Another-Harbor-Kettle-8842"},
    )
    assert done.status_code == 204, done.text
    answer = await login(auth_env, who, password="Another-Harbor-Kettle-8842")
    assert answer.json() == {"state": "signed_in"}


async def test_a_contacts_link_is_dead_once_their_last_way_in_is_gone(auth_env: Any) -> None:
    """Plan decision 4 at the link's use (`_may_still_reset`): a link sent to
    a passkey-only contact, used after that passkey went, sets no password --
    the same 410 as every dead link."""
    who = await _signed_in_contact(auth_env, password=None)
    await add_passkey(auth_env, who)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    [mail] = auth_env.mail.sent
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(delete(AuthPasskey).where(AuthPasskey.user_id == who.user_id))
        await s.commit()
    dead = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": link_token(mail.text), "password": "Another-Harbor-Kettle-8842"},
    )
    assert dead.status_code == 410
    assert dead.json()["code"] == "invalid_link"
    async with AsyncSession(auth_env.admin_engine) as s:
        stored = await s.scalar(
            select(AuthPassword.user_id).where(AuthPassword.user_id == who.user_id)
        )
    assert stored is None


async def test_a_microsoft_only_contact_is_sent_nothing(auth_env: Any) -> None:
    """Operator decision C6: the mailbox does not stand in for the client's
    own Microsoft policy."""
    who = await _signed_in_contact(auth_env, password=None)
    await _linked_to_microsoft(auth_env, who)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    assert auth_env.mail.sent == []


async def test_a_contact_who_turned_a_factor_on_does_not_confirm_with_the_password(
    auth_env: Any,
) -> None:
    """Design §2.6 (plan decision 6): "las acciones de §2.5 siguen pidiendo el
    segundo factor" for the contacts who turned it on."""
    who = await _signed_in_contact(auth_env)
    await _with_totp(auth_env, who)
    refused = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=who.headers(),
        json={"password": CONTACT_PASSWORD},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "method_unavailable"


async def test_a_contact_without_a_password_has_none_to_confirm_with(auth_env: Any) -> None:
    """A contact who signs in with a passkey alone has no password to give
    (plan decision 6): 409, as for a technician."""
    who = await _signed_in_contact(auth_env, password=None)
    await add_passkey(auth_env, who)
    signed = await sign_in(auth_env, who.org_id, who.sub)
    refused = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=signed.headers,
        json={"password": CONTACT_PASSWORD},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "method_unavailable"


async def test_a_contact_confirms_with_the_password_and_a_technician_cannot(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    await login(auth_env, who)
    stale = datetime.now(UTC) - timedelta(minutes=30)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthSession).where(AuthSession.user_id == who.user_id).values(step_up_at=stale)
        )
        await s.commit()

    wrong = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=who.headers(),
        json={"password": "Not-The-Password-41-Harbor"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "factor_failed"
    right = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=who.headers(),
        json={"password": CONTACT_PASSWORD},
    )
    assert right.status_code == 204, right.text
    async with AsyncSession(auth_env.admin_engine) as s:
        fresh = await s.scalar(
            select(AuthSession.step_up_at).where(
                AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
            )
        )
    assert fresh is not None and fresh > stale

    tech = await signed_in(auth_env, who.org_id, "technician")
    refused = await auth_env.client.post(
        "/api/v1/auth/step-up/password", headers=tech.headers(), json={"password": PASSWORD}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "method_unavailable"


async def test_an_unknown_organization_is_answered_as_an_unknown_person(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1 review: a caller that resolves the organization answers a slug
    that names none exactly as it answers an address that names nobody.
    `find_person` resolves it through `its_auth_principal`, so the sign-in
    is the same 401 after the same argon2 work (falsifier 2), and the reset
    route the same 202 with nothing sent."""
    org = await auth_env.create_org("contact-slug")
    who = await contact(auth_env, org)
    await point_at(auth_env, org)

    calls: list[int] = []
    real = PasswordHasher.verify

    def counting(self: PasswordHasher, stored: Any, password: Any) -> Any:
        calls.append(1)
        return real(self, stored, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)

    async def attempt(email: str) -> Any:
        return await auth_env.client.post(
            "/api/v1/auth/login",
            headers=browser(),
            json={"email": email, "password": CONTACT_PASSWORD},
        )

    nobody = await attempt("nobody@acme.test")
    auth_env.settings.default_org_slug = f"no-such-org-{uuid4().hex[:8]}"
    unknown_org = await attempt(who.email)
    assert nobody.status_code == unknown_org.status_code == 401
    assert nobody.json() == unknown_org.json()
    assert unknown_org.json()["code"] == "invalid_credentials"
    assert len(calls) == 2
    assert cookie(unknown_org, SESSION_COOKIE) is None

    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert sent.status_code == 202
    assert auth_env.mail.sent == []

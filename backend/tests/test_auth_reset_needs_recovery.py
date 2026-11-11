"""An administrator's factor reset after a mailed password reset (operator
decision 2026-11-05, "Rechazar y usar recuperación"; final fix wave item 8).

THE HOLE IT CLOSES. Somebody who holds a technician's mailbox resets the
password by mail while the account still has its second factor -- allowed,
because the factor is still asked afterwards (design §2.8). The technician,
now unable to sign in, asks an administrator, whose natural answer is "Reset
sign-in methods". That would leave a factor-less account whose password the
mailbox holder chose: they sign in, enrol their own factor, and own it.

THE RULE. `user_admin.service.reset_factors` is refused (409
`reset_needs_recovery`) while the person's current password was set through a
mailed reset link and no second factor has been verified since -- a passkey, a
confirmed TOTP or a recovery code, at sign-in or at step-up. The mark
(`auth_passwords.mail_reset_at`) is cleared by any later second-factor
verification, by a password change while signed in (or any password not set
by mail), and by the server's recovery link, which stays the way back -- and,
while the mark is set, makes the person choose a new password first.
Invitation-set passwords are not covered: that is onboarding.

THE RACE. `reset_password`'s consume-time factor check and `reset_factors`'
mark check take one per-person lock (`factors.lock_person`), so neither
order of the two can leave a factor-less account with a password the mailbox
set. Both orders are forced below.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import notices, recover
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Role
from app.models.auth import AuthPassword, AuthTotp
from app.modules.user_admin import service
from app.modules.user_admin.service import UserAdminError
from tests.auth_support import (
    Staff,
    browser,
    capture_mail,
    console_ready,
    cookie,
    enrol_totp,
    fresh_code,
    link_token,
    login,
    signed_in,
)
from tests.mcp_support import MCP_BASE, marked_client, mcp_token

NEW_PASSWORD = "Pelican-Orbit-Lantern-58"
REFUSAL = (
    "This person's password was set with an emailed reset link, and no second factor has been "
    "used since. Resetting their sign-in methods now would hand the account to whoever holds "
    "that mailbox. Use the server's recovery command instead, and hand the link to the person "
    "directly."
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


def _reset_links(env: Any) -> list[Any]:
    return [m for m in env.mail.sent if m.subject.lower().startswith("reset your")]


async def _mail_reset(env: Any, who: Staff) -> None:
    """The mailbox holder's act: forgot, then the link, then a new password."""
    forgot = await env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202
    token = link_token([m for m in _reset_links(env) if m.to == who.email][-1].text)
    done = await env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": token, "password": NEW_PASSWORD},
    )
    assert done.status_code == 204, done.text
    who.session, who.device, who.password, who.network = None, None, NEW_PASSWORD, browser()


async def _reset_factors(env: Any, admin: Staff, who: Staff) -> Any:
    return await env.client.post(
        f"/api/v1/users/{who.user_id}/factors/reset", headers=admin.headers()
    )


async def _has_totp(env: Any, who: Staff) -> bool:
    async with AsyncSession(env.admin_engine) as session:
        return await session.get(AuthTotp, who.user_id) is not None


async def _factors_reset_events(env: Any, org: UUID) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org, AuditEvent.action == "user.factors_reset"
                    )
                )
            ).scalars()
        )


async def _mark(env: Any, who: Staff) -> datetime | None:
    async with AsyncSession(env.admin_engine) as session:
        mark: datetime | None = await session.scalar(
            select(AuthPassword.mail_reset_at).where(AuthPassword.user_id == who.user_id)
        )
        return mark


async def _set_mark(env: Any, who: Staff) -> None:
    """The mark as a mail reset leaves it, on a person already signed in --
    so one clearing act can be tested alone."""
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == who.user_id)
            .values(mail_reset_at=datetime.now(UTC))
        )
        await session.commit()


async def test_a_reset_after_a_mail_reset_with_no_factor_since_is_refused(
    auth_env: Any,
) -> None:
    """The rule. 409 with the console's code and the operator's sentence;
    nothing removed, nothing recorded. Delete the check in `reset_factors`
    and this is a 204 with the factors gone: red."""
    org = await auth_env.create_org("rnr-refused")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    assert await _mark(auth_env, who) is not None

    refused = await _reset_factors(auth_env, admin, who)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": REFUSAL, "code": "reset_needs_recovery"}
    assert await _has_totp(auth_env, who)
    assert await _factors_reset_events(auth_env, org) == []


async def test_the_refusal_is_said_in_spanish(auth_env: Any) -> None:
    org = await auth_env.create_org("rnr-es")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)

    refused = await auth_env.client.post(
        f"/api/v1/users/{who.user_id}/factors/reset",
        headers={**admin.headers(), "Accept-Language": "es"},
    )

    assert refused.status_code == 409
    assert refused.json()["code"] == "reset_needs_recovery"
    assert refused.json()["detail"].startswith("La contraseña de esta persona se puso con un")


async def test_a_factor_sign_in_after_the_mail_reset_allows_the_reset(auth_env: Any) -> None:
    """Somebody passed the second factor with the new password: whoever set
    it holds the factor too, so the reset is the administrator's again."""
    org = await auth_env.create_org("rnr-signin")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    done = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who), "remember_device": False},
    )
    assert done.json() == {"state": "signed_in"}, done.text
    assert await _mark(auth_env, who) is None

    assert (await _reset_factors(auth_env, admin, who)).status_code == 204


async def test_a_recovery_code_at_sign_in_after_the_mail_reset_allows_the_reset(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("rnr-code")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    done = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": who.recovery_codes[0], "remember_device": False},
    )
    assert done.json() == {"state": "signed_in"}, done.text

    assert (await _reset_factors(auth_env, admin, who)).status_code == 204


async def test_a_step_up_clears_the_mark(auth_env: Any) -> None:
    """A step-up verifies a factor too. The mark is set by hand on a person
    already signed in, so the step-up is the only act that can clear it."""
    org = await auth_env.create_org("rnr-stepup")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _set_mark(auth_env, who)
    assert (await _reset_factors(auth_env, admin, who)).status_code == 409

    up = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )

    assert up.status_code == 204, up.text
    assert await _mark(auth_env, who) is None
    assert (await _reset_factors(auth_env, admin, who)).status_code == 204


@pytest.mark.parametrize("factor", ["totp", "passkey", "recovery"])
async def test_every_second_factors_success_clears_the_mark_and_the_password_does_not(
    auth_env: Any, factor: str
) -> None:
    """`failures.record_success` is where every verified second factor
    passes -- sign-in and step-up, passkey included; the password's own
    success is not a second factor and leaves the mark."""
    from app.core.auth import failures

    org = await auth_env.create_org(f"rnr-success-{factor}")
    who = await signed_in(auth_env, org, "technician")
    await _set_mark(auth_env, who)
    async with org_session(org, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        await failures.record_success(s, who.user_id, "password", clear_mail_mark=True)
    assert await _mark(auth_env, who) is not None

    async with org_session(org, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        await failures.record_success(
            s, who.user_id, factor, clear_mail_mark=True  # type: ignore[arg-type]
        )

    assert await _mark(auth_env, who) is None


async def test_a_password_changed_while_signed_in_allows_the_reset(auth_env: Any) -> None:
    """A password the person chose from a full session is theirs: no mark.
    The session's step-up is from its enrolment, so no factor is verified
    on the way -- the change itself clears it."""
    org = await auth_env.create_org("rnr-change")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _set_mark(auth_env, who)

    changed = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": NEW_PASSWORD},
    )

    assert changed.status_code == 204, changed.text
    assert await _mark(auth_env, who) is None
    assert (await _reset_factors(auth_env, admin, who)).status_code == 204


async def test_a_password_set_by_invitation_is_not_covered(auth_env: Any) -> None:
    """Onboarding by design: the invitation's password leaves no mark."""
    org = await auth_env.create_org("rnr-invite")
    admin = await signed_in(auth_env, org, "org-admin")
    async with AsyncSession(auth_env.admin_engine) as session:
        tech = (
            await session.execute(
                select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
            )
        ).scalar_one()
    invited = await auth_env.client.post(
        "/api/v1/users/invite",
        headers=admin.headers(),
        json={
            "email": "rnr-ana@toeshee.test",
            "display_name": "Ana",
            "grants": [{"role_id": str(tech)}],
        },
    )
    assert invited.status_code == 201, invited.text
    user_id = UUID(invited.json()["user_id"])
    mail = [m for m in auth_env.mail.sent if m.to == "rnr-ana@toeshee.test"][-1]
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": link_token(mail.text), "password": NEW_PASSWORD},
    )
    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    async with AsyncSession(auth_env.admin_engine) as session:
        mark = await session.scalar(
            select(AuthPassword.mail_reset_at).where(AuthPassword.user_id == user_id)
        )
    assert mark is None

    reset = await auth_env.client.post(
        f"/api/v1/users/{user_id}/factors/reset", headers=admin.headers()
    )

    assert reset.status_code == 204, reset.text


async def test_the_recovery_link_clears_the_mark(auth_env: Any) -> None:
    """The way back the refusal names: the recovery link removes the factors
    and the mark together, so the administrator's reset is allowed again.
    With the mark set it asks for a new password first (operator decision
    2026-11-05, «Pedir contraseña nueva si hay marca»;
    `tests/test_auth_recover_command.py`), so the person gives one."""
    org = await auth_env.create_org("rnr-recover")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    link = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)

    accepted = await auth_env.client.post(
        "/api/v1/auth/recover/accept",
        headers=browser(),
        json={"token": link.split("#", 1)[1], "password": "Saffron-Harbour-Quartz-4127"},
    )

    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    who.session = cookie(accepted, "__Host-itms_session")
    assert await _mark(auth_env, who) is None
    await enrol_totp(auth_env, who)
    assert (await _reset_factors(auth_env, admin, who)).status_code == 204


async def test_the_service_refuses_whatever_the_channel(auth_env: Any) -> None:
    """Console and MCP share `service.reset_factors`: called directly, it
    raises the same 409 and removes nothing."""
    org = await auth_env.create_org("rnr-service")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)

    with pytest.raises(UserAdminError) as refused:
        async with org_session(
            org, engine=auth_env.app_engine, clients=ClientScope.all()
        ) as session:
            await service.reset_factors(
                session,
                org_id=org,
                actor_id=admin.user_id,
                user_id=who.user_id,
                vault=auth_env.app.state.vault,
            )

    assert (refused.value.status_code, refused.value.code) == (409, "reset_needs_recovery")
    assert await _has_totp(auth_env, who)


async def test_the_mcp_channel_is_refused_too(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the MCP channel the route refuses even earlier: it needs the
    administrator's own recent console step-up, which no MCP call carries
    (`deps.require_step_up`, `NOT_FOR_MCP`). Either way the factors stay."""
    org = await auth_env.create_org("rnr-mcp")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    monkeypatch.setattr(auth_env.app.state.settings, "public_console_url", MCP_BASE)
    token = mcp_token(auth_env, await auth_env.org_slug(org), admin.sub)

    async with marked_client(auth_env) as client:
        refused = await client.post(
            f"/api/v1/users/{who.user_id}/factors/reset",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert refused.status_code in (403, 409), refused.text
    assert refused.status_code != 204
    assert await _has_totp(auth_env, who)
    assert await _factors_reset_events(auth_env, org) == []


@pytest.mark.parametrize("method", ["totp", "recovery", "passkey"])
async def test_a_step_up_on_a_session_the_mail_reset_ended_clears_nothing(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Re-review of fix round 1, Important 1 (the reviewer's probe, kept).
    The person's step-up is admitted on a live session; the mailbox holder's
    reset then commits -- the mark set, every session ended; the step-up's
    factor then verifies. `record_success` clears the mark in that
    transaction, so the step-up must see that its session is gone and roll
    the whole transaction back: a dead session's answer, the mark still set,
    and the administrator's reset still refused. Let `mark_step_up` stay a
    silent no-op on an ended row and the step-up commits, the mark is gone
    and the reset is a 204: red."""
    from app.core.auth import flows
    from app.core.auth.refusals import AuthRefusal
    from app.core.auth.sessions import resolve
    from app.models.auth import AuthPasskey
    from tests.auth_support import add_passkey
    from tests.session_support import cookie_header, http_request

    org = await auth_env.create_org(f"rnr-stepup-race-{method}")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    passkey = await add_passkey(auth_env, who) if method == "passkey" else None
    request = http_request(auth_env, cookie=cookie_header(who.session))
    principal = await resolve(request, touch=False)
    assert principal is not None and principal.factor_done
    code = await fresh_code(auth_env, who)

    await _mail_reset(auth_env, who)
    assert await _mark(auth_env, who) is not None

    with pytest.raises(AuthRefusal) as refused:
        if method == "totp":
            await flows.step_up_totp(request, principal, code=code)
        elif method == "recovery":
            await flows.step_up_recovery(request, principal, code=who.recovery_codes[0])
        else:

            async def verify(session: Any, credential: Any, **_kw: Any) -> AuthPasskey:
                return (
                    await session.execute(select(AuthPasskey).where(AuthPasskey.id == passkey))
                ).scalar_one()

            monkeypatch.setattr(flows, "verify_authentication", verify)
            await flows.step_up_passkey(request, principal, credential={"id": "mine"})

    assert (refused.value.status_code, refused.value.code) == (401, "auth_required")
    assert await _mark(auth_env, who) is not None
    reset = await _reset_factors(auth_env, admin, who)
    assert reset.status_code == 409, reset.text
    assert reset.json()["code"] == "reset_needs_recovery"


# --- the race: both orders, forced ---------------------------------------


async def test_a_reset_link_used_while_a_factor_reset_is_open_waits_and_is_refused(
    auth_env: Any,
) -> None:
    """Order 1. `reset_factors` has taken the person's lock and removed the
    factors, uncommitted; the mailbox holder uses a link sent before. The
    link waits for the lock, then sees no factor and is refused (410, spent).
    Without the lock, it reads the factors still there, sets the password,
    and commits after the reset: a factor-less account with the mailbox's
    password -- the reset link answers 204: red."""
    org = await auth_env.create_org("rnr-race-1")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    forgot = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202
    token = link_token(_reset_links(auth_env)[-1].text)

    async with org_session(org, engine=auth_env.app_engine, clients=ClientScope.all()) as session:
        await service.reset_factors(
            session,
            org_id=org,
            actor_id=admin.user_id,
            user_id=who.user_id,
            vault=auth_env.app.state.vault,
        )
        used = asyncio.create_task(
            auth_env.client.post(
                "/api/v1/auth/password/reset",
                headers=browser(),
                json={"token": token, "password": NEW_PASSWORD},
            )
        )
        await asyncio.sleep(0.5)
        assert not used.done(), "the link must wait for the person's lock"
    answer = await used

    assert answer.status_code == 410, answer.text
    who.session, who.network = None, browser()
    assert (await login(auth_env, who, password=NEW_PASSWORD)).status_code == 401


async def test_a_factor_reset_while_a_mail_reset_is_open_waits_and_is_refused(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order 2. `reset_password` holds the person's lock and has set the
    password and the mark, uncommitted (paused at its notice); the
    administrator resets the factors. The reset waits for the lock, then
    sees the mark and is refused (409). Without the lock, it reads no mark,
    removes the factors after the mail reset commits, and answers 204: red."""
    org = await auth_env.create_org("rnr-race-2")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    forgot = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202
    token = link_token(_reset_links(auth_env)[-1].text)
    paused, release = asyncio.Event(), asyncio.Event()
    real_tell = notices.tell

    async def held_tell(session: Any, user: Any, kind: Any, **kwargs: Any) -> bool:
        if kind == "password_changed":
            paused.set()
            await release.wait()
        return await real_tell(session, user, kind, **kwargs)

    monkeypatch.setattr(notices, "tell", held_tell)
    used = asyncio.create_task(
        auth_env.client.post(
            "/api/v1/auth/password/reset",
            headers=browser(),
            json={"token": token, "password": NEW_PASSWORD},
        )
    )
    await asyncio.wait_for(paused.wait(), timeout=10)
    reset = asyncio.create_task(_reset_factors(auth_env, admin, who))
    await asyncio.sleep(0.5)
    assert not reset.done(), "the factor reset must wait for the person's lock"
    release.set()

    assert (await used).status_code == 204
    answer = await reset
    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == "reset_needs_recovery"
    assert await _has_totp(auth_env, who)

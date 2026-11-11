"""The platform's own technician lifecycle (P2 phase 1 Task 8, design §2.4, §7).

- An invitation is a 72-hour, single-use link; the invitee chooses a password
  and goes straight to enrolling the first factor. The link is shown to the
  administrator once, and only when no mail carried it.
- Resend replaces the link; cancel kills it and the account.
- Resetting another person's factors needs the administrator's OWN second
  factor confirmed in the last ten minutes (design §2.5), wipes the factors,
  ends the person's sessions, forgets their devices and tells them.
- Disabling a person ends their sessions and forgets their devices at once
  (design §2.4); ending their sessions is an act of its own.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens
from app.core.auth.passwords import STAFF_MINIMUM, PasswordRejected, hash_password
from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE
from app.models import AuditEvent, Organization, Role, User, UserRole
from app.models.auth import (
    AuthActionToken,
    AuthPassword,
    AuthSession,
    AuthTotp,
    AuthTrustedDevice,
)
from app.modules.user_admin import api as user_admin_api
from tests.auth_support import (
    CONSOLE,
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


@pytest.fixture()
def lifecycle_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _role(env: Any, name: str) -> UUID:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(select(Role.id).where(Role.name == name, Role.org_id.is_(None)))
        ).scalar_one()


async def _audit(env: Any, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _invite(env: Any, headers: dict[str, str], email: str = "ana@toeshee.test") -> Any:
    tech = await _role(env, "technician")
    return await env.client.post(
        "/api/v1/users/invite",
        headers=headers,
        json={"email": email, "display_name": "Ana", "grants": [{"role_id": str(tech)}]},
    )


async def _age_step_up(env: Any, user_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()


async def _row_of(env: Any, sub: str) -> User:
    """A row `ApiEnv.create_user` made: no password, no invitation -- what
    every account from before this phase looks like."""
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()


async def _hand_issued_invite(env: Any, org_id: UUID, user_id: UUID) -> str:
    """A live INVITE link for a row no invitation made, written straight to
    the database: the state a route must not trust, however it arose."""
    async with AsyncSession(env.admin_engine) as session:
        token = await action_tokens.issue(
            session,
            org_id=org_id,
            user_id=user_id,
            purpose=action_tokens.INVITE,
            ttl=action_tokens.INVITE_TTL,
            created_by=None,
        )
        await session.commit()
    return token


async def _invite_tokens(env: Any, user_id: UUID) -> list[AuthActionToken]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuthActionToken).where(
                        AuthActionToken.user_id == user_id,
                        AuthActionToken.purpose == action_tokens.INVITE,
                    )
                )
            ).scalars()
        )


async def _live_devices(env: Any, user_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as session:
        return len(
            (
                await session.execute(
                    select(AuthTrustedDevice.id).where(
                        AuthTrustedDevice.user_id == user_id,
                        AuthTrustedDevice.revoked_at.is_(None),
                    )
                )
            ).all()
        )


async def test_an_invitee_chooses_a_password_and_goes_on_to_the_first_factor(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("invite")
    admin = await signed_in(env, org, "org-admin")
    invited = await _invite(env, admin.headers())
    assert invited.status_code == 201, invited.text
    assert invited.json()["invite_link"] is None
    mail = env.mail.sent[-1]
    assert mail.to == "ana@toeshee.test"
    token = link_token(mail.text)

    details = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    weak = await env.client.post(
        "/api/v1/auth/invite/accept", headers=browser(), json={"token": token, "password": "short"}
    )
    network = browser()
    accepted = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=network,
        json={"token": token, "password": "Pelican-Orbit-Lantern-58"},
    )
    again = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Pelican-Orbit-Lantern-59"},
    )

    # Re-aimed by own authentication phase 2, D40 (Task 8): the page also
    # learns how the invitation may be accepted -- a technician's, with a
    # 12-character password, no passkey and (not set up here) no Microsoft.
    assert details.json() == {
        "email": "ana@toeshee.test",
        "display_name": "Ana",
        "kind": "staff",
        "password_minimum": 12,
        "passkey": False,
        "microsoft": False,
    }
    assert weak.status_code == 422
    assert weak.json() == {"code": "password_rejected", "detail": "Use at least 12 characters."}
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "enroll_required"}
    assert again.status_code == 410 and again.json()["code"] == "invalid_link"
    # SINGLE USE, held by the link itself and not only by the password row the
    # acceptance wrote: the one invitation is spent. `consume` -> `peek` in
    # `flows.accept_invite` leaves it live, and this goes red.
    async with AsyncSession(env.admin_engine) as session:
        spent = (
            await session.execute(
                select(AuthActionToken.used_at).where(
                    AuthActionToken.user_id == UUID(invited.json()["user_id"]),
                    AuthActionToken.purpose == "invite",
                )
            )
        ).scalars().all()
    assert len(spent) == 1 and spent[0] is not None
    pending = cookie(accepted, SESSION_COOKIE)
    assert pending
    ana = Staff(
        org,
        UUID(invited.json()["user_id"]),
        "",
        "ana@toeshee.test",
        "Pelican-Orbit-Lantern-58",
        network,
        session=pending,
    )
    await enrol_totp(env, ana)
    assert (await env.client.get("/api/v1/me", headers=ana.headers())).status_code == 200
    changed = await _audit(env, org, "auth.password_changed")
    assert [e.meta for e in changed][-1] == {"via": "invite"}
    listed = (await env.client.get("/api/v1/users", headers=admin.headers())).json()["users"]
    row = next(u for u in listed if u["id"] == invited.json()["user_id"])
    assert (row["invite_pending"], row["invite_expires_at"]) == (False, None)


async def test_the_link_is_shown_once_when_no_mail_could_carry_it(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("invite-nomail")
    admin = await signed_in(env, org, "org-admin")
    env.mail.configured = False

    invited = await _invite(env, admin.headers())
    listed = await env.client.get("/api/v1/users", headers=admin.headers())

    link = invited.json()["invite_link"]
    assert link and link.startswith(f"{CONSOLE}/invite#")
    token = link.split("#", 1)[1]
    assert token not in listed.text
    row = next(u for u in listed.json()["users"] if u["id"] == invited.json()["user_id"])
    assert row["invite_pending"] is True and row["invite_expires_at"] is not None
    events = await _audit(env, org, "user.invited")
    assert [e.meta["link_shown"] for e in events] == [True]
    assert token not in str(events[0].meta)


async def test_resend_replaces_the_link_and_cancel_kills_it_with_the_account(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("invite-resend")
    admin = await signed_in(env, org, "org-admin")
    env.mail.configured = False
    invited = (await _invite(env, admin.headers())).json()
    first = invited["invite_link"].split("#", 1)[1]

    resent = await env.client.post(
        f"/api/v1/users/{invited['user_id']}/invite/resend", headers=admin.headers()
    )
    assert set(resent.json()) == {"invite_link"}
    second = resent.json()["invite_link"].split("#", 1)[1]
    old = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": first}
    )
    cancelled = await env.client.delete(
        f"/api/v1/users/{invited['user_id']}/invite", headers=admin.headers()
    )
    new = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": second}
    )

    assert resent.status_code == 200 and second != first
    assert old.status_code == 410
    assert cancelled.status_code == 204
    assert new.status_code == 410
    async with AsyncSession(env.admin_engine) as session:
        user = await session.get(User, UUID(invited["user_id"]))
        grants = (
            await session.execute(
                select(UserRole.id).where(UserRole.user_id == UUID(invited["user_id"]))
            )
        ).scalars().all()
    assert user is not None and user.status == "deleted" and grants == []
    # The link itself is spent too, not only refused because the account is
    # deleted: a later re-enable of the row must not bring it back to life.
    async with AsyncSession(env.admin_engine) as session:
        live = (
            await session.execute(
                select(AuthActionToken.id).where(
                    AuthActionToken.user_id == UUID(invited["user_id"]),
                    AuthActionToken.used_at.is_(None),
                )
            )
        ).all()
    assert live == []
    resent_events = await _audit(env, org, "user.invite_resent")
    assert [e.action for e in resent_events] == ["user.invite_resent"]
    assert len(await _audit(env, org, "user.invite_cancelled")) == 1


async def test_an_accepted_invitation_is_neither_resent_nor_cancelled(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("invite-accepted")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")

    resend = await env.client.post(
        f"/api/v1/users/{member.user_id}/invite/resend", headers=admin.headers()
    )
    cancel = await env.client.delete(
        f"/api/v1/users/{member.user_id}/invite", headers=admin.headers()
    )

    assert resend.status_code == 409 and cancel.status_code == 409
    assert resend.json()["detail"] == "this person has already accepted their invitation"


async def test_a_resend_needs_a_pending_invitation(lifecycle_env: Any) -> None:
    """Task 8 review, Critical. "No password" is not "invited": every account
    from before this phase has no password, and a resend to one used to hand
    the administrator a link that sets that person's password. Plan decision 8's
    pending invitation -- an unused INVITE link of somebody without a password,
    the predicate behind `invite_pending` -- is the gate. A never-invited row
    is a 409: no link, no mail, no token written."""
    env = lifecycle_env
    org = await env.create_org("resend-uninvited")
    admin = await signed_in(env, org, "org-admin")
    victim = await _row_of(env, await env.create_user(org, "technician"))
    env.mail.configured = False

    resent = await env.client.post(
        f"/api/v1/users/{victim.id}/invite/resend", headers=admin.headers()
    )

    assert resent.status_code == 409, resent.text
    assert resent.json() == {"detail": "this person has no pending invitation"}
    assert [m for m in env.mail.sent if m.to == victim.email] == []
    assert await _invite_tokens(env, victim.id) == []
    assert await _audit(env, org, "user.invite_resent") == []


async def test_a_super_admin_is_never_invited(lifecycle_env: Any) -> None:
    """Task 8 review, Critical: the reviewer's chain (resend -> accept -> act as
    the super-admin). Refused at the resend however it is tried -- a row with
    no invitation is the 409 above, and one that somehow carries a live INVITE
    link is the super-admin rail's 403 -- and refused again where the link is
    followed: `flows._invited` answers the shared 410 for a super-admin, so the
    hand-issued link sets no password."""
    env = lifecycle_env
    org = await env.create_org("resend-su")
    admin = await signed_in(env, org, "org-admin")
    root = await _row_of(env, await env.create_user(org, "super-admin"))
    env.mail.configured = False

    plain = await env.client.post(
        f"/api/v1/users/{root.id}/invite/resend", headers=admin.headers()
    )
    token = await _hand_issued_invite(env, org, root.id)
    railed = await env.client.post(
        f"/api/v1/users/{root.id}/invite/resend", headers=admin.headers()
    )
    details = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    accepted = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Pelican-Orbit-Lantern-58"},
    )

    assert plain.status_code == 409, plain.text
    assert "invite_link" not in plain.json()
    assert railed.status_code == 403, railed.text
    assert "invite_link" not in railed.json()
    assert (details.status_code, details.json()["code"]) == (410, "invalid_link")
    assert (accepted.status_code, accepted.json()["code"]) == (410, "invalid_link")
    assert cookie(accepted, SESSION_COOKIE) is None
    async with AsyncSession(env.admin_engine) as session:
        assert await session.get(AuthPassword, root.id) is None
    # The hand-issued link is the only one: the refused resend issued none.
    assert len(await _invite_tokens(env, root.id)) == 1
    assert [m for m in env.mail.sent if m.to == root.email] == []


async def test_an_expired_invitation_can_still_be_resent(lifecycle_env: Any) -> None:
    """Plan decision 8: an invitation whose link has expired is still pending --
    the console offers to resend it -- so the gate counts it. The non-vacuity
    half of the two tests above: a gate that also demanded an unexpired link
    would refuse this one."""
    env = lifecycle_env
    org = await env.create_org("resend-expired")
    admin = await signed_in(env, org, "org-admin")
    env.mail.configured = False
    invited = (await _invite(env, admin.headers())).json()
    old = invited["invite_link"].split("#", 1)[1]
    # Issued 73 hours ago, so it expired an hour ago (`expires_at > created_at`
    # is a CHECK of migration 0245).
    now = datetime.now(UTC)
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthActionToken)
            .where(AuthActionToken.user_id == UUID(invited["user_id"]))
            .values(created_at=now - timedelta(hours=73), expires_at=now - timedelta(hours=1))
        )
        await session.commit()

    listed = (await env.client.get("/api/v1/users", headers=admin.headers())).json()["users"]
    row = next(u for u in listed if u["id"] == invited["user_id"])
    dead = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": old}
    )
    resent = await env.client.post(
        f"/api/v1/users/{invited['user_id']}/invite/resend", headers=admin.headers()
    )

    assert row["invite_pending"] is True
    assert dead.status_code == 410
    assert resent.status_code == 200, resent.text
    fresh = resent.json()["invite_link"].split("#", 1)[1]
    details = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": fresh}
    )
    # Re-aimed by own authentication phase 2, D40 (Task 8): the page also
    # learns how the invitation may be accepted -- a technician's, with a
    # 12-character password, no passkey and (not set up here) no Microsoft.
    assert details.json() == {
        "email": "ana@toeshee.test",
        "display_name": "Ana",
        "kind": "staff",
        "password_minimum": 12,
        "passkey": False,
        "microsoft": False,
    }


async def test_a_cancel_needs_a_pending_invitation(lifecycle_env: Any) -> None:
    """Task 8 review, Important: cancelling is a second way to mark an account
    deleted, so it takes the same gate as the resend. A never-invited account
    without a password is a 409 and stays as it was -- active, its grants
    intact."""
    env = lifecycle_env
    org = await env.create_org("cancel-uninvited")
    admin = await signed_in(env, org, "org-admin")
    victim = await _row_of(env, await env.create_user(org, "technician"))

    cancelled = await env.client.delete(
        f"/api/v1/users/{victim.id}/invite", headers=admin.headers()
    )

    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json() == {"detail": "this person has no pending invitation"}
    async with AsyncSession(env.admin_engine) as session:
        row = await session.get(User, victim.id)
        grants = (
            await session.execute(select(UserRole.id).where(UserRole.user_id == victim.id))
        ).scalars().all()
    assert row is not None and row.status == "active" and len(grants) == 1
    assert await _audit(env, org, "user.invite_cancelled") == []


async def test_nobody_cancels_their_own_account(lifecycle_env: Any) -> None:
    """Task 8 review, Important: `delete_user`'s own-account rail, on the other
    delete path. The caller has a pending invitation of their own (a live
    INVITE link, no password) and another org-admin exists, so nothing else
    refuses: only the rail stands between this request and a deleted caller."""
    env = lifecycle_env
    org = await env.create_org("cancel-self")
    await env.create_user(org, "org-admin")
    caller_sub = await env.create_user(org, "org-admin")
    caller = await _row_of(env, caller_sub)
    await _hand_issued_invite(env, org, caller.id)
    headers = await env.headers(org, caller_sub)

    response = await env.client.delete(f"/api/v1/users/{caller.id}/invite", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "you cannot delete your own account"}
    async with AsyncSession(env.admin_engine) as session:
        row = await session.get(User, caller.id)
    assert row is not None and row.status == "active"


async def test_an_invitation_is_dead_once_the_person_has_a_password(lifecycle_env: Any) -> None:
    """An invitation is for somebody who has not chosen a password
    (`flows._invited`). Should a live link ever outlast that -- a password
    written by another path while the link was still open -- the link must
    not be a second way to set one: an old invitation in a mailbox would
    otherwise overwrite the person's password. Drop the `AuthPassword` check
    from `_invited` and both answers turn 200."""
    env = lifecycle_env
    org = await env.create_org("invite-haspw")
    admin = await signed_in(env, org, "org-admin")
    invited = await _invite(env, admin.headers())
    token = link_token(env.mail.sent[-1].text)
    user_id = UUID(invited.json()["user_id"])
    digest, version = await hash_password(org, "Pelican-Orbit-Lantern-58")
    async with AsyncSession(env.admin_engine) as session:
        session.add(
            AuthPassword(
                user_id=user_id,
                org_id=org,
                hash=digest,
                pepper_version=version,
                changed_at=datetime.now(UTC),
            )
        )
        await session.commit()

    details = await env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    accepted = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Marmalade-Quasar-Ferry-71"},
    )

    assert (details.status_code, details.json()["code"]) == (410, "invalid_link")
    assert (accepted.status_code, accepted.json()["code"]) == (410, "invalid_link")
    async with AsyncSession(env.admin_engine) as session:
        stored = await session.get(AuthPassword, user_id)
    assert stored is not None and stored.hash == digest


async def test_a_factor_reset_needs_the_administrators_own_recent_confirmation(
    lifecycle_env: Any,
) -> None:
    """Design §2.5 and plan decision 5: eleven minutes after the
    administrator's own factor, the reset is a 403 and nothing is wiped.
    Delete the `require_step_up(ctx)` line in `api.py::reset_factors` and the
    member's TOTP is gone."""
    env = lifecycle_env
    org = await env.create_org("reset-gate")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    await _age_step_up(env, admin.user_id)

    stale = await env.client.post(
        f"/api/v1/users/{member.user_id}/factors/reset", headers=admin.headers()
    )

    assert stale.status_code == 403 and stale.json()["code"] == "step_up_required"
    async with AsyncSession(env.admin_engine) as session:
        assert await session.get(AuthTotp, member.user_id) is not None


async def test_a_factor_reset_wipes_the_factors_ends_the_sessions_and_tells_the_person(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("reset")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    member.session = None
    await login(env, member)
    done = await env.client.post(
        "/api/v1/auth/factor/totp",
        headers=member.headers(),
        json={"code": await fresh_code(env, member), "remember_device": True},
    )
    member.session, member.device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    assert await _live_devices(env, member.user_id) == 1

    reset = await env.client.post(
        f"/api/v1/users/{member.user_id}/factors/reset", headers=admin.headers()
    )

    assert reset.status_code == 204, reset.text
    assert (await env.client.get("/api/v1/me", headers=member.headers())).status_code == 401
    # Design §7: the remembered browser is forgotten too, or it would stand in
    # for the factor that was just removed.
    assert await _live_devices(env, member.user_id) == 0
    member.session = None
    assert (await login(env, member)).json() == {"state": "enroll_required"}
    assert any(m.to == member.email and "reset" in m.subject.lower() for m in env.mail.sent)
    events = await _audit(env, org, "user.factors_reset")
    assert [(e.actor_id, e.meta["via"]) for e in events] == [(admin.user_id, "administrator")]


async def test_nobody_resets_their_own_factors_here(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("reset-self")
    admin = await signed_in(env, org, "org-admin")

    response = await env.client.post(
        f"/api/v1/users/{admin.user_id}/factors/reset", headers=admin.headers()
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "you cannot reset the sign-in methods of your own account"


async def test_disable_signs_the_person_out_and_forgets_their_devices(lifecycle_env: Any) -> None:
    """Design §2.4: "Desactivar a una persona corta sus sesiones ... y sus equipos
    recordados al instante". Delete the two calls in `service.set_enabled` and the
    re-enabled person signs straight back in through the remembered device --
    or, with only the session end gone, the old cookie works again the moment
    the account is re-enabled."""
    env = lifecycle_env
    org = await env.create_org("disable")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    member.session = None
    await login(env, member)
    done = await env.client.post(
        "/api/v1/auth/factor/totp",
        headers=member.headers(),
        json={"code": await fresh_code(env, member), "remember_device": True},
    )
    member.session, member.device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    before_disable = member.session

    disabled = await env.client.post(
        f"/api/v1/users/{member.user_id}/disable", headers=admin.headers()
    )
    cut = await env.client.get("/api/v1/me", headers=member.headers())
    await env.client.post(f"/api/v1/users/{member.user_id}/enable", headers=admin.headers())
    member.session = before_disable
    still_cut = await env.client.get("/api/v1/me", headers=member.headers())
    member.session = None
    after = await login(env, member)

    assert disabled.status_code == 200 and cut.status_code == 401
    assert still_cut.status_code == 401
    assert after.json()["state"] == "factor_required"


async def test_an_administrator_ends_a_persons_sessions(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("sessions")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")

    ended = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert ended.status_code == 204
    assert (await env.client.get("/api/v1/me", headers=member.headers())).status_code == 401
    assert (await env.client.get("/api/v1/me", headers=admin.headers())).status_code == 200
    events = await _audit(env, org, "auth.sessions_ended")
    assert [(e.actor_id, e.target_id, e.meta) for e in events] == [
        (admin.user_id, str(member.user_id), {"by": "administrator", "scope": "everywhere"})
    ]


async def test_ending_a_deleted_persons_sessions_is_refused_and_records_nothing(
    lifecycle_env: Any,
) -> None:
    """Final review C-M5 (the ledger's Task 8 minor): `end_sessions` checks
    its target like every other lifecycle act (`_require_live`). A deleted
    account has no session to end -- deleting ended them -- so the act is a
    409 and writes no `auth.sessions_ended` for it. Delete the check and this
    is a 204 with a record about nobody: red. No super-admin rail here
    (operator decision 2, 2026-11-05)."""
    env = lifecycle_env
    org = await env.create_org("sessions-deleted")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    gone = await env.client.delete(f"/api/v1/users/{member.user_id}", headers=admin.headers())
    assert gone.status_code == 204, gone.text

    ended = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert ended.status_code == 409, ended.text
    assert await _audit(env, org, "auth.sessions_ended") == []


async def test_an_invitation_needs_the_console_address(
    lifecycle_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `ITS_PUBLIC_CONSOLE_URL` no invitation is made over HTTP. Since
    own authentication Task 13 the CSRF rule (Task 6) refuses the cookie write
    first -- 403 `csrf`, there is no console origin to compare with -- so the
    route's own 503 is held directly by the next test; either way nobody and
    no link is made. (It sent the console's Bearer, which the CSRF rule does
    not judge, and met the route's 503, until the Bearer went.)"""
    env = lifecycle_env
    org = await env.create_org("invite-noconsole")
    admin = await signed_in(env, org, "org-admin")
    monkeypatch.setattr(env.app.state.settings, "public_console_url", None)

    response = await _invite(env, admin.headers(), email="nobody-made@toeshee.test")

    assert response.status_code == 403
    assert response.json()["code"] == "csrf"
    async with AsyncSession(env.admin_engine) as session:
        made = (
            await session.execute(select(User.id).where(User.email == "nobody-made@toeshee.test"))
        ).first()
        links = (
            await session.execute(
                select(func.count())
                .select_from(AuthActionToken)
                .where(AuthActionToken.org_id == org)
            )
        ).scalar_one()
    assert made is None
    assert links == 0


@pytest.mark.parametrize("unset", [None, ""])
def test_the_invitation_route_refuses_to_make_a_link_without_the_console_address(
    unset: str | None,
) -> None:
    """`user_admin.api._console_url`, the route's own refusal: an invitation
    link is made from `ITS_PUBLIC_CONSOLE_URL` and never from the request's
    Host header, so without it the route says so with a 503 before anything
    is written. Unreachable over HTTP since Task 13 (above), so held here."""

    def request(url: str | None) -> Any:
        settings = SimpleNamespace(public_console_url=url)
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    with pytest.raises(HTTPException) as refused:
        user_admin_api._console_url(request(unset))
    assert refused.value.status_code == 503
    assert user_admin_api._console_url(request(CONSOLE)) == CONSOLE


async def test_an_invited_technician_has_a_password_row_only_after_accepting(
    lifecycle_env: Any,
) -> None:
    """Rule 1 of `service.py`: the invitation writes no password."""
    env = lifecycle_env
    org = await env.create_org("invite-nopw")
    admin = await signed_in(env, org, "org-admin")
    invited = await _invite(env, admin.headers())
    async with AsyncSession(env.admin_engine) as session:
        assert await session.get(AuthPassword, UUID(invited.json()["user_id"])) is None


async def test_an_over_long_password_is_refused_without_repeating_it(lifecycle_env: Any) -> None:
    """Task 7's decision 16: `InviteAcceptIn.password` has no Pydantic bound,
    because a field failure repeats its INPUT in the 422 and the input is the
    invitee's password. The rule's `too_long` refuses it -- 422
    `password_rejected`, the body never repeating it -- and the link stays
    usable. A `max_length` back on the field turns this red."""
    env = lifecycle_env
    org = await env.create_org("invite-long")
    admin = await signed_in(env, org, "org-admin")
    await _invite(env, admin.headers())
    token = link_token(env.mail.sent[-1].text)
    long_password = "Pelican-Orbit-Lantern-58" * 100

    refused = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": long_password},
    )
    accepted = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Pelican-Orbit-Lantern-58"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "password_rejected"
    assert long_password not in refused.text
    assert accepted.status_code == 200, accepted.text


async def test_an_invitees_password_is_checked_against_the_organizations_name(
    lifecycle_env: Any,
) -> None:
    """Task 2's decision 5, as Task 7's `staff_password.require_acceptable`
    applies it: the organization's name is context. Drop `org_name=` from
    `flows.accept_invite` and this password -- 16 characters, on no list,
    built on nothing but that name -- is taken: red."""
    env = lifecycle_env
    org = await env.create_org("invite-orgname")
    admin = await signed_in(env, org, "org-admin")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization).where(Organization.id == org).values(name="Quokkaworks Ltd")
        )
        await session.commit()
    await _invite(env, admin.headers())
    token = link_token(env.mail.sent[-1].text)

    refused = await env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Quokkaworks 2026"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason="context", minimum=STAFF_MINIMUM).message,
    }


async def test_a_made_up_link_is_the_same_410_on_every_link_page(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("dead-links")
    # A made-up token, and the empty one a page sends after a reload took the
    # fragment away (Task 11's `useLinkToken`): both are an unknown link, so a
    # length rule on `token` (a 422) would show the person a validation error
    # instead of "this link is no longer valid".
    password = "Pelican-Orbit-58x"
    for made_up in (f"{org.hex}.{'x' * 43}", ""):
        answers = [
            await env.client.post(path, headers=browser(), json=body)
            for path, body in (
                ("/api/v1/auth/invite/details", {"token": made_up}),
                ("/api/v1/auth/invite/accept", {"token": made_up, "password": password}),
                ("/api/v1/auth/recover/details", {"token": made_up}),
                ("/api/v1/auth/recover/accept", {"token": made_up}),
                ("/api/v1/auth/password/reset", {"token": made_up, "password": password}),
            )
        ]
        seen = [(a.status_code, a.json()["code"]) for a in answers]
        assert seen == [(410, "invalid_link")] * 5, made_up


async def test_an_over_long_token_is_a_dead_link_and_is_not_repeated(lifecycle_env: Any) -> None:
    """A one-time link's token is a secret, so it carries no Pydantic bound
    (Global Constraints; Task 7's review made `ResetIn.token` a plain `str`):
    a field failure would repeat it in a 422. Over-long or malformed, it is the
    same 410 `invalid_link` as every other dead link, on the four routes this
    task adds, and the body never contains it. A `max_length` back on
    `LinkTokenIn.token` or `InviteAcceptIn.token` turns this red."""
    env = lifecycle_env
    org = await env.create_org("long-token")
    over_long = f"{org.hex}.{'Q' * 4000}"
    answers = {
        path: await env.client.post(path, headers=browser(), json=body)
        for path, body in (
            ("/api/v1/auth/invite/details", {"token": over_long}),
            ("/api/v1/auth/invite/accept", {"token": over_long, "password": "Pelican-Orbit-58x"}),
            ("/api/v1/auth/recover/details", {"token": over_long}),
            ("/api/v1/auth/recover/accept", {"token": over_long}),
        )
    }

    for path, answer in answers.items():
        assert (answer.status_code, answer.json()["code"]) == (410, "invalid_link"), (
            path,
            answer.text[:300],
        )
        assert over_long not in answer.text, path
        assert "Q" * 200 not in answer.text, path

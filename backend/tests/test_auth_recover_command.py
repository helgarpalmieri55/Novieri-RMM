"""`python -m app.core.auth.recover --email <address>` (design §7, Q5; falsifier 9).

For the one administrator who lost every second factor and has nobody to
reset them: a one-time, 15-minute link, printed on the server's terminal.
Following it (`POST /auth/recover/accept`) removes the factors, forgets the
devices, ends the sessions and opens a session that goes straight to
enrolling a new factor; the password does not change (plan decision 10) --
unless the person sets a new one, which the page always offers and requires
while an emailed link set the current one (operator decisions 2026-11-05,
«Pedir contraseña nueva si hay marca» and «Ofrecerla siempre, aplicar»; the
tests at the end). Audited at both ends, and the holder is told.

FALSIFIER 9: it does not exist over HTTP -- no route reaches the module,
nothing in `app/` imports it, it builds no router, and no other module issues a
`recover` link.
"""

import ast
import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import notices, recover
from app.core.auth.sessions import SESSION_COOKIE
from app.core.router_inspect import flatten
from app.models import AuditEvent, User
from app.models.auth import AuthPasskey, AuthRecoveryCode, AuthTotp, AuthTrustedDevice
from tests.auth_support import (
    CONSOLE,
    Staff,
    add_passkey,
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
from tests.session_support import remembered_device
from tests.test_auth_reset_needs_recovery import NEW_PASSWORD as MAILED
from tests.test_auth_reset_needs_recovery import _has_totp, _mail_reset, _mark, _reset_links
from tests.tool_surface import build_router_only_app

APP = Path(__file__).resolve().parents[1] / "app"
RECOVER_PY = APP / "core" / "auth" / "recover.py"


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def test_the_link_removes_the_factors_once_and_leads_to_a_new_one(auth_env: Any) -> None:
    org = await auth_env.create_org("recover")
    who = await signed_in(auth_env, org, "org-admin")
    await add_passkey(auth_env, who)
    old_session = who.session

    link = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)
    token = link.split("#", 1)[1]
    details = await auth_env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": token}
    )
    network = browser()
    accepted = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=network, json={"token": token}
    )
    # Every second factor went: the passkey, the TOTP and the recovery codes
    # (`clear_second_factors`). Read before a new one is enrolled.
    async with AsyncSession(auth_env.admin_engine) as session:
        left = [
            (await session.execute(select(table.id).where(table.user_id == who.user_id))).all()
            for table in (AuthPasskey, AuthRecoveryCode)
        ]
        totp = await session.get(AuthTotp, who.user_id)
    again = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": token}
    )
    as_reset = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": token, "password": "Pelican-Orbit-Lantern-58"},
    )

    assert link.startswith(f"{CONSOLE}/recover#")
    assert details.json() == {"email": who.email, "password_required": False}
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "enroll_required"}
    assert left == [[], []] and totp is None
    assert again.status_code == 410 and again.json()["code"] == "invalid_link"
    assert as_reset.status_code == 410
    # Every earlier session ended; the new one is pending and enrols a factor.
    who.session = old_session
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401
    who.session, who.network = cookie(accepted, SESSION_COOKIE), network
    await enrol_totp(auth_env, who)
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 200
    # The password did not change.
    who.session = None
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    async with AsyncSession(auth_env.admin_engine) as session:
        events = {
            e.action: e
            for e in (
                await session.execute(select(AuditEvent).where(AuditEvent.org_id == org))
            ).scalars()
        }
    issued = events["user.recovery_link_issued"]
    assert (issued.actor_type, issued.actor_id) == ("system", None)
    assert token not in str(issued.meta)
    used = events["user.recovery_link_used"]
    assert (used.actor_type, used.actor_id) == ("user", who.user_id)
    assert token not in str(used.meta)
    subjects = [m.subject.lower() for m in auth_env.mail.sent if m.to == who.email]
    assert any("recovery link" in s for s in subjects)
    assert any("sign-in methods were reset" in s for s in subjects)


async def test_the_link_forgets_every_remembered_device(auth_env: Any) -> None:
    """Task 8 review, test gap: a remembered browser stands in for the second
    factor (design §2.6), so a recovery that cleared the factors and left it
    would let that browser in without one. Drop `revoke_all_for_user` from
    `flows.accept_recovery` and `revoked_at` stays empty: red."""
    org = await auth_env.create_org("recover-device")
    who = await signed_in(auth_env, org, "org-admin")
    device_id = await remembered_device(auth_env, org, who.sub)

    link = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)
    accepted = await auth_env.client.post(
        "/api/v1/auth/recover/accept",
        headers=browser(),
        json={"token": link.split("#", 1)[1]},
    )

    assert accepted.status_code == 200, accepted.text
    async with AsyncSession(auth_env.admin_engine) as session:
        device = await session.get(AuthTrustedDevice, device_id)
    assert device is not None and device.revoked_at is not None


async def test_a_second_link_replaces_the_first(auth_env: Any) -> None:
    """Running the command again supersedes the earlier link (`action_tokens.
    issue` spends the open ones of the same purpose): only the newest works."""
    org = await auth_env.create_org("recover-twice")
    who = await signed_in(auth_env, org, "org-admin")

    first = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)
    second = await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)
    old = await auth_env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": first.split("#", 1)[1]}
    )
    old_accept = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": first.split("#", 1)[1]}
    )
    new = await auth_env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": second.split("#", 1)[1]}
    )

    assert first != second
    assert (old.status_code, old.json()["code"]) == (410, "invalid_link")
    assert (old_accept.status_code, old_accept.json()["code"]) == (410, "invalid_link")
    assert new.json() == {"email": who.email, "password_required": False}


async def test_only_an_active_technician_gets_a_link(auth_env: Any) -> None:
    org = await auth_env.create_org("recover-none")
    await signed_in(auth_env, org, "org-admin")

    with pytest.raises(recover.RecoveryRefused):
        await recover.issue_recovery_link(settings=auth_env.settings, email="nobody@test.local")


async def test_a_disabled_technician_gets_no_link(auth_env: Any) -> None:
    """Disabled is not recoverable from the server's terminal: an administrator
    turns the account back on first. Nothing is issued, audited or mailed."""
    org = await auth_env.create_org("recover-disabled")
    who = await signed_in(auth_env, org, "technician")
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(update(User).where(User.id == who.user_id).values(status="disabled"))
        await session.commit()
    auth_env.mail.sent.clear()

    with pytest.raises(recover.RecoveryRefused):
        await recover.issue_recovery_link(settings=auth_env.settings, email=who.email)

    async with AsyncSession(auth_env.admin_engine) as session:
        issued = (
            await session.execute(
                select(AuditEvent.id).where(
                    AuditEvent.org_id == org, AuditEvent.action == "user.recovery_link_issued"
                )
            )
        ).all()
    assert issued == [] and auth_env.mail.sent == []


# --------------------------------------------------------------------------- #
# A password an emailed link set (operator decision 2026-11-05, «Pedir
# contraseña nueva si hay marca»; design §7). That password may be whoever
# holds the mailbox's, so while `auth_passwords.mail_reset_at` is set the link
# asks for a new one before it finishes, and the mailbox's stops working.
# And on every recovery the page offers one, applied whenever it is given
# (operator decision 2026-11-05, «Ofrecerla siempre, aplicar»). Without the
# mark and without a password, plan decision 10 is unchanged: it stays.
# --------------------------------------------------------------------------- #

#: The person's own new password, chosen on the recovery page.
RECOVERED = "Saffron-Harbour-Quartz-4127"
NEEDS_PASSWORD = (
    "Your password was last set with an emailed link. Choose a new password to finish "
    "recovering your account."
)


async def _link(env: Any, who: Staff) -> str:
    """A recovery link's token, as the page reads it from the fragment."""
    link = await recover.issue_recovery_link(settings=env.settings, email=who.email)
    return link.split("#", 1)[1]


async def _details(env: Any, token: str) -> Any:
    return await env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": token}
    )


async def _accept(
    env: Any, token: str, *, headers: dict[str, str] | None = None, **body: str
) -> Any:
    return await env.client.post(
        "/api/v1/auth/recover/accept",
        headers={**browser(), **(headers or {})},
        json={"token": token, **body},
    )


async def _events(env: Any, org: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def test_the_details_say_whether_a_new_password_is_needed(auth_env: Any) -> None:
    """`password_required` is the mark, read the moment it is asked; the
    details leave the link unused either way. Drop the field and the page
    never asks: red."""
    org = await auth_env.create_org("recover-says")
    who = await signed_in(auth_env, org, "technician")
    token = await _link(auth_env, who)

    before = await _details(auth_env, token)
    await _mail_reset(auth_env, who)
    after = await _details(auth_env, token)

    assert before.json() == {"email": who.email, "password_required": False}
    assert after.json() == {"email": who.email, "password_required": True}
    assert (await _accept(auth_env, token, password=RECOVERED)).status_code == 200


async def test_with_the_mark_no_password_is_refused_and_the_link_stays(auth_env: Any) -> None:
    """The rule. 409 `recovery_needs_password` with the operator's sentence,
    raised inside the block so the link's `consume` rolls back: nothing is
    removed or recorded, the mailbox's password still signs in (the factor is
    still asked), and the same link finishes with a password. Delete the
    check and this is a 200 that keeps the mailbox's password: red."""
    org = await auth_env.create_org("recover-mark-none")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    token = await _link(auth_env, who)

    refused = await _accept(auth_env, token)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": NEEDS_PASSWORD, "code": "recovery_needs_password"}
    assert (await _details(auth_env, token)).status_code == 200
    assert await _has_totp(auth_env, who)
    assert await _mark(auth_env, who) is not None
    assert await _events(auth_env, org, "user.recovery_link_used") == []
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    finished = await _accept(auth_env, token, password=RECOVERED)
    assert finished.json() == {"state": "enroll_required"}, finished.text


async def test_the_refusal_is_said_in_spanish(auth_env: Any) -> None:
    org = await auth_env.create_org("recover-mark-es")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    token = await _link(auth_env, who)

    refused = await _accept(auth_env, token, headers={"Accept-Language": "es"})

    assert refused.status_code == 409, refused.text
    assert refused.json() == {
        "detail": (
            "Su contraseña se puso por última vez con un enlace enviado por correo. Elija una "
            "contraseña nueva para terminar de recuperar su cuenta."
        ),
        "code": "recovery_needs_password",
    }


async def test_with_the_mark_a_weak_password_is_refused_and_the_link_stays(
    auth_env: Any,
) -> None:
    """The rule every other password route applies (`staff_password.
    require_acceptable`): the same 422 `password_rejected`, the body never
    repeating the password, and the link still usable."""
    org = await auth_env.create_org("recover-mark-weak")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    token = await _link(auth_env, who)
    weak = "tiny-pass"

    refused = await _accept(auth_env, token, password=weak)

    assert refused.status_code == 422, refused.text
    assert refused.json() == {"code": "password_rejected", "detail": "Use at least 12 characters."}
    assert weak not in refused.text
    assert (await _details(auth_env, token)).json()["password_required"] is True
    assert await _has_totp(auth_env, who)
    finished = await _accept(auth_env, token, password=RECOVERED)
    assert finished.json() == {"state": "enroll_required"}, finished.text


async def test_with_the_mark_a_good_password_replaces_the_mailboxs(auth_env: Any) -> None:
    """The decision. The new password is stored with the factors' removal,
    in one transaction (`staff_password.store`, which clears the mark), and
    recorded and told as a mailed reset's is: `auth.password_changed`, its
    `via` the link's purpose, and the `password_changed` notice -- beside the
    recovery's own record and notice. The mailbox's password no longer signs
    in. Keep the password (plan decision 10 regardless) and the mailbox's
    still works: red."""
    org = await auth_env.create_org("recover-mark-good")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    token = await _link(auth_env, who)
    auth_env.mail.sent.clear()

    accepted = await _accept(auth_env, token, password=RECOVERED)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "enroll_required"}
    assert cookie(accepted, SESSION_COOKIE)
    assert await _mark(auth_env, who) is None
    assert not await _has_totp(auth_env, who)
    who.session, who.network = None, browser()
    assert (await login(auth_env, who, password=MAILED)).status_code == 401
    assert (await login(auth_env, who, password=RECOVERED)).json() == {"state": "enroll_required"}
    changed = await _events(auth_env, org, "auth.password_changed")
    assert [e.meta for e in changed] == [{"via": "reset"}, {"via": "recover"}]
    assert (changed[-1].actor_type, changed[-1].actor_id) == ("user", who.user_id)
    used = await _events(auth_env, org, "user.recovery_link_used")
    assert [e.meta for e in used] == [{"email": who.email}]
    for event in [*changed, *used]:
        assert RECOVERED not in str(event.meta) and token not in str(event.meta)
    subjects = [m.subject.lower() for m in auth_env.mail.sent if m.to == who.email]
    assert any("password was changed" in s for s in subjects), subjects
    assert any("sign-in methods were reset" in s for s in subjects), subjects
    assert all(RECOVERED not in m.text for m in auth_env.mail.sent)


async def test_without_the_mark_a_password_sent_is_applied(auth_env: Any) -> None:
    """Operator decision 2026-11-05, «Ofrecerla siempre, aplicar»: the page
    offers a new password on every recovery, and one that is given is
    applied, mark or not -- the same rule, the same store, the same record
    and notice. It closes the later recovery after somebody holding the
    mailbox and a factor cleared the mark by signing in: the person sets a
    password and theirs is the only one that works. Ignore the field
    without the mark and the old password still signs in: red."""
    org = await auth_env.create_org("recover-nomark-sent")
    who = await signed_in(auth_env, org, "technician")
    token = await _link(auth_env, who)
    auth_env.mail.sent.clear()

    accepted = await _accept(auth_env, token, password=RECOVERED)

    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    who.session, who.network = None, browser()
    assert (await login(auth_env, who)).status_code == 401
    assert (await login(auth_env, who, password=RECOVERED)).json() == {"state": "enroll_required"}
    changed = await _events(auth_env, org, "auth.password_changed")
    assert [e.meta for e in changed] == [{"via": "recover"}]
    assert (changed[0].actor_type, changed[0].actor_id) == ("user", who.user_id)
    subjects = [m.subject.lower() for m in auth_env.mail.sent if m.to == who.email]
    assert any("password was changed" in s for s in subjects), subjects
    assert any("sign-in methods were reset" in s for s in subjects), subjects


@pytest.mark.parametrize("empty", [None, ""], ids=["no-field", "empty-field"])
async def test_without_the_mark_an_empty_field_keeps_the_password(
    auth_env: Any, empty: str | None
) -> None:
    """The field is optional without the mark: left empty, the recovery works
    as before and the password stays (plan decision 10). Nothing records or
    tells a password change that did not happen."""
    org = await auth_env.create_org(f"recover-nomark-empty-{'none' if empty is None else 'blank'}")
    who = await signed_in(auth_env, org, "technician")
    token = await _link(auth_env, who)
    auth_env.mail.sent.clear()

    accepted = await (
        _accept(auth_env, token) if empty is None else _accept(auth_env, token, password=empty)
    )

    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    who.session, who.network = None, browser()
    assert (await login(auth_env, who)).json() == {"state": "enroll_required"}
    assert await _events(auth_env, org, "auth.password_changed") == []
    subjects = [m.subject.lower() for m in auth_env.mail.sent if m.to == who.email]
    assert not any("password was changed" in s for s in subjects), subjects


async def test_without_the_mark_a_weak_password_is_refused_and_the_link_stays(
    auth_env: Any,
) -> None:
    """An offered password is held to the same rule: 422 `password_rejected`
    before anything but the `consume` is written, so the link stays usable
    and the old password still signs in."""
    org = await auth_env.create_org("recover-nomark-weak")
    who = await signed_in(auth_env, org, "technician")
    token = await _link(auth_env, who)
    weak = "tiny-pass"

    refused = await _accept(auth_env, token, password=weak)

    assert refused.status_code == 422, refused.text
    assert refused.json() == {"code": "password_rejected", "detail": "Use at least 12 characters."}
    assert weak not in refused.text
    assert (await _details(auth_env, token)).json() == {
        "email": who.email,
        "password_required": False,
    }
    assert await _has_totp(auth_env, who)
    who.session, who.network = None, browser()
    assert (await login(auth_env, who)).json()["state"] == "factor_required"


async def test_a_mark_cleared_between_the_details_and_the_press_still_takes_the_password(
    auth_env: Any,
) -> None:
    """THE REVERSE RACE (fix 3 review, Minor 1). The details said a new
    password is needed; before the press, whoever holds the mailbox's
    password and a factor signs in with both, and that clears the mark
    (`failures.record_success`). The person's typed password is applied all
    the same, so the mailbox's stops working. Read it only while the mark is
    set and the mailbox's password survives the recovery: red."""
    org = await auth_env.create_org("recover-reverse-race")
    who = await signed_in(auth_env, org, "technician")
    await _mail_reset(auth_env, who)
    token = await _link(auth_env, who)
    assert (await _details(auth_env, token)).json()["password_required"] is True
    assert (await login(auth_env, who)).json()["state"] == "factor_required"
    signed = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who), "remember_device": False},
    )
    assert signed.json() == {"state": "signed_in"}, signed.text
    assert await _mark(auth_env, who) is None

    accepted = await _accept(auth_env, token, password=RECOVERED)

    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    who.session, who.network = None, browser()
    assert (await login(auth_env, who, password=MAILED)).status_code == 401
    assert (await login(auth_env, who, password=RECOVERED)).json() == {"state": "enroll_required"}
    changed = await _events(auth_env, org, "auth.password_changed")
    assert [e.meta for e in changed] == [{"via": "reset"}, {"via": "recover"}]


async def test_a_mail_reset_between_the_details_and_the_press_is_caught(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE RACE, forced. The details said no password; the mailbox holder's
    reset then holds the person's lock with the password and the mark
    written, uncommitted (paused at its notice); the person presses. The
    recovery waits for the lock (`factors.lock_person`), then reads the mark
    and refuses: 409, the link unspent. Read the mark before the lock and it
    reads no mark, waits at `clear_second_factors`, and answers 200 with the
    mailbox's password kept: red."""
    org = await auth_env.create_org("recover-race")
    who = await signed_in(auth_env, org, "technician")
    token = await _link(auth_env, who)
    assert (await _details(auth_env, token)).json()["password_required"] is False
    forgot = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202
    reset_token = link_token([m for m in _reset_links(auth_env) if m.to == who.email][-1].text)
    paused, release = asyncio.Event(), asyncio.Event()
    real_tell = notices.tell

    async def held_tell(session: Any, user: Any, kind: Any, **kwargs: Any) -> bool:
        if kind == "password_changed":
            paused.set()
            await release.wait()
        return await real_tell(session, user, kind, **kwargs)

    monkeypatch.setattr(notices, "tell", held_tell)
    reset = asyncio.create_task(
        auth_env.client.post(
            "/api/v1/auth/password/reset",
            headers=browser(),
            json={"token": reset_token, "password": MAILED},
        )
    )
    await asyncio.wait_for(paused.wait(), timeout=10)
    pressed = asyncio.create_task(_accept(auth_env, token))
    await asyncio.sleep(0.5)
    assert not pressed.done(), "the recovery must wait for the person's lock"
    release.set()

    assert (await reset).status_code == 204
    answer = await pressed
    assert answer.status_code == 409, answer.text
    assert answer.json() == {"detail": NEEDS_PASSWORD, "code": "recovery_needs_password"}
    assert await _has_totp(auth_env, who)
    assert (await _details(auth_env, token)).json() == {
        "email": who.email,
        "password_required": True,
    }
    finished = await _accept(auth_env, token, password=RECOVERED)
    assert finished.json() == {"state": "enroll_required"}, finished.text


def test_the_command_prints_the_link_or_says_why_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def gives(*, settings: Any, email: str) -> str:
        return f"{CONSOLE}/recover#abc"

    async def refuses(*, settings: Any, email: str) -> str:
        raise recover.RecoveryRefused("no active technician has that address")

    monkeypatch.setattr(recover, "get_settings", lambda: None)
    monkeypatch.setattr(recover, "issue_recovery_link", gives)
    assert recover.main(["--email", "ada@toeshee.test"]) == 0
    assert capsys.readouterr().out.strip() == f"{CONSOLE}/recover#abc"

    monkeypatch.setattr(recover, "issue_recovery_link", refuses)
    assert recover.main(["--email", "ada@toeshee.test"]) == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "no active technician" in printed.err


def test_the_command_warns_that_the_link_is_the_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review C-M3. The link asks for no password (plan decision 10), so
    the command says so where the operator reads it -- on stderr, so stdout
    stays the link alone for a script: hand it over directly, whoever opens it
    in the next 15 minutes gets the account, never by email (Q5). Delete the
    warning and this is red; a refusal prints no warning, since there is no
    link to hand over."""

    async def gives(*, settings: Any, email: str) -> str:
        return f"{CONSOLE}/recover#abc"

    monkeypatch.setattr(recover, "get_settings", lambda: None)
    monkeypatch.setattr(recover, "issue_recovery_link", gives)
    assert recover.main(["--email", "ada@toeshee.test"]) == 0
    printed = capsys.readouterr()

    assert printed.out.strip() == f"{CONSOLE}/recover#abc"
    warning = printed.err.lower()
    assert "directly" in warning
    assert "15 minutes" in warning and "gets the account" in warning
    assert "never send it by email" in warning

    async def refuses(*, settings: Any, email: str) -> str:
        raise recover.RecoveryRefused("no active technician has that address")

    monkeypatch.setattr(recover, "issue_recovery_link", refuses)
    assert recover.main(["--email", "ada@toeshee.test"]) == 1
    assert "gets the account" not in capsys.readouterr().err


def test_the_warning_states_the_links_real_lifetime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-review Minor 4: the window in the warning is `RECOVER_TTL`, not a
    number typed into a sentence. Change the TTL and the warning changes with
    it; hard-code "15 minutes" again and this is red."""
    from datetime import timedelta

    from app.core.auth import action_tokens

    async def gives(*, settings: Any, email: str) -> str:
        return f"{CONSOLE}/recover#abc"

    monkeypatch.setattr(recover, "get_settings", lambda: None)
    monkeypatch.setattr(recover, "issue_recovery_link", gives)
    monkeypatch.setattr(action_tokens, "RECOVER_TTL", timedelta(minutes=7))

    assert recover.main(["--email", "ada@toeshee.test"]) == 0
    warning = capsys.readouterr().err

    assert "in the next 7 minutes gets the account" in warning
    assert "15 minutes" not in warning


def test_the_recovery_command_does_not_exist_over_http() -> None:
    """FALSIFIER 9. Each assertion fails if somebody wires the command to a route."""
    for path, route in flatten(build_router_only_app().router):
        module = getattr(getattr(route, "endpoint", None), "__module__", "")
        assert module != "app.core.auth.recover", path

    for py in APP.rglob("*.py"):
        if py == RECOVER_PY:
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "app.core.auth.recover", py
                assert not (
                    node.module == "app.core.auth" and any(a.name == "recover" for a in node.names)
                ), py
            elif isinstance(node, ast.Import):
                assert all(a.name != "app.core.auth.recover" for a in node.names), py

    source = RECOVER_PY.read_text()
    assert "APIRouter" not in source and "fastapi" not in source
    issued = _issue_calls()
    # Not vacuous: the walk sees Task 7's reset link and this task's
    # invitation as well as the recovery link, whatever spelling each uses.
    assert {purpose for _, purpose in issued} >= {"INVITE", "RESET", "RECOVER"}, issued
    recovery_issuers = sorted(
        path for path, purpose in issued if purpose not in {"INVITE", "RESET"}
    )
    assert recovery_issuers == ["core/auth/recover.py"], issued


def _purpose(call: ast.Call) -> str:
    """The `purpose=` of one `issue(...)` call as written: a constant's name
    (`action_tokens.RECOVER`, or `RECOVER` imported bare) or a literal
    upper-cased (`"recover"`). Anything else -- a variable, `**kwargs`, no
    keyword at all -- comes back as its own source and counts as a possible
    recovery issuer: the safe direction for this list to be wrong in."""
    for keyword in call.keywords:
        if keyword.arg == "purpose":
            value = keyword.value
            if isinstance(value, ast.Attribute):
                return value.attr
            if isinstance(value, ast.Name):
                return value.id
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.upper()
            return ast.unparse(value)
    return "<no purpose keyword>"


def _issue_calls() -> list[tuple[str, str]]:
    """Every call to `issue(...)` or `<anything>.issue(...)` under `app/` --
    `action_tokens.issue` however it was imported or aliased -- as
    `(path, purpose)`. Matched on the AST, not on text, so neither a line break
    nor another spelling of the purpose hides an issuer."""
    found: list[tuple[str, str]] = []
    for py in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "issue") or (
                isinstance(func, ast.Attribute) and func.attr == "issue"
            ):
                found.append((str(py.relative_to(APP)), _purpose(node)))
    return found

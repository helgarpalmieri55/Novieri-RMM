"""Forgot and reset the password (P2 phase 1 Task 7, design §2.8; OWASP [S8]).

- The same 202, with no body, exista o no la cuenta; the mail goes after the
  answer (decision 8), so the answer's time does not depend on it.
- The link is single use, 30 minutes, and carries its token after a `#`
  (decision 3). Using it ends every session and forgets every remembered
  device; it does not sign in, and the next sign-in still asks for the second
  factor.
- A password the rule refuses leaves the link usable.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import recover
from app.core.auth.passwords import STAFF_MINIMUM, PasswordRejected
from app.core.auth.sessions import DEVICE_COOKIE
from app.models import AuditEvent, Organization
from app.models.auth import (
    AuthActionToken,
    AuthFailure,
    AuthSession,
    AuthTotp,
    AuthTrustedDevice,
)
from tests.auth_support import (
    CONSOLE,
    browser,
    capture_mail,
    console_ready,
    cookie,
    enrol_totp,
    fresh_code,
    link_token,
    login,
    point_at,
    signed_in,
    staff,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _forgot(env: Any, email: str) -> Any:
    return await env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": email}
    )


async def _reset(env: Any, token: str, password: str) -> Any:
    return await env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": token, "password": password},
    )


def _resets(env: Any) -> list[Any]:
    return [m for m in env.mail.sent if "reset" in m.subject.lower()]


async def test_the_answer_is_the_same_whether_the_account_exists_or_not(auth_env: Any) -> None:
    org = await auth_env.create_org("forgot")
    who = await signed_in(auth_env, org, "technician")

    known = await _forgot(auth_env, who.email)
    unknown = await _forgot(auth_env, "nobody@test.local")

    assert (known.status_code, known.content) == (unknown.status_code, unknown.content)
    assert (known.status_code, known.content) == (202, b"")
    assert [m.to for m in _resets(auth_env)] == [who.email]
    assert f"{CONSOLE}/reset-password#" in _resets(auth_env)[0].text


async def test_the_link_resets_once_ends_the_sessions_and_still_asks_for_the_factor(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("reset")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)

    done = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")
    again = await _reset(auth_env, token, "Pelican-Orbit-Lantern-59")

    assert done.status_code == 204, done.text
    assert again.status_code == 410 and again.json()["code"] == "invalid_link"
    async with AsyncSession(auth_env.admin_engine) as session:
        live = (
            await session.execute(
                select(AuthSession.id).where(
                    AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
                )
            )
        ).scalars().all()
        changed = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org, AuditEvent.action == "auth.password_changed"
                    )
                )
            ).scalars()
        )
    assert live == []
    assert [e.meta for e in changed] == [{"via": "reset"}]
    who.session, who.password = None, "Pelican-Orbit-Lantern-58"
    assert (await login(auth_env, who)).json()["state"] == "factor_required"


async def test_a_refused_password_keeps_the_link_usable(auth_env: Any) -> None:
    org = await auth_env.create_org("reset-weak")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)

    short = await _reset(auth_env, token, "short")
    good = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")

    assert short.status_code == 422
    assert short.json() == {"code": "password_rejected", "detail": "Use at least 12 characters."}
    assert good.status_code == 204, good.text


async def test_an_expired_a_foreign_and_a_made_up_link_are_refused_alike(auth_env: Any) -> None:
    org = await auth_env.create_org("reset-bad")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)
    # Aged as a whole: Task 1's CHECK (`ck_auth_action_tokens_expiry`) keeps
    # `expires_at > created_at`, so the link is issued 31 minutes ago.
    now = datetime.now(UTC)
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(AuthActionToken)
            .where(AuthActionToken.user_id == who.user_id)
            .values(
                created_at=now - timedelta(minutes=31), expires_at=now - timedelta(seconds=1)
            )
        )
        await session.commit()
    elsewhere = f"{uuid4().hex}.{token.split('.', 1)[1]}"
    answers = [
        await _reset(auth_env, token, "Pelican-Orbit-Lantern-58"),
        await _reset(auth_env, elsewhere, "Pelican-Orbit-Lantern-58"),
        await _reset(auth_env, "x" * 60, "Pelican-Orbit-Lantern-58"),
    ]

    assert [(a.status_code, a.json()["code"]) for a in answers] == [(410, "invalid_link")] * 3


async def test_an_address_gets_at_most_three_links_an_hour_and_the_answer_does_not_change(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("reset-limit")
    who = await signed_in(auth_env, org, "technician")

    answers = [await _forgot(auth_env, who.email) for _ in range(4)]

    assert [a.status_code for a in answers] == [202] * 4
    assert len(_resets(auth_env)) == 3


async def test_a_newer_link_replaces_the_older_one(auth_env: Any) -> None:
    org = await auth_env.create_org("reset-newer")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    older = link_token(_resets(auth_env)[-1].text)
    await _forgot(auth_env, who.email)
    newer = link_token(_resets(auth_env)[-1].text)

    assert (await _reset(auth_env, older, "Pelican-Orbit-Lantern-58")).status_code == 410
    assert (await _reset(auth_env, newer, "Pelican-Orbit-Lantern-58")).status_code == 204


async def test_an_over_long_password_is_refused_without_repeating_it(auth_env: Any) -> None:
    """Decision 16: `ResetIn.password` has no Pydantic bound -- a field
    failure repeats its INPUT in the 422, and the input is the new password.
    The rule's `too_long` refuses it (422 `password_rejected`), and the link
    stays usable, as for any refused password."""
    org = await auth_env.create_org("reset-long")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)
    long_password = "Pelican-Orbit-Lantern-58" * 100

    refused = await _reset(auth_env, token, long_password)
    good = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")

    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason="too_long", minimum=STAFF_MINIMUM).message,
    }
    assert long_password not in refused.text
    assert good.status_code == 204, good.text


async def test_a_password_built_on_the_organizations_name_is_refused(auth_env: Any) -> None:
    """Task 2's decision 5: the context is the address, the ORGANIZATION'S NAME
    and the person's. Drop `org_name=` from `flows.reset_password` (or the
    name from `staff_password.require_acceptable`'s `context_words`) and this
    password -- 16 characters, on no list, built on nothing but that name --
    is taken: red."""
    org = await auth_env.create_org("reset-org")
    who = await signed_in(auth_env, org, "technician")
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(Organization).where(Organization.id == org).values(name="Quokkaworks Ltd")
        )
        await session.commit()
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)

    refused = await _reset(auth_env, token, "Quokkaworks 2026")

    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "code": "password_rejected",
        "detail": PasswordRejected(reason="context", minimum=STAFF_MINIMUM).message,
    }


async def test_a_reset_forgets_every_device_and_unlocks_the_password(auth_env: Any) -> None:
    """Plan decision 9 and design §2.6: the reset revokes every remembered
    device (`revoke_all_for_user` in `flows.reset_password`) and clears the
    password's failures, a lockout included (`reset_factor`). Delete the first
    and the device keeps `revoked_at` NULL; delete the second and the locked
    password keeps its row, so the login after a reset that said 204 still
    gets the generic 401: red either way. (The login alone could not tell the
    first apart: Task 6's `devices.recognised` already ignores a device older
    than the password, so the row itself is checked.)"""
    org = await auth_env.create_org("reset-device")
    who = await signed_in(auth_env, org, "technician")
    who.session = None
    await login(auth_env, who)
    remembered = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who), "remember_device": True},
    )
    assert remembered.status_code == 200, remembered.text
    who.device = cookie(remembered, DEVICE_COOKIE)
    assert who.device
    now = datetime.now(UTC)
    async with AsyncSession(auth_env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="password",
                consecutive=100,
                last_failed_at=now,
                disabled_at=now,
            )
        )
        await session.commit()
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)

    done = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")

    assert done.status_code == 204, done.text
    async with AsyncSession(auth_env.admin_engine) as session:
        revoked = (
            await session.execute(
                select(AuthTrustedDevice.revoked_at).where(
                    AuthTrustedDevice.user_id == who.user_id
                )
            )
        ).scalars().all()
        failures = (
            await session.execute(
                select(AuthFailure).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "password"
                )
            )
        ).scalars().all()
    assert len(revoked) == 1 and revoked[0] is not None
    assert failures == []
    # The same browser, its device cookie still sent, and the new password.
    who.session, who.password = None, "Pelican-Orbit-Lantern-58"
    after = await login(auth_env, who)
    assert after.status_code == 200, after.text
    assert after.json()["state"] == "factor_required"


async def test_an_over_long_link_is_a_dead_one_and_never_repeated(auth_env: Any) -> None:
    """Global Constraints: a field that carries a secret has no Pydantic bound,
    and the one-time token is one. An over-long or ill-shaped token is a dead
    link like any other -- 410 `invalid_link`, refused by
    `action_tokens.org_of` before anything is read -- and the body does not
    repeat it. Put `max_length` back on `ResetIn.token` and the first of
    these is a 422: red. The live link still works afterwards."""
    org = await auth_env.create_org("reset-long-token")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_resets(auth_env)[-1].text)
    head = token.split(".", 1)[0]
    dead = ["x" * 2000, token + "x" * 2000, f"{head}.{'A' * 5000}"]

    answers = [await _reset(auth_env, value, "Pelican-Orbit-Lantern-58") for value in dead]
    good = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")

    for answer, value in zip(answers, dead, strict=True):
        assert answer.status_code == 410, answer.text
        assert answer.json()["code"] == "invalid_link"
        assert value not in answer.text
    assert good.status_code == 204, good.text


# --------------------------------------------------------------------------- #
# K1 (final review C-I1): the mailbox alone never reaches a session without a
# second factor. Design §2.8: "después sigue pidiendo el segundo factor. El
# segundo factor nunca se restablece por correo"; Q5, "nunca solo por correo".
# --------------------------------------------------------------------------- #


def _reset_links(env: Any) -> list[Any]:
    """The reset mails only -- not the `factors_reset` notice, whose subject
    says "reset" too."""
    return [m for m in env.mail.sent if m.subject.lower().startswith("reset your")]


async def _factorless(env: Any, org: Any, how: str) -> Any:
    """A technician with a password and NO confirmed second factor, left in
    that state the way `how` names."""
    if how == "admin_reset":
        admin = await signed_in(env, org, "org-admin")
        who = await signed_in(env, org, "technician")
        reset = await env.client.post(
            f"/api/v1/users/{who.user_id}/factors/reset", headers=admin.headers()
        )
        assert reset.status_code == 204, reset.text
    elif how == "recovery_abandoned":
        who = await signed_in(env, org, "technician")
        link = await recover.issue_recovery_link(settings=env.settings, email=who.email)
        accepted = await env.client.post(
            "/api/v1/auth/recover/accept",
            headers=browser(),
            json={"token": link.split("#", 1)[1]},
        )
        assert accepted.json() == {"state": "enroll_required"}, accepted.text
    elif how == "recovery_codes_only":
        who = await signed_in(env, org, "technician")
        async with AsyncSession(env.admin_engine) as session:
            await session.execute(delete(AuthTotp).where(AuthTotp.user_id == who.user_id))
            await session.commit()
    elif how == "pending_totp_only":
        # An enrolment started and never confirmed: a staged secret, no code.
        who = await staff(env, org, "technician")
        await point_at(env, org)
        assert (await login(env, who)).json() == {"state": "enroll_required"}
        started = await env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
        assert started.status_code == 200, started.text
    else:
        # An accepted invitation whose enrolment was abandoned, and the
        # first-boot owner before their first sign-in, are both this: a
        # password and nothing else.
        who = await staff(env, org, "technician")
    await point_at(env, org)
    return who


@pytest.mark.parametrize(
    "how",
    [
        "admin_reset",
        "invite_or_first_boot",
        "recovery_abandoned",
        "recovery_codes_only",
        "pending_totp_only",
    ],
)
async def test_no_reset_link_goes_to_an_account_without_a_second_factor(
    auth_env: Any, how: str
) -> None:
    """`flows.send_reset_link` sends nothing to an account with no passkey
    and no confirmed TOTP -- recovery codes alone do not count -- and the
    answer is the same 202, empty, as for any address. Delete the factor
    check and the mailbox holder gets a link, sets a password, enrols THEIR
    OWN factor and is in: red."""
    org = await auth_env.create_org(f"k1-{how.replace('_', '-')}")
    who = await _factorless(auth_env, org, how)
    before = len(_reset_links(auth_env))

    answer = await _forgot(auth_env, who.email)
    unknown = await _forgot(auth_env, "nobody@test.local")

    assert (answer.status_code, answer.content) == (202, b"")
    assert (answer.status_code, answer.content) == (unknown.status_code, unknown.content)
    assert len(_reset_links(auth_env)) == before


async def test_a_link_issued_before_the_factors_went_no_longer_resets(auth_env: Any) -> None:
    """The race the mail check alone leaves open: a link sent while the
    account still had its factor, then an administrator's reset. Used now,
    it would set a password on a factor-less account -- the mailbox alone.
    `flows.reset_password` refuses it with the one 410 of every dead link
    and changes nothing. Delete that check and this is a 204: red."""
    org = await auth_env.create_org("k1-race")
    admin = await signed_in(auth_env, org, "org-admin")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    token = link_token(_reset_links(auth_env)[-1].text)
    reset = await auth_env.client.post(
        f"/api/v1/users/{who.user_id}/factors/reset", headers=admin.headers()
    )
    assert reset.status_code == 204, reset.text

    used = await _reset(auth_env, token, "Pelican-Orbit-Lantern-58")

    assert used.status_code == 410, used.text
    assert used.json()["code"] == "invalid_link"
    who.session, who.network = None, browser()
    assert (await login(auth_env, who, password="Pelican-Orbit-Lantern-58")).status_code == 401


async def test_the_mailbox_alone_does_not_reach_the_console_after_a_factor_reset(
    auth_env: Any,
) -> None:
    """The reviewer's probe (C-I1, `test_mailbox_only_after_admin_reset`), kept:
    after an administrator's factor reset, somebody holding only the
    person's mailbox asks for a reset link -- and there is none to follow, so
    there is no password of theirs and no `GET /me` = 200. The person with
    the password still signs in and enrols (design §7)."""
    org = await auth_env.create_org("k1-probe")
    admin = await signed_in(auth_env, org, "org-admin")
    victim = await signed_in(auth_env, org, "technician")
    reset = await auth_env.client.post(
        f"/api/v1/users/{victim.user_id}/factors/reset", headers=admin.headers()
    )
    assert reset.status_code == 204, reset.text

    forgot = await _forgot(auth_env, victim.email)

    assert forgot.status_code == 202
    assert not [m for m in _reset_links(auth_env) if m.to == victim.email]
    victim.session, victim.network = None, browser()
    assert (await login(auth_env, victim)).json() == {"state": "enroll_required"}
    await enrol_totp(auth_env, victim)
    assert (await auth_env.client.get("/api/v1/me", headers=victim.headers())).status_code == 200


async def test_the_reset_mails_sentence_about_the_second_factor_is_true(auth_env: Any) -> None:
    """"You will still need your second factor to sign in" (`notices.reset_mail`):
    the link is only sent to an account that has one, and after the reset
    the next sign-in asks for it."""
    org = await auth_env.create_org("k1-sentence")
    who = await signed_in(auth_env, org, "technician")
    await _forgot(auth_env, who.email)
    (mail,) = [m for m in _reset_links(auth_env) if m.to == who.email]
    assert "You will still need your second factor" in mail.text

    done = await _reset(auth_env, link_token(mail.text), "Pelican-Orbit-Lantern-58")

    assert done.status_code == 204, done.text
    who.session, who.network, who.password = None, browser(), "Pelican-Orbit-Lantern-58"
    assert (await login(auth_env, who)).json()["state"] == "factor_required"

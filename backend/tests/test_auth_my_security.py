"""My security (P2 phase 1 Task 7, design §2.4-§2.6): the caller's own
factors, remembered devices and sessions.

Every route needs the console session (a Bearer token gets 401), and the ones
that change a factor need the second factor confirmed in the last ten minutes
(Task 6's `require_step_up`). The last second factor cannot be removed.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE
from app.models import AuditEvent
from app.models.auth import AuthFailure, AuthPassword, AuthSession
from tests.auth_support import (
    add_passkey,
    capture_mail,
    console_ready,
    cookie,
    fresh_code,
    login,
    signed_in,
    wrong_code,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _age_step_up(env: Any, who: Any) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == who.user_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()


async def _second_session(env: Any, who: Any, *, remember: bool = False) -> Any:
    who.session = None
    await login(env, who)
    return await env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": await fresh_code(env, who), "remember_device": remember},
    )


async def test_my_security_lists_what_i_have_and_nothing_secret(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec")
    who = await signed_in(auth_env, org, "technician")
    await add_passkey(auth_env, who, "Phone")

    response = await auth_env.client.get("/api/v1/me/security", headers=who.headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_totp"] is True
    assert body["recovery_codes_left"] == 10
    assert [p["name"] for p in body["passkeys"]] == ["Phone"]
    assert set(body["passkeys"][0]) == {"id", "name", "created_at", "last_used_at", "backup_state"}
    assert [s["current"] for s in body["sessions"]].count(True) == 1
    assert body["trusted_devices"] == []
    assert who.session and who.session not in response.text
    for code in who.recovery_codes:
        assert code not in response.text


async def test_my_security_needs_the_console_session(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-bearer")
    sub = await auth_env.create_user(org, "technician")
    bearer = await auth_env.headers(org, sub)

    response = await auth_env.client.get("/api/v1/me/security", headers=bearer)

    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"


async def test_changing_a_factor_needs_a_recent_confirmation(auth_env: Any) -> None:
    """Eleven minutes after the factor, no change (`require_step_up`, and
    `guards.require_fresh` for an enrolment from a full session)."""
    org = await auth_env.create_org("mysec-stale")
    who = await signed_in(auth_env, org, "technician")
    await _age_step_up(auth_env, who)

    for method, path in (
        ("POST", "/api/v1/me/recovery-codes"),
        ("DELETE", "/api/v1/me/totp"),
        ("POST", "/api/v1/auth/enroll/totp/start"),
    ):
        response = await auth_env.client.request(method, path, headers=who.headers())
        assert response.status_code == 403, (path, response.text)
        assert response.json()["code"] == "step_up_required"


async def test_the_last_second_factor_cannot_be_removed(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-last")
    who = await signed_in(auth_env, org, "technician")

    refused = await auth_env.client.delete("/api/v1/me/totp", headers=who.headers())
    key = await add_passkey(auth_env, who)
    removed = await auth_env.client.delete("/api/v1/me/totp", headers=who.headers())
    last = await auth_env.client.delete(f"/api/v1/me/passkeys/{key}", headers=who.headers())

    assert refused.status_code == 409 and refused.json()["code"] == "last_factor"
    assert removed.status_code == 204, removed.text
    assert last.status_code == 409 and last.json()["code"] == "last_factor"
    async with AsyncSession(auth_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org, AuditEvent.action == "auth.factor_removed"
                    )
                )
            ).scalars()
        )
    assert [e.meta for e in events] == [{"factor": "totp"}]
    assert any("removed" in m.subject.lower() for m in auth_env.mail.sent)


async def test_a_new_authenticator_app_replaces_the_old_one_only_once_confirmed(
    auth_env: Any,
) -> None:
    """Decision 7: from My security the new secret waits sealed in Redis
    (`flows._REPLACING`); a wrong code undoes the whole swap and the old app
    keeps working; the right one replaces it. Without the `raise` inside the
    block of `flows._replace_totp`, the wrong code would leave the person with
    an unconfirmed secret and no app."""
    org = await auth_env.create_org("mysec-totp")
    who = await signed_in(auth_env, org, "technician")
    old_secret = who.totp_secret
    assert old_secret

    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    new_secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    assert new_secret != old_secret
    wrong = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": wrong_code(new_secret)},
    )
    still_old = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )
    right = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(new_secret).now()},
    )
    old_now = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": pyotp.TOTP(old_secret).now()},
    )
    who.totp_secret = new_secret
    new_now = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )

    assert wrong.status_code == 400 and wrong.json()["code"] == "factor_failed"
    assert still_old.status_code == 204, still_old.text
    assert right.status_code == 204, right.text
    assert old_now.status_code == 400
    assert new_now.status_code == 204, new_now.text


async def test_new_recovery_codes_replace_the_old_ones(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-codes")
    who = await signed_in(auth_env, org, "technician")
    old = who.recovery_codes[0]

    response = await auth_env.client.post("/api/v1/me/recovery-codes", headers=who.headers())
    who.session = None
    await login(auth_env, who)
    with_old = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": old, "remember_device": False},
    )

    assert response.status_code == 200, response.text
    codes = response.json()["recovery_codes"]
    assert len(codes) == 10 and old not in codes
    assert with_old.status_code == 401


async def test_a_wrong_current_password_is_refused_and_counted(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-pw")
    who = await signed_in(auth_env, org, "technician")

    response = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": "Not-The-Password-41", "new": "Pelican-Orbit-Lantern-58"},
    )

    assert response.status_code == 403 and response.json()["code"] == "invalid_credentials"
    async with AsyncSession(auth_env.admin_engine) as session:
        failure = (
            await session.execute(
                select(AuthFailure).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "password"
                )
            )
        ).scalar_one()
    assert failure.consecutive == 1


async def test_a_current_password_under_an_unknown_pepper_version_is_refused_not_a_500(
    auth_env: Any,
) -> None:
    """Coordinator note: `verify_password` raises `UnknownKeyVersion` (local
    backend) for a pepper version it does not hold. `PUT /me/password` answers
    the same 403 `invalid_credentials` as a wrong current password, counts no
    failure the person did not make, and changes nothing. Without the
    `except UnknownKeyVersion` in `account.change_password` this is a 500."""
    org = await auth_env.create_org("mysec-pepper")
    who = await signed_in(auth_env, org, "technician")
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == who.user_id)
            .values(pepper_version=99)
        )
        await session.commit()

    response = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "invalid_credentials"
    async with AsyncSession(auth_env.admin_engine) as session:
        counted = (
            await session.execute(select(AuthFailure).where(AuthFailure.user_id == who.user_id))
        ).scalars().all()
        stored = (
            await session.execute(
                select(AuthPassword.pepper_version).where(AuthPassword.user_id == who.user_id)
            )
        ).scalar_one()
    assert counted == []
    assert stored == 99


async def test_i_can_end_one_of_my_sessions_and_nobody_elses(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-sessions")
    who = await signed_in(auth_env, org, "technician")
    other = await signed_in(auth_env, org, "technician")
    first = who.session
    who.session = cookie(await _second_session(auth_env, who), SESSION_COOKIE)
    listed = (await auth_env.client.get("/api/v1/me/security", headers=who.headers())).json()
    not_current = next(s["id"] for s in listed["sessions"] if not s["current"])
    theirs = (await auth_env.client.get("/api/v1/me/security", headers=other.headers())).json()

    ended = await auth_env.client.delete(
        f"/api/v1/me/sessions/{not_current}", headers=who.headers()
    )
    foreign = await auth_env.client.delete(
        f"/api/v1/me/sessions/{theirs['sessions'][0]['id']}", headers=who.headers()
    )

    assert ended.status_code == 204, ended.text
    assert foreign.status_code == 404
    who.session = first
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401
    assert (await auth_env.client.get("/api/v1/me", headers=other.headers())).status_code == 200


async def test_forgetting_a_device_brings_the_factor_back(auth_env: Any) -> None:
    org = await auth_env.create_org("mysec-device")
    who = await signed_in(auth_env, org, "technician")
    done = await _second_session(auth_env, who, remember=True)
    who.session, who.device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    listed = (await auth_env.client.get("/api/v1/me/security", headers=who.headers())).json()
    device = listed["trusted_devices"][0]
    assert device["current"] is True

    forgot = await auth_env.client.delete(
        f"/api/v1/me/trusted-devices/{device['id']}", headers=who.headers()
    )
    who.session = None
    after = await login(auth_env, who)

    assert forgot.status_code == 204
    assert after.json()["state"] == "factor_required"


async def test_an_over_long_code_is_refused_as_a_wrong_one_and_never_repeated(
    auth_env: Any,
) -> None:
    """Decision 16, with the session open: a step-up or an enrolment code has
    no Pydantic bound (a 422 would repeat it), and a code of any other shape
    is a wrong one -- 400 `factor_failed` (decision 14), never echoed. A
    `pattern` or a length back on `StepUpCodeIn`, `StepUpRecoveryIn` or
    `EnrollCodeIn` turns this red with a 422 that carries the code."""
    org = await auth_env.create_org("mysec-long-code")
    who = await signed_in(auth_env, org, "technician")
    long_code = "9" * 2000
    long_recovery = "Z" * 2000

    step_up = await auth_env.client.post(
        "/api/v1/auth/step-up/totp", headers=who.headers(), json={"code": long_code}
    )
    by_recovery = await auth_env.client.post(
        "/api/v1/auth/step-up/recovery", headers=who.headers(), json={"code": long_recovery}
    )
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    confirm = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm", headers=who.headers(), json={"code": long_code}
    )

    for response, secret in (
        (step_up, long_code),
        (by_recovery, long_recovery),
        (confirm, long_code),
    ):
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "factor_failed"
        assert secret not in response.text


async def test_an_over_long_password_is_refused_without_repeating_it(auth_env: Any) -> None:
    """Decision 16 for `PUT /me/password`: neither field has a Pydantic bound
    (a 422 would repeat it). An over-long CURRENT password is a wrong one -- 403
    `invalid_credentials`, never handed to the pepper
    (`staff_password.SUBMITTED_MAX`); an over-long NEW one is the rule's
    `too_long` -- 422 `password_rejected`. Neither body repeats it, and the
    password does not change."""
    org = await auth_env.create_org("mysec-pw-long")
    who = await signed_in(auth_env, org, "technician")
    long_current = who.password + "x" * 2000
    long_new = "Pelican-Orbit-Lantern-58" * 100

    by_current = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": long_current, "new": "Pelican-Orbit-Lantern-58"},
    )
    by_new = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": long_new},
    )

    assert by_current.status_code == 403, by_current.text
    assert by_current.json()["code"] == "invalid_credentials"
    assert long_current not in by_current.text
    assert by_new.status_code == 422, by_new.text
    assert by_new.json()["code"] == "password_rejected"
    assert long_new not in by_new.text
    who.session = None
    assert (await login(auth_env, who)).json()["state"] == "factor_required"


async def test_a_new_authenticator_app_does_not_clear_the_failures(auth_env: Any) -> None:
    """Task 4's decision 7 (NIST SP 800-63B-4 §3.1.3: "Generating a new
    authentication secret SHALL NOT reset the failed authentication count"):
    confirming a new authenticator app leaves the TOTP's failures where they
    were. A `reset_factor` in `flows.enroll_totp_confirm` deletes the row:
    red."""
    org = await auth_env.create_org("mysec-totp-failures")
    who = await signed_in(auth_env, org, "technician")
    assert who.totp_secret
    for _ in range(3):
        wrong = await auth_env.client.post(
            "/api/v1/auth/step-up/totp",
            headers=who.headers(),
            json={"code": wrong_code(who.totp_secret)},
        )
        assert wrong.status_code == 400, wrong.text
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    new_secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]

    right = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(new_secret).now()},
    )

    assert right.status_code == 204, right.text
    async with AsyncSession(auth_env.admin_engine) as session:
        failure = (
            await session.execute(
                select(AuthFailure).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "totp"
                )
            )
        ).scalar_one_or_none()
    assert failure is not None and failure.consecutive == 3

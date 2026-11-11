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

from app.core.auth.session_policy import DEFAULT_POLICY
from app.core.auth.sessions import DEVICE_COOKIE, SESSION_COOKIE, token_hash
from app.models import AuditEvent
from app.models.auth import AuthFailure, AuthPassword, AuthSession, AuthTrustedDevice
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
    """The token the console used to send is no way into My security: 401
    `auth_required`. Minted here, because `auth_env.headers` makes a session
    cookie since own authentication Task 13 (and the door, not only
    `_account_ctx`, now refuses the token)."""
    org = await auth_env.create_org("mysec-bearer")
    sub = await auth_env.create_user(org, "technician")
    token = auth_env.idp.token(sub, await auth_env.org_slug(org), email=f"{sub}@test.local")

    response = await auth_env.client.get(
        "/api/v1/me/security", headers={"Authorization": f"Bearer {token}"}
    )

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


async def _session_row(env: Any, token: str) -> AuthSession:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == token_hash(token))
            )
        ).scalar_one()


async def _session_by_id(env: Any, session_id: Any) -> AuthSession:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(select(AuthSession).where(AuthSession.id == session_id))
        ).scalar_one()


def _max_age(response: Any, name: str) -> int:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return int(header.lower().split("max-age=", 1)[1].split(";", 1)[0])
    raise AssertionError(f"{name} not set")


async def test_a_password_change_on_a_remembered_device_keeps_only_the_caller_signed_in(
    auth_env: Any,
) -> None:
    """Design §2.8/[S8] and `account.change_password`'s rule: the OTHER
    sessions end, the caller's does not -- even when a remembered device
    started it. The device is still revoked (falsifier 5), so the caller's
    session is first detached from it (`account._detach_from_device`) and cut
    to an ordinary session's lifetime from the change, the moment it proved
    the current password with a recent step-up: no session outlives the trust
    that gave it its length. Without the detach, Task 6's
    `revoke_all_for_user` ends the caller's session with the device and the
    new cookie reads 401: red.

    THE SESSION KEEPS, ITS IDENTIFIER DOES NOT (final review B-I1, OWASP [S6],
    "Renew the Session ID After Any Privilege Level Change"): the change sets
    a new cookie on the same row, and a copy of the old one -- an infostealer's,
    a HAR file's -- is a 401. Drop `sessions.rotate` from `change_password`
    and the old cookie still reads 200: red."""
    org = await auth_env.create_org("mysec-pw-device")
    who = await signed_in(auth_env, org, "technician")
    done = await _second_session(auth_env, who, remember=True)
    who.device = cookie(done, DEVICE_COOKIE)
    assert who.device
    # Two sessions started by the remembered device: the caller's, and another.
    who.session = None
    assert (await login(auth_env, who)).json() == {"state": "signed_in"}
    mine = who.session
    who.session = None
    assert (await login(auth_env, who)).json() == {"state": "signed_in"}
    sibling = who.session
    assert mine and sibling and mine != sibling
    who.session = mine
    confirm = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert confirm.status_code == 204, confirm.text
    before_row = await _session_row(auth_env, mine)
    proved_at = before_row.factor_at
    before = datetime.now(UTC)

    changed = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    after = datetime.now(UTC)
    assert changed.status_code == 204, changed.text
    renewed = cookie(changed, SESSION_COOKIE)
    assert renewed and renewed != mine
    old = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert old.status_code == 401, old.text
    who.session = renewed
    me = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert me.status_code == 200, me.text
    row = await _session_row(auth_env, renewed)
    assert row.id == before_row.id  # the same session, a new identifier
    assert row.ended_at is None and row.trusted_device_id is None
    assert row.factor_at == proved_at  # Task 6's record of the proof is left alone
    ordinary = DEFAULT_POLICY.lifetime("staff", remembered=False)
    assert before < row.expires_at <= after + ordinary
    # The new cookie expires with the capped row, not with the remembered one.
    assert abs(_max_age(changed, SESSION_COOKIE) - (row.expires_at - after).total_seconds()) < 5
    other = await _session_row(auth_env, sibling)
    assert other.ended_at is not None
    async with AsyncSession(auth_env.admin_engine) as session:
        revoked = (
            await session.execute(
                select(AuthTrustedDevice.revoked_at).where(
                    AuthTrustedDevice.user_id == who.user_id
                )
            )
        ).scalars().all()
    assert len(revoked) == 1 and revoked[0] is not None
    # A new sign-in from that browser (its device cookie still sent): the factor.
    who.session, who.password = None, "Pelican-Orbit-Lantern-58"
    assert (await login(auth_env, who)).json()["state"] == "factor_required"


async def test_a_password_change_keeps_a_remembered_session_older_than_an_ordinary_one(
    auth_env: Any,
) -> None:
    """The cap counts from the CHANGE, not from the sign-in (coordinator
    decision, fix round 2). A remembered-device session lives up to
    `remembered_max_hours` (168 h by default) and an ordinary one 12 h, so a
    browser kept on a device is usually signed in more than 12 h ago. At the
    change this session has just proved the current password and holds a
    recent step-up -- what a fresh sign-in would have -- so it keeps at most an
    ordinary lifetime from now. Anchor the cap on `factor_at` and this session,
    13 h old, gets an expiry in the past: 204, then 401 on the same cookie --
    red. `factor_at` and `step_up_at` stay Task 6's record of the proofs."""
    org = await auth_env.create_org("mysec-pw-device-old")
    who = await signed_in(auth_env, org, "technician")
    done = await _second_session(auth_env, who, remember=True)
    who.device = cookie(done, DEVICE_COOKIE)
    assert who.device
    who.session = None
    assert (await login(auth_env, who)).json() == {"state": "signed_in"}
    mine = who.session
    assert mine
    # Signed in on the device 13 hours ago, and used a moment ago (so not idle).
    signed = datetime.now(UTC) - timedelta(hours=13)
    async with AsyncSession(auth_env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == token_hash(mine))
            .values(
                created_at=signed,
                factor_at=signed,
                expires_at=signed + DEFAULT_POLICY.lifetime("staff", remembered=True),
            )
        )
        await session.commit()
    confirm = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert confirm.status_code == 204, confirm.text
    before_row = await _session_row(auth_env, mine)
    stepped_up_at = before_row.step_up_at

    changed = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    now = datetime.now(UTC)
    assert changed.status_code == 204, changed.text
    # The identifier changed with the password (B-I1); the session is the same row.
    who.session = cookie(changed, SESSION_COOKIE)
    me = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert me.status_code == 200, me.text
    row = await _session_by_id(auth_env, before_row.id)
    assert row.ended_at is None and row.trusted_device_id is None
    assert now < row.expires_at <= now + DEFAULT_POLICY.lifetime("staff", remembered=False)
    assert (row.factor_at, row.step_up_at) == (signed, stepped_up_at)


async def test_a_password_change_renews_the_callers_identifier(auth_env: Any) -> None:
    """B-I1 / C-I3, on an ordinary session: the same row, a new cookie, the
    old value a 401, Max-Age to the row's expiry -- and only after the change
    committed: a new password the rule refuses (422) renews nothing, so the
    old cookie still works. Move `rotate` before the rule and the 422 leaves
    a session whose cookie the browser never got: red."""
    org = await auth_env.create_org("mysec-rotate")
    who = await signed_in(auth_env, org, "technician")
    first = who.session
    assert first
    row_id = (await _session_row(auth_env, first)).id

    refused = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "short"},
    )
    assert refused.status_code == 422, refused.text
    assert cookie(refused, SESSION_COOKIE) is None
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 200

    changed = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    assert changed.status_code == 204, changed.text
    renewed = cookie(changed, SESSION_COOKIE)
    assert renewed and renewed != first
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 401
    who.session = renewed
    assert (await auth_env.client.get("/api/v1/me", headers=who.headers())).status_code == 200
    row = await _session_by_id(auth_env, row_id)
    assert row.token_hash == token_hash(renewed) and row.ended_at is None
    left = (row.expires_at - datetime.now(UTC)).total_seconds()
    assert abs(_max_age(changed, SESSION_COOKIE) - left) < 5


async def test_change_password_asks_for_a_recent_confirmation_itself(auth_env: Any) -> None:
    """`guards.require_fresh` INSIDE `account.change_password` (Task 7 minor
    (a)): the route's `require_step_up` is not the only thing between a stale
    session and a new password. Called directly with a principal whose
    step-up is eleven minutes old, it refuses before anything is read; delete
    the line and the password changes: red."""
    from app.core.auth import account
    from app.core.auth.refusals import AuthRefusal
    from app.core.auth.sessions import resolve
    from app.core.client_scope import ClientScope
    from tests.session_support import cookie_header, http_request

    org = await auth_env.create_org("mysec-fresh")
    who = await signed_in(auth_env, org, "technician")
    await _age_step_up(auth_env, who)
    request = http_request(auth_env, cookie=cookie_header(who.session))
    principal = await resolve(request, touch=False)
    assert principal is not None
    async with AsyncSession(auth_env.admin_engine) as session:
        before = (
            await session.execute(
                select(AuthPassword.hash).where(AuthPassword.user_id == who.user_id)
            )
        ).scalar_one()

    with pytest.raises(AuthRefusal) as refused:
        await account.change_password(
            request,
            principal,
            current=who.password,
            new="Pelican-Orbit-Lantern-58",
            # A technician's own rows run under every client whatever is passed.
            clients=ClientScope.all(),
        )

    assert refused.value.status_code == 403
    assert refused.value.code == "step_up_required"
    async with AsyncSession(auth_env.admin_engine) as session:
        after = (
            await session.execute(
                select(AuthPassword.hash).where(AuthPassword.user_id == who.user_id)
            )
        ).scalar_one()
    assert after == before


async def _password_failures(
    env: Any, who: Any, consecutive: int, *, disabled: bool = False
) -> None:
    now = datetime.now(UTC)
    async with AsyncSession(env.admin_engine) as session:
        session.add(
            AuthFailure(
                org_id=who.org_id,
                user_id=who.user_id,
                factor="password",
                consecutive=consecutive,
                last_failed_at=now,
                disabled_at=now if disabled else None,
            )
        )
        await session.commit()


async def test_a_password_change_while_the_password_waits_says_so(auth_env: Any) -> None:
    """B-M4 / C-M1: the caller is signed in, so the wait is told as every
    step after the password tells it (`factors.gate`): 429 `factor_wait`,
    `wait_s` and `Retry-After` -- never "the current password is not right"
    for a password nobody tried. Use the bare `check_allowed` again and this
    reads 403 `invalid_credentials`: red."""
    org = await auth_env.create_org("mysec-pw-wait")
    who = await signed_in(auth_env, org, "technician")
    await _password_failures(auth_env, who, 20)

    response = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "factor_wait"
    assert isinstance(body["wait_s"], int) and 0 < body["wait_s"] <= 900
    assert response.headers["Retry-After"] == str(body["wait_s"])
    assert cookie(response, SESSION_COOKIE) is None


async def test_a_password_change_with_the_password_locked_says_so(auth_env: Any) -> None:
    """B-M4 / C-M1: a disabled password is 403 `factor_disabled`, with a
    sentence about the password and what undoes it."""
    org = await auth_env.create_org("mysec-pw-locked")
    who = await signed_in(auth_env, org, "technician")
    await _password_failures(auth_env, who, 100, disabled=True)

    response = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "factor_disabled"
    assert "password" in response.json()["detail"]


async def test_a_password_change_from_the_remembered_browser_is_not_held_by_the_wait(
    auth_env: Any,
) -> None:
    """K3 at `/me/password` (design §2.3): the browser chooses the counter
    wherever the password is tried. While somebody without the cookie keeps
    the account's counter waiting, the person can still change the password
    from the browser the platform recognises -- otherwise whoever holds the
    sign-in wait also holds the remedy. Gate `change_password` on the
    account's counter alone and this is a 429: red."""
    org = await auth_env.create_org("mysec-pw-device-wait")
    who = await signed_in(auth_env, org, "technician")
    done = await _second_session(auth_env, who, remember=True)
    who.session, who.device = cookie(done, SESSION_COOKIE), cookie(done, DEVICE_COOKIE)
    assert who.device
    await _password_failures(auth_env, who, 20)

    response = await auth_env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": "Pelican-Orbit-Lantern-58"},
    )

    assert response.status_code == 204, response.text


async def test_a_session_ended_while_waiting_for_the_lock_gets_no_new_recovery_codes(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2-38 for the recovery codes (own authentication phase 2, Task 7 fix
    round 1). A reset of the person's factors that wipes the codes and ends
    the session after the door admitted the request, and before the
    person's lock is taken, must not be followed by fresh codes for the
    session it cut: `new_recovery_codes` checks under the lock that the
    session is still live, as `add_password` does. Without the check the
    codes the reset just wiped come back -- 200 and ten codes: red."""
    from uuid import UUID

    from sqlalchemy import func

    from app.core.auth import account
    from app.core.auth.factors import clear_second_factors
    from app.models.auth import AuthRecoveryCode

    org = await auth_env.create_org("mysec-codes-race")
    who = await signed_in(auth_env, org, "technician")
    real_lock = account.lock_person

    async def reset_lands_first(session: AsyncSession, user_id: UUID) -> None:
        async with AsyncSession(auth_env.admin_engine) as s:
            await clear_second_factors(s, who.user_id)
            await s.execute(
                update(AuthSession)
                .where(AuthSession.user_id == who.user_id)
                .values(ended_at=datetime.now(UTC), ended_reason="factors_reset")
            )
            await s.commit()
        await real_lock(session, user_id)

    monkeypatch.setattr(account, "lock_person", reset_lands_first)
    response = await auth_env.client.post("/api/v1/me/recovery-codes", headers=who.headers())

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "auth_required"
    async with AsyncSession(auth_env.admin_engine) as session:
        left = await session.scalar(
            select(func.count())
            .select_from(AuthRecoveryCode)
            .where(AuthRecoveryCode.user_id == who.user_id)
        )
    assert left == 0

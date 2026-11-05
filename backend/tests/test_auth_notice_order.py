"""A notice is the last act of its transaction (fix round 3; re-review of fix
round 2, Important 1).

A notice is mail, and mail does not roll back. `notices.tell` -> `mail.
send_account_mail` is synchronous and runs INSIDE the caller's transaction:
with a relay that signs in, `mail._sign_in` checks the relay's secret out of
the vault and records `email.credential_used` in that same session (Task 3).
So a notice sent before something that can still refuse is two defects when
the refusal comes:

- the person is told of an act that did not happen (a recovery code "used"
  while all ten are still unused);
- the vault checkout happened and the mail went, but its audit row rolled
  back with the transaction: a credential access with no record
  (CLAUDE.md invariant 4).

`flows._spend_recovery_code` sent the "recovery code used" notice before the
session-liveness check of the step that called it (`_stepped_up` at
step-up, `mark_factor` at sign-in), which refuses -- inside the block --
when the session ended meanwhile. The notice now follows that check. These
tests use the REAL account mail with a password relay (only the SMTP socket is
faked), so the checkout and its audit row are the production ones, and they
assert the pair both ways: no mail without its `email.credential_used` row,
no row without its mail.
"""

import smtplib
from email.message import EmailMessage
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import flows
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import SESSION_COOKIE, end_all_for_user, resolve
from app.core.client_scope import ClientScope
from app.core.mail import MailSettingsInput, save_settings
from app.core.tenancy import org_session
from app.models import AuditEvent
from app.models.auth import AuthRecoveryCode
from tests.auth_support import Staff, browser, console_ready, cookie, login, signed_in
from tests.session_support import cookie_header, http_request
from tests.test_auth_reset_needs_recovery import _mark, _set_mark

RELAY = MailSettingsInput(
    host="smtp.relay.test",
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
)


class _Relay:
    """The SMTP socket, faked: every message the relay accepted."""

    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int, timeout: float | None = None, **_: Any) -> None:
        pass

    def __enter__(self) -> "_Relay":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ehlo_or_helo_if_needed(self) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def send_message(self, message: EmailMessage) -> None:
        _Relay.sent.append(message)


@pytest.fixture()
def relay_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    _Relay.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Relay)
    return api_env


async def _with_relay(env: Any, org: UUID, admin: Staff) -> None:
    """Settings -> Email: a relay that signs in with a vaulted password."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        await save_settings(
            session,
            env.app.state.vault,
            org_id=org,
            actor_id=admin.user_id,
            values=RELAY,
            password="relay-password-SENTINEL",
            client_secret=None,
        )


def _mails_to(who: Staff) -> list[EmailMessage]:
    return [m for m in _Relay.sent if m["To"] == who.email]


async def _count(env: Any, org: UUID, action: str) -> int:
    async with AsyncSession(env.admin_engine) as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.org_id == org, AuditEvent.action == action)
                )
            ).scalar_one()
        )


async def _unused(env: Any, who: Staff) -> int:
    async with AsyncSession(env.admin_engine) as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(AuthRecoveryCode)
                    .where(
                        AuthRecoveryCode.user_id == who.user_id,
                        AuthRecoveryCode.used_at.is_(None),
                    )
                )
            ).scalar_one()
        )


async def _setup(env: Any, name: str) -> tuple[UUID, Staff]:
    org = await env.create_org(name)
    who = await signed_in(env, org, "technician")
    await _with_relay(env, org, who)
    return org, who


async def _end_sessions(env: Any, org: UUID, who: Staff) -> None:
    """Any end between the door and the step: sign-out elsewhere, an
    administrator's end, a password change, a mail reset."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
        await end_all_for_user(s, who.user_id, "ended_by_admin")


async def _assert_mail_and_checkout_paired(env: Any, org: UUID) -> None:
    """Invariant 4: every mail the relay took had its vault checkout recorded,
    and every recorded checkout sent a mail."""
    assert await _count(env, org, "email.credential_used") == len(_Relay.sent)


async def test_a_recovery_step_up_on_an_ended_session_sends_nothing(relay_env: Any) -> None:
    """The reviewer's probe, kept. Send the notice inside `_spend_recovery_code`
    again and the mail goes while the spend and the checkout's audit row roll
    back: red on the mail and on the pairing."""
    org, who = await _setup(relay_env, "notice-stepup-dead")
    request = http_request(relay_env, cookie=cookie_header(who.session))
    principal = await resolve(request, touch=False)
    assert principal is not None
    await _end_sessions(relay_env, org, who)
    before = len(_Relay.sent)

    with pytest.raises(AuthRefusal) as refused:
        await flows.step_up_recovery(request, principal, code=who.recovery_codes[0])

    assert (refused.value.status_code, refused.value.code) == (401, "auth_required")
    assert _Relay.sent[before:] == []
    assert await _unused(relay_env, who) == 10
    assert await _count(relay_env, org, "auth.recovery_code_used") == 0
    await _assert_mail_and_checkout_paired(relay_env, org)


async def test_a_recovery_sign_in_on_an_ended_session_sends_nothing(relay_env: Any) -> None:
    """The same order in `factor_recovery`, since Task 7: the pending session
    ends between the door and `mark_factor`, which refuses inside the block."""
    org, who = await _setup(relay_env, "notice-signin-dead")
    who.session, who.network = None, browser()
    assert (await login(relay_env, who)).json()["state"] == "factor_required"
    request = http_request(relay_env, cookie=cookie_header(who.session))
    principal = await resolve(request, touch=False)
    assert principal is not None and not principal.factor_done
    await _end_sessions(relay_env, org, who)
    before = len(_Relay.sent)

    with pytest.raises(AuthRefusal) as refused:
        await flows.factor_recovery(
            request, principal, code=who.recovery_codes[0], remember=False
        )

    assert (refused.value.status_code, refused.value.code) == (401, "auth_required")
    assert _Relay.sent[before:] == []
    assert await _unused(relay_env, who) == 10
    assert await _count(relay_env, org, "auth.recovery_code_used") == 0
    await _assert_mail_and_checkout_paired(relay_env, org)


async def test_a_recovery_code_used_on_a_live_session_is_told_once_and_recorded(
    relay_env: Any,
) -> None:
    """Non-vacuity, and the pairing on the success path: at step-up and at
    sign-in, one notice per code used, each with its checkout recorded."""
    org, who = await _setup(relay_env, "notice-live")

    up = await relay_env.client.post(
        "/api/v1/auth/step-up/recovery",
        headers=who.headers(),
        json={"code": who.recovery_codes[0]},
    )
    assert up.status_code == 204, up.text
    who.session, who.network = None, browser()
    assert (await login(relay_env, who)).json()["state"] == "factor_required"
    signed = await relay_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=who.headers(),
        json={"code": who.recovery_codes[1], "remember_device": False},
    )
    assert signed.json() == {"state": "signed_in"}, signed.text

    used = [m for m in _mails_to(who) if "recovery code was used" in m["Subject"]]
    assert len(used) == 2
    assert await _unused(relay_env, who) == 8
    assert await _count(relay_env, org, "auth.recovery_code_used") == 2
    await _assert_mail_and_checkout_paired(relay_env, org)


async def test_a_recovery_link_tells_only_once_its_session_is_made(
    relay_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`flows.accept_recovery`: the "sign-in methods reset" notice now follows
    the new session's creation, the last step that can fail. Make that step
    fail and no mail goes, the factors stay and nothing is recorded; send the
    notice first again and the mail goes for a recovery that rolled back:
    red."""
    from app.core.auth import action_tokens

    org, who = await _setup(relay_env, "notice-recover")
    # The link as the server's command issues it, without the command's own
    # notice (it opens the vault through its own client).
    async with org_session(org, engine=relay_env.app_engine, clients=ClientScope.all()) as s:
        token = await action_tokens.issue(
            s,
            org_id=org,
            user_id=who.user_id,
            purpose=action_tokens.RECOVER,
            ttl=action_tokens.RECOVER_TTL,
            created_by=None,
        )
    link = f"https://itms.test/recover#{token}"
    issued = len(_Relay.sent)
    real_started = flows._started

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the session could not be made")

    monkeypatch.setattr(flows, "_started", failing)
    with pytest.raises(RuntimeError):
        await flows.accept_recovery(
            http_request(relay_env), token=link.split("#", 1)[1]
        )
    assert _Relay.sent[issued:] == []
    assert await _unused(relay_env, who) == 10
    assert await _count(relay_env, org, "user.recovery_link_used") == 0
    await _assert_mail_and_checkout_paired(relay_env, org)

    monkeypatch.setattr(flows, "_started", real_started)
    accepted = await relay_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": link.split("#", 1)[1]}
    )
    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    assert cookie(accepted, SESSION_COOKIE)
    assert [m["Subject"] for m in _Relay.sent[issued:]] == [
        "Your Novieri sign-in methods were reset"
    ]
    await _assert_mail_and_checkout_paired(relay_env, org)


# --- the recovery link's new password (operator decision 2026-11-05,
# «Pedir contraseña nueva si hay marca»): while an emailed link set the
# password, the recovery asks for a new one. Its refusals come before any
# notice, and its "password changed" notice is told last, with the
# factors' reset, after the new session is made.

RECOVERED = "Saffron-Harbour-Quartz-4127"


async def _recovery_token(env: Any, org: UUID, who: Staff) -> str:
    """A recovery link's token as the server's command issues it, without the
    command's own notice (it opens the vault through its own client)."""
    from app.core.auth import action_tokens

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
        return await action_tokens.issue(
            s,
            org_id=org,
            user_id=who.user_id,
            purpose=action_tokens.RECOVER,
            ttl=action_tokens.RECOVER_TTL,
            created_by=None,
        )


@pytest.mark.parametrize(
    ("mark", "password", "answer"),
    [
        (True, None, (409, "recovery_needs_password")),
        (True, "tiny-pass", (422, "password_rejected")),
        # Offered without the mark (operator decision 2026-11-05, «Ofrecerla
        # siempre, aplicar»), and held to the same rule.
        (False, "tiny-pass", (422, "password_rejected")),
    ],
    ids=["no-password", "weak-password", "weak-password-no-mark"],
)
async def test_a_recovery_refused_for_its_password_sends_nothing(
    relay_env: Any, mark: bool, password: str | None, answer: tuple[int, str]
) -> None:
    """With the mark set, no password (409) and a weak one (422) are refused
    before any notice -- and a weak one without it: no mail, no relay
    checkout, nothing recorded, the factors in place, and the link still
    usable. Raise either after a `notices.tell` and the mail goes for a
    recovery that rolled back, its checkout's audit row with it: red on the
    mail and on the pairing."""
    org, who = await _setup(relay_env, f"notice-recover-refused-{answer[0]}-{int(mark)}")
    if mark:
        await _set_mark(relay_env, who)
    token = await _recovery_token(relay_env, org, who)
    before = len(_Relay.sent)
    body = {"token": token} if password is None else {"token": token, "password": password}

    refused = await relay_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json=body
    )

    assert (refused.status_code, refused.json()["code"]) == answer, refused.text
    assert _Relay.sent[before:] == []
    assert await _count(relay_env, org, "auth.password_changed") == 0
    assert await _count(relay_env, org, "user.recovery_link_used") == 0
    assert await _unused(relay_env, who) == 10
    await _assert_mail_and_checkout_paired(relay_env, org)
    details = await relay_env.client.post(
        "/api/v1/auth/recover/details", headers=browser(), json={"token": token}
    )
    assert details.json() == {"email": who.email, "password_required": mark}, details.text


async def test_a_recovery_that_sets_a_password_tells_only_once_its_session_is_made(
    relay_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the mark set and a good password, both notices -- "password
    changed", then "sign-in methods reset" -- follow the new session's
    creation, the last step that can fail. Make that step fail and neither
    mail goes, and the password, the mark, the factors and the records are
    as they were; tell the password change where it is stored and its mail
    goes for a recovery that rolled back: red. Then the real press: the two
    mails in that order, each with its checkout recorded."""
    org, who = await _setup(relay_env, "notice-recover-password")
    await _set_mark(relay_env, who)
    token = await _recovery_token(relay_env, org, who)
    issued = len(_Relay.sent)
    real_started = flows._started

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the session could not be made")

    monkeypatch.setattr(flows, "_started", failing)
    with pytest.raises(RuntimeError):
        await flows.accept_recovery(http_request(relay_env), token=token, password=RECOVERED)
    assert _Relay.sent[issued:] == []
    assert await _mark(relay_env, who) is not None
    assert await _count(relay_env, org, "auth.password_changed") == 0
    assert await _count(relay_env, org, "user.recovery_link_used") == 0
    assert await _unused(relay_env, who) == 10
    await _assert_mail_and_checkout_paired(relay_env, org)

    monkeypatch.setattr(flows, "_started", real_started)
    accepted = await relay_env.client.post(
        "/api/v1/auth/recover/accept",
        headers=browser(),
        json={"token": token, "password": RECOVERED},
    )
    assert accepted.json() == {"state": "enroll_required"}, accepted.text
    assert await _mark(relay_env, who) is None
    assert [m["Subject"] for m in _Relay.sent[issued:]] == [
        "Your Novieri password was changed",
        "Your Novieri sign-in methods were reset",
    ]
    assert await _count(relay_env, org, "auth.password_changed") == 1
    await _assert_mail_and_checkout_paired(relay_env, org)


async def test_a_vault_error_in_the_second_closing_notice_lets_the_first_stand(
    relay_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 3 review, Minor 2. The recovery that sets a password closes with two
    notices; the vault does not answer at the second one's relay checkout.
    That send fails (`mail._sign_in`: `MailSendFailed(SIGN_IN_UNAVAILABLE)`,
    and `notices.deliver` answers False), so the transaction commits: the
    first mail went, its checkout is recorded, the second has neither, and the
    recovery stands -- the password set, the factors gone, both records
    written. Let the vault's error through and it rolls the whole recovery back
    after the first mail went, that mail's checkout row with it: red."""
    import httpx

    org, who = await _setup(relay_env, "notice-recover-vault")
    await _set_mark(relay_env, who)
    token = await _recovery_token(relay_env, org, who)
    issued = len(_Relay.sent)
    vault = relay_env.app.state.vault
    real_checkout = vault.checkout
    calls = 0

    async def second_fails(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ConnectError("connection refused to https://openbao.internal:8200")
        result: str = await real_checkout(*args, **kwargs)
        return result

    monkeypatch.setattr(vault, "checkout", second_fails)
    accepted = await relay_env.client.post(
        "/api/v1/auth/recover/accept",
        headers=browser(),
        json={"token": token, "password": RECOVERED},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "enroll_required"}
    assert calls == 2
    assert [m["Subject"] for m in _Relay.sent[issued:]] == ["Your Novieri password was changed"]
    await _assert_mail_and_checkout_paired(relay_env, org)
    assert await _count(relay_env, org, "auth.password_changed") == 1
    assert await _count(relay_env, org, "user.recovery_link_used") == 1
    assert await _mark(relay_env, who) is None
    assert await _unused(relay_env, who) == 0

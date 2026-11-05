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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.passwords import STAFF_MINIMUM, PasswordRejected
from app.models import AuditEvent, Organization
from app.models.auth import AuthActionToken, AuthSession
from tests.auth_support import (
    CONSOLE,
    browser,
    capture_mail,
    console_ready,
    link_token,
    login,
    signed_in,
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

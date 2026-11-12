"""A contact signs in with a passkey alone, without an address (plan
2026-11-06, Task 6; design §3, decisions D27, D28).

Most tests replace `flows.verify_authentication`, as
`tests/auth_support.py::add_passkey` says, and hold the rule around it: who
may enter this way, what a refusal says. Three run the real cryptography at
the route, with phase 1's software authenticator
(`tests/webauthn_authenticator.py`): falsifier 4 -- an assertion without
user verification signs nobody in, and leaves its challenge spent --;
decision 4, the second factor refusing the anonymous challenge; and the
organization that is unknown or not active, answered as a spent challenge
is."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import flows
from app.core.auth.passkeys import CHALLENGE_BYTES, PasskeyRejected
from app.core.auth.sessions import SESSION_COOKIE, token_hash
from app.models import AuditEvent, Organization, User
from app.models.auth import (
    AuthFailure,
    AuthPasskey,
    AuthPassword,
    AuthSession,
    AuthWebauthnChallenge,
)
from tests.auth_support import (
    PASSKEY_RP_ID,
    add_passkey,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    login,
    point_at,
    register_passkey,
    signed_in,
)
from tests.session_support import CONSOLE
from tests.webauthn_authenticator import unb64url

RP_ID = PASSKEY_RP_ID


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


def _verifying(monkeypatch: pytest.MonkeyPatch, passkey_id: UUID | None) -> None:
    """`flows.verify_authentication` answers that passkey, or refuses."""

    async def fake(session: Any, credential: Any, **_kwargs: Any) -> AuthPasskey:
        if passkey_id is None:
            raise PasskeyRejected(reason="verification")
        found = await session.get(AuthPasskey, passkey_id)
        assert found is not None
        return found

    monkeypatch.setattr(flows, "verify_authentication", fake)


async def _post(env: Any, who: Any) -> Any:
    return await env.client.post(
        "/api/v1/auth/passkey", headers=who.network, json={"credential": {"id": "x"}}
    )


async def test_the_options_name_no_credential_and_require_user_verification(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("pk-options")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    got = await auth_env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    assert got.status_code == 200, got.text
    options = got.json()
    assert options["allowCredentials"] == []
    assert options["userVerification"] == "required"
    async with AsyncSession(auth_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuthWebauthnChallenge.user_id, AuthWebauthnChallenge.purpose).where(
                    AuthWebauthnChallenge.org_id == org
                )
            )
        ).all()
    assert [(r.user_id, r.purpose) for r in rows] == [(None, "authenticate")]


async def test_a_contacts_passkey_alone_signs_the_contact_in(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-contact")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    answer = await _post(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}
    who.session = cookie(answer, SESSION_COOKIE)
    me = await auth_env.client.get("/api/v1/portal/me", headers=who.headers())
    assert me.status_code == 200 and me.json()["kind"] == "contact"
    # The session this entry point mints is a contact's: the console's door
    # keeps it off every technician route (D42).
    console = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert console.status_code == 403
    assert console.json()["code"] == "portal_only"


async def test_a_technicians_passkey_signs_nobody_in_here(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-tech")
    tech = await signed_in(auth_env, org, "technician")
    _verifying(monkeypatch, await add_passkey(auth_env, tech))
    answer = await _post(auth_env, tech)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"
    assert cookie(answer, SESSION_COOKIE) is None


async def test_a_refused_assertion_is_the_same_answer(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-refused")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, None)
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"


async def test_a_locked_passkey_signs_nobody_in(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-locked")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="passkey",
                consecutive=100,
                last_failed_at=datetime.now(UTC),
                disabled_at=datetime.now(UTC),
            )
        )
        await s.commit()
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"


async def test_falsifier_4_no_user_verification_signs_nobody_in_and_spends_the_challenge(
    auth_env: Any,
) -> None:
    """Design §10.4 at this route, with real signatures."""
    org = await auth_env.create_org("pk-uv")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    authenticator = await register_passkey(auth_env, who)

    options = await auth_env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    unverified = authenticator.get(options.json(), origin=CONSOLE, uv=False)
    refused = await auth_env.client.post(
        "/api/v1/auth/passkey", headers=who.network, json={"credential": unverified}
    )
    assert refused.status_code == 401
    assert refused.json()["code"] == "passkey_rejected"
    assert cookie(refused, SESSION_COOKIE) is None
    async with AsyncSession(auth_env.admin_engine) as s:
        used = (
            (
                await s.execute(
                    select(AuthWebauthnChallenge.used_at).where(AuthWebauthnChallenge.org_id == org)
                )
            )
            .scalars()
            .all()
        )
    assert len(used) == 1 and used[0] is not None

    options = await auth_env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    verified = authenticator.get(options.json(), origin=CONSOLE, uv=True)
    accepted = await auth_env.client.post(
        "/api/v1/auth/passkey", headers=who.network, json={"credential": verified}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}


async def test_the_second_factor_refuses_a_challenge_that_is_nobodys(auth_env: Any) -> None:
    """Decision 4: a pending session's factor step answered with the
    anonymous challenge of `/auth/passkey/options` is refused."""
    org = await auth_env.create_org("pk-owner")
    tech = await signed_in(auth_env, org, "technician")
    authenticator = await register_passkey(auth_env, tech)
    await auth_env.client.post("/api/v1/auth/logout", headers=tech.headers())
    tech.session = None
    first = await login(auth_env, tech)
    assert first.json()["state"] == "factor_required"
    anonymous = await auth_env.client.post("/api/v1/auth/passkey/options", headers=tech.network)
    answer = await auth_env.client.post(
        "/api/v1/auth/factor/passkey",
        headers=tech.headers(),
        json={
            "credential": authenticator.get(anonymous.json(), origin=CONSOLE),
            "remember_device": False,
        },
    )
    assert answer.status_code == 401, answer.text


async def test_a_contacts_new_passkey_is_discoverable_and_a_technicians_is_preferred(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("pk-register")
    who = await contact(auth_env, org)
    await point_at(auth_env, org)
    await login(auth_env, who)
    mine = await auth_env.client.post("/api/v1/auth/enroll/passkey/options", headers=who.headers())
    assert mine.status_code == 200, mine.text
    assert mine.json()["authenticatorSelection"]["residentKey"] == "required"
    assert mine.json()["authenticatorSelection"]["userVerification"] == "required"

    tech = await signed_in(auth_env, org, "technician")
    theirs = await auth_env.client.post(
        "/api/v1/auth/enroll/passkey/options", headers=tech.headers()
    )
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["authenticatorSelection"]["residentKey"] == "preferred"


# --------------------------------------------------------------------------- #
# Beyond the plan's tests: the coordinator's two review notes for this task,
# and the refusals the rule names that the plan's tests do not reach.
# --------------------------------------------------------------------------- #


def _rejections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The REAL `verify_authentication`, watched: the reason of every refusal
    it raises, in order. Proves the verification ran, and where it failed."""
    real = flows.verify_authentication
    reasons: list[str] = []

    async def watched(session: Any, credential: Any, **kwargs: Any) -> AuthPasskey:
        try:
            return await real(session, credential, **kwargs)
        except PasskeyRejected as refused:
            reasons.append(refused.reason)
            raise

    monkeypatch.setattr(flows, "verify_authentication", watched)
    return reasons


async def _send(env: Any, who: Any, credential: dict[str, Any]) -> Any:
    return await env.client.post(
        "/api/v1/auth/passkey", headers=who.network, json={"credential": credential}
    )


async def _options(env: Any, who: Any) -> dict[str, Any]:
    got = await env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    assert got.status_code == 200, got.text
    options: dict[str, Any] = got.json()
    return options


async def test_an_unknown_or_suspended_organization_is_answered_as_a_replayed_assertion(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coordinator's review note: `lookup.org_of_slug` names an organization
    whatever its status. An organization that is not active, and a slug that
    names none, get the one refusal every other assertion gets -- the same
    401 body -- and options that look like any other's.

    The baseline is a REPLAYED assertion: its challenge is already spent, so
    the verification refuses it at the challenge (`challenge`). A suspended
    organization's assertion is verified in full and refused for the
    organization alone. A slug that names none is refused at the challenge
    too, as the replay is: its options stored no challenge (no session, no
    sweep, no insert -- quicker, and harmless, because the slug is the
    server's configuration, never the request's)."""
    org = await auth_env.create_org("pk-org-state")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    authenticator = await register_passkey(auth_env, who)
    reasons = _rejections(monkeypatch)

    # The baseline: a replayed assertion, whose challenge is already spent.
    issued = await _options(auth_env, who)
    assert (
        await _send(auth_env, who, authenticator.get(issued, origin=CONSOLE))
    ).status_code == 200
    replayed = await _send(auth_env, who, authenticator.get(issued, origin=CONSOLE))
    assert replayed.status_code == 401
    replay_refusal = replayed.json()
    assert replay_refusal["code"] == "passkey_rejected"
    assert reasons == ["challenge"]

    # Suspended: the contact's own, genuine passkey -- verified, and refused alike.
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(Organization).where(Organization.id == org).values(status="suspended")
        )
        await s.commit()
    suspended_options = await _options(auth_env, who)
    assert set(suspended_options) == set(issued)
    assert suspended_options["allowCredentials"] == []
    suspended = await _send(auth_env, who, authenticator.get(suspended_options, origin=CONSOLE))
    assert suspended.status_code == 401
    assert suspended.json() == replay_refusal
    assert cookie(suspended, SESSION_COOKIE) is None
    assert reasons == ["challenge"]  # it verified: refused for the organization alone
    async with AsyncSession(auth_env.admin_engine) as s:
        live = (
            await s.execute(
                select(AuthWebauthnChallenge.id).where(
                    AuthWebauthnChallenge.org_id == org, AuthWebauthnChallenge.used_at.is_(None)
                )
            )
        ).all()
    assert live == []  # its challenge is spent, as a refused assertion's is

    # A slug that names no organization: options of the same shape, and the
    # same refusal, at the same step as the replay -- the challenge.
    auth_env.settings.default_org_slug = f"no-such-org-{uuid4().hex[:8]}"
    nowhere = await _options(auth_env, who)
    assert set(nowhere) == set(issued)
    assert nowhere["allowCredentials"] == [] and nowhere["userVerification"] == "required"
    assert nowhere["rpId"] == RP_ID and len(unb64url(nowhere["challenge"])) == CHALLENGE_BYTES
    unknown = await _send(auth_env, who, authenticator.get(nowhere, origin=CONSOLE))
    assert unknown.status_code == 401
    assert unknown.json() == replay_refusal
    assert cookie(unknown, SESSION_COOKIE) is None
    assert reasons == ["challenge", "challenge"]


async def test_no_organization_named_at_all_is_the_installations_503(auth_env: Any) -> None:
    """No `ITS_DEFAULT_ORG_SLUG`: what `/auth/login` answers, on both routes --
    a fact about the installation, the same for every caller."""
    org = await auth_env.create_org("pk-no-slug")
    who = await contact(auth_env, org, password=None)
    auth_env.settings.default_org_slug = ""
    for path, body in (("/passkey/options", None), ("/passkey", {"credential": {"id": "x"}})):
        got = await auth_env.client.post(f"/api/v1/auth{path}", headers=who.network, json=body)
        assert got.status_code == 503, got.text
        assert got.json()["code"] == "sign_in_unavailable"


async def test_a_contact_who_is_not_active_signs_nobody_in(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-disabled")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(update(User).where(User.id == who.user_id).values(status="disabled"))
        await s.commit()
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"
    assert cookie(answer, SESSION_COOKIE) is None


async def test_a_waiting_passkey_signs_nobody_in_and_nothing_is_counted(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 2: a passkey factor in its wait does not enter here, and a
    refusal on this route counts against nobody -- not even the person whose
    passkey verified."""
    org = await auth_env.create_org("pk-waiting")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="passkey",
                consecutive=20,
                last_failed_at=datetime.now(UTC),
            )
        )
        await s.commit()
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"
    _verifying(monkeypatch, None)
    assert (await _post(auth_env, who)).status_code == 401
    async with AsyncSession(auth_env.admin_engine) as s:
        consecutive = (
            await s.execute(
                select(AuthFailure.consecutive).where(
                    AuthFailure.user_id == who.user_id, AuthFailure.factor == "passkey"
                )
            )
        ).scalar_one()
    assert consecutive == 20


async def test_a_contacts_passkey_sign_in_is_audited(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-audit")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    assert (await _post(auth_env, who)).status_code == 200
    async with AsyncSession(auth_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.meta).where(
                    AuditEvent.org_id == org, AuditEvent.target_id == str(who.user_id)
                )
            )
        ).all()
    assert [(r.action, r.meta) for r in rows] == [
        ("auth.signed_in", {"method": "passkey", "trusted_device": False})
    ]


async def test_a_passkey_alone_leaves_the_password_set_by_mail_mark(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coordinator decision (Task 6 fix round 1), a departure from the plan's
    Step 4: `failures.record_success` clears the "password set by mail" mark
    because whoever set that password then holds a factor too. Here no
    password is presented, so that does not follow: a contact whose password
    an emailed link set signs in with the passkey alone, and the mark stays
    -- for a later contact access reset (Task 8) to read."""
    org = await auth_env.create_org("pk-mail-mark")
    who = await contact(auth_env, org)
    await point_at(auth_env, org)
    passkey = await add_passkey(auth_env, who)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    [mail] = auth_env.mail.sent
    reset = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": link_token(mail.text), "password": "Cobalt-Lantern-4826-Meadow"},
    )
    assert reset.status_code == 204, reset.text

    async def mark() -> datetime | None:
        async with AsyncSession(auth_env.admin_engine) as s:
            value: datetime | None = await s.scalar(
                select(AuthPassword.mail_reset_at).where(AuthPassword.user_id == who.user_id)
            )
            return value

    assert await mark() is not None
    _verifying(monkeypatch, passkey)
    answer = await _post(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}
    assert await mark() is not None


async def test_a_passkey_alone_session_records_that_it_started_with_a_passkey(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 9b, item B: `auth_sessions.first_step`, which no later factor of
    this session can turn into a password presented."""
    org = await auth_env.create_org("pk-first-step")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    answer = await _post(auth_env, who)
    assert answer.status_code == 200, answer.text
    token = cookie(answer, SESSION_COOKIE)
    assert token is not None
    async with AsyncSession(auth_env.admin_engine) as s:
        step = await s.scalar(
            select(AuthSession.first_step).where(AuthSession.token_hash == token_hash(token))
        )
    assert step == "passkey"

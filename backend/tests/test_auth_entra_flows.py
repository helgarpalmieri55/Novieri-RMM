"""Sign in with Microsoft, the flows (plan 2026-11-06, Task 9; design §4,
§2.4, §2.5, falsifier 8; decisions D30-D32, D41; operator decisions C3 and
C4). Each branch that refuses has a test that it refuses (A2-50)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pyotp
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import entra_flows, recover
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import ENTRA_COOKIE, SESSION_COOKIE, token_hash
from app.core.client_scope import ClientScope
from app.models import AuditEvent, Role, User
from app.models.auth import (
    ENTRA,
    AuthEntraSettings,
    AuthExternalIdentity,
    AuthPassword,
    AuthSession,
)
from tests.auth_support import (
    CONTACT_PASSWORD,
    PASSWORD,
    browser,
    capture_mail,
    console_ready,
    contact,
    cookie,
    fresh_code,
    link_token,
    login,
    point_at,
    signed_in,
)
from tests.entra_support import (
    OTHER_TENANT,
    TENANT,
    FakeMicrosoft,
    entra_org,
    finish_microsoft,
    list_other_tenant,
    round_trip,
    seed_link,
    start_microsoft,
    with_cookie,
)
from tests.session_support import http_request, principal_for, refusal_code, sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    api_env.microsoft = FakeMicrosoft()
    api_env.microsoft.install(monkeypatch, api_env.app)
    return api_env


async def _step_up(env: Any, who: Any, *, minutes_ago: int) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == who.user_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
        )
        await s.commit()


async def _invite_contact(env: Any, who: Any, admin: dict[str, str]) -> str:
    sent = await env.client.post(
        f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite", headers=admin
    )
    assert sent.status_code == 200, sent.text
    return link_token(env.mail.sent[-1].text)


async def _invite_technician(env: Any, admin: dict[str, str]) -> tuple[UUID, str]:
    async with AsyncSession(env.admin_engine) as s:
        role = await s.scalar(
            select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
        )
    email = f"tech-{uuid4().hex[:6]}@toeshee.test"
    invited = await env.client.post(
        "/api/v1/users/invite",
        headers=admin,
        json={"email": email, "display_name": "Ana", "grants": [{"role_id": str(role)}]},
    )
    assert invited.status_code == 201, invited.text
    mail = [m for m in env.mail.sent if m.to == email][-1]
    return UUID(invited.json()["user_id"]), link_token(mail.text)


async def _counts(env: Any, org: UUID) -> tuple[int, int]:
    async with AsyncSession(env.admin_engine) as s:
        users = await s.scalar(select(func.count()).select_from(User).where(User.org_id == org))
        links = await s.scalar(
            select(func.count())
            .select_from(AuthExternalIdentity)
            .where(AuthExternalIdentity.org_id == org)
        )
    return int(users or 0), int(links or 0)


async def _links_of(env: Any, user_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as s:
        found = await s.scalar(select(func.count()).where(AuthExternalIdentity.user_id == user_id))
    return int(found or 0)


def _clears_the_transaction(response: httpx.Response) -> bool:
    """A2-55: the transaction's cookie set to expire now."""
    return any(
        value.startswith(f"{ENTRA_COOKIE}=") and "max-age=0" in value.lower()
        for name, value in response.headers.multi_items()
        if name == "set-cookie"
    )


async def test_falsifier_8_an_unknown_account_is_refused_and_nothing_is_made(
    auth_env: Any,
) -> None:
    org, _admin = await entra_org(auth_env)
    # A technician whose address is the very one the token's `email` claim says.
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            User(org_id=org, external_subject=f"sub-{uuid4().hex[:8]}", email="dana@example.test")
        )
        await s.commit()
    before = await _counts(auth_env, org)
    answer = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 401
    assert answer.json()["code"] == "microsoft_not_linked"
    assert cookie(answer, SESSION_COOKIE) is None
    assert await _counts(auth_env, org) == before


async def test_the_transaction_cookie_is_host_only_strict_and_never_in_the_body(
    auth_env: Any,
) -> None:
    """A2-50 (8): the verifier travels only in `__Host-itms_entra`."""
    await entra_org(auth_env)
    start = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    assert start.status_code == 200, start.text
    [header] = [
        value
        for name, value in start.headers.multi_items()
        if name == "set-cookie" and value.startswith(f"{ENTRA_COOKIE}=")
    ]
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "path=/", "samesite=strict", "max-age=600"} <= attributes
    assert not any(part.startswith("domain") for part in attributes)
    verifier = cookie(start, ENTRA_COOKIE)
    assert verifier and verifier not in start.text


async def test_a_technician_links_from_my_security_then_signs_in_and_is_asked_the_factor(
    auth_env: Any,
) -> None:
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    mails = len(auth_env.mail.sent)
    linked = await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    assert linked.status_code == 200, linked.text
    assert linked.json() == {"state": "linked"}
    async with AsyncSession(auth_env.admin_engine) as s:
        row = (
            await s.execute(
                select(AuthExternalIdentity).where(AuthExternalIdentity.user_id == tech.user_id)
            )
        ).scalar_one()
        events = (
            (
                await s.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == org, AuditEvent.action == "auth.identity_linked"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert (row.tenant_id, row.subject) == (TENANT, oid)
    assert list(events) == [{"provider": "entra", "tenant_id": str(TENANT), "source": "profile"}]
    [told] = auth_env.mail.sent[mails:]
    assert told.to == tech.email and "Microsoft" in told.text

    signed = await round_trip(auth_env, tech.network, {"intent": "sign_in"}, oid=oid)
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "factor_required"
    assert cookie(signed, SESSION_COOKIE) is not None


async def test_microsoft_does_not_resume_an_idle_session(auth_env: Any) -> None:
    """B2-16, A2-22: within the maximum, only the PASSWORD may resume an idle
    session (design §2.4, NIST 800-63B-4 §2.2.3). The same browser, its
    cookie still naming the idle session, is asked its second factor, and
    the idle session is replaced, not resumed."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    async with AsyncSession(auth_env.admin_engine) as s:
        idle_id = await s.scalar(
            select(AuthSession.id).where(
                AuthSession.user_id == tech.user_id, AuthSession.ended_at.is_(None)
            )
        )
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == idle_id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()
    signed = await round_trip(auth_env, tech.headers(), {"intent": "sign_in"}, oid=oid)
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "factor_required"
    async with AsyncSession(auth_env.admin_engine) as s:
        idle = await s.get(AuthSession, idle_id)
    assert idle is not None and idle.ended_reason == "replaced"


async def test_linking_needs_a_recent_confirmation_at_both_ends(auth_env: Any) -> None:
    """Design §2.5 and A2-53: the last 10 minutes at `/start` AND at the return."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await _step_up(auth_env, tech, minutes_ago=30)
    refused = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    assert refused.status_code == 403
    assert refusal_code(refused) == "step_up_required"
    await _step_up(auth_env, tech, minutes_ago=0)
    start = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    await _step_up(auth_env, tech, minutes_ago=30)
    late = await finish_microsoft(auth_env, tech.headers(), start, oid=uuid4())
    assert late.status_code == 403
    assert refusal_code(late) == "step_up_required"
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_a_link_is_finished_only_by_the_session_that_started_it(auth_env: Any) -> None:
    """A2-50 (2): another session of the same person, or the same one ended
    on the way, is a 401 -- and nothing is linked."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    other = await sign_in(auth_env, org, tech.sub, step_up=True)
    start = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    elsewhere = await finish_microsoft(
        auth_env, {**tech.network, **other.headers}, start, oid=uuid4()
    )
    assert elsewhere.status_code == 401
    again = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == tech.user_id, AuthSession.id != other.session_id)
            .values(ended_at=datetime.now(UTC), ended_reason="signed_out")
        )
        await s.commit()
    ended = await finish_microsoft(auth_env, tech.headers(), again, oid=uuid4())
    assert ended.status_code == 401
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_one_microsoft_account_is_one_person_and_a_person_has_one(auth_env: Any) -> None:
    """A2-50 (3), B2-22."""
    org, _admin = await entra_org(auth_env)
    first = await signed_in(auth_env, org, "technician")
    second = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    mine = await round_trip(auth_env, first.headers(), {"intent": "link"}, oid=oid)
    assert mine.status_code == 200, mine.text
    taken = await round_trip(auth_env, second.headers(), {"intent": "link"}, oid=oid)
    assert taken.status_code == 409
    assert taken.json()["code"] == "microsoft_already_linked"
    another = await round_trip(auth_env, first.headers(), {"intent": "link"}, oid=uuid4())
    assert another.status_code == 409
    assert another.json()["code"] == "microsoft_already_linked"
    assert await _links_of(auth_env, first.user_id) == 1
    assert await _links_of(auth_env, second.user_id) == 0


async def test_a_technician_from_another_tenant_is_refused_a_link(auth_env: Any) -> None:
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    refused = await round_trip(
        auth_env, tech.headers(), {"intent": "link"}, oid=uuid4(), tid=OTHER_TENANT
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "microsoft_tenant_refused"


async def test_at_sign_in_every_refusal_reads_as_an_account_linked_to_nobody(
    auth_env: Any,
) -> None:
    """A2-54 and A2-50 (4): a technician linked from a tenant that is no
    longer Novieri's, a contact whose client no longer lists theirs, a
    contact of an archived client and a person switched off each get the 401
    of an unknown account -- whoever administers a tenant learns nothing
    about who is linked here."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oids = {name: uuid4() for name in ("tech", "dropped", "archived", "off")}
    await seed_link(auth_env, tech, tenant=OTHER_TENANT, oid=oids["tech"])
    dropped = await contact(auth_env, org)
    await list_other_tenant(auth_env, dropped.client_id, tenants=[])
    await seed_link(auth_env, dropped, tenant=OTHER_TENANT, oid=oids["dropped"])
    archived = await contact(auth_env, org)
    await list_other_tenant(auth_env, archived.client_id)
    await seed_link(auth_env, archived, tenant=OTHER_TENANT, oid=oids["archived"])
    gone = await auth_env.client.post(
        f"/api/v1/clients/{archived.client_id}/archive", headers=admin
    )
    assert gone.status_code == 200, gone.text
    off = await contact(auth_env, org)
    await list_other_tenant(auth_env, off.client_id)
    await seed_link(auth_env, off, tenant=OTHER_TENANT, oid=oids["off"])
    switched = await auth_env.client.patch(
        f"/api/v1/contacts/{off.user_id}", headers=admin, json={"status": "disabled"}
    )
    assert switched.status_code == 200, switched.text
    for name, oid in oids.items():
        answer = await round_trip(
            auth_env, browser(), {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT
        )
        assert answer.status_code == 401, name
        assert answer.json()["code"] == "microsoft_not_linked", name


async def test_a_contact_accepts_the_invitation_with_microsoft_in_a_tenant_of_their_client(
    auth_env: Any,
) -> None:
    org, admin = await entra_org(auth_env)
    who = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.json()["microsoft"] is True
    with_password = await start_microsoft(
        auth_env, browser(), {"intent": "invite", "token": token, "password": CONTACT_PASSWORD}
    )
    assert with_password.status_code == 422
    assert with_password.json()["code"] == "password_not_used"
    oid = uuid4()
    accepted = await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=oid, tid=OTHER_TENANT
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    again = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT)
    assert again.json() == {"state": "signed_in"}


async def test_an_invitation_refused_for_its_tenant_stays_usable(auth_env: Any) -> None:
    """A2-50 (5), B2-30: the link is consumed in the transaction that links,
    and a refused tenant rolls both back."""
    org, admin = await entra_org(auth_env)
    who = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    refused = await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=TENANT
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "microsoft_tenant_refused"
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text


async def test_a_technician_accepts_with_microsoft_and_a_password_of_their_own(
    auth_env: Any,
) -> None:
    """Operator decision C3: Microsoft is never a technician's only way in.
    The password is stored at `/start`, before Microsoft is even asked, so a
    round trip that never comes back still leaves one; `user_admin` then
    reads the invitation as accepted (A2-50 (6), A2-57)."""
    org, admin = await entra_org(auth_env)
    user_id, token = await _invite_technician(auth_env, admin)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.json()["microsoft"] is True
    bare = await start_microsoft(auth_env, browser(), {"intent": "invite", "token": token})
    assert bare.status_code == 422
    assert bare.json()["code"] == "password_required"
    assert cookie(bare, ENTRA_COOKIE) is None
    network = browser()
    start = await start_microsoft(
        auth_env, network, {"intent": "invite", "token": token, "password": PASSWORD}
    )
    assert start.status_code == 200, start.text
    session = cookie(start, SESSION_COOKIE)
    assert session is not None
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, user_id) is not None
    done = await finish_microsoft(
        auth_env,
        {**network, "Cookie": f"{SESSION_COOKIE}={session}"},
        start,
        oid=uuid4(),
    )
    assert done.status_code == 200, done.text
    assert done.json() == {"state": "enroll_required"}
    assert await _links_of(auth_env, user_id) == 1
    resend = await auth_env.client.post(f"/api/v1/users/{user_id}/invite/resend", headers=admin)
    assert resend.status_code == 409
    assert "already accepted" in resend.json()["detail"]


async def test_every_use_of_the_key_is_audited_with_whoever_is_known(auth_env: Any) -> None:
    """Invariant 4 and A2-51: a link and an invitation name their person; a
    sign-in, where nobody is known yet, is the system's."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    await round_trip(auth_env, tech.network, {"intent": "sign_in"}, oid=oid)
    who = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=OTHER_TENANT
    )
    async with AsyncSession(auth_env.admin_engine) as s:
        used = (
            await s.execute(
                select(AuditEvent.actor_type, AuditEvent.actor_id)
                .where(AuditEvent.org_id == org, AuditEvent.action == "auth.entra_credential_used")
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        ).all()
    assert [tuple(row) for row in used] == [
        ("user", tech.user_id),
        ("system", None),
        ("user", who.user_id),
    ]


async def test_a_reset_or_a_recovery_keeps_the_microsoft_link(auth_env: Any) -> None:
    """B2-24, A2-64: the link is a first factor, like the password. The
    administrator's reset and the recovery link take the second factors and
    leave it; the next sign-in with Microsoft enrols a new factor."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    reset = await auth_env.client.post(f"/api/v1/users/{tech.user_id}/factors/reset", headers=admin)
    assert reset.status_code == 204, reset.text
    assert await _links_of(auth_env, tech.user_id) == 1
    link = await recover.issue_recovery_link(settings=auth_env.settings, email=tech.email)
    recovered = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": link.split("#", 1)[1]}
    )
    assert recovered.status_code == 200, recovered.text
    assert await _links_of(auth_env, tech.user_id) == 1
    signed = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid)
    assert signed.json()["state"] == "enroll_required"


async def test_the_transaction_is_single_use_and_bound_to_its_browser(auth_env: Any) -> None:
    await entra_org(auth_env)
    start = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = auth_env.microsoft.issue_code(nonce=query["nonce"][0], oid=uuid4())
    other_browser = with_cookie(browser(), ENTRA_COOKIE, "A" * 43)
    stolen = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=other_browser,
        json={"code": code, "state": query["state"][0]},
    )
    assert stolen.status_code == 400
    assert stolen.json()["code"] == "microsoft_failed"
    assert auth_env.microsoft.token_requests == []
    # A2-55: the transaction is over, and its cookie is cleared, refusal or not.
    assert _clears_the_transaction(stolen)
    replay = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=with_cookie(browser(), ENTRA_COOKIE, cookie(start, ENTRA_COOKIE) or ""),
        json={"code": code, "state": query["state"][0]},
    )
    assert replay.status_code == 400, "the state was spent by the first attempt"


async def test_a_token_with_another_nonce_is_refused(auth_env: Any) -> None:
    await entra_org(auth_env)
    answer = await round_trip(
        auth_env, browser(), {"intent": "sign_in"}, oid=uuid4(), nonce="not-the-nonce-that-was-sent"
    )
    assert answer.status_code == 400
    assert answer.json()["code"] == "microsoft_failed"


async def test_an_empty_code_spends_the_transaction_and_clears_its_cookie(
    auth_env: Any,
) -> None:
    """The return page sends an empty `code` when Microsoft answered with an
    error (Task 12, A2-70): the state is spent, the cookie cleared, nothing
    asked of Microsoft (f2-2 M5)."""
    await entra_org(auth_env)
    start = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    headers = with_cookie(browser(), ENTRA_COOKIE, cookie(start, ENTRA_COOKIE) or "")
    empty = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=headers,
        json={"code": "", "state": query["state"][0]},
    )
    assert empty.status_code == 400
    assert empty.json()["code"] == "microsoft_failed"
    assert _clears_the_transaction(empty)
    code = auth_env.microsoft.issue_code(nonce=query["nonce"][0], oid=uuid4())
    late = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=headers,
        json={"code": code, "state": query["state"][0]},
    )
    assert late.status_code == 400, "the empty code spent the state"
    assert auth_env.microsoft.token_requests == []


async def test_a_console_address_changed_on_the_way_is_a_503(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2-55 (f2-2 M5): `complete` reads the address again; configuration
    that changed since `/start` is a 503, not a 500. A query is enough to
    make it unusable as a redirect URI and still name the console's origin,
    so the CSRF rule lets the request through to the flow."""
    await entra_org(auth_env)
    start = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    settings = auth_env.app.state.settings
    monkeypatch.setattr(settings, "public_console_url", f"{settings.public_console_url}?x=1")
    answer = await finish_microsoft(auth_env, browser(), start, oid=uuid4())
    assert answer.status_code == 503
    assert answer.json()["code"] == "microsoft_unavailable"
    assert auth_env.microsoft.token_requests == []


async def test_microsofts_keys_never_read_are_a_503_not_a_refusal(auth_env: Any) -> None:
    """Task 5 decision 3 (f2-2 M5): with no key ever read, the token cannot
    be judged -- `EntraUnavailable`, the 503 of a sign-in that is down."""
    await entra_org(auth_env)
    auth_env.microsoft.jwks_down = True
    answer = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 503
    assert answer.json()["code"] == "sign_in_unavailable"


async def test_an_expired_certificate_stops_the_sign_in_before_microsoft(
    auth_env: Any,
) -> None:
    """Task 5 decision 5 (f2-2 M5): `usable` is false, and nobody is sent to
    Microsoft to come back with a `microsoft_failed`."""
    org, _admin = await entra_org(auth_env)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
            .values(certificate_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    refused = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "microsoft_unavailable"
    assert cookie(refused, ENTRA_COOKIE) is None


async def test_no_organization_to_sign_in_to_is_the_503_of_every_sign_in(
    auth_env: Any,
) -> None:
    """f2-2 M7: the answer the password and the passkey give (Task 6)."""
    await entra_org(auth_env)
    auth_env.settings.default_org_slug = None
    refused = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    assert refused.status_code == 503
    assert refused.json()["code"] == "sign_in_unavailable"


async def test_a_contact_whose_client_lists_no_tenant_is_not_sent_to_microsoft(
    auth_env: Any,
) -> None:
    """A2-58 (f2-2 M5): `/start` refuses the link before anything is made."""
    org, _admin = await entra_org(auth_env)
    who = await contact(auth_env, org)
    signed = await login(auth_env, who)
    assert signed.json()["state"] == "signed_in", signed.text
    await _step_up(auth_env, who, minutes_ago=0)
    refused = await start_microsoft(auth_env, who.headers(), {"intent": "link"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "microsoft_unavailable"
    assert cookie(refused, ENTRA_COOKIE) is None


async def test_a_link_microsoft_is_not_offered_for_asks_no_confirmation_first(
    auth_env: Any,
) -> None:
    """Final review of phase 2, item 7. `/start` asked for a recent
    confirmation before it asked whether Microsoft is offered to the person
    at all: somebody it is not offered to typed their password or code, and
    only then read "not set up here". Offered-ness is checked first now. It
    protects nothing to check it second: whether Microsoft is offered is not
    secret from a signed-in person (My security shows it), and a link that
    is offered still asks for the confirmation, at both ends
    (`test_linking_needs_a_recent_confirmation_at_both_ends`)."""
    org, _admin = await entra_org(auth_env)
    who = await contact(auth_env, org)
    signed = await login(auth_env, who)
    assert signed.json()["state"] == "signed_in", signed.text
    await _step_up(auth_env, who, minutes_ago=30)
    refused = await start_microsoft(auth_env, who.headers(), {"intent": "link"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "microsoft_unavailable"
    assert cookie(refused, ENTRA_COOKIE) is None


async def test_a_key_that_cannot_be_used_to_sign_is_a_503_and_nothing_is_sent(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 5's contract: "the key cannot be read" is 503 `sign_in_unavailable`,
    never a 500 -- the vault's answer included when it opens but cannot sign
    (PyJWT says so with `InvalidKeyError`, not a `ValueError`). Microsoft is
    asked nothing."""
    await entra_org(auth_env)

    async def unusable(*_args: Any, **_kwargs: Any) -> str:
        return "not a private key"

    monkeypatch.setattr("app.core.auth.entra_settings.signing_key", unusable)
    answer = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "sign_in_unavailable"
    assert auth_env.microsoft.token_requests == []


async def test_a_technician_without_a_password_cannot_link_microsoft(auth_env: Any) -> None:
    """C3 (f2-2 M1): a technician with no password row would enter with
    Microsoft alone, and could not unlink it. Refused at the return, where
    the password went on the way, and at `/start`."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    start = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(delete(AuthPassword).where(AuthPassword.user_id == tech.user_id))
        await s.commit()
    late = await finish_microsoft(auth_env, tech.headers(), start, oid=uuid4())
    assert late.status_code == 409
    assert late.json()["code"] == "password_required"
    refused = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "password_required"
    assert cookie(refused, ENTRA_COOKIE) is None
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_unlinking_is_told_and_audited_and_never_leaves_somebody_without_a_way_in(
    auth_env: Any,
) -> None:
    """A2-50 (7): the mail and the audit row of an unlink; decision 5."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
    mails = len(auth_env.mail.sent)
    unlinked = await auth_env.client.delete("/api/v1/me/microsoft", headers=tech.headers())
    assert unlinked.status_code == 204, unlinked.text
    [told] = auth_env.mail.sent[mails:]
    assert told.to == tech.email and "Microsoft" in told.text
    async with AsyncSession(auth_env.admin_engine) as s:
        meta = await s.scalar(
            select(AuditEvent.meta).where(
                AuditEvent.org_id == org, AuditEvent.action == "auth.identity_unlinked"
            )
        )
    assert meta == {"provider": "entra", "tenant_id": str(TENANT)}

    who = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    accepted = await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=OTHER_TENANT
    )
    session = cookie(accepted, SESSION_COOKIE)
    refused = await auth_env.client.delete(
        "/api/v1/me/microsoft",
        headers={**browser(), "Cookie": f"{SESSION_COOKIE}={session}"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_method"
    assert await _links_of(auth_env, who.user_id) == 1


# --------------------------------------------------------------------------- #
# The coordinator's decisions this task's dispatch carried (items 1, 2 and 5)
# --------------------------------------------------------------------------- #


async def test_an_unknown_organization_answers_as_one_without_microsoft(auth_env: Any) -> None:
    """Dispatch item 5 (the Task 6 review note): a slug that names no
    organization is answered as everybody else -- at `/start`, where nobody
    is known yet, as an organization where sign-in with Microsoft is not set
    up: the same status and the same body, and no transaction."""
    org = await auth_env.create_org("entra-off")
    await point_at(auth_env, org)
    off = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    assert off.status_code == 409, off.text
    auth_env.settings.default_org_slug = f"no-such-org-{uuid4().hex[:8]}"
    unknown = await start_microsoft(auth_env, browser(), {"intent": "sign_in"})
    assert (unknown.status_code, unknown.json()) == (off.status_code, off.json())
    assert cookie(unknown, ENTRA_COOKIE) is None


async def test_a_contact_unlinks_in_their_own_clients_scope(auth_env: Any) -> None:
    """Invariant 5a, dispatch item 2 (the coordinator's Task 7 decision):
    `DELETE /me/microsoft` acts on a signed-in person's own rows, so a
    contact's runs in the scope the door established -- their own client --
    never `_entra_session`'s every client. Naming another client in
    `X-Client-Id` narrows to none, where not even their own row exists: 401
    `auth_required`, and the link stays. Handed every client, the flow fails
    closed. In their own client's scope the link goes."""
    org, _admin = await entra_org(auth_env)
    who = await contact(auth_env, org)
    await list_other_tenant(auth_env, who.client_id)
    await seed_link(auth_env, who, tenant=OTHER_TENANT, oid=uuid4())
    other = await contact(auth_env, org)
    signed = await sign_in(auth_env, org, who.sub, step_up=True)
    headers = {**who.network, **signed.headers}
    elsewhere = await auth_env.client.delete(
        "/api/v1/me/microsoft", headers={**headers, "X-Client-Id": str(other.client_id)}
    )
    assert elsewhere.status_code == 401, elsewhere.text
    assert refusal_code(elsewhere) == "auth_required"
    assert await _links_of(auth_env, who.user_id) == 1
    principal = await principal_for(auth_env, signed.session_id)
    with pytest.raises(AuthRefusal) as refused:
        await entra_flows.unlink(http_request(auth_env), principal, clients=ClientScope.all())
    assert refused.value.code == "auth_required"
    assert await _links_of(auth_env, who.user_id) == 1
    removed = await auth_env.client.delete("/api/v1/me/microsoft", headers=headers)
    assert removed.status_code == 204, removed.text
    assert await _links_of(auth_env, who.user_id) == 0


async def test_unlinking_needs_a_recent_confirmation(auth_env: Any) -> None:
    """Design §2.5: the route's `require_step_up`, and the flow's own."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
    await _step_up(auth_env, tech, minutes_ago=30)
    refused = await auth_env.client.delete("/api/v1/me/microsoft", headers=tech.headers())
    assert refused.status_code == 403
    assert refusal_code(refused) == "step_up_required"
    assert await _links_of(auth_env, tech.user_id) == 1


async def test_a_microsoft_sign_in_leaves_the_mail_reset_mark(auth_env: Any) -> None:
    """Dispatch item 1 (the coordinator's Task 6 decision): Microsoft
    presents no password, so the "password set by mail" mark stays -- for a
    technician asked their factor, and for a contact signed in outright.
    Microsoft has no failure counter of its own (B2-17): the first step
    records no success at all. Its link's `last_used_at` is written at that
    first step, whether or not a second follows (A2-59)."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    tech_oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=tech_oid)
    who = await contact(auth_env, org)
    await list_other_tenant(auth_env, who.client_id)
    who_oid = uuid4()
    await seed_link(auth_env, who, tenant=OTHER_TENANT, oid=who_oid)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id.in_([tech.user_id, who.user_id]))
            .values(mail_reset_at=datetime.now(UTC))
        )
        await s.commit()
    asked = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=tech_oid)
    assert asked.json()["state"] == "factor_required", asked.text
    entered = await round_trip(
        auth_env, browser(), {"intent": "sign_in"}, oid=who_oid, tid=OTHER_TENANT
    )
    assert entered.json()["state"] == "signed_in", entered.text
    async with AsyncSession(auth_env.admin_engine) as s:
        marks = (
            (
                await s.execute(
                    select(AuthPassword.mail_reset_at).where(
                        AuthPassword.user_id.in_([tech.user_id, who.user_id])
                    )
                )
            )
            .scalars()
            .all()
        )
        used = (
            (
                await s.execute(
                    select(AuthExternalIdentity.last_used_at).where(
                        AuthExternalIdentity.user_id.in_([tech.user_id, who.user_id])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(marks) == 2 and all(mark is not None for mark in marks)
    assert len(used) == 2 and all(when is not None for when in used)


# --------------------------------------------------------------------------- #
# Task 9b, item B: the "password set by mail" mark after a first step that
# was not the password (Task 9 review, I1)
# --------------------------------------------------------------------------- #


async def _mark_of(env: Any, user_id: UUID) -> datetime | None:
    async with AsyncSession(env.admin_engine) as s:
        mark: datetime | None = await s.scalar(
            select(AuthPassword.mail_reset_at).where(AuthPassword.user_id == user_id)
        )
        return mark


async def _set_mark(env: Any, user_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == user_id)
            .values(mail_reset_at=datetime.now(UTC))
        )
        await s.commit()


async def _first_step(env: Any, response: httpx.Response) -> str | None:
    """The `first_step` of the session whose cookie `response` set."""
    token = cookie(response, SESSION_COOKIE)
    assert token is not None
    async with AsyncSession(env.admin_engine) as s:
        step: str | None = await s.scalar(
            select(AuthSession.first_step).where(AuthSession.token_hash == token_hash(token))
        )
        return step


def _carrying(network: dict[str, str], response: httpx.Response) -> dict[str, str]:
    token = cookie(response, SESSION_COOKIE)
    assert token is not None
    return {**network, "Cookie": f"{SESSION_COOKIE}={token}"}


async def test_a_factor_after_a_microsoft_first_step_leaves_the_mail_reset_mark(
    auth_env: Any,
) -> None:
    """Task 9b item B. The mark holds off an administrator's factor reset
    until somebody proves a second factor AFTER PRESENTING THE PASSWORD the
    mailbox chose. A session Microsoft started presented none: its code at
    sign-in and at a recent confirmation leave the mark, and the reset is
    still refused -- else the mailbox holder resets the password, the
    victim's next Microsoft and code sign-in clears the mark, the
    administrator's reset goes through, and the mailed password takes the
    account. The same code after the password clears it, as it always did."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    linked = await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    assert linked.status_code == 200, linked.text
    await _set_mark(auth_env, tech.user_id)

    network = browser()
    asked = await round_trip(auth_env, network, {"intent": "sign_in"}, oid=oid)
    assert asked.json()["state"] == "factor_required", asked.text
    code = await fresh_code(auth_env, tech)
    entered = await auth_env.client.post(
        "/api/v1/auth/factor/totp", headers=_carrying(network, asked), json={"code": code}
    )
    assert entered.json()["state"] == "signed_in", entered.text
    assert await _mark_of(auth_env, tech.user_id) is not None
    assert await _first_step(auth_env, entered) == "microsoft"

    up = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=_carrying(network, entered),
        json={"code": await fresh_code(auth_env, tech)},
    )
    assert up.status_code == 204, up.text
    assert await _mark_of(auth_env, tech.user_id) is not None
    refused = await auth_env.client.post(
        f"/api/v1/users/{tech.user_id}/factors/reset", headers=admin
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "reset_needs_recovery"

    tech.session, tech.network = None, browser()
    first = await login(auth_env, tech)
    assert first.json()["state"] == "factor_required", first.text
    assert await _first_step(auth_env, first) == "password"
    done = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=tech.headers(),
        json={"code": await fresh_code(auth_env, tech)},
    )
    assert done.json()["state"] == "signed_in", done.text
    assert await _mark_of(auth_env, tech.user_id) is None
    allowed = await auth_env.client.post(
        f"/api/v1/users/{tech.user_id}/factors/reset", headers=admin
    )
    assert allowed.status_code == 204, allowed.text


async def _signed_in_meta(env: Any, user_id: UUID) -> list[dict[str, Any]]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent.meta)
                    .where(
                        AuditEvent.target_id == str(user_id),
                        AuditEvent.action == "auth.signed_in",
                    )
                    .order_by(AuditEvent.ts, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def test_the_record_of_a_factor_sign_in_says_how_it_started(auth_env: Any) -> None:
    """Final review of phase 2, item 4. `auth.signed_in` after a second
    factor said only the factor (`{"method": "totp"}`): in the immutable
    audit, a sign-in Microsoft started was indistinguishable from one the
    password started. It carries the session's first step now, the column
    `_after_the_password` reads."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    linked = await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    assert linked.status_code == 200, linked.text

    network = browser()
    asked = await round_trip(auth_env, network, {"intent": "sign_in"}, oid=oid)
    assert asked.json()["state"] == "factor_required", asked.text
    by_microsoft = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=_carrying(network, asked),
        json={"code": await fresh_code(auth_env, tech)},
    )
    assert by_microsoft.json()["state"] == "signed_in", by_microsoft.text
    assert (await _signed_in_meta(auth_env, tech.user_id))[-1] == {
        "method": "totp",
        "trusted_device": False,
        "first_step": "microsoft",
    }

    tech.session, tech.network = None, browser()
    assert (await login(auth_env, tech)).json()["state"] == "factor_required"
    by_password = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=tech.headers(),
        json={"code": await fresh_code(auth_env, tech)},
    )
    assert by_password.json()["state"] == "signed_in", by_password.text
    assert (await _signed_in_meta(auth_env, tech.user_id))[-1] == {
        "method": "totp",
        "trusted_device": False,
        "first_step": "password",
    }


async def test_a_step_up_in_a_session_microsoft_started_leaves_the_mark(auth_env: Any) -> None:
    """Task 9b item B. A contact who turned the app on signs in with
    Microsoft and the code; the mark is set while that session is open (as a
    mail reset leaves it, set here by hand so the step-up is the only act
    that could clear it) and stays through the step-up. A session the
    password started clears it with the same step-up."""
    org, _admin = await entra_org(auth_env)
    who = await contact(auth_env, org)
    await list_other_tenant(auth_env, who.client_id)
    oid = uuid4()
    await seed_link(auth_env, who, tenant=OTHER_TENANT, oid=oid)
    first = await login(auth_env, who)
    assert first.json() == {"state": "signed_in"}, first.text
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    who.totp_secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirmed = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(who.totp_secret).now()},
    )
    assert confirmed.status_code == 204, confirmed.text

    network = browser()
    asked = await round_trip(
        auth_env, network, {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT
    )
    assert asked.json()["state"] == "factor_required", asked.text
    entered = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=_carrying(network, asked),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert entered.json()["state"] == "signed_in", entered.text
    await _set_mark(auth_env, who.user_id)
    up = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=_carrying(network, entered),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert up.status_code == 204, up.text
    assert await _mark_of(auth_env, who.user_id) is not None

    up_after_password = await auth_env.client.post(
        "/api/v1/auth/step-up/totp",
        headers=who.headers(),
        json={"code": await fresh_code(auth_env, who)},
    )
    assert up_after_password.status_code == 204, up_after_password.text
    assert await _mark_of(auth_env, who.user_id) is None


async def test_each_session_records_the_step_it_started_with(auth_env: Any) -> None:
    """Task 9b item B, `auth_sessions.first_step`: the password, Microsoft,
    and a contact's invitation accepted with Microsoft."""
    org, admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    tech.session = None
    assert await _first_step(auth_env, await login(auth_env, tech)) == "password"
    by_microsoft = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid)
    assert await _first_step(auth_env, by_microsoft) == "microsoft"
    who = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    accepted = await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=OTHER_TENANT
    )
    assert accepted.json() == {"state": "signed_in"}, accepted.text
    assert await _first_step(auth_env, accepted) == "microsoft"


# --------------------------------------------------------------------------- #
# Task 9b, item C (invariant 5a) and item D's tests
# --------------------------------------------------------------------------- #


async def test_a_contact_links_microsoft_from_my_security_in_their_own_clients_scope(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Items C and D5. A contact links their account from My security: a
    tenant their client does not list is a 403 `microsoft_tenant_refused`
    and nothing is linked; one it lists is linked, told and audited
    (`source: profile`). The start and the return run under the contact's
    OWN client (invariant 5a), never `_entra_session`'s every client: a probe
    at each one's first act -- `signed_user` at the start, `locked_user` at
    the return -- looks up and touches another client's contact by id, and
    finds nothing, while the caller's own row is there."""
    org, _admin = await entra_org(auth_env)
    who = await contact(auth_env, org)
    await list_other_tenant(auth_env, who.client_id)
    other = await contact(auth_env, org)
    signed = await sign_in(auth_env, org, who.sub, step_up=True)
    headers = {**who.network, **signed.headers}
    seen: list[tuple[int, int, int]] = []

    async def probe(session: AsyncSession) -> None:
        theirs = await session.scalar(
            select(func.count())
            .select_from(AuthPassword)
            .where(AuthPassword.user_id == other.user_id)
        )
        touched = await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == other.user_id)
            .values(changed_at=AuthPassword.changed_at)
            .execution_options(synchronize_session=False)
        )
        mine = await session.scalar(
            select(func.count())
            .select_from(AuthPassword)
            .where(AuthPassword.user_id == who.user_id)
        )
        seen.append((theirs or 0, touched.rowcount, mine or 0))

    real_signed, real_locked = entra_flows.signed_user, entra_flows.locked_user

    async def probing_signed(session: AsyncSession, principal: Any) -> Any:
        await probe(session)
        return await real_signed(session, principal)

    async def probing_locked(session: AsyncSession, principal: Any) -> Any:
        await probe(session)
        return await real_locked(session, principal)

    monkeypatch.setattr(entra_flows, "signed_user", probing_signed)
    monkeypatch.setattr(entra_flows, "locked_user", probing_locked)

    refused = await round_trip(auth_env, headers, {"intent": "link"}, oid=uuid4(), tid=TENANT)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "microsoft_tenant_refused"
    assert await _links_of(auth_env, who.user_id) == 0

    mails = len(auth_env.mail.sent)
    oid = uuid4()
    linked = await round_trip(
        auth_env, headers, {"intent": "link"}, oid=oid, tid=OTHER_TENANT
    )
    assert linked.status_code == 200, linked.text
    assert linked.json() == {"state": "linked"}
    async with AsyncSession(auth_env.admin_engine) as s:
        row = (
            await s.execute(
                select(AuthExternalIdentity).where(AuthExternalIdentity.user_id == who.user_id)
            )
        ).scalar_one()
        meta = await s.scalar(
            select(AuditEvent.meta).where(
                AuditEvent.org_id == org,
                AuditEvent.action == "auth.identity_linked",
                AuditEvent.target_id == str(who.user_id),
            )
        )
    assert (row.tenant_id, row.subject, row.client_id) == (OTHER_TENANT, oid, who.client_id)
    assert meta == {"provider": "entra", "tenant_id": str(OTHER_TENANT), "source": "profile"}
    [told] = auth_env.mail.sent[mails:]
    assert told.to == who.email and "Microsoft" in told.text
    # Two round trips, each a start and a return.
    assert seen == [(0, 0, 1)] * 4, seen


async def test_a_technician_is_refused_at_sign_in_when_novieris_tenant_is_not_set(
    auth_env: Any,
) -> None:
    """D5. A technician signs in with Microsoft only from Novieri's own
    tenant (`staff_tenant_id`). With none set, a linked technician is the
    401 of an account linked to nobody (A2-54)."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
            .values(staff_tenant_id=None)
        )
        await s.commit()
    answer = await round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid)
    assert answer.status_code == 401, answer.text
    assert answer.json()["code"] == "microsoft_not_linked"
    assert cookie(answer, SESSION_COOKIE) is None


async def test_the_key_is_audited_with_the_technician_who_accepts_an_invitation(
    auth_env: Any,
) -> None:
    """D5. A technician accepting their invitation with Microsoft (C3) is
    known at the return -- the session their password opened at `/start` --
    so the use of the platform's key is audited with them as the actor
    (decision 6, A2-51), never the system."""
    org, admin = await entra_org(auth_env)
    user_id, token = await _invite_technician(auth_env, admin)
    network = browser()
    start = await start_microsoft(
        auth_env, network, {"intent": "invite", "token": token, "password": PASSWORD}
    )
    session = cookie(start, SESSION_COOKIE)
    assert session is not None
    done = await finish_microsoft(
        auth_env, {**network, "Cookie": f"{SESSION_COOKIE}={session}"}, start, oid=uuid4()
    )
    assert done.status_code == 200, done.text
    async with AsyncSession(auth_env.admin_engine) as s:
        used = (
            await s.execute(
                select(AuditEvent.actor_type, AuditEvent.actor_id).where(
                    AuditEvent.org_id == org, AuditEvent.action == "auth.entra_credential_used"
                )
            )
        ).all()
    assert [tuple(row) for row in used] == [("user", user_id)]


async def _end_sessions_of(env: Any, user_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.ended_at.is_(None))
            .values(ended_at=datetime.now(UTC), ended_reason="signed_out_everywhere")
        )
        await s.commit()


@pytest.mark.parametrize("when", ["during_the_exchange", "at_the_persons_lock"])
async def test_a_link_whose_session_ended_on_the_way_links_nothing(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """D1. The return checks its session at the door (`_caller`), then talks
    to Microsoft, then links. A session ended in between -- "sign out
    everywhere" while Microsoft answers, or an act that ends it under the
    person's advisory lock just before the link takes it -- links nothing:
    401 `auth_required`, checked again under that lock."""
    from app.core.auth import entra

    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    start = await start_microsoft(auth_env, tech.headers(), {"intent": "link"})
    if when == "during_the_exchange":
        real_exchange = entra.exchange

        async def exchange_then_sign_out(**kwargs: Any) -> str:
            await _end_sessions_of(auth_env, tech.user_id)
            return await real_exchange(**kwargs)

        monkeypatch.setattr(entra, "exchange", exchange_then_sign_out)
    else:
        real_lock = entra_flows.lock_person

        async def ended_just_before(session: AsyncSession, user_id: UUID) -> None:
            await _end_sessions_of(auth_env, tech.user_id)
            await real_lock(session, user_id)

        monkeypatch.setattr(entra_flows, "lock_person", ended_just_before)
    late = await finish_microsoft(auth_env, tech.headers(), start, oid=uuid4())
    assert late.status_code == 401, late.text
    assert refusal_code(late) == "auth_required"
    assert await _links_of(auth_env, tech.user_id) == 0


@pytest.mark.parametrize("same_account", [True, False])
async def test_a_technician_who_linked_meanwhile_is_told_so_precisely(
    auth_env: Any, same_account: bool
) -> None:
    """D3. A technician accepts their invitation with Microsoft; before the
    return comes back, a Microsoft account is already linked to them (from
    another tab). The answer says so -- the same account is already theirs,
    or they already have one -- never that it is "somebody here"'s."""
    org, admin = await entra_org(auth_env)
    user_id, token = await _invite_technician(auth_env, admin)
    network = browser()
    start = await start_microsoft(
        auth_env, network, {"intent": "invite", "token": token, "password": PASSWORD}
    )
    session = cookie(start, SESSION_COOKIE)
    assert session is not None
    oid = uuid4()
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            AuthExternalIdentity(
                org_id=org,
                user_id=user_id,
                provider=ENTRA,
                tenant_id=TENANT,
                subject=oid if same_account else uuid4(),
            )
        )
        await s.commit()
    done = await finish_microsoft(
        auth_env, {**network, "Cookie": f"{SESSION_COOKIE}={session}"}, start, oid=oid
    )
    assert done.status_code == 409, done.text
    assert done.json()["code"] == "microsoft_already_linked"
    assert done.json()["detail"] == (
        "this Microsoft account is already linked to your account"
        if same_account
        else "you already have a Microsoft account linked; remove it first"
    )
    assert await _links_of(auth_env, user_id) == 1


@pytest.mark.parametrize("intent", ["sign_in", "link"])
async def test_a_password_sent_with_a_sign_in_or_a_link_is_refused(
    auth_env: Any, intent: str
) -> None:
    """D4. Only a technician's invitation carries a password to `/start`
    (C3). Sent with a sign-in or a link it is refused -- 422
    `password_not_used`, as a contact's invitation refuses one -- never
    silently dropped, and no transaction is begun."""
    org, _admin = await entra_org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    headers = tech.headers() if intent == "link" else browser()
    refused = await start_microsoft(auth_env, headers, {"intent": intent, "password": PASSWORD})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "password_not_used"
    assert cookie(refused, ENTRA_COOKIE) is None


async def _actions_on(env: Any, user_id: UUID, actions: tuple[str, ...]) -> list[str]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.target_id == str(user_id), AuditEvent.action.in_(actions))
                    .order_by(AuditEvent.ts, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def test_every_row_a_sign_in_step_writes_is_recorded_in_order(auth_env: Any) -> None:
    """Task 9b fix round 1, item 3. Since item A a step writes its rows
    first and its audit rows last (the chain's lock goes last); the wrappers
    that do both -- `flows.contact_admitted`, `flows._factor_done`,
    `entra_flows._add_link` -- record every row they write, in the order
    the acts were done: a contact accepting with a password and with
    Microsoft, a technician's recovery code at sign-in, and a technician's
    link from My security."""
    org, admin = await entra_org(auth_env)
    by_password = await contact(auth_env, org, password=None, role=False)
    token = await _invite_contact(auth_env, by_password, admin)
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    by_microsoft = await contact(auth_env, org, password=None, role=False)
    await list_other_tenant(auth_env, by_microsoft.client_id)
    token = await _invite_contact(auth_env, by_microsoft, admin)
    linked = await round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=OTHER_TENANT
    )
    assert linked.status_code == 200, linked.text
    tech = await signed_in(auth_env, org, "technician")
    profile = await round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
    assert profile.status_code == 200, profile.text
    tech.session, tech.network = None, browser()
    assert (await login(auth_env, tech)).json()["state"] == "factor_required"
    recovered = await auth_env.client.post(
        "/api/v1/auth/factor/recovery",
        headers=tech.headers(),
        json={"code": tech.recovery_codes[0]},
    )
    assert recovered.json()["state"] == "signed_in", recovered.text

    accepting = ("auth.password_changed", "auth.identity_linked", "contact.invite_accepted")
    assert await _actions_on(auth_env, by_password.user_id, (*accepting, "auth.signed_in")) == [
        "auth.password_changed",
        "contact.invite_accepted",
        "auth.signed_in",
    ]
    assert await _actions_on(auth_env, by_microsoft.user_id, (*accepting, "auth.signed_in")) == [
        "auth.identity_linked",
        "contact.invite_accepted",
        "auth.signed_in",
    ]
    steps = ("auth.identity_linked", "auth.recovery_code_used", "auth.signed_in")
    assert (await _actions_on(auth_env, tech.user_id, steps))[-3:] == [
        "auth.identity_linked",
        "auth.recovery_code_used",
        "auth.signed_in",
    ]

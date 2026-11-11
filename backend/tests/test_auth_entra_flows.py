"""Sign in with Microsoft, the flows (plan 2026-11-06, Task 9; design §4,
§2.4, §2.5, falsifier 8; decisions D30-D32, D41; operator decisions C3 and
C4). Each branch that refuses has a test that it refuses (A2-50)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import entra_flows, recover
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import ENTRA_COOKIE, SESSION_COOKIE
from app.core.client_scope import ClientScope
from app.models import AuditEvent, Client, Role, User
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
    link_token,
    login,
    point_at,
    signed_in,
)
from tests.entra_support import APP, TENANT, FakeMicrosoft
from tests.session_support import http_request, principal_for, refusal_code, sign_in

OTHER_TENANT = UUID("22222222-3333-4444-8555-666666666666")
SETTINGS = "/api/v1/settings/microsoft-sign-in"


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    api_env.microsoft = FakeMicrosoft()
    api_env.microsoft.install(monkeypatch, api_env.app)
    return api_env


async def _org(env: Any) -> tuple[UUID, dict[str, str]]:
    """An organization with sign-in with Microsoft switched on -- the
    platform's certificate made and "uploaded" (C4) -- and an admin."""
    org = await env.create_org("entra-flow")
    await point_at(env, org)
    admin = await env.create_user(org, "org-admin")
    headers = await env.headers(org, admin)
    saved = await env.client.put(
        SETTINGS,
        headers=headers,
        json={
            "enabled": True,
            "app_client_id": str(APP),
            "staff_tenant_id": str(TENANT),
            "new_certificate": True,
        },
    )
    assert saved.status_code == 200, saved.text
    env.microsoft.trust(saved.json()["certificate"])
    return org, headers


def _with_cookie(headers: dict[str, str], name: str, value: str) -> dict[str, str]:
    jar = [part for part in headers.get("Cookie", "").split("; ") if part]
    return {**headers, "Cookie": "; ".join([*jar, f"{name}={value}"])}


async def _start(env: Any, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    return await env.client.post("/api/v1/auth/microsoft/start", headers=headers, json=body)


async def _finish(
    env: Any,
    headers: dict[str, str],
    start: httpx.Response,
    *,
    oid: UUID,
    tid: UUID = TENANT,
    nonce: str | None = None,
) -> httpx.Response:
    assert start.status_code == 200, start.text
    verifier = cookie(start, ENTRA_COOKIE)
    assert verifier is not None
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = env.microsoft.issue_code(nonce=nonce or query["nonce"][0], tid=tid, oid=oid)
    return await env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=_with_cookie(headers, ENTRA_COOKIE, verifier),
        json={"code": code, "state": query["state"][0]},
    )


async def _round_trip(
    env: Any,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    oid: UUID,
    tid: UUID = TENANT,
    nonce: str | None = None,
) -> httpx.Response:
    start = await _start(env, headers, body)
    return await _finish(env, headers, start, oid=oid, tid=tid, nonce=nonce)


async def _lists_other_tenant(env: Any, client_id: UUID, tenants: list[UUID] | None = None) -> None:
    """The contact's client lists `OTHER_TENANT` (D35), written past the API."""
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(microsoft_tenants=[OTHER_TENANT] if tenants is None else tenants)
        )
        await s.commit()


async def _linked(env: Any, who: Any, *, tenant: UUID, oid: UUID) -> None:
    """A link written past the API: one the flows would refuse to make today."""
    async with AsyncSession(env.admin_engine) as s:
        s.add(
            AuthExternalIdentity(
                org_id=who.org_id,
                user_id=who.user_id,
                provider=ENTRA,
                tenant_id=tenant,
                subject=oid,
            )
        )
        await s.commit()


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
    org, _admin = await _org(auth_env)
    # A technician whose address is the very one the token's `email` claim says.
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            User(org_id=org, external_subject=f"sub-{uuid4().hex[:8]}", email="dana@example.test")
        )
        await s.commit()
    before = await _counts(auth_env, org)
    answer = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 401
    assert answer.json()["code"] == "microsoft_not_linked"
    assert cookie(answer, SESSION_COOKIE) is None
    assert await _counts(auth_env, org) == before


async def test_the_transaction_cookie_is_host_only_strict_and_never_in_the_body(
    auth_env: Any,
) -> None:
    """A2-50 (8): the verifier travels only in `__Host-itms_entra`."""
    await _org(auth_env)
    start = await _start(auth_env, browser(), {"intent": "sign_in"})
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
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    mails = len(auth_env.mail.sent)
    linked = await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
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

    signed = await _round_trip(auth_env, tech.network, {"intent": "sign_in"}, oid=oid)
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "factor_required"
    assert cookie(signed, SESSION_COOKIE) is not None


async def test_microsoft_does_not_resume_an_idle_session(auth_env: Any) -> None:
    """B2-16, A2-22: within the maximum, only the PASSWORD may resume an idle
    session (design §2.4, NIST 800-63B-4 §2.2.3). The same browser, its
    cookie still naming the idle session, is asked its second factor, and
    the idle session is replaced, not resumed."""
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
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
    signed = await _round_trip(auth_env, tech.headers(), {"intent": "sign_in"}, oid=oid)
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "factor_required"
    async with AsyncSession(auth_env.admin_engine) as s:
        idle = await s.get(AuthSession, idle_id)
    assert idle is not None and idle.ended_reason == "replaced"


async def test_linking_needs_a_recent_confirmation_at_both_ends(auth_env: Any) -> None:
    """Design §2.5 and A2-53: the last 10 minutes at `/start` AND at the return."""
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await _step_up(auth_env, tech, minutes_ago=30)
    refused = await _start(auth_env, tech.headers(), {"intent": "link"})
    assert refused.status_code == 403
    assert refusal_code(refused) == "step_up_required"
    await _step_up(auth_env, tech, minutes_ago=0)
    start = await _start(auth_env, tech.headers(), {"intent": "link"})
    await _step_up(auth_env, tech, minutes_ago=30)
    late = await _finish(auth_env, tech.headers(), start, oid=uuid4())
    assert late.status_code == 403
    assert refusal_code(late) == "step_up_required"
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_a_link_is_finished_only_by_the_session_that_started_it(auth_env: Any) -> None:
    """A2-50 (2): another session of the same person, or the same one ended
    on the way, is a 401 -- and nothing is linked."""
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    other = await sign_in(auth_env, org, tech.sub, step_up=True)
    start = await _start(auth_env, tech.headers(), {"intent": "link"})
    elsewhere = await _finish(auth_env, {**tech.network, **other.headers}, start, oid=uuid4())
    assert elsewhere.status_code == 401
    again = await _start(auth_env, tech.headers(), {"intent": "link"})
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == tech.user_id, AuthSession.id != other.session_id)
            .values(ended_at=datetime.now(UTC), ended_reason="signed_out")
        )
        await s.commit()
    ended = await _finish(auth_env, tech.headers(), again, oid=uuid4())
    assert ended.status_code == 401
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_one_microsoft_account_is_one_person_and_a_person_has_one(auth_env: Any) -> None:
    """A2-50 (3), B2-22."""
    org, _admin = await _org(auth_env)
    first = await signed_in(auth_env, org, "technician")
    second = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    mine = await _round_trip(auth_env, first.headers(), {"intent": "link"}, oid=oid)
    assert mine.status_code == 200, mine.text
    taken = await _round_trip(auth_env, second.headers(), {"intent": "link"}, oid=oid)
    assert taken.status_code == 409
    assert taken.json()["code"] == "microsoft_already_linked"
    another = await _round_trip(auth_env, first.headers(), {"intent": "link"}, oid=uuid4())
    assert another.status_code == 409
    assert another.json()["code"] == "microsoft_already_linked"
    assert await _links_of(auth_env, first.user_id) == 1
    assert await _links_of(auth_env, second.user_id) == 0


async def test_a_technician_from_another_tenant_is_refused_a_link(auth_env: Any) -> None:
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    refused = await _round_trip(
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
    org, admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oids = {name: uuid4() for name in ("tech", "dropped", "archived", "off")}
    await _linked(auth_env, tech, tenant=OTHER_TENANT, oid=oids["tech"])
    dropped = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, dropped.client_id, tenants=[])
    await _linked(auth_env, dropped, tenant=OTHER_TENANT, oid=oids["dropped"])
    archived = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, archived.client_id)
    await _linked(auth_env, archived, tenant=OTHER_TENANT, oid=oids["archived"])
    gone = await auth_env.client.post(
        f"/api/v1/clients/{archived.client_id}/archive", headers=admin
    )
    assert gone.status_code == 200, gone.text
    off = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, off.client_id)
    await _linked(auth_env, off, tenant=OTHER_TENANT, oid=oids["off"])
    switched = await auth_env.client.patch(
        f"/api/v1/contacts/{off.user_id}", headers=admin, json={"status": "disabled"}
    )
    assert switched.status_code == 200, switched.text
    for name, oid in oids.items():
        answer = await _round_trip(
            auth_env, browser(), {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT
        )
        assert answer.status_code == 401, name
        assert answer.json()["code"] == "microsoft_not_linked", name


async def test_a_contact_accepts_the_invitation_with_microsoft_in_a_tenant_of_their_client(
    auth_env: Any,
) -> None:
    org, admin = await _org(auth_env)
    who = await contact(auth_env, org, password=None, role=False)
    await _lists_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.json()["microsoft"] is True
    with_password = await _start(
        auth_env, browser(), {"intent": "invite", "token": token, "password": CONTACT_PASSWORD}
    )
    assert with_password.status_code == 422
    assert with_password.json()["code"] == "password_not_used"
    oid = uuid4()
    accepted = await _round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=oid, tid=OTHER_TENANT
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    again = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid, tid=OTHER_TENANT)
    assert again.json() == {"state": "signed_in"}


async def test_an_invitation_refused_for_its_tenant_stays_usable(auth_env: Any) -> None:
    """A2-50 (5), B2-30: the link is consumed in the transaction that links,
    and a refused tenant rolls both back."""
    org, admin = await _org(auth_env)
    who = await contact(auth_env, org, password=None, role=False)
    await _lists_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    refused = await _round_trip(
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
    org, admin = await _org(auth_env)
    user_id, token = await _invite_technician(auth_env, admin)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.json()["microsoft"] is True
    bare = await _start(auth_env, browser(), {"intent": "invite", "token": token})
    assert bare.status_code == 422
    assert bare.json()["code"] == "password_required"
    assert cookie(bare, ENTRA_COOKIE) is None
    network = browser()
    start = await _start(
        auth_env, network, {"intent": "invite", "token": token, "password": PASSWORD}
    )
    assert start.status_code == 200, start.text
    session = cookie(start, SESSION_COOKIE)
    assert session is not None
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, user_id) is not None
    done = await _finish(
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
    org, admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    await _round_trip(auth_env, tech.network, {"intent": "sign_in"}, oid=oid)
    who = await contact(auth_env, org, password=None, role=False)
    await _lists_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    await _round_trip(
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
    org, admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    reset = await auth_env.client.post(f"/api/v1/users/{tech.user_id}/factors/reset", headers=admin)
    assert reset.status_code == 204, reset.text
    assert await _links_of(auth_env, tech.user_id) == 1
    link = await recover.issue_recovery_link(settings=auth_env.settings, email=tech.email)
    recovered = await auth_env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": link.split("#", 1)[1]}
    )
    assert recovered.status_code == 200, recovered.text
    assert await _links_of(auth_env, tech.user_id) == 1
    signed = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=oid)
    assert signed.json()["state"] == "enroll_required"


async def test_the_transaction_is_single_use_and_bound_to_its_browser(auth_env: Any) -> None:
    await _org(auth_env)
    start = await _start(auth_env, browser(), {"intent": "sign_in"})
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = auth_env.microsoft.issue_code(nonce=query["nonce"][0], oid=uuid4())
    other_browser = _with_cookie(browser(), ENTRA_COOKIE, "A" * 43)
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
        headers=_with_cookie(browser(), ENTRA_COOKIE, cookie(start, ENTRA_COOKIE) or ""),
        json={"code": code, "state": query["state"][0]},
    )
    assert replay.status_code == 400, "the state was spent by the first attempt"


async def test_a_token_with_another_nonce_is_refused(auth_env: Any) -> None:
    await _org(auth_env)
    answer = await _round_trip(
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
    await _org(auth_env)
    start = await _start(auth_env, browser(), {"intent": "sign_in"})
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    headers = _with_cookie(browser(), ENTRA_COOKIE, cookie(start, ENTRA_COOKIE) or "")
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
    await _org(auth_env)
    start = await _start(auth_env, browser(), {"intent": "sign_in"})
    settings = auth_env.app.state.settings
    monkeypatch.setattr(settings, "public_console_url", f"{settings.public_console_url}?x=1")
    answer = await _finish(auth_env, browser(), start, oid=uuid4())
    assert answer.status_code == 503
    assert answer.json()["code"] == "microsoft_unavailable"
    assert auth_env.microsoft.token_requests == []


async def test_microsofts_keys_never_read_are_a_503_not_a_refusal(auth_env: Any) -> None:
    """Task 5 decision 3 (f2-2 M5): with no key ever read, the token cannot
    be judged -- `EntraUnavailable`, the 503 of a sign-in that is down."""
    await _org(auth_env)
    auth_env.microsoft.jwks_down = True
    answer = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 503
    assert answer.json()["code"] == "sign_in_unavailable"


async def test_an_expired_certificate_stops_the_sign_in_before_microsoft(
    auth_env: Any,
) -> None:
    """Task 5 decision 5 (f2-2 M5): `usable` is false, and nobody is sent to
    Microsoft to come back with a `microsoft_failed`."""
    org, _admin = await _org(auth_env)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthEntraSettings)
            .where(AuthEntraSettings.org_id == org)
            .values(certificate_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    refused = await _start(auth_env, browser(), {"intent": "sign_in"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "microsoft_unavailable"
    assert cookie(refused, ENTRA_COOKIE) is None


async def test_no_organization_to_sign_in_to_is_the_503_of_every_sign_in(
    auth_env: Any,
) -> None:
    """f2-2 M7: the answer the password and the passkey give (Task 6)."""
    await _org(auth_env)
    auth_env.settings.default_org_slug = None
    refused = await _start(auth_env, browser(), {"intent": "sign_in"})
    assert refused.status_code == 503
    assert refused.json()["code"] == "sign_in_unavailable"


async def test_a_contact_whose_client_lists_no_tenant_is_not_sent_to_microsoft(
    auth_env: Any,
) -> None:
    """A2-58 (f2-2 M5): `/start` refuses the link before anything is made."""
    org, _admin = await _org(auth_env)
    who = await contact(auth_env, org)
    signed = await login(auth_env, who)
    assert signed.json()["state"] == "signed_in", signed.text
    await _step_up(auth_env, who, minutes_ago=0)
    refused = await _start(auth_env, who.headers(), {"intent": "link"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "microsoft_unavailable"
    assert cookie(refused, ENTRA_COOKIE) is None


async def test_a_key_that_cannot_be_used_to_sign_is_a_503_and_nothing_is_sent(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 5's contract: "the key cannot be read" is 503 `sign_in_unavailable`,
    never a 500 -- the vault's answer included when it opens but cannot sign
    (PyJWT says so with `InvalidKeyError`, not a `ValueError`). Microsoft is
    asked nothing."""
    await _org(auth_env)

    async def unusable(*_args: Any, **_kwargs: Any) -> str:
        return "not a private key"

    monkeypatch.setattr("app.core.auth.entra_settings.signing_key", unusable)
    answer = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=uuid4())
    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "sign_in_unavailable"
    assert auth_env.microsoft.token_requests == []


async def test_a_technician_without_a_password_cannot_link_microsoft(auth_env: Any) -> None:
    """C3 (f2-2 M1): a technician with no password row would enter with
    Microsoft alone, and could not unlink it. Refused at the return, where
    the password went on the way, and at `/start`."""
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    start = await _start(auth_env, tech.headers(), {"intent": "link"})
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(delete(AuthPassword).where(AuthPassword.user_id == tech.user_id))
        await s.commit()
    late = await _finish(auth_env, tech.headers(), start, oid=uuid4())
    assert late.status_code == 409
    assert late.json()["code"] == "password_required"
    refused = await _start(auth_env, tech.headers(), {"intent": "link"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "password_required"
    assert cookie(refused, ENTRA_COOKIE) is None
    assert await _links_of(auth_env, tech.user_id) == 0


async def test_unlinking_is_told_and_audited_and_never_leaves_somebody_without_a_way_in(
    auth_env: Any,
) -> None:
    """A2-50 (7): the mail and the audit row of an unlink; decision 5."""
    org, admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
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
    await _lists_other_tenant(auth_env, who.client_id)
    token = await _invite_contact(auth_env, who, admin)
    accepted = await _round_trip(
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
    off = await _start(auth_env, browser(), {"intent": "sign_in"})
    assert off.status_code == 409, off.text
    auth_env.settings.default_org_slug = f"no-such-org-{uuid4().hex[:8]}"
    unknown = await _start(auth_env, browser(), {"intent": "sign_in"})
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
    org, _admin = await _org(auth_env)
    who = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, who.client_id)
    await _linked(auth_env, who, tenant=OTHER_TENANT, oid=uuid4())
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
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
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
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    tech_oid = uuid4()
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=tech_oid)
    who = await contact(auth_env, org)
    await _lists_other_tenant(auth_env, who.client_id)
    who_oid = uuid4()
    await _linked(auth_env, who, tenant=OTHER_TENANT, oid=who_oid)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id.in_([tech.user_id, who.user_id]))
            .values(mail_reset_at=datetime.now(UTC))
        )
        await s.commit()
    asked = await _round_trip(auth_env, browser(), {"intent": "sign_in"}, oid=tech_oid)
    assert asked.json()["state"] == "factor_required", asked.text
    entered = await _round_trip(
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

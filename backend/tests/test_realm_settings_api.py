"""Settings -> Email: `/api/v1/settings/email`.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §7
("Correo de cuentas"); the surface is
`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

RE-POINTED 2026-11-04. Until the platform had its own sign-in, these routes
wrote the identity provider's realm and this file drove a fake realm. They now
keep the settings in `account_mail_settings` and the relay's secret in the
vault (`app/core/mail.py`), and the test message is sent by the platform
itself, through a recording `smtplib.SMTP`. The properties are the ones this
tab always had, and the test that would notice each one breaking:

1. **The password is never echoed.** `test_no_route_ever_echoes_the_password`
   drives all three routes, a refused save included, and scans the RAW BODIES
   for the literal -- not for the absence of a key, which a renamed field
   would satisfy while still shipping the value. (The refused save earned its
   place: a model-level Pydantic rule once echoed the whole body, password
   included, into a 422. The rules live in the route, `_refuse_unusable`.)
2. **Every write is gated server-side and audited**, with WHICH fields
   changed and none of their values.
3. **Unconfigured is a state, not an error**: `configured: false`, not 404.
4. **The secret is in the vault and nowhere else** -- one global
   `account_mail` credential, never a column, never a response, never a
   `/credentials` checkout.
"""

import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.v1.realm_settings import MAX_CLIENT_SECRET, MAX_PASSWORD, TEST_SENDS_PER_MINUTE
from app.core.client_scope import ClientScope
from app.core.mail import (
    ACCOUNT_MAIL_CREDENTIAL_TYPE,
    MailSettingsInput,
    read_settings,
    save_settings,
)
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.core.tenancy import org_session
from app.models import AuditEvent, Credential, User
from app.models.auth import AccountMailSettings

#: What a relay already holds when a test starts: it must never come back out.
STORED = "hunter2-stored-in-the-vault"

#: What the operator types into the form. It must reach the vault verbatim and
#: appear nowhere else -- not in a response, not in an audit row.
TYPED = "s3cr3t-typed-once"

CLIENT_SECRET = "s3cr3t-client-value"

SAVED = MailSettingsInput(
    host="smtp.internal",
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

VALID_BODY: dict[str, Any] = {
    "host": "smtp.new.internal",
    "port": 465,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "support@toeshee.com",
    "encryption": "ssl",
    "username": "itms",
    "password": TYPED,
}

TOKEN_BODY: dict[str, Any] = {
    "host": "smtp.office365.com",
    "port": 587,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "",
    "encryption": "starttls",
    "username": "itms@toeshee.com",
    "auth_type": "token",
    "token_url": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
    "token_scope": "https://outlook.office365.com/.default",
    "token_client_id": "client-abc",
    "token_client_secret": CLIENT_SECRET,
}


class _RecordingSMTP:
    """`smtplib.SMTP`, recording; opens no socket."""

    instances: list["_RecordingSMTP"] = []
    raise_on_send: BaseException | None = None
    implicit_tls = False

    def __init__(
        self, host: str, port: int, timeout: float | None = None, context: Any = None
    ) -> None:
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: EmailMessage | None = None
        _RecordingSMTP.instances.append(self)

    def __enter__(self) -> "_RecordingSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, context: Any = None) -> None:
        self.starttls_called = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        if _RecordingSMTP.raise_on_send is not None:
            raise _RecordingSMTP.raise_on_send
        self.sent = message


class _RecordingSMTPSSL(_RecordingSMTP):
    implicit_tls = True


@pytest.fixture()
def email_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    _RecordingSMTP.instances = []
    _RecordingSMTP.raise_on_send = None
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", _RecordingSMTPSSL)
    api_env.smtp = _RecordingSMTP
    return api_env


async def _user_id(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as session:
        user_id: UUID = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        return user_id


async def _admin(env: Any, name: str) -> tuple[UUID, str, dict[str, str]]:
    org = await env.create_org(name)
    sub = await env.create_user(org, "org-admin")
    return org, sub, await env.headers(org, sub)


async def _seed(env: Any, org: UUID, sub: str, *, password: str | None = STORED) -> None:
    """A saved relay, written the way the route writes it (the service), with
    `password` in the same vault the routes use."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        await save_settings(
            session,
            env.app.state.vault,
            org_id=org,
            actor_id=await _user_id(env, sub),
            values=SAVED,
            password=password,
            client_secret=None,
        )


async def _stored(env: Any, org: UUID) -> tuple[AccountMailSettings | None, dict[str, str] | None]:
    """The row, and the vault secret it points at."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        row = await read_settings(session, org)
        if row is None or row.credential_id is None:
            return row, None
        secret: dict[str, str] = json.loads(
            await env.app.state.vault.checkout(session, org_id=org, credential_id=row.credential_id)
        )
        return row, secret


async def _credentials(env: Any, org: UUID) -> list[Credential]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(Credential).where(
                        Credential.org_id == org, Credential.type == ACCOUNT_MAIL_CREDENTIAL_TYPE
                    )
                )
            ).scalars()
        )


async def _audit(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    """Through the ADMIN engine: an assertion about what a request recorded
    must not itself be filtered by the mechanism under test."""
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


async def test_the_saved_settings_are_reported(email_env: Any) -> None:
    org, sub, headers = await _admin(email_env, "email-read")
    await _seed(email_env, org, sub)

    response = await email_env.client.get("/api/v1/settings/email", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["host"] == "smtp.internal"
    assert body["port"] == "587"
    assert body["from_address"] == "itms@toeshee.com"
    assert body["encryption"] == "starttls"
    assert body["username"] == "itms"
    assert body["authenticated"] is True


async def test_an_unconfigured_org_reports_configured_false_rather_than_404(
    email_env: Any,
) -> None:
    """THE STATE A NEW ORGANIZATION IS IN. A 404 would read in the console as
    "this route is not deployed", and the Users tab's banner -- the whole point
    of the flag -- would never appear."""
    _org, _sub, headers = await _admin(email_env, "email-empty")

    response = await email_env.client.get("/api/v1/settings/email", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["host"] == ""
    assert body["authenticated"] is False


# --------------------------------------------------------------------------- #
# the password -- the reason this surface is written the way it is
# --------------------------------------------------------------------------- #


async def test_no_route_ever_echoes_the_password(email_env: Any) -> None:
    """THE LITERAL, IN THE RAW BODY, ON EVERY ROUTE. Two literals: the one just
    typed (the form must not round-trip what it was given) and the one already
    in the vault (a read must never decrypt it into a response)."""
    org, sub, headers = await _admin(email_env, "email-echo")
    await _seed(email_env, org, sub)

    bodies = [
        (await email_env.client.get("/api/v1/settings/email", headers=headers)).text,
        (await email_env.client.post("/api/v1/settings/email/test", headers=headers)).text,
        (
            await email_env.client.put(
                "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "username": ""}
            )
        ).text,
        (
            await email_env.client.put(
                "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "port": 70000}
            )
        ).text,
        (
            await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)
        ).text,
    ]

    for body in bodies:
        assert TYPED not in body
        assert STORED not in body


async def test_the_typed_password_reaches_the_vault_verbatim(email_env: Any) -> None:
    """Non-vacuity for the test above: "never echoed" must not be achieved by
    never storing it."""
    org, _sub, headers = await _admin(email_env, "email-through")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=VALID_BODY
    )

    assert response.status_code == 200
    _row, secret = await _stored(email_env, org)
    assert secret == {"password": TYPED}


async def test_the_save_answers_with_what_is_STORED_not_what_was_typed(email_env: Any) -> None:
    """THE FORM SHOWS THE SERVER'S ANSWER, NEVER ITS OWN INPUT. The host is
    stored lower-cased (Decisión del plan 9); a route that echoed the body would
    show a value that is not what is stored."""
    _org, _sub, headers = await _admin(email_env, "email-reread")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, "host": "SMTP.Shouty.Internal"},
    )

    assert response.status_code == 200
    assert response.json()["host"] == "smtp.shouty.internal"


async def test_a_save_without_a_password_keeps_the_stored_one(email_env: Any) -> None:
    """EDITING THE PORT MUST NOT CLEAR THE CREDENTIAL. The console cannot send a
    password it never received, so absent means "leave it alone"."""
    org, sub, headers = await _admin(email_env, "email-keep")
    await _seed(email_env, org, sub)
    body = {key: value for key, value in VALID_BODY.items() if key != "password"}

    response = await email_env.client.put("/api/v1/settings/email", headers=headers, json=body)

    assert response.status_code == 200
    row, secret = await _stored(email_env, org)
    assert secret == {"password": STORED}
    assert row is not None and row.host == "smtp.new.internal"


async def test_clearing_the_username_clears_the_stored_credential(email_env: Any) -> None:
    """THE OTHER HALF OF "an absent password means keep it". No username, no
    sign-in -- and a credential nothing uses is one nobody rotates, so it goes."""
    org, sub, headers = await _admin(email_env, "email-noauth")
    await _seed(email_env, org, sub)
    body = {key: value for key, value in VALID_BODY.items() if key != "password"}

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**body, "username": ""}
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    row, secret = await _stored(email_env, org)
    assert row is not None and row.credential_id is None and secret is None
    assert await _credentials(email_env, org) == []


async def test_an_over_long_password_is_refused_without_repeating_it(email_env: Any) -> None:
    """The bound lives in the route, not on the field: a Pydantic field failure
    echoes its INPUT into the 422, and the input here is the password."""
    org, _sub, headers = await _admin(email_env, "email-long")
    over_long = "x" * (MAX_PASSWORD + 1)

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "password": over_long}
    )

    assert response.status_code == 422
    assert over_long not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_a_password_with_no_username_is_refused_rather_than_dropped(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-passwordless")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "username": ""}
    )

    assert response.status_code == 422
    assert TYPED not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_the_relay_secret_is_one_global_vault_credential(email_env: Any) -> None:
    """Invariant 3, at the database: one `account_mail` credential, every
    client's (one relay per organization), encrypted, and no column of the
    settings row carrying the value."""
    org, _sub, headers = await _admin(email_env, "email-vault")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)
    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)

    (credential,) = await _credentials(email_env, org)
    assert credential.client_id is None
    assert credential.ciphertext is not None and TYPED not in credential.ciphertext
    row, _secret = await _stored(email_env, org)
    assert row is not None and row.credential_id == credential.id
    assert all(TYPED not in str(getattr(row, c.key)) for c in row.__table__.columns)


async def test_the_relay_credential_cannot_be_checked_out(email_env: Any) -> None:
    """Decisión del plan 8: only the sender opens it. `credentials.checkout`
    (which `org-admin` holds) is not a way round Settings -> Email."""
    org, sub, headers = await _admin(email_env, "email-checkout")
    await _seed(email_env, org, sub)
    (credential,) = await _credentials(email_env, org)

    response = await email_env.client.post(
        f"/api/v1/credentials/{credential.id}/checkout",
        headers=headers,
        json={"reason": "curious"},
    )

    assert response.status_code == 409
    assert STORED not in response.text


# --------------------------------------------------------------------------- #
# authorization, audit, tenancy
# --------------------------------------------------------------------------- #


async def test_every_route_on_this_surface_is_gated(email_env: Any) -> None:
    """Invariant 6, read off the router rather than off three status codes."""
    routes = [
        (path, route)
        for path, route in flatten(email_env.app.router)
        if isinstance(route, APIRoute) and path.startswith("/api/v1/settings/email")
    ]

    assert routes
    for path, route in routes:
        assert route_permission(route) == "orgs.manage", f"{path} is not gated"


async def test_a_write_without_the_permission_is_refused_and_changes_nothing(
    email_env: Any,
) -> None:
    org = await email_env.create_org("email-rbac")
    nobody = await email_env.create_user(org, "read-only")
    headers = await email_env.headers(org, nobody)

    save = await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)
    read = await email_env.client.get("/api/v1/settings/email", headers=headers)
    test = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert [save.status_code, read.status_code, test.status_code] == [403, 403, 403]
    assert (await _stored(email_env, org))[0] is None
    assert email_env.smtp.instances == []


async def test_an_anonymous_caller_is_refused(email_env: Any) -> None:
    response = await email_env.client.get("/api/v1/settings/email")

    assert response.status_code == 401
    assert email_env.smtp.instances == []


async def test_a_read_never_opens_the_vault_and_a_refusal_never_reaches_it(
    email_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RE-POINTED from "authorization runs before the provisioning secret is
    read": there is no provisioning secret on disk any more. What replaces it
    is invariant 3's "decrypted only at point of use": reading the settings
    never decrypts the relay secret, and a refused caller never gets as far as
    the vault. The vault is swapped for one that fails the test on ANY call."""
    org, sub, headers = await _admin(email_env, "email-vault-order")
    await _seed(email_env, org, sub)
    nobody = await email_env.create_user(org, "read-only")
    unprivileged = await email_env.headers(org, nobody)

    class _Tripped:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the vault was touched: {name}")

    monkeypatch.setattr(email_env.app.state, "vault", _Tripped())

    anonymous = await email_env.client.get("/api/v1/settings/email")
    refused = await email_env.client.put(
        "/api/v1/settings/email", headers=unprivileged, json=VALID_BODY
    )
    tested = await email_env.client.post("/api/v1/settings/email/test", headers=unprivileged)
    read = await email_env.client.get("/api/v1/settings/email", headers=headers)

    assert [anonymous.status_code, refused.status_code, tested.status_code] == [401, 403, 403]
    assert read.status_code == 200 and read.json()["authenticated"] is True


async def test_the_change_is_audited_with_the_fields_and_none_of_the_values(
    email_env: Any,
) -> None:
    org, _sub, headers = await _admin(email_env, "email-audit")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)

    events = await _audit(email_env.admin_engine, org)
    written = [e for e in events if e.action == "email.settings_updated"]
    assert len(written) == 1
    assert "host" in written[0].meta["fields"]
    assert "password" in written[0].meta["fields"]  # the NAME, so a rotation is on record
    serialized = json.dumps(written[0].meta)
    assert TYPED not in serialized
    assert "smtp.new.internal" not in serialized


async def test_a_refused_write_records_nothing(email_env: Any) -> None:
    """A log saying a change happened when it did not is worse than no log."""
    org, _sub, headers = await _admin(email_env, "email-audit-refused")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "username": ""}
    )

    assert response.status_code == 422
    events = await _audit(email_env.admin_engine, org)
    assert [e for e in events if e.action.startswith("email.")] == []
    assert (await _stored(email_env, org))[0] is None


# --------------------------------------------------------------------------- #
# the test message
# --------------------------------------------------------------------------- #


async def test_the_test_send_goes_through_the_saved_relay_and_is_audited(email_env: Any) -> None:
    """It sends real mail through the organization's own relay, so it is on the
    record -- and so is the vault checkout that signed it in."""
    org, sub, headers = await _admin(email_env, "email-test-send")
    await _seed(email_env, org, sub)

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"sent": True, "recipient": "itms@toeshee.com"}
    (client,) = email_env.smtp.instances
    assert (client.host, client.port) == ("smtp.internal", 587)
    assert client.starttls_called and client.login_args == ("itms", STORED)
    assert client.sent is not None and client.sent["To"] == "itms@toeshee.com"
    assert client.sent["Subject"] == "ITMS test message"
    sent = [e for e in await _audit(email_env.admin_engine, org) if e.action.startswith("email.")]
    assert [e.action for e in sent] == ["email.credential_used", "email.test_requested"]
    assert sent[1].meta == {"recipient": "itms@toeshee.com", "outcome": "sent"}


async def test_a_test_send_the_relay_refuses_is_reported_not_swallowed(email_env: Any) -> None:
    """The failing case is the reason the button exists. And the checkout that
    preceded the refusal is still on the record (Decisión del plan 3)."""
    org, sub, headers = await _admin(email_env, "email-test-refused")
    await _seed(email_env, org, sub)
    email_env.smtp.raise_on_send = smtplib.SMTPResponseException(554, b"5.7.1 relay denied")

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"] == "The mail relay refused the message."
    assert "relay denied" not in response.text
    events = [e for e in await _audit(email_env.admin_engine, org) if e.action.startswith("email.")]
    # CHANGED in the review fix: a failed test is an event too, with its reason.
    assert [e.action for e in events] == ["email.credential_used", "email.test_requested"]
    assert events[1].meta == {"recipient": "itms@toeshee.com", "outcome": "relay_refused"}


async def test_the_test_send_goes_to_the_address_asked_for_and_audits_it(email_env: Any) -> None:
    org, sub, headers = await _admin(email_env, "email-test-chosen")
    await _seed(email_env, org, sub)
    tagged = f"{sub}+itms-test@test.local"

    response = await email_env.client.post(
        "/api/v1/settings/email/test", json={"recipient": tagged}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"sent": True, "recipient": tagged}
    assert email_env.smtp.instances[0].sent["To"] == tagged
    events = await _audit(email_env.admin_engine, org)
    sent = [e for e in events if e.action == "email.test_requested"]
    assert sent[0].meta == {"recipient": tagged, "outcome": "sent"}


async def test_a_colleague_at_the_callers_domain_is_refused_without_a_sign_in_domain(
    email_env: Any,
) -> None:
    """The caller's own address proves one mailbox, not a domain."""
    org, sub, headers = await _admin(email_env, "email-test-own-domain")
    await _seed(email_env, org, sub)

    response = await email_env.client.post(
        "/api/v1/settings/email/test", json={"recipient": "someone@test.local"}, headers=headers
    )

    assert response.status_code == 409
    assert email_env.smtp.instances == []


async def test_a_test_send_to_an_outside_address_is_refused_and_sends_nothing(
    email_env: Any,
) -> None:
    org, sub, headers = await _admin(email_env, "email-test-outside")
    await _seed(email_env, org, sub)

    response = await email_env.client.post(
        "/api/v1/settings/email/test", json={"recipient": "victim@elsewhere.com"}, headers=headers
    )

    assert response.status_code == 409
    assert "your own address" in response.json()["detail"]
    assert email_env.smtp.instances == []
    events = await _audit(email_env.admin_engine, org)
    assert [e for e in events if e.action.startswith("email.")] == []


async def test_a_test_with_nothing_saved_is_refused_and_names_no_vendor(email_env: Any) -> None:
    """RE-POINTED from "a deployment without provisioning is a 503": the
    supported absence is now an organization with nothing saved. It answers
    409 with a sentence the reader can act on -- never 500, and never the
    identity vendor's name (`tests/test_no_vendor_names_backend.py`)."""
    _org, _sub, headers = await _admin(email_env, "email-unconfigured")

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 409
    assert "KEYCLOAK" not in response.text.upper()
    assert "nothing to test" in response.json()["detail"]
    assert email_env.smtp.instances == []


async def test_test_sends_are_limited_per_organization(email_env: Any) -> None:
    """Decisión del plan 7: each press is a signed-in SMTP conversation."""
    org, sub, headers = await _admin(email_env, "email-test-limit")
    await _seed(email_env, org, sub)

    statuses = [
        (await email_env.client.post("/api/v1/settings/email/test", headers=headers)).status_code
        for _ in range(TEST_SENDS_PER_MINUTE + 1)
    ]

    assert statuses == [200] * TEST_SENDS_PER_MINUTE + [429]
    assert len(email_env.smtp.instances) == TEST_SENDS_PER_MINUTE


async def test_with_redis_down_no_test_message_is_sent(
    email_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review A-M1: the per-organization cap FAILS CLOSED
    (`rate_limit.hit(..., fail_closed=True)`). Nothing else caps a signed-in
    SMTP conversation per organization, so while the counter cannot count,
    the press is refused and no message goes. Let it fail open and this is a
    200 with a message sent: red."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    org, sub, headers = await _admin(email_env, "email-test-redis-down")
    await _seed(email_env, org, sub)

    class _Down:
        async def incr(self, key: str) -> int:
            raise RedisConnectionError("Connection refused.")

        async def ttl(self, key: str) -> int:
            raise RedisConnectionError("Connection refused.")

        async def expire(self, key: str, seconds: int) -> bool:
            raise RedisConnectionError("Connection refused.")

    monkeypatch.setattr(email_env.app.state, "redis", _Down())

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 429, response.text
    assert email_env.smtp.instances == []


async def test_the_settings_suggest_a_tagged_form_of_the_callers_address(email_env: Any) -> None:
    _org, sub, headers = await _admin(email_env, "email-test-suggest")

    response = await email_env.client.get("/api/v1/settings/email", headers=headers)

    assert response.json()["suggested_test_recipient"] == f"{sub}+itms-test@test.local"


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("helgar@toeshee.com", "helgar+itms-test@toeshee.com"),
        ("helgar+old@Toeshee.com", "helgar+itms-test@toeshee.com"),
        ("", ""),
        ("not-an-address", ""),
        ("a@b@c.com", ""),
    ],
)
def test_the_suggestion_replaces_a_tag_rather_than_stacking_one(email: str, expected: str) -> None:
    from app.api.v1.realm_settings import suggested_test_recipient

    assert suggested_test_recipient(email) == expected


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #


async def test_a_body_carrying_an_unknown_field_is_refused(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-extra")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, "smtpServer": {"host": "evil"}},
    )

    assert response.status_code == 422
    assert (await _stored(email_env, org))[0] is None


async def test_an_impossible_port_is_refused(email_env: Any) -> None:
    _org, _sub, headers = await _admin(email_env, "email-port")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "port": 70000}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field", ["host", "from_address", "from_display_name", "reply_to", "username"]
)
async def test_a_header_field_with_a_line_break_is_refused(email_env: Any, field: str) -> None:
    """Decisión del plan 9: a CR or LF would reach a mail header."""
    org, _sub, headers = await _admin(email_env, f"email-crlf-{field[:6]}")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, field: "itms@toeshee.com\r\nBcc: everyone@elsewhere.example"},
    )

    assert response.status_code == 422
    assert TYPED not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_the_tls_mode_is_a_closed_set(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-tls")

    refused = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "encryption": "both"}
    )
    assert refused.status_code == 422

    saved = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "encryption": "starttls"}
    )
    assert saved.json()["encryption"] == "starttls"
    row, _secret = await _stored(email_env, org)
    assert row is not None and row.encryption == "starttls"


# --------------------------------------------------------------------------- #
# sign-in mode: password or Microsoft (token)
# docs/superpowers/specs/2026-09-11-mail-oauth-design.md §3.2-3.3, §5.1
# --------------------------------------------------------------------------- #


async def test_password_mode_stores_what_a_password_relay_needs(email_env: Any) -> None:
    """The compatibility guarantee, as a whole-row comparison."""
    org, _sub, headers = await _admin(email_env, "email-mode-password")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=VALID_BODY
    )

    assert response.status_code == 200
    row, secret = await _stored(email_env, org)
    assert row is not None
    assert {
        "host": row.host,
        "port": row.port,
        "from_address": row.from_address,
        "from_display_name": row.from_display_name,
        "reply_to": row.reply_to,
        "encryption": row.encryption,
        "username": row.username,
        "auth_type": row.auth_type,
        "token_url": row.token_url,
        "token_scope": row.token_scope,
        "token_client_id": row.token_client_id,
    } == {
        "host": "smtp.new.internal",
        "port": 465,
        "from_address": "itms@toeshee.com",
        "from_display_name": "ITMS",
        "reply_to": "support@toeshee.com",
        "encryption": "ssl",
        "username": "itms",
        "auth_type": "password",
        "token_url": "",
        "token_scope": "",
        "token_client_id": "",
    }
    assert secret == {"password": TYPED}


async def test_token_mode_stores_the_application_and_drops_the_password(email_env: Any) -> None:
    org, sub, headers = await _admin(email_env, "email-mode-token")
    await _seed(email_env, org, sub)

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=TOKEN_BODY
    )

    assert response.status_code == 200
    row, secret = await _stored(email_env, org)
    assert row is not None and row.auth_type == "token"
    assert row.token_url == TOKEN_BODY["token_url"]
    assert row.token_scope == TOKEN_BODY["token_scope"]
    assert row.token_client_id == "client-abc"
    # One credential per relay, and it is the client secret: the password is gone.
    assert secret == {"client_secret": CLIENT_SECRET}
    body = response.json()
    assert body["auth_type"] == "token"
    assert body["microsoft_client_id"] == "client-abc"
    assert body["microsoft_sign_in_url"] == TOKEN_BODY["token_url"]
    assert body["microsoft_scope"] == TOKEN_BODY["token_scope"]


async def test_switching_back_to_password_clears_the_token_keys(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-mode-back")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY)
    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)

    row, secret = await _stored(email_env, org)
    assert row is not None and row.auth_type == "password"
    assert (row.token_url, row.token_scope, row.token_client_id) == ("", "", "")
    assert secret == {"password": TYPED}


async def test_an_omitted_client_secret_keeps_the_stored_one(email_env: Any) -> None:
    """Correcting the scope must not clear a working client secret."""
    org, _sub, headers = await _admin(email_env, "email-mode-keep-secret")
    await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY)
    body = {k: v for k, v in TOKEN_BODY.items() if k != "token_client_secret"}

    response = await email_env.client.put("/api/v1/settings/email", headers=headers, json=body)

    assert response.status_code == 200
    _row, secret = await _stored(email_env, org)
    assert secret == {"client_secret": CLIENT_SECRET}


@pytest.mark.parametrize(
    ("change", "phrase"),
    [
        ({"token_client_id": ""}, "client id"),
        ({"token_url": ""}, "token url"),
        ({"username": ""}, "username"),
    ],
)
async def test_an_incomplete_microsoft_sign_in_is_refused(
    email_env: Any, change: dict[str, str], phrase: str
) -> None:
    org, _sub, headers = await _admin(email_env, f"email-mode-incomplete-{phrase[:5]}")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**TOKEN_BODY, **change}
    )

    assert response.status_code == 422
    assert phrase in response.json()["detail"].lower()
    assert CLIENT_SECRET not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_a_token_url_that_is_not_microsofts_is_refused(email_env: Any) -> None:
    """Decisión del plan 4: the platform now presents the client secret itself,
    so the URL it presents it to is Microsoft's or nothing."""
    org, _sub, headers = await _admin(email_env, "email-mode-evil-url")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**TOKEN_BODY, "token_url": "https://evil.example/oauth2/v2.0/token"},
    )

    assert response.status_code == 422
    assert "login.microsoftonline.com" in response.json()["detail"]
    assert CLIENT_SECRET not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_a_client_secret_without_a_client_id_is_refused_in_password_mode(
    email_env: Any,
) -> None:
    org, _sub, headers = await _admin(email_env, "email-mode-stray-secret")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, "token_client_secret": CLIENT_SECRET},
    )

    assert response.status_code == 422
    assert CLIENT_SECRET not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_an_over_long_client_secret_is_refused_without_repeating_it(email_env: Any) -> None:
    _org, _sub, headers = await _admin(email_env, "email-mode-long-secret")
    over_long = "y" * (MAX_CLIENT_SECRET + 1)

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**TOKEN_BODY, "token_client_secret": over_long},
    )

    assert response.status_code == 422
    assert over_long not in response.text


async def test_no_route_ever_returns_the_client_secret(email_env: Any) -> None:
    _org, _sub, headers = await _admin(email_env, "email-mode-echo")

    responses = [
        await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY),
        await email_env.client.get("/api/v1/settings/email", headers=headers),
    ]

    for response in responses:
        assert response.status_code == 200
        assert CLIENT_SECRET not in response.text


async def test_the_mode_change_is_audited_with_field_names_only(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-mode-audit")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY)

    events = await _audit(email_env.admin_engine, org)
    (written,) = [e for e in events if e.action == "email.settings_updated"]
    assert "token_client_secret" in written.meta["fields"]
    assert "auth_type" in written.meta["fields"]
    assert CLIENT_SECRET not in json.dumps(written.meta)


async def test_a_relay_without_a_sign_in_reads_as_password_mode(email_env: Any) -> None:
    """RE-POINTED from "an existing password relay reads as password mode":
    there are no pre-existing realms any more; what remains is that a relay
    with no sign-in is a password relay, not an unknown one."""
    _org, _sub, headers = await _admin(email_env, "email-mode-legacy")
    body = {k: v for k, v in VALID_BODY.items() if k != "password"}
    await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**body, "username": ""}
    )

    read = (await email_env.client.get("/api/v1/settings/email", headers=headers)).json()

    assert read["auth_type"] == "password"
    assert read["microsoft_client_id"] == ""
    assert read["microsoft_sign_in_url"] == ""


# --------------------------------------------------------------------------- #
# review fixes: one transaction, failure reasons, pinned Microsoft relay,
# the generic credential routes
# --------------------------------------------------------------------------- #


class _VaultWhoseStoreFails:
    """The real vault, except that writing a secret raises -- validation has
    passed by then, so the failure is inside the save's own transaction."""

    def __init__(self, real: Any) -> None:
        self._real = real

    async def store(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vault store failed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_a_save_and_its_record_are_one_transaction(email_env: Any) -> None:
    """`test_a_refused_write_records_nothing` refuses before a session opens, so
    it cannot pin "the save and its record are one transaction". This one passes
    validation and fails INSIDE the session, after the row was added: neither the
    audit row, the settings row nor the credential may survive. It goes red if
    `audit_service.record` moves into its own committed session, or before
    `save_settings`."""
    org, _sub, headers = await _admin(email_env, "email-one-transaction")
    real = email_env.app.state.vault
    email_env.app.state.vault = _VaultWhoseStoreFails(real)
    try:
        with pytest.raises(RuntimeError, match="vault store failed"):
            await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)
    finally:
        email_env.app.state.vault = real

    events = await _audit(email_env.admin_engine, org)
    assert [e for e in events if e.action == "email.settings_updated"] == []
    assert (await _stored(email_env, org))[0] is None
    assert await _credentials(email_env, org) == []


async def test_an_unverifiable_relay_certificate_has_its_own_reason(email_env: Any) -> None:
    org, sub, headers = await _admin(email_env, "email-cert")
    await _seed(email_env, org, sub)
    email_env.smtp.raise_on_send = ssl.SSLCertVerificationError(1, "self-signed certificate")

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"] == "The mail relay's certificate could not be verified."
    assert "self-signed" not in response.text


async def test_a_non_ascii_password_is_refused_without_repeating_it(email_env: Any) -> None:
    """smtplib encodes AUTH as ASCII: saved, it would fail every send."""
    org, _sub, headers = await _admin(email_env, "email-non-ascii")
    secret = "contraseña-ñ-secreta"

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "password": secret}
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert (await _stored(email_env, org))[0] is None


async def test_a_username_with_no_saved_password_does_not_sign_in_empty(email_env: Any) -> None:
    org, sub, headers = await _admin(email_env, "email-no-secret")
    await _seed(email_env, org, sub, password=None)

    response = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "The saved sign-in is missing or unreadable; enter it again."
    )
    assert email_env.smtp.instances == []


async def test_microsoft_mode_stores_and_reports_the_relay_it_really_uses(email_env: Any) -> None:
    """Sending is pinned to smtp.office365.com:587 STARTTLS in this mode, so the
    row and the answer must say that, not what was typed."""
    org, _sub, headers = await _admin(email_env, "email-ms-pinned")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**TOKEN_BODY, "host": "smtp.elsewhere.example", "port": 25, "encryption": "none"},
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["host"], body["port"], body["encryption"]) == (
        "smtp.office365.com",
        "587",
        "starttls",
    )
    row, _secret = await _stored(email_env, org)
    assert row is not None
    assert (row.host, row.port, row.encryption) == ("smtp.office365.com", 587, "starttls")


async def test_the_save_record_names_the_credential_later_uses_point_at(email_env: Any) -> None:
    org, _sub, headers = await _admin(email_env, "email-credential-id")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)

    (credential,) = await _credentials(email_env, org)
    (written,) = [
        e for e in await _audit(email_env.admin_engine, org) if e.action == "email.settings_updated"
    ]
    assert written.meta["credential_id"] == str(credential.id)
    assert TYPED not in json.dumps(written.meta)


async def test_the_relay_credential_cannot_be_rotated_or_deleted_generically(
    email_env: Any,
) -> None:
    """Settings -> Email is the only way to change it: a generic rotate would
    leave the row pointing at a secret of the wrong shape, a delete at nothing."""
    org, sub, headers = await _admin(email_env, "email-generic-routes")
    await _seed(email_env, org, sub)
    (credential,) = await _credentials(email_env, org)

    rotated = await email_env.client.put(
        f"/api/v1/credentials/{credential.id}",
        headers=headers,
        json={"name": "renamed", "secret": "not-json-at-all"},
    )
    deleted = await email_env.client.delete(
        f"/api/v1/credentials/{credential.id}", headers=headers
    )

    assert rotated.status_code == 409
    assert deleted.status_code == 409
    (still,) = await _credentials(email_env, org)
    assert still.name == credential.name
    assert (await _stored(email_env, org))[1] == {"password": STORED}

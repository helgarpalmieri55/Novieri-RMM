"""Account email: `app/core/mail.py` (design §7, "Correo de cuentas").

The platform sends its own invitations and resets now, so it holds the relay's
sign-in -- in the vault, opened at the moment of sending, every opening on the
audit chain. `smtplib.SMTP` and `SMTP_SSL` are replaced by recording stubs;
nothing opens a socket. The Microsoft token endpoint is replaced at its one
seam, `microsoft.http_client`, as `tests/test_notification_microsoft_smtp.py`
does.
"""

import json
import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.i18n import use_locale
from app.core.mail import (
    ACCOUNT_MAIL_CREDENTIAL_TYPE,
    MailFailure,
    MailNotConfigured,
    MailRecipientRefused,
    MailSendFailed,
    MailSettingsInput,
    failure_message,
    mail_configured,
    microsoft_tenant,
    pick_test_recipient,
    read_settings,
    save_settings,
    send_account_mail,
)
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Credential, Organization, User
from app.modules.email_intake import microsoft

_REAL_CLIENT = httpx.AsyncClient

RELAY_PASSWORD = "relay-pass-SENTINEL"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "SMTP-CLIENT-SECRET-SENTINEL"
ACCESS = "SMTP-ACCESS-TOKEN-SENTINEL"
TENANT = "contoso.onmicrosoft.com"
SUBJECT = "You have been invited"
TEXT = "Open the link to choose a password."

PASSWORD_RELAY = MailSettingsInput(
    host="SMTP.Internal",
    port=587,
    from_address="itms@toeshee.com",
    from_display_name="ITMS",
    reply_to="support@toeshee.com",
    encryption="starttls",
    username="itms",
    auth_type="password",
    token_url="",
    token_scope="",
    token_client_id="",
)

MICROSOFT_RELAY = MailSettingsInput(
    host="smtp.elsewhere.example",
    port=25,
    from_address="itms@toeshee.com",
    from_display_name="",
    reply_to="",
    encryption="none",
    username="itms@toeshee.com",
    auth_type="token",
    token_url=f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
    token_scope="",
    token_client_id=CLIENT_ID,
)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class _RecordingSMTP:
    instances: list["_RecordingSMTP"] = []
    raise_on_init: BaseException | None = None
    raise_on_send: BaseException | None = None
    refuse_auth = False
    implicit_tls = False

    def __init__(
        self, host: str, port: int, timeout: float | None = None, context: Any = None
    ) -> None:
        if _RecordingSMTP.raise_on_init is not None:
            raise _RecordingSMTP.raise_on_init
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.auth_mechanism: str | None = None
        self.auth_initial: str | None = None
        self.sent: EmailMessage | None = None
        _RecordingSMTP.instances.append(self)

    def __enter__(self) -> "_RecordingSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ehlo_or_helo_if_needed(self) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.starttls_called = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)
        if _RecordingSMTP.refuse_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed")

    def auth(
        self, mechanism: str, authobject: Callable[..., str], *, initial_response_ok: bool = True
    ) -> None:
        self.auth_mechanism = mechanism
        self.auth_initial = authobject()
        if _RecordingSMTP.refuse_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.3 Authentication unsuccessful")

    def send_message(self, message: EmailMessage) -> None:
        if _RecordingSMTP.raise_on_send is not None:
            raise _RecordingSMTP.raise_on_send
        self.sent = message


class _RecordingSMTPSSL(_RecordingSMTP):
    implicit_tls = True


@pytest.fixture()
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    _RecordingSMTP.instances = []
    _RecordingSMTP.raise_on_init = None
    _RecordingSMTP.raise_on_send = None
    _RecordingSMTP.refuse_auth = False
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", _RecordingSMTPSSL)
    return _RecordingSMTP


class _TokenEndpoint:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def forms(self) -> list[dict[str, str]]:
        return [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in self.requests]

    def factory(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"access_token": ACCESS, "token_type": "Bearer"})

        return _REAL_CLIENT(transport=httpx.MockTransport(handle), timeout=5.0)


@pytest.fixture()
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> _TokenEndpoint:
    endpoint = _TokenEndpoint()
    monkeypatch.setattr(microsoft, "http_client", endpoint.factory)
    return endpoint


# --------------------------------------------------------------------------- #
# world
# --------------------------------------------------------------------------- #


@dataclass
class _World:
    org_id: UUID
    actor_id: UUID
    vault: LocalVaultService
    app_engine: AsyncEngine
    admin_engine: AsyncEngine


@pytest.fixture()
async def world(admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path) -> _World:
    slug = f"mail-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        actor = User(
            org_id=org.id, external_subject=f"sub-{uuid4().hex[:10]}", email="helgar@toeshee.com"
        )
        session.add(actor)
        await session.commit()
        vault = LocalVaultService(tmp_path / "root.key")
        return _World(org.id, actor.id, vault, app_engine, admin_engine)


async def _save(
    w: _World,
    values: MailSettingsInput = PASSWORD_RELAY,
    *,
    password: str | None = RELAY_PASSWORD,
    client_secret: str | None = None,
) -> None:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        await save_settings(
            session,
            w.vault,
            org_id=w.org_id,
            actor_id=w.actor_id,
            values=values,
            password=password,
            client_secret=client_secret,
        )


async def _send(w: _World, *, html: str | None = None) -> None:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        await send_account_mail(
            session,
            org_id=w.org_id,
            to="new.person@toeshee.com",
            subject=SUBJECT,
            text=TEXT,
            html=html,
            vault=w.vault,
            actor_id=w.actor_id,
        )


async def _send_refused(w: _World) -> MailSendFailed:
    """The caller's pattern (`app/core/mail.py` docstring): the failure is caught
    INSIDE the session, which then commits -- so the checkout's audit row stays."""
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(MailSendFailed) as raised:
            await send_account_mail(
                session,
                org_id=w.org_id,
                to="new.person@toeshee.com",
                subject=SUBJECT,
                text=TEXT,
                vault=w.vault,
                actor_id=w.actor_id,
            )
    return raised.value


async def _audit(w: _World) -> list[AuditEvent]:
    async with AsyncSession(w.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == w.org_id).order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _account_mail_credentials(w: _World) -> list[Credential]:
    async with AsyncSession(w.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(Credential).where(
                        Credential.org_id == w.org_id,
                        Credential.type == ACCOUNT_MAIL_CREDENTIAL_TYPE,
                    )
                )
            ).scalars()
        )


async def _secret(w: _World) -> dict[str, str] | None:
    async with org_session(w.org_id, engine=w.app_engine, clients=ClientScope.all()) as session:
        row = await read_settings(session, w.org_id)
        if row is None or row.credential_id is None:
            return None
        decoded: dict[str, str] = json.loads(
            await w.vault.checkout(session, org_id=w.org_id, credential_id=row.credential_id)
        )
        return decoded


def _plain(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    return str(body.get_content()).strip()


# --------------------------------------------------------------------------- #
# configured or not
# --------------------------------------------------------------------------- #


async def test_nothing_saved_is_not_configured(world: _World, smtp: type[_RecordingSMTP]) -> None:
    """A state, not a fault: the invitation flow (Task 8) shows the link once
    instead of mailing it."""
    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        assert not await mail_configured(session, world.org_id)
        with pytest.raises(MailNotConfigured):
            await send_account_mail(
                session,
                org_id=world.org_id,
                to="x@toeshee.com",
                subject=SUBJECT,
                text=TEXT,
                vault=world.vault,
            )
    assert smtp.instances == []


# --------------------------------------------------------------------------- #
# the password relay
# --------------------------------------------------------------------------- #


async def test_a_password_relay_sends_with_the_vaulted_password(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    await _save(world)

    await _send(world)

    (client,) = smtp.instances
    assert (client.host, client.port, client.implicit_tls) == ("smtp.internal", 587, False)
    assert client.starttls_called
    assert client.login_args == ("itms", RELAY_PASSWORD)
    assert client.sent is not None
    assert client.sent["From"] == "ITMS <itms@toeshee.com>"
    assert client.sent["To"] == "new.person@toeshee.com"
    assert client.sent["Reply-To"] == "support@toeshee.com"
    assert client.sent["Subject"] == SUBJECT
    assert _plain(client.sent) == TEXT


async def test_the_html_part_travels_beside_the_text(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    await _save(world)

    await _send(world, html="<p>Open the link</p>")

    sent = smtp.instances[0].sent
    assert sent is not None
    assert sent.get_content_type() == "multipart/alternative"
    html = sent.get_body(preferencelist=("html",))
    assert html is not None and "<p>Open the link</p>" in str(html.get_content())
    assert _plain(sent) == TEXT


async def test_ssl_means_implicit_tls_and_no_starttls(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    """`implicit_tls` in `send_smtp_sync`: the shared client used to speak only
    STARTTLS, which the account-mail form's `ssl` choice cannot use."""
    await _save(world, replace(PASSWORD_RELAY, port=465, encryption="ssl"))

    await _send(world)

    (client,) = smtp.instances
    assert client.implicit_tls and client.port == 465
    assert not client.starttls_called


async def test_a_relay_without_a_sign_in_needs_no_credential(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    await _save(world, replace(PASSWORD_RELAY, username="", encryption="none"), password=None)

    await _send(world)

    (client,) = smtp.instances
    assert client.login_args is None and not client.starttls_called
    assert await _account_mail_credentials(world) == []
    assert [e.action for e in await _audit(world) if e.action == "email.credential_used"] == []


# --------------------------------------------------------------------------- #
# the secret: in the vault, and nowhere else (invariants 3 and 4)
# --------------------------------------------------------------------------- #


async def test_the_password_is_in_the_vault_and_nowhere_else(world: _World) -> None:
    await _save(world)

    (credential,) = await _account_mail_credentials(world)
    assert credential.client_id is None  # every client's: one relay per organization
    assert credential.ciphertext is not None and RELAY_PASSWORD not in credential.ciphertext
    assert await _secret(world) == {"password": RELAY_PASSWORD}
    async with AsyncSession(world.admin_engine) as session:
        row = await read_settings(session, world.org_id)
        assert row is not None
        stored = [str(getattr(row, column.key)) for column in row.__table__.columns]
    assert all(RELAY_PASSWORD not in value for value in stored)


async def test_every_checkout_is_audited_without_the_secret(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    await _save(world)

    await _send(world)

    (used,) = [e for e in await _audit(world) if e.action == "email.credential_used"]
    (credential,) = await _account_mail_credentials(world)
    assert used.target_type == "credential" and used.target_id == str(credential.id)
    assert used.actor_type == "user" and used.actor_id == world.actor_id
    assert used.meta["sign_in"] == "password"
    assert RELAY_PASSWORD not in json.dumps(used.meta)


async def test_a_failed_send_still_leaves_the_checkout_on_record(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    """Decisión del plan 3: the audit row is written in the caller's session
    BEFORE the send, so a caller that catches the failure inside the session
    keeps it. A checkout that left no record would break invariant 4."""
    await _save(world)
    smtp.raise_on_send = smtplib.SMTPResponseException(554, b"5.7.1 rejected")

    refused = await _send_refused(world)

    assert refused.reason is MailFailure.RELAY_REFUSED
    assert [e.action for e in await _audit(world) if e.action == "email.credential_used"] == [
        "email.credential_used"
    ]


async def test_switching_modes_drops_the_other_modes_secret(world: _World) -> None:
    await _save(world)
    await _save(world, MICROSOFT_RELAY, password=None, client_secret=None)

    assert await _secret(world) is None
    assert await _account_mail_credentials(world) == []

    await _save(world, MICROSOFT_RELAY, password=None, client_secret=CLIENT_SECRET)
    assert await _secret(world) == {"client_secret": CLIENT_SECRET}

    await _save(world, password=None)
    assert await _secret(world) is None
    assert await _account_mail_credentials(world) == []


# --------------------------------------------------------------------------- #
# failures: classified, never provider text, never the secret
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stub", "reason"),
    [
        ("auth", MailFailure.SIGN_IN_REFUSED),
        ("send", MailFailure.RELAY_REFUSED),
        ("recipients", MailFailure.RECIPIENT_REFUSED),
        ("disconnect", MailFailure.UNREACHABLE),
        ("connect", MailFailure.UNREACHABLE),
    ],
)
async def test_a_failure_is_classified_without_provider_text(
    world: _World, smtp: type[_RecordingSMTP], stub: str, reason: MailFailure
) -> None:
    await _save(world)
    if stub == "auth":
        smtp.refuse_auth = True
    elif stub == "send":
        smtp.raise_on_send = smtplib.SMTPResponseException(550, b"mailbox unavailable SENTINEL")
    elif stub == "recipients":
        smtp.raise_on_send = smtplib.SMTPRecipientsRefused(
            {"x@toeshee.com": (550, b"no such user")}
        )
    elif stub == "disconnect":
        smtp.raise_on_send = smtplib.SMTPServerDisconnected("connection unexpectedly closed")
    else:
        smtp.raise_on_init = ConnectionRefusedError("connection refused SENTINEL")

    refused = await _send_refused(world)

    assert refused.reason is reason
    assert str(refused) == reason.value
    assert refused.__cause__ is None and refused.__suppress_context__
    assert RELAY_PASSWORD not in str(refused)


#: Where OpenBao lives in the HTTP errors below: it must reach no log line.
_VAULT_URL = "https://openbao.internal.SENTINEL:8200/v1/transit/decrypt/org-1"


def _vault_errors() -> list[httpx.HTTPError]:
    """What `OpenBaoVaultService._unwrap_dek` raises when OpenBao does not
    open the data key: a transport error (refused, timed out) or its
    `raise_for_status` on a sealed (503) or refusing (403) vault."""
    request = httpx.Request("POST", _VAULT_URL)
    return [
        httpx.ConnectError(f"connection refused to {_VAULT_URL}", request=request),
        httpx.ReadTimeout(f"timed out reading {_VAULT_URL}", request=request),
        httpx.HTTPStatusError(
            f"Server error '503 Service Unavailable' for url '{_VAULT_URL}'",
            request=request,
            response=httpx.Response(503, request=request),
        ),
        httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url '{_VAULT_URL}'",
            request=request,
            response=httpx.Response(403, request=request),
        ),
    ]


@pytest.mark.parametrize(
    "error", _vault_errors(), ids=["connect", "timeout", "sealed", "forbidden"]
)
async def test_a_vault_that_does_not_open_the_sign_in_is_sign_in_unavailable(
    world: _World,
    smtp: type[_RecordingSMTP],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: httpx.HTTPError,
) -> None:
    """Fix 3 review, Minor 2: the vault's backend not answering is a send that
    failed (`MailSendFailed(SIGN_IN_UNAVAILABLE)`), not an exception out of
    the caller's transaction -- so `notices.deliver` never raises for
    delivery, and a later notice can never roll back an earlier one. Nothing
    was checked out, so nothing is recorded and nothing is sent. One static
    WARNING says so, with neither the exception's text (it names OpenBao's
    address and the key) nor a traceback. Let the httpx error through and
    this raises it: red."""
    await _save(world)

    async def unavailable(*_args: Any, **_kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(world.vault, "checkout", unavailable)
    with caplog.at_level(logging.WARNING, logger="app.core.mail"):
        refused = await _send_refused(world)

    assert refused.reason is MailFailure.SIGN_IN_UNAVAILABLE
    assert refused.__cause__ is None and refused.__suppress_context__
    assert smtp.instances == []
    assert [e for e in await _audit(world) if e.action == "email.credential_used"] == []
    (record,) = [r for r in caplog.records if r.name == "app.core.mail"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "account mail not sent: the vault did not open the relay sign-in"
    assert record.exc_info is None
    assert "SENTINEL" not in caplog.text and "openbao" not in caplog.text


async def test_an_error_that_is_not_the_vaults_is_not_swallowed(
    world: _World, smtp: type[_RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the vault's transport and HTTP errors are a failed send: anything
    else -- a programming error -- still raises, as it did."""
    await _save(world)

    async def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("a defect, not the vault")

    monkeypatch.setattr(world.vault, "checkout", broken)
    with pytest.raises(RuntimeError):
        await _send(world)
    assert smtp.instances == []


async def test_a_header_the_relay_cannot_carry_is_unsendable(
    world: _World, smtp: type[_RecordingSMTP]
) -> None:
    await _save(world)
    async with org_session(
        world.org_id, engine=world.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(MailSendFailed) as raised:
            await send_account_mail(
                session,
                org_id=world.org_id,
                to="x@toeshee.com\r\nBcc: everyone@elsewhere.example",
                subject=SUBJECT,
                text=TEXT,
                vault=world.vault,
            )
    assert raised.value.reason is MailFailure.UNSENDABLE
    assert all(client.sent is None for client in smtp.instances)


@pytest.mark.parametrize("reason", list(MailFailure))
def test_every_failure_has_words_in_both_languages(reason: MailFailure) -> None:
    english = failure_message(reason)
    with use_locale("es"):
        spanish = failure_message(reason)
    assert english.endswith(".") and spanish.endswith(".")
    assert english != spanish


# --------------------------------------------------------------------------- #
# Microsoft 365: pinned token endpoint, pinned SMTP host, XOAUTH2
# --------------------------------------------------------------------------- #


async def test_microsoft_mode_pins_the_host_and_signs_in_with_xoauth2(
    world: _World, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    await _save(world, MICROSOFT_RELAY, password=None, client_secret=CLIENT_SECRET)

    await _send(world)

    (request,) = token_endpoint.requests
    assert str(request.url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert token_endpoint.forms() == [
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "https://outlook.office365.com/.default",
        }
    ]
    (client,) = smtp.instances
    # Whatever the settings said (`smtp.elsewhere.example:25`, no encryption).
    assert (client.host, client.port) == ("smtp.office365.com", 587)
    assert client.starttls_called and client.login_args is None
    assert client.auth_mechanism == "XOAUTH2"
    assert client.auth_initial == f"user=itms@toeshee.com\x01auth=Bearer {ACCESS}\x01\x01"
    (used,) = [e for e in await _audit(world) if e.action == "email.credential_used"]
    assert used.meta["sign_in"] == "token"


async def test_a_token_url_that_is_not_microsofts_is_never_called(
    world: _World, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    """Decisión del plan 4, at the sender: a row that somehow holds another
    token URL is refused BEFORE the vault is opened -- the secret never moves."""
    await _save(
        world,
        replace(MICROSOFT_RELAY, token_url="https://evil.example/oauth2/v2.0/token"),
        password=None,
        client_secret=CLIENT_SECRET,
    )

    refused = await _send_refused(world)

    assert refused.reason is MailFailure.SIGN_IN_MISSING
    assert token_endpoint.requests == []
    assert smtp.instances == []
    assert [e for e in await _audit(world) if e.action == "email.credential_used"] == []


@pytest.mark.parametrize(
    ("token_url", "tenant"),
    [
        (f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token", TENANT),
        (
            "https://login.microsoftonline.com/0c8a3b54-1111-2222-3333-444455556666"
            "/oauth2/v2.0/token",
            "0c8a3b54-1111-2222-3333-444455556666",
        ),
        ("https://evil.example/oauth2/v2.0/token", None),
        ("http://login.microsoftonline.com/contoso/oauth2/v2.0/token", None),
        ("https://login.microsoftonline.com.evil.example/contoso/oauth2/v2.0/token", None),
        ("https://login.microsoftonline.com/a/b/oauth2/v2.0/token", None),
        ("https://login.microsoftonline.com/x%2F/oauth2/v2.0/token", None),
        ("https://login.microsoftonline.com/contoso/oauth2/v2.0/token?x=1", None),
        ("", None),
    ],
)
def test_only_microsofts_own_token_endpoint_is_accepted(token_url: str, tenant: str | None) -> None:
    assert microsoft_tenant(token_url) == tenant


# --------------------------------------------------------------------------- #
# where a test message may go
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "domain", "expected"),
    [
        (None, None, "itms@toeshee.com"),
        ("   ", None, "itms@toeshee.com"),
        ("itms@toeshee.com", None, "itms@toeshee.com"),
        ("helgar+itms-test@toeshee.com", None, "helgar+itms-test@toeshee.com"),
        ("colleague@toeshee.com", "toeshee.com", "colleague@toeshee.com"),
    ],
)
def test_a_test_goes_to_a_mailbox_this_platform_can_vouch_for(
    requested: str | None, domain: str | None, expected: str
) -> None:
    picked = pick_test_recipient(
        requested,
        sender="itms@toeshee.com",
        caller_email="helgar@toeshee.com",
        org_domain=domain,
    )
    assert picked == expected


@pytest.mark.parametrize(
    "requested",
    ["colleague@toeshee.com", "victim@elsewhere.com", "a@b@c.com", "two@x.com, three@y.com"],
)
def test_anything_else_is_refused(requested: str) -> None:
    with pytest.raises(MailRecipientRefused):
        pick_test_recipient(
            requested, sender="itms@toeshee.com", caller_email="helgar@toeshee.com", org_domain=None
        )

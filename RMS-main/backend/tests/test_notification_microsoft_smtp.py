"""Notification email over Microsoft 365: client-credentials XOAUTH2.

The token exchange is the email-intake one (`email_intake.tokens`), mocked at
its one seam (`microsoft.http_client`). The SMTP side is tested twice: the
XOAUTH2 conversation against a real socket (what goes on the wire, including
Microsoft's failure continuation), and the delivery path against a recording
`smtplib.SMTP` stand-in (where it dials, and with what).
"""

import base64
import json
import smtplib
import socket
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.config import override_settings
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Credential, DomainEvent, NotificationDelivery, User
from app.modules.email_intake import microsoft, tokens
from app.modules.email_intake.tokens import TokenRevoked, TokenTransportError
from app.modules.notification import delivery as delivery_module
from app.modules.notification.channels import create_channel
from app.modules.notification.delivery import attempt_delivery
from app.modules.notification.schema import ChannelCreate, MicrosoftSignIn

API = "/api/v1/notifications"
TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "SMTP-CLIENT-SECRET-SENTINEL"
ACCESS = "SMTP-ACCESS-TOKEN-SENTINEL"
SENDER = "alerts@toeshee.com"
_REAL_CLIENT = httpx.AsyncClient

MICROSOFT_CONFIG = {
    "auth": "microsoft",
    "host": "smtp.office365.com",
    "port": 587,
    "from_address": SENDER,
    "use_tls": True,
}


# --------------------------------------------------------------------------- #
# The token endpoint
# --------------------------------------------------------------------------- #


class _TokenEndpoint:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict[str, Any] = {"access_token": ACCESS, "token_type": "Bearer"}
        self.unreachable = False

    def forms(self) -> list[dict[str, str]]:
        from urllib.parse import parse_qs

        return [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in self.requests]

    def factory(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.unreachable:
                raise httpx.ConnectError("no route to host", request=request)
            return httpx.Response(self.status, json=self.body)

        return _REAL_CLIENT(transport=httpx.MockTransport(handle), timeout=5.0)


@pytest.fixture()
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> _TokenEndpoint:
    endpoint = _TokenEndpoint()
    monkeypatch.setattr(microsoft, "http_client", endpoint.factory)
    return endpoint


async def test_the_app_token_is_a_client_credentials_grant_to_microsoft(
    token_endpoint: _TokenEndpoint,
) -> None:
    token = await tokens.app_access_token(
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scope=microsoft.SMTP_APP_SCOPE,
    )

    assert token == ACCESS
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


@pytest.mark.parametrize("code", ["invalid_client", "unauthorized_client", "invalid_scope"])
async def test_a_refused_application_is_permanent(
    token_endpoint: _TokenEndpoint, code: str
) -> None:
    token_endpoint.status = 401
    token_endpoint.body = {"error": code, "error_description": CLIENT_SECRET}

    with pytest.raises(TokenRevoked) as raised:
        await tokens.app_access_token(
            tenant_id=TENANT,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scope=microsoft.SMTP_APP_SCOPE,
        )
    assert raised.value.cause == "application"
    assert CLIENT_SECRET not in str(raised.value)


async def test_an_unreachable_token_endpoint_is_transient(token_endpoint: _TokenEndpoint) -> None:
    token_endpoint.unreachable = True
    with pytest.raises(TokenTransportError) as raised:
        await tokens.app_access_token(
            tenant_id=TENANT,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scope=microsoft.SMTP_APP_SCOPE,
        )
    assert raised.value.reason == "microsoft-unreachable"


async def test_a_server_error_is_transient(token_endpoint: _TokenEndpoint) -> None:
    token_endpoint.status = 503
    token_endpoint.body = {}
    with pytest.raises(TokenTransportError):
        await tokens.app_access_token(
            tenant_id=TENANT,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scope=microsoft.SMTP_APP_SCOPE,
        )


def test_the_smtp_endpoint_is_pinned_in_code() -> None:
    assert microsoft.SMTP_HOST == "smtp.office365.com"
    assert microsoft.SMTP_PORT == 587


# --------------------------------------------------------------------------- #
# XOAUTH2 on the wire
# --------------------------------------------------------------------------- #


class _FakeSmtpServer:
    """Plain-text SMTP on loopback: enough to watch an AUTH exchange."""

    def __init__(self, *, accept: bool) -> None:
        self.accept = accept
        self.lines: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        conn.settimeout(5)
        reader = conn.makefile("rb")

        def send(text: str) -> None:
            conn.sendall(text.encode() + b"\r\n")

        send("220 fake ESMTP")
        try:
            while True:
                raw = reader.readline()
                if not raw:
                    break
                line = raw.decode().rstrip("\r\n")
                self.lines.append(line)
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    send("250-fake")
                    send("250 AUTH XOAUTH2")
                elif verb == "AUTH":
                    if self.accept:
                        send("235 2.7.0 Authentication successful")
                    else:
                        challenge = base64.b64encode(
                            json.dumps({"status": "401", "schemes": "bearer"}).encode()
                        ).decode()
                        send(f"334 {challenge}")
                        continuation = reader.readline().decode().rstrip("\r\n")
                        self.lines.append(continuation)
                        send("535 5.7.3 Authentication unsuccessful")
                elif verb == "QUIT":
                    send("221 bye")
                    break
                else:
                    send("250 ok")
        finally:
            conn.close()
            self._sock.close()


def test_xoauth2_sends_the_sasl_string_and_nothing_else() -> None:
    server = _FakeSmtpServer(accept=True)
    with smtplib.SMTP("127.0.0.1", server.port, timeout=5) as client:
        delivery_module._authenticate_xoauth2(client, SENDER, ACCESS)

    auth = next(line for line in server.lines if line.startswith("AUTH "))
    mechanism, encoded = auth.split(" ")[1:]
    assert mechanism == "XOAUTH2"
    assert base64.b64decode(encoded) == f"user={SENDER}\x01auth=Bearer {ACCESS}\x01\x01".encode()


def test_a_refused_xoauth2_finishes_the_exchange_instead_of_hanging() -> None:
    """Microsoft answers a bad token with a 334 JSON challenge and waits for an
    empty line; without it the connection sits until the timeout."""
    server = _FakeSmtpServer(accept=False)
    started = time.monotonic()
    with pytest.raises(smtplib.SMTPAuthenticationError) as raised:
        with smtplib.SMTP("127.0.0.1", server.port, timeout=5) as client:
            delivery_module._authenticate_xoauth2(client, SENDER, ACCESS)

    assert time.monotonic() - started < 2
    assert raised.value.smtp_code == 535
    auth_at = next(i for i, line in enumerate(server.lines) if line.startswith("AUTH "))
    assert server.lines[auth_at + 1] == ""


# --------------------------------------------------------------------------- #
# The delivery path
# --------------------------------------------------------------------------- #


class _RecordingSMTP:
    instances: list["_RecordingSMTP"] = []
    refuse_auth = False

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.auth_mechanism: str | None = None
        self.auth_initial: str | None = None
        self.sent: Any = None
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

    def auth(self, mechanism: str, authobject: Any, *, initial_response_ok: bool = True) -> None:
        self.auth_mechanism = mechanism
        self.auth_initial = authobject()
        if _RecordingSMTP.refuse_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.3 Authentication unsuccessful")

    def send_message(self, message: Any) -> None:
        self.sent = message


@pytest.fixture()
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    _RecordingSMTP.instances = []
    _RecordingSMTP.refuse_auth = False
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    return _RecordingSMTP


@pytest.fixture(autouse=True)
def _settings(request: pytest.FixtureRequest):
    if "api_env" not in request.fixturenames:
        yield
        return
    api_env = request.getfixturevalue("api_env")
    with override_settings(api_env.settings):
        yield


async def _org_with_user(api_env: Any, slug: str) -> tuple[UUID, UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _microsoft_delivery(
    api_env: Any, tmp_path: Path, slug: str
) -> tuple[AsyncSession, NotificationDelivery, LocalVaultService, UUID]:
    org_id, user_id = await _org_with_user(api_env, slug)
    vault = LocalVaultService(tmp_path / "root.key")
    session = AsyncSession(api_env.admin_engine, expire_on_commit=False)
    channel = await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(kind="email", name="m365", config=MICROSOFT_CONFIG),
        secret=None,
        sign_in=MicrosoftSignIn(tenant_id=TENANT, client_id=CLIENT_ID, client_secret=CLIENT_SECRET),
        vault=vault,
    )
    now = delivery_module.datetime.now(delivery_module.UTC)
    event = DomainEvent(
        org_id=org_id,
        event_type="ticket.resolved",
        subject_type="platform_probe",
        subject_id=uuid7(),
        payload={"ticket_number": 7, "subject": "Printer"},
        occurred_at=now,
        created_at=now,
    )
    session.add(event)
    await session.flush()
    row = NotificationDelivery(
        org_id=org_id,
        event_id=event.id,
        channel_id=channel.id,
        recipient="someone@example.com",
        status="pending",
        attempts=0,
    )
    session.add(row)
    await session.flush()
    return session, row, vault, org_id


async def _actions(session: AsyncSession, org_id: UUID, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.org_id == org_id, AuditEvent.action == action)
            )
        ).scalars()
    )


async def test_a_microsoft_channel_sends_through_office365_with_xoauth2(
    api_env: Any,
    tmp_path: Path,
    smtp: type[_RecordingSMTP],
    token_endpoint: _TokenEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, row, vault, org_id = await _microsoft_delivery(api_env, tmp_path, "m365-send")
    async with session:
        with caplog.at_level("DEBUG"):
            await attempt_delivery(session, delivery=row, vault=vault)

        assert row.status == "sent", row.last_error
        (client,) = smtp.instances
        assert (client.host, client.port) == ("smtp.office365.com", 587)
        assert client.starttls_called is True
        assert client.login_args is None
        assert client.auth_mechanism == "XOAUTH2"
        assert client.auth_initial == f"user={SENDER}\x01auth=Bearer {ACCESS}\x01\x01"
        assert client.sent["From"] == SENDER

        (form,) = token_endpoint.forms()
        assert form["grant_type"] == "client_credentials"
        assert form["client_secret"] == CLIENT_SECRET

        (used,) = await _actions(session, org_id, "notification.credential_used")
        assert used.actor_type == "system"
        assert used.meta["sign_in"] == "microsoft"
        dump = json.dumps([e.meta for e in await _actions(session, org_id, used.action)])
        assert CLIENT_SECRET not in dump and ACCESS not in dump

    assert CLIENT_SECRET not in caplog.text and ACCESS not in caplog.text


async def test_the_configured_host_is_never_dialled_for_microsoft(
    api_env: Any, tmp_path: Path, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    """The token can send as the mailbox; it only ever goes to Microsoft."""
    session, row, vault, _org = await _microsoft_delivery(api_env, tmp_path, "m365-pin")
    async with session:
        channel = await session.get(delivery_module.NotificationChannel, row.channel_id)
        assert channel is not None
        channel.config = {**MICROSOFT_CONFIG, "host": "attacker.example", "port": 25}
        await session.flush()

        await attempt_delivery(session, delivery=row, vault=vault)

    (client,) = smtp.instances
    assert (client.host, client.port) == ("smtp.office365.com", 587)


async def test_a_refused_application_is_dead_not_retried(
    api_env: Any, tmp_path: Path, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    token_endpoint.status = 401
    token_endpoint.body = {"error": "invalid_client"}
    session, row, vault, _org = await _microsoft_delivery(api_env, tmp_path, "m365-refused")
    async with session:
        await attempt_delivery(session, delivery=row, vault=vault)

    assert row.status == "dead"
    assert row.last_error == "Microsoft 365 refused the sign-in"
    assert smtp.instances == []


async def test_an_unreachable_token_endpoint_is_retried(
    api_env: Any, tmp_path: Path, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    token_endpoint.unreachable = True
    session, row, vault, _org = await _microsoft_delivery(api_env, tmp_path, "m365-transient")
    async with session:
        await attempt_delivery(session, delivery=row, vault=vault)

    assert row.status == "failed"
    assert row.next_attempt_at is not None
    assert smtp.instances == []


async def test_a_refused_token_at_the_smtp_server_is_permanent(
    api_env: Any, tmp_path: Path, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    smtp.refuse_auth = True
    session, row, vault, _org = await _microsoft_delivery(api_env, tmp_path, "m365-535")
    async with session:
        await attempt_delivery(session, delivery=row, vault=vault)

    assert row.status == "dead"
    assert row.last_status_code == 535


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


async def _admin(api_env: Any, slug: str) -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    return org_id, await api_env.headers(org_id, sub)


def _microsoft_body(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "email",
        "name": "Microsoft 365",
        "config": MICROSOFT_CONFIG,
        "microsoft_tenant_id": TENANT,
        "microsoft_client_id": CLIENT_ID,
        "microsoft_client_secret": CLIENT_SECRET,
        **extra,
    }


async def _stored(api_env: Any, org_id: UUID, channel_id: str) -> dict[str, Any] | None:
    async with AsyncSession(api_env.admin_engine) as session:
        channel = await session.get(delivery_module.NotificationChannel, UUID(channel_id))
        assert channel is not None
        if channel.credential_id is None:
            return None
        vault = api_env.app.state.vault
        secret = await vault.checkout(session, org_id=org_id, credential_id=channel.credential_id)
        return json.loads(secret)  # type: ignore[no-any-return]


async def test_the_three_microsoft_values_go_to_the_vault_and_never_come_back(
    api_env: Any,
) -> None:
    org_id, headers = await _admin(api_env, "m365-api")
    created = await api_env.client.post(f"{API}/channels", json=_microsoft_body(), headers=headers)

    assert created.status_code == 201, created.text
    for value in (CLIENT_SECRET, CLIENT_ID, TENANT):
        assert value not in created.text
    assert created.json()["has_credential"] is True
    assert created.json()["config"]["auth"] == "microsoft"
    listed = await api_env.client.get(f"{API}/channels", headers=headers)
    assert CLIENT_SECRET not in listed.text and CLIENT_ID not in listed.text

    assert await _stored(api_env, org_id, created.json()["id"]) == {
        "tenant_id": TENANT,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    async with AsyncSession(api_env.admin_engine) as session:
        audit = (
            (await session.execute(select(AuditEvent.meta).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
    assert CLIENT_SECRET not in json.dumps(audit)


async def test_a_microsoft_channel_needs_all_three_values(api_env: Any) -> None:
    _org, headers = await _admin(api_env, "m365-api-missing")
    body = _microsoft_body()
    del body["microsoft_client_secret"]
    got = await api_env.client.post(f"{API}/channels", json=body, headers=headers)
    assert got.status_code == 400, got.text
    assert "directory" in got.json()["detail"].lower()


@pytest.mark.parametrize(
    "config",
    [
        {**MICROSOFT_CONFIG, "host": "smtp.example.com"},
        {**MICROSOFT_CONFIG, "port": 25},
        {**MICROSOFT_CONFIG, "use_tls": False},
        {**MICROSOFT_CONFIG, "auth": "kerberos"},
    ],
)
async def test_a_microsoft_channel_cannot_be_pointed_elsewhere(
    api_env: Any, config: dict[str, Any]
) -> None:
    _org, headers = await _admin(api_env, "m365-api-host")
    got = await api_env.client.post(
        f"{API}/channels", json=_microsoft_body(config=config), headers=headers
    )
    assert got.status_code in (400, 422), got.text


async def test_a_bad_directory_id_is_refused_on_save(api_env: Any) -> None:
    _org, headers = await _admin(api_env, "m365-api-tenant")
    got = await api_env.client.post(
        f"{API}/channels",
        json=_microsoft_body(microsoft_tenant_id="evil.example/../x?"),
        headers=headers,
    )
    assert got.status_code == 400, got.text


async def test_password_sign_in_is_stored_as_username_and_password(api_env: Any) -> None:
    org_id, headers = await _admin(api_env, "smtp-api-password")
    got = await api_env.client.post(
        f"{API}/channels",
        json={
            "kind": "email",
            "name": "relay",
            "config": {"host": "relay.lan", "port": 587, "from_address": SENDER, "use_tls": True},
            "smtp_username": "svc",
            "smtp_password": "relay-password-sentinel",
        },
        headers=headers,
    )
    assert got.status_code == 201, got.text
    assert "relay-password-sentinel" not in got.text
    assert await _stored(api_env, org_id, got.json()["id"]) == {
        "username": "svc",
        "password": "relay-password-sentinel",
    }


async def test_a_bare_secret_is_refused_for_email(api_env: Any) -> None:
    """It used to be stored as-is and then failed to parse at every send."""
    _org, headers = await _admin(api_env, "smtp-api-bare")
    got = await api_env.client.post(
        f"{API}/channels",
        json={
            "kind": "email",
            "name": "relay",
            "config": {"host": "relay.lan", "port": 587, "from_address": SENDER},
            "secret": "just-a-password",
        },
        headers=headers,
    )
    assert got.status_code == 400, got.text


async def test_switching_to_password_drops_the_microsoft_credential(api_env: Any) -> None:
    org_id, headers = await _admin(api_env, "m365-api-switch")
    created = await api_env.client.post(f"{API}/channels", json=_microsoft_body(), headers=headers)
    channel_id = created.json()["id"]
    async with AsyncSession(api_env.admin_engine) as session:
        channel = await session.get(delivery_module.NotificationChannel, UUID(channel_id))
        assert channel is not None
        old_credential = channel.credential_id

    got = await api_env.client.patch(
        f"{API}/channels/{channel_id}",
        json={"config": {"host": "relay.lan", "port": 25, "from_address": SENDER}},
        headers=headers,
    )

    assert got.status_code == 200, got.text
    assert got.json()["has_credential"] is False
    async with AsyncSession(api_env.admin_engine) as session:
        assert await session.get(Credential, old_credential) is None


async def test_switching_to_microsoft_needs_the_three_values(api_env: Any) -> None:
    _org, headers = await _admin(api_env, "m365-api-switch-in")
    created = await api_env.client.post(
        f"{API}/channels",
        json={
            "kind": "email",
            "name": "relay",
            "config": {"host": "relay.lan", "port": 587, "from_address": SENDER},
            "smtp_username": "svc",
            "smtp_password": "pw",
        },
        headers=headers,
    )
    channel_id = created.json()["id"]

    got = await api_env.client.patch(
        f"{API}/channels/{channel_id}", json={"config": MICROSOFT_CONFIG}, headers=headers
    )
    assert got.status_code == 400, got.text


async def test_the_channel_page_test_send_uses_microsoft(
    api_env: Any, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    org_id, headers = await _admin(api_env, "m365-api-test")
    created = await api_env.client.post(f"{API}/channels", json=_microsoft_body(), headers=headers)
    channel_id = created.json()["id"]

    got = await api_env.client.post(f"{API}/channels/{channel_id}/test", headers=headers)

    assert got.status_code == 200, got.text
    assert got.json()["delivered"] is True
    for value in (CLIENT_SECRET, ACCESS):
        assert value not in got.text
    (client,) = smtp.instances
    assert client.host == "smtp.office365.com"
    assert client.sent["To"] == SENDER
    async with AsyncSession(api_env.admin_engine) as session:
        used = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "notification.credential_used",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.actor_type for e in used] == ["user"]


async def test_the_test_send_reports_a_refused_sign_in(
    api_env: Any, smtp: type[_RecordingSMTP], token_endpoint: _TokenEndpoint
) -> None:
    token_endpoint.status = 400
    token_endpoint.body = {"error": "unauthorized_client"}
    _org, headers = await _admin(api_env, "m365-api-test-refused")
    created = await api_env.client.post(f"{API}/channels", json=_microsoft_body(), headers=headers)

    got = await api_env.client.post(f"{API}/channels/{created.json()['id']}/test", headers=headers)

    assert got.json() == {"delivered": False, "status_code": None, "error": "sign_in_refused"}

"""Notification emails go out in the recipient's language
(docs/superpowers/specs/2026-10-09-spanish-language-design.md §3.4).

WHAT IS PINNED HERE
- `_recipient_language`: an address that is a user of THIS organization takes
  that user's `locale` (anything starting `es`, in any case, is Spanish;
  everything else, `None` included, is English); an address that is no user of
  this organization is English -- even when another organization has a Spanish
  user at that address (tenancy, invariant 5).
- `_send_email` renders inside `use_locale(<recipient's language>)`, both on the
  worker's path (`deliver_pending` -> `attempt_delivery`, through the RLS
  engine) and on `send_test`'s.
- The worker never takes the language of whoever triggered the event: a
  `locale_var` of "es" around a worker call for an English recipient still
  yields English, and the reverse.
- `send_test` uses the recipient's language, looked up the same way, NOT the
  requester's `Accept-Language`.
- Text that is stored (`last_error`, the delivery row) stays English.

`smtplib.SMTP` is replaced by a recording stub; nothing opens a socket.
"""

import json
import smtplib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

import app.modules.notification.delivery as delivery_module
from app.core.i18n import locale_var, use_locale
from app.core.vault import LocalVaultService
from app.models import DomainEvent, NotificationChannel, NotificationDelivery, User
from app.modules.notification.channels import create_channel
from app.modules.notification.delivery import (
    _recipient_language,
    attempt_delivery,
    send_test,
)
from app.modules.notification.schema import ChannelCreate
from app.worker import deliver_pending

API = "/api/v1/notifications"

# The two renderings of the ticket.status_changed email below, spelled out so a
# catalogue edit that changes either is noticed here.
EN_SUBJECT = "[Toeshee] Ticket #T-7 — Printer: status changed to resolved"
EN_BODY = "Ticket #T-7 — Printer changed status from open to resolved.\n"
ES_SUBJECT = "[Toeshee] Ticket #T-7 — Printer: el estado cambió a resuelto"
ES_BODY = "El ticket #T-7 — Printer cambió de estado: de abierto a resuelto.\n"


class _RecordingSMTP:
    instances: list["_RecordingSMTP"] = []
    raise_on_send: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.sent: dict[str, Any] | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "_RecordingSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def send_message(self, message: Any) -> None:
        if self.__class__.raise_on_send is not None:
            raise self.__class__.raise_on_send
        self.sent = {
            "to": message["To"],
            "subject": message["Subject"],
            "body": message.get_content(),
        }


@pytest.fixture(autouse=True)
def _smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    _RecordingSMTP.instances = []
    _RecordingSMTP.raise_on_send = None
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    return _RecordingSMTP


def _only_sent(stub: type[_RecordingSMTP]) -> dict[str, Any]:
    assert len(stub.instances) == 1, stub.instances
    sent = stub.instances[0].sent
    assert sent is not None
    return sent


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _org_and_admin(api_env, slug: str) -> tuple[UUID, UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _add_user(
    api_env, org_id: UUID, *, email: str, locale: str | None, status: str = "active"
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(
            org_id=org_id,
            external_subject=f"sub-{uuid7().hex[:12]}",
            email=email,
            locale=locale,
            status=status,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _email_channel(
    session: AsyncSession,
    org_id: UUID,
    admin_id: UUID,
    vault: LocalVaultService,
    *,
    from_address: str = "noreply@toeshee.test",
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=admin_id,
        data=ChannelCreate(
            kind="email",
            name="ops-email",
            config={
                "host": "mail.internal.example.com",
                "port": 587,
                "from_address": from_address,
                "use_tls": True,
            },
        ),
        secret=None,
        vault=vault,
    )


async def _status_changed_event(session: AsyncSession, org_id: UUID) -> DomainEvent:
    now = datetime.now(UTC)
    event = DomainEvent(
        org_id=org_id,
        event_type="ticket.status_changed",
        subject_type="ticket",
        subject_id=uuid7(),
        payload={
            "ticket_number": "T-7",
            "subject": "Printer",
            "from_status": "open",
            "to_status": "resolved",
        },
        occurred_at=now,
        created_at=now,
    )
    session.add(event)
    await session.flush()
    return event


async def _delivery(
    session: AsyncSession,
    org_id: UUID,
    *,
    channel_id: UUID,
    event_id: UUID,
    recipient: str,
    due: bool = False,
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        org_id=org_id,
        event_id=event_id,
        channel_id=channel_id,
        recipient=recipient,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1) if due else None,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def _deliver_through_attempt(
    api_env, tmp_path: Path, org_id: UUID, admin_id: UUID, recipient: str
) -> dict[str, Any]:
    """One `ticket.status_changed` email to `recipient`, through `attempt_delivery`."""
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, admin_id, vault)
        event = await _status_changed_event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient=recipient
        )
        await attempt_delivery(session, delivery=delivery, vault=vault)
        assert delivery.status == "sent"
    return _only_sent(_RecordingSMTP)


# ---------------------------------------------------------------------------
# The lookup.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("es", "es"),
        ("es-MX", "es"),
        ("ES", "es"),
        ("Es-419", "es"),
        ("en", "en"),
        ("en-GB", "en"),
        ("fr", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
async def test_recipient_language_follows_the_users_locale(
    api_env, stored: str | None, expected: str
) -> None:
    org_id, _ = await _org_and_admin(api_env, "mail-lang-map")
    await _add_user(api_env, org_id, email="ana@example.com", locale=stored)
    async with AsyncSession(api_env.admin_engine) as session:
        assert await _recipient_language(session, org_id, "ana@example.com") == expected


async def test_recipient_language_matches_the_address_case_insensitively(api_env) -> None:
    org_id, _ = await _org_and_admin(api_env, "mail-lang-case")
    await _add_user(api_env, org_id, email="Ana.Perez@Example.COM", locale="es")
    async with AsyncSession(api_env.admin_engine) as session:
        assert await _recipient_language(session, org_id, "ana.perez@example.com") == "es"
        assert await _recipient_language(session, org_id, "ANA.PEREZ@EXAMPLE.COM") == "es"


async def test_recipient_language_of_an_address_that_is_no_user_is_english(api_env) -> None:
    org_id, _ = await _org_and_admin(api_env, "mail-lang-nouser")
    async with AsyncSession(api_env.admin_engine) as session:
        assert await _recipient_language(session, org_id, "nobody@example.com") == "en"


async def test_recipient_language_never_reads_another_organizations_user(api_env) -> None:
    """The same address is a Spanish user in org B. In org A, where it is no
    one's, it is English -- even on the admin engine, which has no RLS to lean on:
    the `org_id` predicate is the lookup's own."""
    org_a, _ = await _org_and_admin(api_env, "mail-lang-tenant-a")
    org_b, _ = await _org_and_admin(api_env, "mail-lang-tenant-b")
    await _add_user(api_env, org_b, email="shared@example.com", locale="es")
    async with AsyncSession(api_env.admin_engine) as session:
        assert await _recipient_language(session, org_a, "shared@example.com") == "en"
        assert await _recipient_language(session, org_b, "shared@example.com") == "es"


async def test_recipient_language_under_row_level_security(api_env) -> None:
    """On the application engine (RLS bound to one org) the lookup still finds
    the org's own user, and still does not find another org's."""
    from app.core.tenancy import org_session

    org_a, _ = await _org_and_admin(api_env, "mail-lang-rls-a")
    org_b, _ = await _org_and_admin(api_env, "mail-lang-rls-b")
    await _add_user(api_env, org_a, email="own@example.com", locale="es")
    await _add_user(api_env, org_b, email="other@example.com", locale="es")
    async with org_session(org_a, engine=api_env.app_engine) as session:
        assert await _recipient_language(session, org_a, "own@example.com") == "es"
        assert await _recipient_language(session, org_a, "other@example.com") == "en"
        # Even asked with the other org's id, RLS yields no row: English.
        assert await _recipient_language(session, org_b, "other@example.com") == "en"


async def test_two_users_with_one_address_resolve_to_the_oldest(api_env) -> None:
    """Which should not happen (the index on (org_id, lower(email)) is not
    unique). The answer is deterministic -- the earliest-created user (uuid7 ids
    sort by creation time) -- not whichever row the planner returns first."""
    org_id, _ = await _org_and_admin(api_env, "mail-lang-dup")
    await _add_user(api_env, org_id, email="dup@example.com", locale="es")
    await _add_user(api_env, org_id, email="DUP@example.com", locale=None)
    async with AsyncSession(api_env.admin_engine) as session:
        for _ in range(5):
            assert await _recipient_language(session, org_id, "dup@example.com") == "es"

    org_id2, _ = await _org_and_admin(api_env, "mail-lang-dup2")
    await _add_user(api_env, org_id2, email="dup@example.com", locale=None)
    await _add_user(api_env, org_id2, email="dup@example.com", locale="es")
    async with AsyncSession(api_env.admin_engine) as session:
        for _ in range(5):
            assert await _recipient_language(session, org_id2, "dup@example.com") == "en"


# ---------------------------------------------------------------------------
# What is sent.
# ---------------------------------------------------------------------------


async def test_a_spanish_user_gets_spanish_and_an_english_user_gets_english(
    api_env, tmp_path: Path
) -> None:
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-two-users")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es")
    await _add_user(api_env, org_id, email="bob@example.com", locale=None)

    spanish = await _deliver_through_attempt(api_env, tmp_path, org_id, admin_id, "ana@example.com")
    assert spanish["to"] == "ana@example.com"
    assert spanish["subject"] == ES_SUBJECT
    assert spanish["body"] == ES_BODY

    _RecordingSMTP.instances = []
    english = await _deliver_through_attempt(api_env, tmp_path, org_id, admin_id, "bob@example.com")
    assert english["to"] == "bob@example.com"
    assert english["subject"] == EN_SUBJECT
    assert english["body"] == EN_BODY


async def test_a_channel_default_address_that_is_no_users_gets_english(
    api_env, tmp_path: Path
) -> None:
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-default-box")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es")
    sent = await _deliver_through_attempt(
        api_env, tmp_path, org_id, admin_id, "ops-mailbox@example.com"
    )
    assert (sent["subject"], sent["body"]) == (EN_SUBJECT, EN_BODY)


async def test_another_organizations_spanish_user_does_not_make_the_mail_spanish(
    api_env, tmp_path: Path
) -> None:
    org_a, admin_a = await _org_and_admin(api_env, "mail-lang-iso-a")
    org_b, _ = await _org_and_admin(api_env, "mail-lang-iso-b")
    await _add_user(api_env, org_b, email="shared@example.com", locale="es")
    sent = await _deliver_through_attempt(api_env, tmp_path, org_a, admin_a, "shared@example.com")
    assert (sent["subject"], sent["body"]) == (EN_SUBJECT, EN_BODY)


# ---------------------------------------------------------------------------
# The worker has no language of its own.
# ---------------------------------------------------------------------------


async def test_the_worker_ignores_the_language_of_whoever_triggered_the_event(
    api_env, tmp_path: Path
) -> None:
    """`locale_var` is "es" around the worker call -- as it would be if the
    request that published the event had been Spanish and the context leaked --
    and the recipient is English: the mail is English."""
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-worker-en")
    await _add_user(api_env, org_id, email="bob@example.com", locale="en")
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, admin_id, vault)
        event = await _status_changed_event(session, org_id)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="bob@example.com",
            due=True,
        )
        await session.commit()

    token = locale_var.set("es")
    try:
        delivered, _ = await deliver_pending(org_id, api_env.app_engine, vault)
        assert locale_var.get() == "es"  # the caller's value is put back, untouched
    finally:
        locale_var.reset(token)

    assert delivered == 1
    sent = _only_sent(_RecordingSMTP)
    assert (sent["subject"], sent["body"]) == (EN_SUBJECT, EN_BODY)


async def test_the_worker_writes_spanish_for_a_spanish_recipient_whatever_the_ambient_language(
    api_env, tmp_path: Path
) -> None:
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-worker-es")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es-MX")
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, admin_id, vault)
        event = await _status_changed_event(session, org_id)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="ana@example.com",
            due=True,
        )
        await session.commit()

    assert locale_var.get() == "en"  # a worker has no request: English by default
    delivered, _ = await deliver_pending(org_id, api_env.app_engine, vault)
    assert delivered == 1
    sent = _only_sent(_RecordingSMTP)
    assert (sent["subject"], sent["body"]) == (ES_SUBJECT, ES_BODY)
    assert locale_var.get() == "en"  # nothing leaked out of the render


async def test_attempt_delivery_for_an_english_recipient_under_an_es_context(
    api_env, tmp_path: Path
) -> None:
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-attempt-ctx")
    await _add_user(api_env, org_id, email="bob@example.com", locale=None)
    with use_locale("es"):
        sent = await _deliver_through_attempt(
            api_env, tmp_path, org_id, admin_id, "bob@example.com"
        )
    assert (sent["subject"], sent["body"]) == (EN_SUBJECT, EN_BODY)


# ---------------------------------------------------------------------------
# send_test: the recipient's language, never the requester's.
# ---------------------------------------------------------------------------


async def _api_admin(api_env, slug: str) -> tuple[UUID, dict[str, str], UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, sub)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, headers, user_id


async def _test_send_over_http(
    api_env, headers: dict[str, str], from_address: str, accept_language: str
) -> dict[str, Any]:
    created = await api_env.client.post(
        f"{API}/channels",
        json={
            "kind": "email",
            "name": "ops-email",
            "config": {
                "host": "mail.internal.example.com",
                "port": 587,
                "from_address": from_address,
                "use_tls": True,
            },
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    got = await api_env.client.post(
        f"{API}/channels/{created.json()['id']}/test",
        headers={**headers, "Accept-Language": accept_language},
    )
    assert got.status_code == 200, got.text
    assert got.json()["delivered"] is True
    return _only_sent(_RecordingSMTP)


async def test_test_send_to_an_english_recipient_stays_english_on_a_spanish_request(
    api_env,
) -> None:
    org_id, headers, _ = await _api_admin(api_env, "mail-lang-test-en")
    await _add_user(api_env, org_id, email="bob@example.com", locale="en")
    sent = await _test_send_over_http(api_env, headers, "bob@example.com", "es")
    assert sent["body"].startswith("Event: notification.test_send\n")
    assert "Occurred: " in sent["body"]
    assert "Evento" not in sent["body"]
    assert "Ocurrió" not in sent["body"]


async def test_test_send_to_a_spanish_recipient_is_spanish_on_an_english_request(
    api_env,
) -> None:
    org_id, headers, _ = await _api_admin(api_env, "mail-lang-test-es")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es")
    sent = await _test_send_over_http(api_env, headers, "ana@example.com", "en")
    assert sent["body"].startswith("Evento: notification.test_send\n")
    assert "Ocurrió: " in sent["body"]
    assert "Event:" not in sent["body"]
    assert "Occurred" not in sent["body"]


async def test_test_send_to_a_mailbox_that_is_no_user_is_english_on_a_spanish_request(
    api_env,
) -> None:
    _, headers, _ = await _api_admin(api_env, "mail-lang-test-nouser")
    sent = await _test_send_over_http(api_env, headers, "noreply@toeshee.test", "es")
    assert sent["body"].startswith("Event: notification.test_send\n")


async def test_send_test_renders_in_the_recipients_language_not_the_ambient_one(
    api_env, tmp_path: Path
) -> None:
    """Calling `send_test` directly, with `locale_var` set to each language."""
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-test-direct")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es")
    await _add_user(api_env, org_id, email="bob@example.com", locale="en")
    vault = _vault(tmp_path)

    for ambient, address, expected_prefix in [
        ("es", "bob@example.com", "Event: "),
        ("en", "ana@example.com", "Evento: "),
    ]:
        _RecordingSMTP.instances = []
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            channel = await _email_channel(session, org_id, admin_id, vault, from_address=address)
            with use_locale(ambient):
                outcome = await send_test(
                    session, org_id=org_id, channel=channel, vault=vault, actor_id=admin_id
                )
            assert outcome.delivered is True
            assert locale_var.get() == "en"
        assert _only_sent(_RecordingSMTP)["body"].startswith(expected_prefix)


# ---------------------------------------------------------------------------
# Stored text stays English.
# ---------------------------------------------------------------------------


async def test_what_a_delivery_stores_is_english_and_holds_no_rendered_text(
    api_env, tmp_path: Path
) -> None:
    """A Spanish recipient, a Spanish ambient context, and an SMTP failure: the
    row's `last_error` is the English excerpt, and nothing on the row carries the
    rendered subject or body (the row has no such column; this pins it)."""
    org_id, admin_id = await _org_and_admin(api_env, "mail-lang-stored")
    await _add_user(api_env, org_id, email="ana@example.com", locale="es")
    vault = _vault(tmp_path)
    _RecordingSMTP.raise_on_send = smtplib.SMTPResponseException(550, b"mailbox unavailable")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _email_channel(session, org_id, admin_id, vault)
        event = await _status_changed_event(session, org_id)
        delivery = await _delivery(
            session, org_id, channel_id=channel.id, event_id=event.id, recipient="ana@example.com"
        )
        with use_locale("es"):
            await attempt_delivery(session, delivery=delivery, vault=vault)
        await session.commit()
        delivery_id = delivery.id

    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            )
        ).scalar_one()
    assert row.status == "dead"
    assert row.last_error == "smtp 550 rejected"
    stored = json.dumps(
        {c.name: str(getattr(row, c.name)) for c in NotificationDelivery.__table__.columns}
    )
    for rendered in (ES_SUBJECT, ES_BODY.strip(), EN_SUBJECT, EN_BODY.strip(), "resuelto"):
        assert rendered not in stored


def test_the_delivery_module_stores_no_rendered_email() -> None:
    """Structural: the only assignments of `subject`/`body` in `_send_email` go to
    the SMTP call. `NotificationDelivery` has no column that could hold them."""
    columns = {c.name for c in NotificationDelivery.__table__.columns}
    assert not columns & {"subject", "body", "message", "rendered"}
    assert delivery_module._recipient_language.__doc__

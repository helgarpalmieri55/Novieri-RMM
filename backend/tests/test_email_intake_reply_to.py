"""Outbound ticket mail carries a Reply-To that this module can resolve.

Spec §5.2 (correction C3). Without these, `email_token` is never minted in
production and the whole reply path is unreachable -- while every other test
passes, because each constructs its own token.

WHY THE SMTP FAKE IS `smtplib.SMTP` AND NOT `delivery._send_smtp_sync`.
The task brief suggested monkeypatching `_send_smtp_sync` itself and
recording its kwargs. That would be strictly weaker, and weaker in exactly
the direction this task exists to close: `_send_smtp_sync` is the function
that BUILDS the `Reply-To` header, so patching it out means no test ever
observes a real header on a real `EmailMessage` -- the round-trip test below
would round-trip a kwarg through `extract_reply_token`, not a header. So
this module reuses `tests/test_notification_delivery.py`'s existing
convention (that file's module docstring: "`smtplib.SMTP` itself is
monkeypatched to a stub that records what it was called with and never opens
a socket"), importing that file's `_RecordingSMTP` rather than defining a
second one. `last_message` is the real `email.message.EmailMessage` that
`_send_smtp_sync` handed to `send_message`.

WHY THE INBOUND HALF IS DRIVEN THROUGH THE REAL `process_message`.
`test_a_reply_to_the_published_address_lands_as_a_public_comment` is the
first test in this module to exercise BOTH halves against each other: the
address it feeds to `parsing`/`intake` is literally the `Reply-To` header
`_send_smtp_sync` emitted, not a token this test minted. Two
independently-correct halves that disagree on format is precisely the defect
neither half's own suite can see.

WHY SEVERAL TESTS ASSERT ON HEADERS AND NOT ON THE DATABASE (review F1).
"The token is stable" is a claim about what the RECIPIENT sees. Asserting it
on `ticket.email_token` alone is satisfied by an implementation that stores
one token and publishes a fresh, unstored one on every message after the
first -- which passes every DB-level assertion in this file while the
requester's reply to the RESOLUTION email (the message they are most likely
to reply to) silently opens a new ticket. `test_the_emitted_reply_to_is_
identical_on_every_message_in_the_thread` compares the two emitted headers
directly, so that mutation is red.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import DomainEvent, EmailMailbox, NotificationChannel, NotificationDelivery
from app.modules.email_intake.identity import _TOKEN_RE, reply_to_for_ticket
from app.modules.email_intake.intake import process_message
from app.modules.email_intake.models import InboundMessage
from app.modules.email_intake.parsing import ParsedMessage
from app.modules.notification.channels import create_channel
from app.modules.notification.delivery import attempt_delivery
from app.modules.notification.schema import ChannelCreate
from app.modules.service_desk.models import Ticket, TicketComment
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.tickets import create_ticket
from tests.test_email_intake_migration import _seed_category, _seed_credential, _seed_user
from tests.test_notification_delivery import _RecordingSMTP

# The requester of every ticket this module builds, and therefore the only
# address that may be handed a `Reply-To` (review F2).
REQUESTER = "alice@acme.test"


@pytest.fixture(autouse=True)
def _reset_recording_smtp() -> None:
    """`_RecordingSMTP` keeps its recordings in CLASS state, and it is shared
    with `tests/test_notification_delivery.py` -- which resets it with its
    own module-local autouse fixture that does not apply here. Without this,
    `last_message` could return a message a previous test in this file
    sent."""
    _RecordingSMTP.instances = []
    _RecordingSMTP.raise_on_init = None
    _RecordingSMTP.raise_on_send = None


@dataclass
class ReplyEnv:
    """One org with a configured inbound mailbox (`helpdesk@acme.test`), one
    requester (`alice@acme.test`), and one email notification channel --
    everything needed to send a real notification through the real
    `attempt_delivery` and then feed the resulting `Reply-To` back in through
    the real `process_message`."""

    api_env: Any
    engine: AsyncEngine
    admin_engine: AsyncEngine
    org_id: UUID
    alice_id: UUID
    category_id: UUID
    mailbox_id: UUID
    channel_id: UUID
    vault: LocalVaultService
    store: ObjectStore

    # -- outbound ---------------------------------------------------------

    @property
    def messages(self) -> list[EmailMessage]:
        """Every message sent so far, in order -- one `_RecordingSMTP`
        instance per `_send_smtp_sync` call."""
        messages = []
        for instance in _RecordingSMTP.instances:
            if instance.sent is None:
                continue
            message = instance.sent["message"]
            assert isinstance(message, EmailMessage)
            messages.append(message)
        return messages

    @property
    def last_message(self) -> EmailMessage:
        sent = self.messages
        assert sent, "no message was sent"
        return sent[-1]

    async def a_ticket(self) -> UUID:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            ticket = await create_ticket(
                session,
                self.org_id,
                self.alice_id,
                TicketCreate(
                    type="incident",
                    category_id=self.category_id,
                    subject="Printer trouble",
                    description="It is on fire.",
                    requester_id=self.alice_id,
                    client_id=self.org_id,
                ),
                origin="portal",
            )
            return ticket.id

    async def token_for(self, ticket_id: UUID) -> str | None:
        async with AsyncSession(self.admin_engine) as session:
            return (
                await session.execute(
                    select(Ticket.email_token).where(Ticket.id == ticket_id)
                )
            ).scalar_one()

    async def _deliver(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        event_type: str,
        recipient: str = REQUESTER,
        rollback: bool = False,
    ) -> None:
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            now = datetime.now(UTC)
            event = DomainEvent(
                org_id=self.org_id,
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                payload={"ticket_number": "T-1", "subject": "Printer trouble"},
                occurred_at=now,
                created_at=now,
            )
            session.add(event)
            await session.flush()
            delivery = NotificationDelivery(
                org_id=self.org_id,
                event_id=event.id,
                channel_id=self.channel_id,
                recipient=recipient,
                status="pending",
                attempts=0,
            )
            session.add(delivery)
            await session.flush()
            await attempt_delivery(session, delivery=delivery, vault=self.vault)
            # Fail loudly here rather than let a silently-dead delivery make
            # a "no Reply-To" assertion below pass for the wrong reason.
            assert delivery.status == "sent", delivery.last_error
            if rollback:
                # The message has already gone out; this is the connection
                # loss / later-error case (review F4).
                await session.rollback()
            else:
                await session.commit()

    async def send_ticket_notification(
        self, ticket_id: UUID, *, recipient: str = REQUESTER, rollback: bool = False
    ) -> None:
        await self._deliver(
            subject_type="ticket",
            subject_id=ticket_id,
            event_type="ticket.resolved",
            recipient=recipient,
            rollback=rollback,
        )

    async def send_non_ticket_notification(self, *, subject_id: UUID | None = None) -> None:
        """A non-ticket event. `subject_id` defaults to this env's channel,
        but a caller may pass a REAL ticket id -- see
        `test_a_non_ticket_event_carrying_a_real_ticket_id_gets_no_reply_to`,
        which is what makes `delivery.py`'s `subject_type == "ticket"`
        condition load-bearing rather than incidentally satisfied."""
        await self._deliver(
            subject_type="notification_channel",
            subject_id=subject_id if subject_id is not None else self.channel_id,
            event_type="notification.test_send",
        )

    # -- channel / recipient configuration ---------------------------------

    async def a_user(self, email: str) -> UUID:
        """Another real user in this org -- e.g. the technician who receives
        the same ticket's mail as assignee."""
        return await _seed_user(self.api_env, self.org_id, email)

    async def use_credentialed_channel(self, *, username: str, password: str) -> None:
        """Re-point this env at an email channel that ACTUALLY HOLDS a vault
        credential (review F5).

        The default channel is created with `secret=None`, so
        `channel.credential_id is None` and `_send_email`'s entire vault
        branch -- checkout, audit record, `json.loads`, login -- is skipped:
        a "no credential leaks" assertion against that channel is checking a
        path on which no credential exists, and cannot fail."""
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            channel = await create_channel(
                session,
                org_id=self.org_id,
                actor_id=self.alice_id,
                data=ChannelCreate(
                    kind="email",
                    name="ops-email-authenticated",
                    config={
                        "host": "mail.internal.example.com",
                        "port": 587,
                        "from_address": "noreply@toeshee.test",
                        "use_tls": True,
                    },
                ),
                secret=json.dumps({"username": username, "password": password}),
                vault=self.vault,
            )
            await session.commit()
            assert channel.credential_id is not None, "the vault branch would be skipped"
            self.channel_id = channel.id

    async def channel_credential_id(self) -> UUID | None:
        async with AsyncSession(self.admin_engine) as session:
            return (
                await session.execute(
                    select(NotificationChannel.credential_id).where(
                        NotificationChannel.id == self.channel_id
                    )
                )
            ).scalar_one()

    async def another_org_with_a_mailbox(self) -> UUID:
        """A SECOND, fully configured org: its own active mailbox, so a
        `reply_to_for_ticket` call under its id gets past the mailbox gate
        and actually reaches the ticket lookup (review F9)."""
        org_id = await self.api_env.create_org(f"other-{uuid4().hex[:8]}")
        category_id = await _seed_category(self.api_env, org_id)
        actor_id = await _seed_user(self.api_env, org_id, f"boss-{uuid4().hex[:8]}@other.test")
        async with org_session(org_id, engine=self.engine, clients=ClientScope.all()) as session:
            session.add(
                EmailMailbox(
                    org_id=org_id,
                    address="helpdesk@other.test",
                    host="imap.other.test",
                    port=993,
                    username="helpdesk@other.test",
                    credential_id=await _seed_credential(session, org_id, actor_id),
                    default_category_id=category_id,
                    created_by=actor_id,
                )
            )
        async with org_session(org_id, engine=self.engine, clients=ClientScope.all()) as session:
            # Without this the caller's "returns None" assertion could pass
            # for the wrong reason -- the no-mailbox gate rather than
            # tenancy.
            mailboxes = (
                (
                    await session.execute(
                        select(EmailMailbox).where(EmailMailbox.active.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            assert len(mailboxes) == 1
        return org_id

    # -- mailbox configuration --------------------------------------------

    async def remove_mailbox(self) -> None:
        async with AsyncSession(self.admin_engine) as session:
            await session.execute(
                delete(EmailMailbox).where(EmailMailbox.id == self.mailbox_id)
            )
            await session.commit()

    async def deactivate_mailbox(self) -> None:
        async with AsyncSession(self.admin_engine) as session:
            await session.execute(
                update(EmailMailbox)
                .where(EmailMailbox.id == self.mailbox_id)
                .values(active=False)
            )
            await session.commit()

    async def set_mailbox_address(self, address: str) -> None:
        async with AsyncSession(self.admin_engine) as session:
            await session.execute(
                update(EmailMailbox)
                .where(EmailMailbox.id == self.mailbox_id)
                .values(address=address)
            )
            await session.commit()

    # -- inbound ----------------------------------------------------------

    async def deliver_inbound_reply(
        self, *, to: str, body: str, sender: str = REQUESTER
    ) -> InboundMessage:
        """Drive a real inbound message, addressed to `to`, through the real
        `process_message` -- the receive half of the round trip."""
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            mailbox = (
                await session.execute(
                    select(EmailMailbox).where(EmailMailbox.id == self.mailbox_id)
                )
            ).scalar_one()
        parsed = ParsedMessage(
            message_id=f"<{uuid4().hex}@acme.test>",
            from_address=sender,
            subject="Re: Printer trouble",
            recipients=(to,),
            body_text=body,
            attachments=(),
            received_at=datetime.now(UTC),
            refusal=None,
            refusal_reason="",
        )
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return await process_message(
                session, self.org_id, mailbox, parsed, store=self.store
            )

    async def comments(self, ticket_id: UUID) -> list[TicketComment]:
        async with AsyncSession(self.admin_engine) as session:
            return list(
                (
                    await session.execute(
                        select(TicketComment)
                        .where(TicketComment.ticket_id == ticket_id)
                        .order_by(TicketComment.created_at, TicketComment.id)
                    )
                )
                .scalars()
                .all()
            )


@pytest.fixture()
async def reply_env(api_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)
    with mock_aws():
        store = ObjectStore(api_env.settings)
        vault = LocalVaultService(tmp_path / "root.key")
        org_id = await api_env.create_org("email-reply-to")
        category_id = await _seed_category(api_env, org_id)
        alice_id = await _seed_user(api_env, org_id, REQUESTER)

        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=org_id,
                address="helpdesk@acme.test",
                host="imap.acme.test",
                port=993,
                username="helpdesk@acme.test",
                credential_id=await _seed_credential(session, org_id, alice_id),
                default_category_id=category_id,
                created_by=alice_id,
            )
            session.add(mailbox)
            await session.flush()
            mailbox_id = mailbox.id

        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            channel = await create_channel(
                session,
                org_id=org_id,
                actor_id=alice_id,
                data=ChannelCreate(
                    kind="email",
                    name="ops-email",
                    config={
                        "host": "mail.internal.example.com",
                        "port": 587,
                        "from_address": "noreply@toeshee.test",
                        "use_tls": True,
                    },
                ),
                secret=None,
                vault=vault,
            )
            await session.commit()
            channel_id = channel.id

        yield ReplyEnv(
            api_env=api_env,
            engine=api_env.app_engine,
            admin_engine=api_env.admin_engine,
            org_id=org_id,
            alice_id=alice_id,
            category_id=category_id,
            mailbox_id=mailbox_id,
            channel_id=channel_id,
            vault=vault,
            store=store,
        )


# ---------------------------------------------------------------------------
# The brief's test list, verbatim.
# ---------------------------------------------------------------------------


async def test_an_email_notification_for_a_ticket_mints_a_token(reply_env) -> None:
    ticket_id = await reply_env.a_ticket()
    assert await reply_env.token_for(ticket_id) is None
    await reply_env.send_ticket_notification(ticket_id)
    assert await reply_env.token_for(ticket_id) is not None


async def test_the_reply_to_header_carries_the_token_at_the_mailbox_address(reply_env) -> None:
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    token = await reply_env.token_for(ticket_id)
    assert reply_env.last_message["Reply-To"] == f"helpdesk+t-{token}@acme.test"


async def test_the_token_is_stable_across_notifications(reply_env) -> None:
    """It is in the headers of every message in the thread; a second one
    would orphan the first."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    first = await reply_env.token_for(ticket_id)
    await reply_env.send_ticket_notification(ticket_id)
    assert await reply_env.token_for(ticket_id) == first


async def test_the_emitted_reply_to_is_identical_on_every_message_in_the_thread(
    reply_env,
) -> None:
    """Review F1 -- the same claim as above, but ON THE WIRE.

    The database assertion above is satisfied by an implementation that
    stores the first token and publishes a fresh, unstored one on every
    later message. The requester then replies to the resolution email, the
    token in it resolves to no ticket, and their reply silently opens a NEW
    ticket instead of reopening theirs -- with the whole suite green. Only
    comparing the two emitted headers can see that."""
    ticket_id = await reply_env.a_ticket()

    await reply_env.send_ticket_notification(ticket_id)
    await reply_env.send_ticket_notification(ticket_id)

    headers = [str(m["Reply-To"]) for m in reply_env.messages]
    assert len(headers) == 2, headers
    assert headers[0] == headers[1]
    # ... and not two absent headers, which would compare equal too.
    token = await reply_env.token_for(ticket_id)
    assert headers[1] == f"helpdesk+t-{token}@acme.test"


async def test_no_mailbox_means_no_reply_to_and_no_token(reply_env) -> None:
    """Email intake is per-org configuration. An org that has not configured
    a mailbox must not receive a Reply-To pointing at an address nobody
    reads -- a reply there vanishes silently."""
    await reply_env.remove_mailbox()
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


async def test_an_inactive_mailbox_gets_no_reply_to_and_no_token(reply_env) -> None:
    """Deactivating a mailbox is how an admin stops polling it. A token
    minted afterwards would be published against an address nobody reads --
    so the "and no token" half is asserted here too (review F8), not only in
    the removed-mailbox case."""
    await reply_env.deactivate_mailbox()
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


async def test_a_non_ticket_notification_gets_no_reply_to(reply_env) -> None:
    """Only ticket mail is repliable. A Reply-To on anything else invites a
    reply that resolves to no ticket."""
    await reply_env.send_non_ticket_notification()
    assert reply_env.last_message.get("Reply-To") is None


async def test_a_non_ticket_event_carrying_a_real_ticket_id_gets_no_reply_to(
    reply_env,
) -> None:
    """Review F6: pin the MECHANISM, not an end state that holds by accident.

    The test above passes even with `delivery.py`'s condition replaced by
    `if True`, because a channel id names no ticket, so the lookup returns
    None anyway. Here the event is still not a ticket event, but its
    `subject_id` IS a real ticket in this org whose requester IS the
    recipient -- so with the condition removed, a `Reply-To` would be
    emitted and a token minted. Both are asserted absent."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_non_ticket_notification(subject_id=ticket_id)
    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


# ---------------------------------------------------------------------------
# Review F2: the address is only offered to whoever can actually use it.
# ---------------------------------------------------------------------------


async def test_the_requester_gets_a_reply_to(reply_env) -> None:
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id, recipient=REQUESTER)
    token = await reply_env.token_for(ticket_id)
    assert token is not None
    assert reply_env.last_message["Reply-To"] == f"helpdesk+t-{token}@acme.test"


async def test_a_technician_on_the_same_ticket_gets_no_reply_to_and_no_token(
    reply_env,
) -> None:
    """The same ticket event goes to the assignee and to queue members
    (`notification/routing.py`), not only to the requester. Spec §13 defers
    staff replying by email: `intake.py` quarantines a reply whose sender is
    not the ticket's requester -- silently, with no comment added and no
    bounce sent. A technician who hits Reply on "ticket resolved" would
    believe they had updated the ticket.

    Case is irrelevant to the gate, so the recipient here is spelled
    differently from the seeded user's own address on purpose."""
    # A real user in this org, so the recipient is a genuine staff address
    # rather than a stranger's -- the gate must refuse it all the same.
    await reply_env.a_user("tech@acme.test")
    ticket_id = await reply_env.a_ticket()

    await reply_env.send_ticket_notification(ticket_id, recipient="Tech@ACME.test")

    assert reply_env.last_message.get("Reply-To") is None
    # Lazily minted: a ticket only ever discussed internally never
    # accumulates a token at all.
    assert await reply_env.token_for(ticket_id) is None


async def test_the_requester_gate_is_case_and_whitespace_insensitive(reply_env) -> None:
    """The requester's own address, differently cased, is still theirs --
    refusing it would silence the reply path for anyone whose mail client
    capitalises differently from the seeded row."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id, recipient=" Alice@ACME.test ")
    token = await reply_env.token_for(ticket_id)
    assert token is not None
    assert reply_env.last_message["Reply-To"] == f"helpdesk+t-{token}@acme.test"


async def test_the_reply_to_round_trips_through_extract_reply_token(reply_env) -> None:
    """The end-to-end claim: what we SEND is what `identity.py` can PARSE.
    Two independently-correct halves that disagree on format is exactly the
    defect a per-half test cannot see."""
    from app.modules.email_intake.identity import extract_reply_token

    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    header = reply_env.last_message["Reply-To"]
    assert extract_reply_token([header]) == await reply_env.token_for(ticket_id)


# ---------------------------------------------------------------------------
# Beyond the brief: the two halves, connected.
# ---------------------------------------------------------------------------


async def test_a_reply_to_the_published_address_lands_as_a_public_comment(reply_env) -> None:
    """The whole point of the feature, end to end and in one test: send a
    real notification, take the `Reply-To` header off the wire, address a
    real inbound message to it, and drive it through the real
    `process_message`. Nothing here mints or hand-writes a token."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)

    row = await reply_env.deliver_inbound_reply(
        to=reply_env.last_message["Reply-To"], body="Still broken, please reopen."
    )

    assert row.disposition == "commented"
    assert row.ticket_id == ticket_id
    comments = await reply_env.comments(ticket_id)
    assert [(c.body, c.visibility) for c in comments] == [
        ("Still broken, please reopen.", "public")
    ]


async def test_the_published_address_still_does_not_authorise_a_non_requester(
    reply_env,
) -> None:
    """Spec §0.2, re-verified against the ACTUALLY-PUBLISHED address rather
    than a hand-written token: the token identifies the ticket, it does not
    authorise the sender. A technician who obtains the address (a forward, a
    quoted thread) and replies from their own mailbox is quarantined, and
    the ticket is untouched -- exactly as before this task's gate existed."""
    await reply_env.a_user("tech@acme.test")
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)

    row = await reply_env.deliver_inbound_reply(
        to=str(reply_env.last_message["Reply-To"]),
        body="I ordered the fuser.",
        sender="tech@acme.test",
    )

    assert row.disposition == "quarantined"
    assert row.reason == "reply token does not belong to this sender"
    assert await reply_env.comments(ticket_id) == []


async def test_the_minted_token_fits_the_column_and_the_extraction_regex(reply_env) -> None:
    """`ticket.email_token` is `String(43)` and `identity._TOKEN_RE` matches
    EXACTLY 43 characters of the urlsafe-base64 alphabet. A minted token one
    character off, or one carrying a character outside that alphabet, would
    be truncated by the column or silently unmatchable by the regex.

    Review F10: the regex the name promises is now actually applied -- the
    earlier version asserted only `len(token) == 43`, which is a weaker
    claim than either half of its own title."""
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    token = await reply_env.token_for(ticket_id)
    assert token is not None

    assert len(token) == 43  # the column's String(43)
    match = _TOKEN_RE.search(str(reply_env.last_message["Reply-To"]))
    assert match is not None, "the emitted header does not match the extraction regex"
    assert match.group(1) == token


async def test_a_plus_addressed_mailbox_gets_no_reply_to(reply_env) -> None:
    """A mailbox address that is itself a plus-address cannot carry a nested
    tag: `helpdesk+support+t-<token>@acme.test` is not an address the mail
    system routes back to `helpdesk+support`, and `_TOKEN_RE` would extract
    from it anyway. Refuse, rather than publish an address that does not
    work."""
    await reply_env.set_mailbox_address("helpdesk+support@acme.test")
    ticket_id = await reply_env.a_ticket()
    await reply_env.send_ticket_notification(ticket_id)
    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


async def test_no_vault_credential_leaks_into_a_message_that_carries_a_reply_to(
    reply_env, caplog: pytest.LogCaptureFixture
) -> None:
    """Invariant #3, re-checked on the path this task touched: adding a
    header must not put anything from the vault into the message or a log
    line.

    Review F5: this test used to run against the fixture's channel, which
    has `secret=None` -- so `channel.credential_id` was None, `_send_email`
    skipped its entire vault branch, and there was no credential anywhere on
    the path for the assertion to catch. It also grepped for the vault's
    key-FILE path rather than for secret material. It now sends through a
    channel that really does hold an SMTP credential, and greps for the
    password itself."""
    password = "hunter2-outbound-swordfish"
    await reply_env.use_credentialed_channel(username="svc", password=password)
    assert await reply_env.channel_credential_id() is not None

    ticket_id = await reply_env.a_ticket()
    with caplog.at_level("DEBUG"):
        await reply_env.send_ticket_notification(ticket_id)

    token = await reply_env.token_for(ticket_id)
    assert token is not None
    message = reply_env.last_message
    assert message["From"] == "noreply@toeshee.test"
    assert message["Reply-To"] == f"helpdesk+t-{token}@acme.test"
    # The credential really was used on this send (so the assertions below
    # are checking a path a secret actually travelled)...
    logins = [i.login_args for i in _RecordingSMTP.instances if i.login_args is not None]
    assert logins == [("svc", password)]
    # ... and it appears in none of the places this task added or touched.
    assert password not in message.as_string()
    assert password not in caplog.text
    assert "root.key" not in caplog.text


# ---------------------------------------------------------------------------
# Review F4: the token is durable BEFORE it is published.
# ---------------------------------------------------------------------------


async def test_a_published_token_survives_a_rollback_of_the_delivery_transaction(
    reply_env,
) -> None:
    """The mint runs in its own short transaction, committed before the SMTP
    conversation starts.

    Minted inside the delivery transaction instead, a rollback AFTER a
    successful send (connection loss, a later error while recording the
    outcome) throws away a token that has already left the building. The
    address in the requester's inbox would then resolve to no ticket
    forever, which is the exact failure this whole feature exists to
    prevent. Here the delivery transaction is rolled back after the send,
    and the published address must still be resolvable."""
    ticket_id = await reply_env.a_ticket()

    await reply_env.send_ticket_notification(ticket_id, rollback=True)

    header = str(reply_env.last_message["Reply-To"])
    token = await reply_env.token_for(ticket_id)
    assert token is not None, "the published token was discarded with the transaction"
    assert header == f"helpdesk+t-{token}@acme.test"

    # And the published address still round-trips all the way to a comment.
    row = await reply_env.deliver_inbound_reply(to=header, body="Still broken.")
    assert row.disposition == "commented"
    assert row.ticket_id == ticket_id


# ---------------------------------------------------------------------------
# Review F7: a malformed mailbox address must not dead-letter the org's mail.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "helpdesk@acme.test\r\nBcc: attacker@evil.test",  # header injection attempt
        "help desk@acme.test",  # a bare space
        "helpdesk\t@acme.test",  # a tab
        "helpdesk@acme.test\x1b",  # a bare escape character
        "helpdesk@acme.tëst",  # non-ASCII: no SMTPUTF8 here, so unrepliable
    ],
)
async def test_a_mailbox_address_that_cannot_be_a_header_gets_no_reply_to(
    reply_env, address: str
) -> None:
    """`email_mailbox.address` is a bare `String(320)` -- neither
    `MailboxCreate` nor `MailboxUpdate` applies a format pattern -- so an org
    admin can store a control character in it.

    `email.policy.default` refuses to build a header from that, correctly,
    but it refuses by RAISING `ValueError` out of `_send_smtp_sync`, which
    `_send_email` does not classify. It escapes to the worker's blanket
    `except Exception`, which marks the delivery `dead` -- terminal, no
    retry. One bad character would stop EVERY ticket-subject email for the
    org, permanently. So the address is refused here instead: no
    `Reply-To`, and the mail still goes out (`_deliver` asserts the delivery
    reached `sent`, so a raise anywhere in this path fails this test)."""
    await reply_env.set_mailbox_address(address)
    ticket_id = await reply_env.a_ticket()

    await reply_env.send_ticket_notification(ticket_id)

    assert reply_env.last_message.get("Reply-To") is None
    assert await reply_env.token_for(ticket_id) is None


# ---------------------------------------------------------------------------
# Review F9: the tenancy claim, under RLS rather than through the admin engine.
# ---------------------------------------------------------------------------


async def test_another_orgs_ticket_is_invisible_and_mints_nothing_under_rls(
    reply_env,
) -> None:
    """Invariant #5, exercised where it is actually enforced.

    Every other test in this file drives `attempt_delivery` through
    `AsyncSession(admin_engine)`, which BYPASSES row-level security -- so
    nothing there would fail if the explicit `org_id` predicates were
    removed and RLS were the only guard, and RLS is not even active on that
    path. This test calls `reply_to_for_ticket` directly through
    `org_session` on the RLS-constrained app engine, in both directions:
    org B may not resolve (or mint on) org A's ticket, and org A still
    can."""
    other_org_id = await reply_env.another_org_with_a_mailbox()
    ticket_id = await reply_env.a_ticket()

    async with org_session(
        other_org_id, engine=reply_env.engine, clients=ClientScope.all()
    ) as session:
        stolen = await reply_to_for_ticket(session, other_org_id, ticket_id, REQUESTER)
    assert stolen is None
    assert await reply_env.token_for(ticket_id) is None

    # The positive control: the same call, from the owning org, does work --
    # so the None above is tenancy, not a broken fixture.
    async with org_session(
        reply_env.org_id, engine=reply_env.engine, clients=ClientScope.all()
    ) as session:
        mine = await reply_to_for_ticket(session, reply_env.org_id, ticket_id, REQUESTER)
    token = await reply_env.token_for(ticket_id)
    assert token is not None
    assert mine == f"helpdesk+t-{token}@acme.test"

"""The poller: claim, fetch, process, \\Seen-after-commit, error isolation.

A FakeImapClient stands in for imaplib. The point of these tests is the
ORDER of operations and the failure behaviour, neither of which needs a real
mail server -- and a test that needed one would not run in CI.

WHAT THIS FILE CANNOT SEE, AND WHERE THAT LIVES INSTEAD. A fake transport is
by construction incapable of testing the transport: `.PEEK` versus `RFC822`,
UID threading, folder encoding and -- the one that mattered -- whether the
TLS certificate is verified before the password is sent are all invisible
here, because the fake *is* the thing being asserted about. Those properties
are tested on the wire in `tests/test_email_intake_imap.py`, against a
scripted TLS socket. Neither file substitutes for the other.

THE PASSWORD THESE TESTS SEED IS THE STRING `hunter2`, and the connection
failure below embeds it in its exception message exactly the way a real IMAP
library embeds a server's rejection text. That is deliberate: invariant #3
and spec §11.12 are only actually tested if the credential is present in the
thing the poller is handed on the failure path.
"""

import asyncio
import inspect
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import VaultService, get_vault
from app.models import Credential, EmailMailbox
from app.modules.email_intake.imap import ImapError
from app.modules.email_intake.models import InboundMessage
from app.modules.email_intake.poller import (
    MAX_MESSAGES_PER_POLL,
    poll_all_mailboxes,
    poll_mailbox,
)
from app.modules.service_desk.models import Ticket
from tests.test_email_intake_migration import _seed_category, _seed_user

_PASSWORD = "hunter2"


class FakeImapClient:
    def __init__(self, messages, *, fail_on_connect=False, fail_on_mark=False):
        self.messages = list(messages)
        self.seen: set[str] = set()
        self.fail_on_connect = fail_on_connect
        self.fail_on_mark = fail_on_mark
        # Every `limit` the poller asked for, so a test can prove the cap is
        # actually plumbed through rather than merely defined.
        self.limits: list[int] = []
        if fail_on_connect:
            raise OSError(f"imap: connection refused for user 'helpdesk' password '{_PASSWORD}'")

    def fetch_unseen(self, limit):
        self.limits.append(limit)
        # The real client applies the cap to the UID list before it issues a
        # single FETCH; mirroring that here is what makes the backlog test
        # meaningful.
        return [(uid, raw) for uid, raw in self.messages if uid not in self.seen][:limit]

    def mark_seen(self, uid):
        if self.fail_on_mark:
            raise OSError("imap: STORE failed")
        self.seen.add(uid)

    def close(self):
        pass


async def _seed_stored_credential(
    session: AsyncSession, org_id: UUID, actor_id: UUID, vault: VaultService
) -> UUID:
    """A `Credential` the vault can actually check out -- unlike
    `test_email_intake_migration._seed_credential`, which leaves `ciphertext`
    NULL because nothing before Task 7 ever decrypted it. The poller does, at
    point of use, so these tests need the real envelope."""
    credential = Credential(
        org_id=org_id,
        name="email-mailbox-secret",
        type="email_mailbox",
        vault_ref="",
        created_by=actor_id,
    )
    session.add(credential)
    await session.flush()
    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=_PASSWORD
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


@dataclass
class PollEnv:
    """One org, one known sender (`alice@acme.test`), one mailbox with a real
    vault-held credential, and an object store."""

    api_env: Any
    engine: AsyncEngine
    admin_engine: AsyncEngine
    vault: VaultService
    store: ObjectStore
    org_id: UUID
    mailbox_id: UUID
    mailbox_address: str
    actor_id: UUID
    category_id: UUID
    loop: asyncio.AbstractEventLoop

    # -- inputs ------------------------------------------------------------

    def raw(
        self,
        *,
        sender: str,
        message_id: str | None = None,
        subject: str = "Printer trouble",
        body: str = "The printer on 2 is jammed.",
    ) -> bytes:
        message = EmailMessage()
        message["Message-ID"] = message_id or f"<{uuid4().hex}@acme.test>"
        message["From"] = sender
        message["To"] = self.mailbox_address
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message.set_content(body)
        return message.as_bytes()

    # -- running the poller ------------------------------------------------

    async def poll(self, client: FakeImapClient) -> dict[str, int]:
        return await poll_mailbox(
            self.org_id,
            self.mailbox_id,
            engine=self.engine,
            vault=self.vault,
            store=self.store,
            client_factory=lambda **_: client,
        )

    async def poll_with(self, factory: Any) -> dict[str, int]:
        return await poll_mailbox(
            self.org_id,
            self.mailbox_id,
            engine=self.engine,
            vault=self.vault,
            store=self.store,
            client_factory=factory,
        )

    async def poll_expecting_failure(self) -> dict[str, int]:
        def factory(**_: Any) -> FakeImapClient:
            return FakeImapClient([], fail_on_connect=True)

        return await self.poll_with(factory)

    async def poll_all(self, factory: Any) -> int:
        """The real all-orgs sweep, unmodified.

        NOTE it is deliberately NOT wrapped in anything that deactivates
        other orgs' mailboxes: the pg_cluster database is shared session-wide,
        and a helper that mutates rows belonging to tests that are not running
        leaves them corrupted if anything between the two writes fails. Every
        assertion below is therefore scoped to THIS env's own mailboxes rather
        than to the sweep's global return value.
        """
        return await poll_all_mailboxes(
            engine=self.engine,
            admin_engine=self.admin_engine,
            vault=self.vault,
            store=self.store,
            client_factory=factory,
        )

    # -- state -------------------------------------------------------------

    async def ticket_count(self) -> int:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return (
                await session.execute(
                    select(func.count()).select_from(Ticket).where(Ticket.org_id == self.org_id)
                )
            ).scalar_one()

    async def inbound_count(self) -> int:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(InboundMessage)
                    .where(InboundMessage.org_id == self.org_id)
                )
            ).scalar_one()

    def ticket_count_sync(self) -> int:
        """`ticket_count` callable from the worker thread `mark_seen` runs on.

        The poller drives every blocking imaplib call through
        `asyncio.to_thread`, so `mark_seen` executes OFF the event loop while
        the loop itself is idle -- which is exactly what makes
        `run_coroutine_threadsafe` safe here. The bounded `timeout` turns a
        regression that put `mark_seen` back on the loop into a failure
        rather than a hang.
        """
        return asyncio.run_coroutine_threadsafe(self.ticket_count(), self.loop).result(timeout=30)

    async def reload_mailbox(self, mailbox_id: UUID | None = None) -> EmailMailbox:
        async with AsyncSession(self.admin_engine, expire_on_commit=False) as session:
            return (
                await session.execute(
                    select(EmailMailbox).where(EmailMailbox.id == (mailbox_id or self.mailbox_id))
                )
            ).scalar_one()

    async def set_claimed_at(self, when: datetime) -> None:
        await self._update_mailbox(claimed_at=when)

    async def deactivate(self) -> None:
        await self._update_mailbox(active=False)

    # -- helpers -----------------------------------------------------------

    async def _update_mailbox(self, **values: Any) -> None:
        async with AsyncSession(self.admin_engine) as session:
            await session.execute(
                update(EmailMailbox).where(EmailMailbox.id == self.mailbox_id).values(**values)
            )
            await session.commit()

    async def add_mailbox(self, address: str) -> UUID:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=self.org_id,
                address=address,
                host="imap.acme.test",
                port=993,
                username=address,
                credential_id=await _seed_stored_credential(
                    session, self.org_id, self.actor_id, self.vault
                ),
                default_category_id=self.category_id,
                created_by=self.actor_id,
            )
            session.add(mailbox)
            await session.flush()
            return mailbox.id


@pytest.fixture()
async def poll_env(api_env: Any) -> Any:
    with mock_aws():
        store = ObjectStore(api_env.settings)
        vault = get_vault(api_env.settings)
        org_id = await api_env.create_org("email-poller")
        category_id = await _seed_category(api_env, org_id)
        actor_id = await _seed_user(api_env, org_id, "alice@acme.test")

        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=org_id,
                address="helpdesk@acme.test",
                host="imap.acme.test",
                port=993,
                username="helpdesk@acme.test",
                credential_id=await _seed_stored_credential(session, org_id, actor_id, vault),
                default_category_id=category_id,
                created_by=actor_id,
            )
            session.add(mailbox)
            await session.flush()
            mailbox_id = mailbox.id
            mailbox_address = mailbox.address

        yield PollEnv(
            api_env=api_env,
            engine=api_env.app_engine,
            admin_engine=api_env.admin_engine,
            vault=vault,
            store=store,
            org_id=org_id,
            mailbox_id=mailbox_id,
            mailbox_address=mailbox_address,
            actor_id=actor_id,
            category_id=category_id,
            loop=asyncio.get_running_loop(),
        )


async def test_a_polled_message_becomes_a_ticket_and_is_marked_seen(poll_env) -> None:
    client = FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))])
    result = await poll_env.poll(client)
    assert result["processed"] == 1
    assert client.seen == {"1"}
    assert await poll_env.ticket_count() == 1
    assert client.limits == [MAX_MESSAGES_PER_POLL], "the per-poll cap must reach the transport"


async def test_seen_is_set_only_after_the_commit(poll_env) -> None:
    """Spec §4 rule 2. If \\Seen were set first, a crash before the commit
    would lose the message permanently -- unread is recoverable, read is
    not."""
    observed: list[int] = []

    class ObservingClient(FakeImapClient):
        def mark_seen(self, uid):
            # How many tickets exist AT THE MOMENT \Seen is set? If the
            # commit already happened, 1. If \Seen came first, 0.
            observed.append(poll_env.ticket_count_sync())
            super().mark_seen(uid)

    await poll_env.poll(ObservingClient([("1", poll_env.raw(sender="alice@acme.test"))]))
    assert observed == [1], "the ticket must be committed BEFORE \\Seen is set"


async def test_a_redelivered_message_is_marked_seen_and_not_duplicated(poll_env) -> None:
    """Spec §4 rule 3 and §11.2: the unique-violation path is a NORMAL
    outcome. Treating it as a failure is how one stuck message blocks a
    mailbox forever."""
    raw = poll_env.raw(sender="alice@acme.test", message_id="<same@acme.test>")
    await poll_env.poll(FakeImapClient([("1", raw)]))
    # The same message comes back with a new UID -- a crash between the
    # commit and the flag, exactly as §11.2 describes.
    client = FakeImapClient([("2", raw)])
    result = await poll_env.poll(client)
    assert client.seen == {"2"}, "a redelivery must still be flagged, or it repeats forever"
    assert result["already_processed"] == 1
    assert await poll_env.ticket_count() == 1
    assert await poll_env.inbound_count() == 1, "the redelivery must not write a second row"


async def test_one_bad_message_does_not_stop_the_rest(poll_env) -> None:
    """Spec §7, §11.10."""
    client = FakeImapClient(
        [
            ("1", b"\x00\xff garbage"),
            ("2", poll_env.raw(sender="alice@acme.test", message_id="<good@acme.test>")),
        ]
    )
    await poll_env.poll(client)
    assert client.seen == {"1", "2"}
    assert await poll_env.ticket_count() == 1
    assert await poll_env.inbound_count() == 2, "both messages must be recorded, good or not"


# ---------------------------------------------------------------------------
# a refused \Seen is per-message, not per-mailbox
# ---------------------------------------------------------------------------


async def test_a_refused_flag_does_not_stop_the_rest_of_the_batch(poll_env) -> None:
    """REVIEW FINDING F2. A `STORE` that is refused -- a read-only selected
    folder, a quota condition, a shared mailbox that will not take `+FLAGS`
    -- used to abort the whole poll at message one. Message two was never
    processed, and because the refusal is persistent the next poll made the
    same trip and stopped in the same place: zero forward progress, forever,
    while the backlog grew behind it."""
    client = FakeImapClient(
        [
            ("1", poll_env.raw(sender="alice@acme.test", message_id="<one@acme.test>")),
            ("2", poll_env.raw(sender="alice@acme.test", message_id="<two@acme.test>")),
        ],
        fail_on_mark=True,
    )
    result = await poll_env.poll(client)

    assert result["processed"] == 2, "message 2 must be processed despite message 1's STORE"
    assert result["failed"] == 0, "a refused flag is not a mailbox-level failure"
    assert result["flag_failed"] == 2
    assert client.seen == set()
    assert await poll_env.ticket_count() == 2

    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_error == "flag failed on 2 message(s): network"
    assert _PASSWORD not in mailbox.last_error
    assert mailbox.claimed_at is None


async def test_a_mailbox_wedged_by_a_refused_flag_still_makes_progress(poll_env) -> None:
    """The consequence F2 is really about. The STORE keeps failing, so the
    same messages keep being redelivered -- but rule 1 already committed
    their rows, so the redelivery resolves as `AlreadyProcessed` and NEW mail
    behind them is still processed. Nothing is duplicated and nothing is
    stuck."""
    first = poll_env.raw(sender="alice@acme.test", message_id="<one@acme.test>")
    second = poll_env.raw(sender="alice@acme.test", message_id="<two@acme.test>")

    await poll_env.poll(FakeImapClient([("1", first)], fail_on_mark=True))
    # Nothing was flagged, so the server hands both back next interval.
    result = await poll_env.poll(FakeImapClient([("1", first), ("2", second)], fail_on_mark=True))

    assert result["already_processed"] == 1, "the unflagged redelivery is a normal outcome"
    assert result["processed"] == 1, "the new message behind it must still get through"
    assert result["failed"] == 0
    assert await poll_env.ticket_count() == 2
    assert await poll_env.inbound_count() == 2


async def test_a_recovered_flag_clears_the_error(poll_env) -> None:
    """A mailbox that has started working again must stop reporting an error
    it no longer has, or §10's surface fills with stale complaints."""
    raw = poll_env.raw(sender="alice@acme.test", message_id="<one@acme.test>")
    await poll_env.poll(FakeImapClient([("1", raw)], fail_on_mark=True))
    assert (await poll_env.reload_mailbox()).last_error is not None

    client = FakeImapClient([("1", raw)])
    await poll_env.poll(client)
    assert client.seen == {"1"}
    assert (await poll_env.reload_mailbox()).last_error is None


# ---------------------------------------------------------------------------
# last_error says WHERE and WHY, and never says the password
# ---------------------------------------------------------------------------


async def test_a_connection_failure_records_a_sanitized_last_error(poll_env) -> None:
    """Invariant #3 and spec §2.1: `last_error` is a bounded, sanitized
    excerpt -- never a raw server response, never the password. The seeded
    exception embeds `hunter2` the way a real IMAP library embeds a server's
    rejection text."""
    await poll_env.poll_expecting_failure()
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_error
    assert len(mailbox.last_error) <= 500
    assert "hunter2" not in mailbox.last_error
    assert mailbox.claimed_at is None, "a failed poll must release its claim"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ImapError("x", stage="connect", reason="dns"), "connect failed: dns"),
        (
            ImapError("x", stage="tls", reason="certificate-invalid"),
            "tls failed: certificate-invalid",
        ),
        (
            ImapError("x", stage="tls", reason="starttls-unsupported"),
            "tls failed: starttls-unsupported",
        ),
        (ImapError("x", stage="login", reason="rejected"), "login failed: rejected"),
        (ImapError("x", stage="select", reason="rejected"), "select failed: rejected"),
        (socket.gaierror(-2, "Name or service not known"), "connect failed: dns"),
        (ssl.SSLCertVerificationError("bad cert"), "connect failed: certificate-invalid"),
        (ConnectionRefusedError("nope"), "connect failed: connection-refused"),
        (TimeoutError("slow"), "connect failed: timeout"),
    ],
)
async def test_each_failure_stage_records_a_distinguishable_reason(
    poll_env, exc: BaseException, expected: str
) -> None:
    """REVIEW FINDING F3. Every one of these used to record the identical
    string `poll failed: error` -- `type(exc).__name__` for `imaplib`'s
    exceptions is the unqualified `error` -- so an operator looking at §10's
    mailbox list could not tell a wrong password from a missing folder from
    a DNS failure from a refused certificate. The safety property is
    unchanged: this is a CONSTRUCTED classification, never `str(exc)`."""

    def factory(**_: Any) -> FakeImapClient:
        raise exc

    result = await poll_env.poll_with(factory)
    assert result["failed"] == 1
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_error == expected
    assert _PASSWORD not in (mailbox.last_error or "")
    assert mailbox.claimed_at is None


async def test_a_fetch_failure_is_recorded_at_the_fetch_stage(poll_env) -> None:
    """Connecting fine and then failing to read is a different problem from
    failing to connect, and the row must say so."""

    class FetchFails(FakeImapClient):
        def fetch_unseen(self, limit):
            raise ImapError("x", stage="fetch", reason="timeout")

    result = await poll_env.poll(FetchFails([]))
    assert result["failed"] == 1
    assert (await poll_env.reload_mailbox()).last_error == "fetch failed: timeout"


async def test_a_processing_failure_is_recorded_at_the_process_stage(
    poll_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception escaping `process_message` is by construction NOT a bad
    message (the parser refuses in a field rather than raising) -- it is
    infrastructure or a defect, and it is a MAILBOX-level failure."""
    from app.modules.email_intake import poller as poller_module

    async def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("the store is unreachable")

    monkeypatch.setattr(poller_module, "_process_one", boom)
    client = FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))])
    result = await poll_env.poll(client)

    assert result["failed"] == 1
    assert client.seen == set(), "nothing committed, so nothing may be flagged"
    assert (await poll_env.reload_mailbox()).last_error == "process failed: RuntimeError"


# ---------------------------------------------------------------------------
# the per-poll bound
# ---------------------------------------------------------------------------


async def test_a_backlog_larger_than_the_cap_is_drained_across_polls(
    poll_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW FINDING F7 / open risk 1. Without a bound, a first-enrolment
    backlog makes one poll issue one round trip and one transaction per
    message; such a poll routinely outlives `STUCK_POLL_CLAIM_AFTER`, at
    which point the next sweep releases its own still-running claim and a
    second worker starts the same mailbox.

    The cap is monkeypatched down so the test is about the MECHANISM rather
    than about 200 tickets; that the production constant is what actually
    reaches the transport is asserted in
    `test_a_polled_message_becomes_a_ticket_and_is_marked_seen`.
    """
    from app.modules.email_intake import poller as poller_module

    monkeypatch.setattr(poller_module, "MAX_MESSAGES_PER_POLL", 2)
    backlog = [
        (str(n), poll_env.raw(sender="alice@acme.test", message_id=f"<m{n}@acme.test>"))
        for n in range(1, 6)
    ]

    client = FakeImapClient(backlog)
    first = await poll_env.poll(client)
    assert first["processed"] == 2, "one poll must be bounded"
    assert client.limits == [2]

    # The remaining three are still UNSEEN, so the next interval picks them
    # up -- there is no cursor to keep and nothing to resume.
    second = await poll_env.poll(client)
    third = await poll_env.poll(client)
    assert second["processed"] == 2
    assert third["processed"] == 1

    assert client.seen == {"1", "2", "3", "4", "5"}
    assert await poll_env.ticket_count() == 5, "and nothing may be processed twice"
    assert await poll_env.inbound_count() == 5


# ---------------------------------------------------------------------------
# the claim
# ---------------------------------------------------------------------------


async def test_a_failing_mailbox_does_not_stall_the_others(poll_env) -> None:
    """Spec §4: a mailbox that is down must not stall the others.

    Asserts the SPECIFIC mailboxes rather than the sweep's global count,
    which is order-dependent (`poll_all_mailboxes` has no `ORDER BY`) and
    session-shared: if Postgres happened to return the healthy mailbox first,
    a count-based assertion passed even when a failing mailbox stalled
    everything queued behind it.
    """
    broken_username = "broken@acme.test"
    broken_id = await poll_env.add_mailbox(broken_username)
    message = poll_env.raw(sender="alice@acme.test", message_id="<healthy@acme.test>")

    def factory(**kwargs: Any) -> FakeImapClient:
        if kwargs["username"] == broken_username:
            return FakeImapClient([], fail_on_connect=True)
        return FakeImapClient([("1", message)])

    await poll_env.poll_all(factory)

    healthy = await poll_env.reload_mailbox()
    broken = await poll_env.reload_mailbox(broken_id)
    assert healthy.last_polled_at is not None, "the healthy mailbox must have been polled"
    assert healthy.last_error is None
    assert broken.last_error, "the broken mailbox must record why"
    assert _PASSWORD not in broken.last_error
    assert broken.claimed_at is None
    assert await poll_env.ticket_count() == 1


async def test_a_locked_mailbox_row_is_skipped_rather_than_waited_on(poll_env) -> None:
    """`FOR UPDATE SKIP LOCKED`, specifically.

    A claim written with a plain `FOR UPDATE` -- or with no lock at all and a
    `claimed_at` read -- passes a test that merely runs two polls
    concurrently. This one holds a real row lock on the mailbox from an
    outside transaction while the poll runs: `SKIP LOCKED` returns nothing
    immediately, anything else BLOCKS until the lock is released, which the
    timeout turns into a failure rather than a hang.
    """
    async with org_session(
        poll_env.org_id, engine=poll_env.engine, clients=ClientScope.all()
    ) as blocker:
        await blocker.execute(
            select(EmailMailbox)
            .where(
                EmailMailbox.org_id == poll_env.org_id,
                EmailMailbox.id == poll_env.mailbox_id,
            )
            .with_for_update()
        )
        result = await asyncio.wait_for(poll_env.poll(FakeImapClient([])), timeout=20)

    assert result["skipped_claimed"] == 1


async def test_a_poll_holds_its_claim_across_the_imap_conversation(poll_env) -> None:
    """Genuine overlap, not merely interleaved starts.

    The first poll is blocked INSIDE `fetch_unseen` -- claim committed, IMAP
    conversation in flight, which is the whole window the claim exists to
    cover -- while a second poll of the same mailbox runs to completion.
    Exactly one may do the work.
    """
    raw = poll_env.raw(sender="alice@acme.test", message_id="<race@acme.test>")
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeImapClient):
        def fetch_unseen(self, limit):
            entered.set()
            assert release.wait(30), "the second poll never finished"
            return super().fetch_unseen(limit)

    first = asyncio.ensure_future(poll_env.poll(BlockingClient([("1", raw)])))
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        second = await poll_env.poll(FakeImapClient([("1", raw)]))
    finally:
        release.set()
    first_result = await first

    assert second["skipped_claimed"] == 1, "the claim must hold across the IMAP conversation"
    assert first_result["processed"] == 1
    assert await poll_env.ticket_count() == 1


async def test_a_stuck_claim_is_released_and_repolled(poll_env) -> None:
    """A worker that dies mid-poll leaves claimed_at set forever, and the
    mailbox is never polled again -- silently."""
    from app.modules.email_intake.poller import STUCK_POLL_CLAIM_AFTER

    await poll_env.set_claimed_at(datetime.now(UTC) - STUCK_POLL_CLAIM_AFTER * 2)
    client = FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))])
    result = await poll_env.poll(client)
    assert result["processed"] == 1


async def test_an_inactive_mailbox_is_not_polled(poll_env) -> None:
    """Scoped to THIS env's mailbox: the sweep's global return value counts
    every org in a session-shared database, so it cannot be the assertion."""
    await poll_env.deactivate()
    await poll_env.poll_all(lambda **_: FakeImapClient([]))
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_polled_at is None, "an inactive mailbox must not be polled at all"
    assert mailbox.claimed_at is None


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------


async def test_the_credential_checkout_is_audited_and_outside_the_claim(poll_env) -> None:
    """Invariant #4: `VaultService.checkout` does not itself audit, so the
    caller must. REVIEW FINDING F6: it happens in its OWN transaction, after
    the claim has committed -- the production vault is `OpenBaoVaultService`,
    whose unwrap is a 10-second-timeout HTTP round trip, and running that
    inside the claim holds a pooled connection and a `FOR UPDATE` row lock
    across a third party's network."""
    from app.models import AuditEvent

    claimed_during_checkout: list[Any] = []

    class WatchingVault:
        def __init__(self, inner: VaultService) -> None:
            self._inner = inner

        async def checkout(self, session, *, org_id, credential_id):
            # Read the mailbox row on a DIFFERENT connection. If the claim
            # transaction were still open this would block until it timed
            # out; that it returns the COMMITTED `claimed_at` is the proof
            # the claim is already done.
            async with AsyncSession(poll_env.admin_engine) as probe:
                claimed_during_checkout.append(
                    (
                        await probe.execute(
                            select(EmailMailbox.claimed_at).where(
                                EmailMailbox.id == poll_env.mailbox_id
                            )
                        )
                    ).scalar_one()
                )
            return await self._inner.checkout(session, org_id=org_id, credential_id=credential_id)

    result = await poll_mailbox(
        poll_env.org_id,
        poll_env.mailbox_id,
        engine=poll_env.engine,
        vault=WatchingVault(poll_env.vault),
        store=poll_env.store,
        client_factory=lambda **_: FakeImapClient([("1", poll_env.raw(sender="alice@acme.test"))]),
    )
    assert result["processed"] == 1
    assert claimed_during_checkout and claimed_during_checkout[0] is not None, (
        "the claim must be committed before the vault round trip starts"
    )

    async with org_session(
        poll_env.org_id, engine=poll_env.engine, clients=ClientScope.all()
    ) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == poll_env.org_id,
                        AuditEvent.action == "email_intake.credential_used",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert event.target_type == "credential"
    assert event.meta == {"mailbox_id": str(poll_env.mailbox_id)}
    assert _PASSWORD not in str(event.meta)


async def test_a_credential_failure_is_recorded_and_releases_the_claim(poll_env) -> None:
    """The checkout moved out of the claim transaction, so it needs its own
    place in the failure taxonomy -- and it must still release the claim."""

    class BrokenVault:
        async def checkout(self, session, *, org_id, credential_id):
            raise RuntimeError("openbao is unreachable")

    result = await poll_mailbox(
        poll_env.org_id,
        poll_env.mailbox_id,
        engine=poll_env.engine,
        vault=BrokenVault(),
        store=poll_env.store,
        client_factory=lambda **_: FakeImapClient([]),
    )
    assert result["failed"] == 1
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_error == "credential failed: RuntimeError"
    assert mailbox.claimed_at is None


# ---------------------------------------------------------------------------
# `test_mailbox` -- spec §10's connection test
# ---------------------------------------------------------------------------


class RecordingImapClient:
    """A client that records what the connection test asks of it.

    Separate from `FakeImapClient` on purpose: this one asserts that
    `mark_seen` is NEVER called, so it must raise rather than quietly record
    if it ever is.
    """

    def __init__(self) -> None:
        self.limits: list[int] = []
        self.closed = 0

    def fetch_unseen(self, limit: int) -> list[tuple[str, bytes]]:
        self.limits.append(limit)
        # `uids[:0]` on the real client -- the SEARCH happens, no FETCH does.
        return []

    def mark_seen(self, uid: str) -> None:
        raise AssertionError("the connection test must never touch a message's flags")

    def close(self) -> None:
        self.closed += 1


async def test_the_connection_test_succeeds_without_reading_any_mail(poll_env) -> None:
    """The SUCCESS path of `test_mailbox`, which nothing exercised: every
    other test of this route goes through a refused `127.0.0.1:1` connection,
    so `ok=True`, the detail string, the `close()` in the `finally` and --
    the load-bearing one -- `fetch_unseen(0)` were all unpinned.

    `fetch_unseen(0)` is what makes this a folder LISTING rather than a mail
    read: it issues the `UID SEARCH UNSEEN` that proves SELECT and the
    credential work, and `uids[:0]` means no `UID FETCH` follows. A future
    `ImapClient` reading `limit=0` as "unlimited" would start downloading a
    customer's mail to test a connection, and before this nothing went red.
    """
    from app.modules.email_intake.poller import TEST_CONVERSATION_DEADLINE_S, test_mailbox

    client = RecordingImapClient()
    seen_kwargs: dict[str, Any] = {}

    def factory(**kwargs: Any) -> RecordingImapClient:
        seen_kwargs.update(kwargs)
        return client

    result = await test_mailbox(
        poll_env.org_id,
        poll_env.mailbox_id,
        actor_id=poll_env.actor_id,
        engine=poll_env.engine,
        vault=poll_env.vault,
        client_factory=factory,
    )

    assert result.ok is True
    assert result.detail == "connected to imap.acme.test:993 and listed 'INBOX'"
    assert len(result.detail) <= 200
    assert client.limits == [0], "the test must LIST the folder, never fetch a message"
    assert client.closed == 1, "the connection is released in a `finally`"

    # The transport is handed the mailbox's own configuration and a WALL-CLOCK
    # budget: this route runs on the API process, where an unbounded
    # conversation holds a thread `asyncio.to_thread` cannot cancel.
    assert seen_kwargs["host"] == "imap.acme.test"
    assert seen_kwargs["port"] == 993
    assert seen_kwargs["folder"] == "INBOX"
    assert seen_kwargs["password"] == _PASSWORD
    assert seen_kwargs["deadline_s"] == TEST_CONVERSATION_DEADLINE_S


async def test_the_connection_test_audits_the_operator_who_ran_it(poll_env) -> None:
    """Invariant #4: "which admin decrypted this mailbox password" is exactly
    what the hash-chained log exists to answer, so the operator-triggered
    checkout is recorded as a USER action -- distinguishable from the
    scheduled poll's `system` one -- and the test itself is recorded too.
    Neither event carries the password."""
    from app.models import AuditEvent
    from app.modules.email_intake.poller import test_mailbox

    await test_mailbox(
        poll_env.org_id,
        poll_env.mailbox_id,
        actor_id=poll_env.actor_id,
        engine=poll_env.engine,
        vault=poll_env.vault,
        client_factory=lambda **_: RecordingImapClient(),
    )

    async with org_session(
        poll_env.org_id, engine=poll_env.engine, clients=ClientScope.all()
    ) as session:
        events = {
            event.action: event
            for event in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == poll_env.org_id)
                )
            )
            .scalars()
            .all()
        }
    assert set(events) == {"email_intake.credential_used", "email_intake.mailbox_tested"}
    checkout = events["email_intake.credential_used"]
    assert checkout.actor_type == "user"
    assert checkout.actor_id == poll_env.actor_id
    tested = events["email_intake.mailbox_tested"]
    assert tested.meta["ok"] is True
    assert _PASSWORD not in str(tested.meta) and _PASSWORD not in str(checkout.meta)

    # ...and the manual test does NOT overwrite the scheduled poll's health.
    mailbox = await poll_env.reload_mailbox()
    assert mailbox.last_polled_at is None
    assert mailbox.claimed_at is None


# ---------------------------------------------------------------------------
# the beat wiring (review finding F8)
# ---------------------------------------------------------------------------


async def test_email_intake_poll_all_orgs_drives_the_poller(poll_env, monkeypatch) -> None:
    """Nothing tested the worker shell, so a rename inside it would break
    polling in production with the whole suite green."""
    from app import worker
    from app.modules.email_intake import poller as poller_module

    captured: dict[str, Any] = {}

    async def fake_poll_all_mailboxes(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(poller_module, "poll_all_mailboxes", fake_poll_all_mailboxes)
    with override_settings(poll_env.api_env.settings):
        assert await worker.email_intake_poll_all_orgs() == 7
    assert set(captured) == {"engine", "admin_engine", "vault", "store"}, (
        "production must NOT pass client_factory -- it takes the real transport"
    )


def test_create_celery_registers_the_email_intake_poll(pg_cluster, tmp_path: Path) -> None:
    """The beat entry's task name and the `@celery_app.task(name=...)` string
    are two strings in two places that must match exactly; nothing checked
    that they do, so a typo would stop mail intake silently."""
    from app import worker

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    with override_settings(settings):
        celery = worker.create_celery()

    entry = celery.conf.beat_schedule["email-intake-poll"]
    assert entry["task"] == "app.worker.email_intake_poll"
    assert entry["schedule"] == 120.0

    # `worker.celery_app` is None in a test process (the module-level build
    # needs settings that are not configured at import time), so the registry
    # itself cannot be read here. The two strings that must agree are the
    # beat entry's `task` and the `@celery_app.task(name=...)` decorator, and
    # those are checked directly -- for EVERY beat entry, since a rename in
    # any of them fails the same silent way.
    source = inspect.getsource(worker)
    for schedule_name, scheduled in celery.conf.beat_schedule.items():
        assert f'@celery_app.task(name="{scheduled["task"]}")' in source, (
            f"beat entry {schedule_name!r} names a task no decorator registers"
        )
    assert inspect.iscoroutinefunction(worker.email_intake_poll_all_orgs)

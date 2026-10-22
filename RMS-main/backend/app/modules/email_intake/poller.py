"""The poller: claim a mailbox, fetch, process, flag (spec §4).

THE ORDER OF THREE OPERATIONS IS THE WHOLE MODULE. IMAP has no
transactions: the fetch, the database write and the `\\Seen` flag are three
operations across two systems, and a crash between the second and the third
re-delivers a message that was already processed. Three rules together make
that safe, and every one of them is load-bearing:

1. **The `inbound_message` row is written FIRST**, in the same transaction
   as the ticket or comment it produces. `intake.process_message` owns this;
   this module only guarantees it runs inside ONE transaction per message.
2. **`\\Seen` is set only AFTER that transaction commits** -- here, after
   the `org_session` block has exited, never inside it. A crash before the
   flag leaves the message unread and the row present, so the next poll
   collides on `uq_inbound_message_org_message_id` and flags it. Unread is
   recoverable; read is not, and no amount of later cleverness recovers a
   message the server believes was delivered.
3. **`AlreadyProcessed` is a NORMAL outcome.** It is counted, the message is
   STILL marked `\\Seen`, and the loop continues. Treating it as a failure
   is how one stuck message blocks a mailbox forever.

A FAILED `\\Seen` IS PER-MESSAGE, NOT PER-MAILBOX -- and rule 1 is what
makes that safe. If the STORE is refused (a read-only selected folder, a
quota condition, a shared mailbox that will not take `+FLAGS`) the row is
ALREADY COMMITTED, so the inevitable redelivery lands on
`uq_inbound_message_org_message_id` and resolves as `AlreadyProcessed`: a
duplicate flag attempt is wasted work, never a duplicate ticket. Aborting the
batch instead is what wedges a mailbox forever -- every later poll re-fetches
the same first message, fails the same STORE, and never reaches message two,
while the backlog grows behind it and `last_error` never changes. So a flag
failure is counted (`flag_failed`), surfaced in `last_error`, and the loop
CONTINUES.

CLAIM IN ONE SHORT TRANSACTION; TALK TO THE NETWORK OUTSIDE IT. The claim
(release stuck claims, then `SELECT ... FOR UPDATE SKIP LOCKED` on an active,
unclaimed mailbox, stamp `claimed_at`, commit) is database-only and finishes
immediately. EVERYTHING that touches a third party's network -- the vault
checkout, which against `OpenBaoVaultService` is a 10-second-timeout HTTP
round trip to OpenBao, and the IMAP conversation, which can take up to
`IMAP_TIMEOUT_S` per call -- happens in its own transaction or none at all,
AFTER the claim's `FOR UPDATE` row lock has been released. Holding a database
connection open for the length of someone else's network timeout is how a
handful of slow mailboxes exhausts the pool and takes down the control plane;
this is the same split `worker.py::deliver_pending` makes, for the same
reason.

`claimed_at` IS CLEARED ON THE FAILURE PATH TOO. A mailbox whose poll throws
must not stay claimed: `STUCK_POLL_CLAIM_AFTER` would eventually return it,
but "eventually" is ten minutes of a mailbox nobody is reading, and the
failure is already known here. The release is in a `finally`.

`last_error` NEVER CONTAINS A RAW SERVER RESPONSE OR A CREDENTIAL
(invariant #3, spec §11.12). Only the failing STAGE and a CLASSIFICATION are
recorded -- never `str(exc)`, because an IMAP library routinely embeds the
login it was rejected on ("authentication failed for user X") and a
badly-behaved server can put anything at all in a response line. Both halves
come from `imap.py`'s fixed vocabularies, so the string is constructed, not
quoted: `tls failed: certificate-invalid`, `login failed: rejected`,
`connect failed: dns`, `select failed: rejected`, `fetch failed: timeout`.
This mirrors `notification/delivery.py`, which likewise records constructed
strings (`f"smtp {code} rejected"`) rather than exception text, and reuses
its `_sanitize_excerpt` shape for the bound and the control-character strip.

MESSAGE-LEVEL AND MAILBOX-LEVEL FAILURES ARE DIFFERENT. A bad MESSAGE never
reaches this module as an exception: `parse_message` refuses in a field
rather than raising, and `process_message` records the misconfiguration
cases (a queue-less default category, an unresolvable sender) on the row.
So an exception escaping `process_message` is by construction NOT a bad
message -- it is infrastructure or a defect -- and it is handled at the
MAILBOX level: the poll stops, `last_error` names the stage, the claim is
released, and the sweep moves on to the next mailbox. Deliberately NOT
"count it and continue": nothing was committed, so the message cannot be
flagged `\\Seen`, and continuing would re-fetch and re-fail it every
interval while the mailbox still reported healthy. Stopping puts it in
`last_error`, where §10's admin UI shows it.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked, use_locale
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import Organization
from app.modules.email_intake import tokens
from app.modules.email_intake.errors import MailboxNotFoundError
from app.modules.email_intake.imap import (
    ImapClient,
    ImapClientFactory,
    failure_reason,
    failure_stage,
    imap_client_factory,
)
from app.modules.email_intake.intake import AlreadyProcessed, process_message
from app.modules.email_intake.models import EmailMailbox
from app.modules.email_intake.parsing import parse_message
from app.modules.email_intake.schema import MAX_TEST_DETAIL, MailboxTestResult
from app.modules.email_intake.tokens import TokenRevoked, TokenTransportError

logger = logging.getLogger("its.email-intake")

# A worker that dies mid-poll leaves `claimed_at` set and the mailbox is
# never polled again -- silently, which is the worst shape a mail failure can
# take. Same threshold as `worker.py`'s `STUCK_DISPATCH_AFTER` and
# `STUCK_NOTIFICATION_CLAIM_AFTER`, deliberately: it is the same failure
# (a dead worker holding a claim) and one number is easier to reason about
# than three.
STUCK_POLL_CLAIM_AFTER = timedelta(minutes=10)

# ONE POLL IS BOUNDED. Without a cap, enrolling a mailbox that already has a
# five-figure unread backlog -- the normal first-enrolment case, and exactly
# when the backlog is largest -- makes one poll issue one `UID FETCH` round
# trip per message plus one transaction each. That poll routinely outlives
# `STUCK_POLL_CLAIM_AFTER`, at which point the NEXT sweep releases the still
# running poll's own claim and a second worker starts the same mailbox. Rule
# 1 keeps that correct (the overlap resolves to `AlreadyProcessed`), so the
# damage is wasted work and a mailbox that may never converge rather than
# duplicate tickets -- but it is unbounded wasted work, and it holds a worker
# slot the whole time.
#
# 200 is chosen against the 120s beat and the 10-minute claim threshold: even
# at a pessimistic 1s per message (round trip plus transaction) a full batch
# is ~200s, comfortably inside the threshold with room for a slow server,
# while sustaining ~100 messages/minute of drain. A 5,000-message backlog
# converges in ~25 polls (~50 minutes) with every poll bounded, and the
# remainder is picked up by the next interval because it is still UNSEEN --
# no cursor to keep, nothing to resume.
MAX_MESSAGES_PER_POLL = 200

# THE OPERATOR-TRIGGERED TEST RUNS ON THE API PROCESS, SO IT IS DEADLINED.
# `poll_mailbox` runs on the worker and may legitimately spend minutes
# draining a backlog; `test_mailbox` is one connect, one LOGIN, one SELECT
# and one UID SEARCH, behind a rate-limited admin route, on the process that
# also serves the agent gateway and `egress.py`'s resolver out of the same
# default executor. `IMAP_TIMEOUT_S` bounds none of that -- it is per socket
# operation and restarts on every byte -- so this bounds it twice over:
#
# * `TEST_CONVERSATION_DEADLINE_S` is handed to the transport as a
#   wall-clock budget for the whole session and is enforced by CLOSING THE
#   SOCKET, which is the only thing that ends a thread `asyncio.to_thread`
#   cannot cancel. It is the bound on THREAD occupancy.
# * `TEST_REQUEST_DEADLINE_S` bounds what the REQUEST waits for. It is the
#   larger of the two on purpose: the socket watchdog should be what fires,
#   because it produces a real classified failure the operator can act on
#   (`fetch failed: network`) rather than the bare `timeout` this produces.
#   It is the backstop for the one thing the watchdog cannot reach -- name
#   resolution, which happens before any socket exists.
#
# 15s is chosen to be generous for a working mailbox (a healthy IMAP
# handshake plus SEARCH is well under a second, and a slow on-prem box under
# load is still a small number of seconds) while keeping a hostile server's
# hold on an API-process thread short enough that the 5-per-minute rate
# limit cannot accumulate them: 5/min x 15s is at most ~1.25 threads in
# flight per org, against a default executor of `min(32, cpu+4)`.
TEST_CONVERSATION_DEADLINE_S = 15.0
TEST_REQUEST_DEADLINE_S = 20.0

# `email_mailbox.last_error` is `String(500)`.
_EXCERPT_MAX = 500


def _sanitize_excerpt(raw: str) -> str:
    """Bounded and control-character-stripped, copying
    `notification/delivery.py::_sanitize_excerpt`.

    THE BOUND IS NOT THE PROTECTION. Callers in this module only ever pass
    strings this module itself constructed (a stage name and a classification
    drawn from `imap.py`'s fixed vocabulary); this function is what guarantees
    the column width and that nothing smuggles a terminal escape into an
    operator's console, not what keeps the password out. That is the call
    sites' job, and every one of them is written to it.
    """
    return "".join(ch for ch in raw if ch.isprintable())[:_EXCERPT_MAX]


def _failure_excerpt(stage: str, exc: BaseException) -> str:
    """What a failed poll records: the STAGE and a CLASSIFICATION.

    Never `str(exc)`. An IMAP failure's message routinely quotes the
    username it was refused for and can quote anything at all the server
    sent back (invariant #3, spec §11.12).

    `stage` is this module's coarse guess -- which `await` was in flight --
    and an `ImapError` overrides it with what the transport actually knows:
    the factory is one `await` here but `connect`, `tls`, `login` and
    `select` to the operator who has to fix it. Those are four different
    problems with four different owners, and before this they all recorded
    the identical string `poll failed: error`.
    """
    return _sanitize_excerpt(f"{failure_stage(exc, stage)} failed: {failure_reason(exc)}")


#: What `last_error` says when Microsoft refuses the stored sign-in. Two
#: messages because two different people fix them: the mailbox's owner
#: reconnects; an administrator fixes the Azure application.
REVOKED_MESSAGES = {
    "consent": N_("Microsoft sign-in is no longer valid — reconnect this mailbox"),
    "application": N_(
        "Microsoft refused this organisation's application — check Email Intake → "
        "Microsoft connection (reconnecting the mailbox will not fix this)"
    ),
}


def _describe(stage: str, exc: BaseException) -> str:
    """`_failure_excerpt`, plus the Microsoft sign-in's own fixed vocabulary.

    The sentences are worded in the current language, so this is for the one
    caller that answers a person (`test_mailbox`'s response). The stage and
    reason tokens stay as recorded, like `last_error`, which the console
    prints as written. Whatever is STORED goes through `_describe_stored`."""
    if isinstance(exc, TokenRevoked):
        return _sanitize_excerpt(tr_marked(REVOKED_MESSAGES[exc.cause]))
    if isinstance(exc, TokenTransportError):
        return _sanitize_excerpt(f"sign-in failed: {exc.reason}")
    if isinstance(exc, MailboxNotConnected):
        return tr("sign-in failed: not connected to Microsoft yet")
    return _failure_excerpt(stage, exc)


def _describe_stored(stage: str, exc: BaseException) -> str:
    """`_describe` in English, for `last_error` and the audit chain: what is
    stored is never translated."""
    with use_locale("en"):
        return _describe(stage, exc)


def _flag_failure_excerpt(count: int, exc: BaseException) -> str:
    """A per-message `\\Seen` failure, which does NOT fail the poll (module
    docstring). Says how many, because one refused STORE is a hiccup and
    "every message in the batch" is a read-only folder."""
    return _sanitize_excerpt(f"flag failed on {count} message(s): {failure_reason(exc)}")


class MailboxNotConnected(Exception):
    """The mailbox has no credential to present (a Microsoft mailbox nobody
    has connected yet)."""


class _MailboxPlan:
    """The mailbox fields the IMAP conversation needs, snapshotted inside the
    claim transaction so nothing outside it touches an ORM object bound to a
    closed session.

    `password` is filled in AFTER the claim commits (see
    `_checkout_credential`), lives for exactly as long as it takes to build
    the connection, and is dropped by `poll_mailbox` immediately afterwards;
    it is never written to the mailbox row, an audit `metadata` dict, a log
    line (this module calls no logger with a mailbox secret in scope) or
    `last_error`.
    """

    __slots__ = (
        "address",
        "host",
        "port",
        "use_ssl",
        "folder",
        "username",
        "credential_id",
        "password",
        "auth_kind",
        # Fetched per poll, never stored, cleared with the password.
        "access_token",
    )

    def __init__(self, mailbox: EmailMailbox) -> None:
        self.address = mailbox.address
        self.host = mailbox.host
        self.port = mailbox.port
        self.use_ssl = mailbox.use_ssl
        self.folder = mailbox.folder
        self.username = mailbox.username
        self.credential_id = mailbox.credential_id
        self.password = ""
        self.auth_kind = mailbox.auth_kind
        self.access_token = ""


async def _claim(
    org_id: UUID,
    mailbox_id: UUID,
    *,
    engine: AsyncEngine,
) -> _MailboxPlan | None:
    """Claim the mailbox in ONE short DATABASE-ONLY transaction. `None` means
    another worker holds it, or it is inactive, or it is gone.

    Database-only is meant literally: no vault call, no HTTP, nothing whose
    latency belongs to a third party happens between the `FOR UPDATE` and the
    commit. The credential checkout used to live here, which put an
    `OpenBaoVaultService` round trip (10s timeout) inside the row lock and
    reintroduced exactly the pool-exhaustion risk this split exists to avoid;
    it is now `_checkout_credential`, run after this returns.

    Stuck claims are released FIRST and in the SAME transaction as the claim
    that follows, so a mailbox this call just released is immediately
    eligible for the claim query below rather than waiting a whole extra
    interval (`worker.py::deliver_pending` does the same).

    `SELECT ... FOR UPDATE SKIP LOCKED` is what makes two workers polling the
    same mailbox concurrently impossible: the loser's SELECT skips the locked
    row and returns nothing rather than blocking on it.
    """
    now = datetime.now(UTC)
    async with org_session(org_id, engine=engine) as session:
        await session.execute(
            update(EmailMailbox)
            .where(
                EmailMailbox.org_id == org_id,
                EmailMailbox.claimed_at.is_not(None),
                EmailMailbox.claimed_at < now - STUCK_POLL_CLAIM_AFTER,
            )
            .values(claimed_at=None)
        )

        mailbox = (
            await session.execute(
                select(EmailMailbox)
                .where(
                    EmailMailbox.org_id == org_id,
                    EmailMailbox.id == mailbox_id,
                    EmailMailbox.active.is_(True),
                    EmailMailbox.claimed_at.is_(None),
                    # A mailbox waiting for a person to reconnect it is not
                    # polled: retrying a dead sign-in is noise, not resilience.
                    EmailMailbox.credential_state == "ok",
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if mailbox is None:
            return None

        mailbox.claimed_at = now
        session.add(mailbox)
        return _MailboxPlan(mailbox)


async def _checkout_credential(
    org_id: UUID,
    mailbox_id: UUID,
    credential_id: UUID | None,
    *,
    engine: AsyncEngine,
    vault: VaultService,
    actor_id: UUID | None = None,
) -> str:
    """Decrypt the mailbox password at point of use, and AUDIT the checkout.

    Its own transaction, deliberately outside the claim (see `_claim`): the
    production vault is `OpenBaoVaultService`, whose `_unwrap_dek` is an
    `httpx` POST with a 10-second timeout, and that must not run while a
    `FOR UPDATE` lock is held on the mailbox row.

    `VaultService.checkout` does not itself write an audit event (see
    `notification/delivery.py` and `remote/service.py`, which carry the
    identical note) -- the caller must, and invariant #4 makes credential
    access one of the privileged actions the hash-chained log has to contain.
    The event is written in the SAME transaction as the checkout, so it
    cannot be absent for a checkout that happened. `actor_type="system"` with
    no actor for a scheduled poll: no human triggered it. `actor_id` is
    supplied only by `test_mailbox`, where a named operator DID -- and
    "which admin decrypted this mailbox password" is exactly what invariant
    #4 exists to answer.
    """
    if credential_id is None:
        # Unreachable for a claimed mailbox (`ck_email_mailbox_credential`), but
        # a manual test of an unconnected one lands here.
        raise MailboxNotConnected(  # i18n-exempt: _describe words it, str(exc) unused
            "this mailbox has no saved sign-in yet"
        )
    async with org_session(org_id, engine=engine) as session:
        password = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system" if actor_id is None else "user",
            actor_id=actor_id,
            action="email_intake.credential_used",
            target_type="credential",
            target_id=str(credential_id),
            # The MAILBOX, never the secret and never the address's password.
            metadata={"mailbox_id": str(mailbox_id)},
        )
    return password


async def _mark_needs_reconsent(
    org_id: UUID, mailbox_id: UUID, cause: str, *, engine: AsyncEngine
) -> None:
    """A refused Microsoft sign-in is a STATE: the console offers Reconnect and
    the claim filter stops polling. The claim is released here too."""
    async with org_session(org_id, engine=engine) as session:
        await session.execute(
            update(EmailMailbox)
            .where(EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id)
            .values(
                credential_state="needs_reconsent",
                last_error=_sanitize_excerpt(REVOKED_MESSAGES[cause]),
                last_polled_at=datetime.now(UTC),
                claimed_at=None,
            )
        )
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="email_intake.oauth_revoked",
            target_type="email_mailbox",
            target_id=str(mailbox_id),
            metadata={"cause": cause},
        )


async def _sign_in(
    plan: _MailboxPlan,
    org_id: UUID,
    mailbox_id: UUID,
    *,
    engine: AsyncEngine,
    vault: VaultService,
    actor_id: UUID | None = None,
) -> tuple[str, str | None]:
    """`(password, access_token)` for the client factory: a password, or a
    fresh Microsoft access token and no password at all."""
    if plan.auth_kind == "microsoft_oauth":
        if plan.credential_id is None:
            raise MailboxNotConnected(  # i18n-exempt: _describe words it, str(exc) unused
                "this mailbox is not connected to Microsoft yet"
            )
        plan.access_token = await tokens.access_token_for(
            org_id, mailbox_id, engine=engine, vault=vault, actor_id=actor_id
        )
        return "", plan.access_token
    plan.password = await _checkout_credential(
        org_id, mailbox_id, plan.credential_id, engine=engine, vault=vault, actor_id=actor_id
    )
    return plan.password, None


async def _release(
    org_id: UUID,
    mailbox_id: UUID,
    *,
    engine: AsyncEngine,
    last_error: str | None,
) -> None:
    """Stamp `last_polled_at`, record (or CLEAR) `last_error`, and drop the
    claim -- on the success path and the failure path alike.

    `last_error=None` clears a previous failure: a mailbox that has started
    working again must stop reporting an error it no longer has, or §10's
    surface degrades into a list of stale complaints nobody reads.
    """
    async with org_session(org_id, engine=engine) as session:
        await session.execute(
            update(EmailMailbox)
            .where(EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id)
            .values(
                last_polled_at=datetime.now(UTC),
                last_error=last_error,
                claimed_at=None,
            )
        )


async def _process_one(
    org_id: UUID,
    mailbox_id: UUID,
    raw: bytes,
    *,
    engine: AsyncEngine,
    store: ObjectStore,
    mailbox_address: str,
) -> str:
    """One message, one transaction. Returns `"processed"` or
    `"already_processed"`.

    THE TRANSACTION MUST BE CLOSED BEFORE THE CALLER FLAGS `\\Seen` (spec §4
    rule 2), which is why this returns rather than flagging: the commit
    happens when the `org_session` block below exits, and `mark_seen` is the
    caller's next statement, outside it.

    The mailbox row is re-read in THIS session rather than carried in from
    the claim transaction: `process_message` reads `default_category_id` and
    `reopen_window_days` off it, and a detached instance from a closed
    session is a lazy-load error waiting for the first attribute the ORM
    happens to have expired.
    """
    parsed = parse_message(raw, mailbox_address=mailbox_address)
    try:
        async with org_session(org_id, engine=engine) as session:
            mailbox = (
                await session.execute(
                    select(EmailMailbox).where(
                        EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
                    )
                )
            ).scalar_one()
            await process_message(session, org_id, mailbox, parsed, store=store)
    except AlreadyProcessed:
        # Spec §4 rule 3: a NORMAL outcome. The transaction rolled back
        # having written nothing (the row was already there from the first
        # delivery), and the caller still flags the message.
        return "already_processed"
    return "processed"


async def poll_mailbox(
    org_id: UUID,
    mailbox_id: UUID,
    *,
    engine: AsyncEngine,
    vault: VaultService,
    store: ObjectStore,
    client_factory: ImapClientFactory = imap_client_factory,
) -> dict[str, int]:
    """Poll one mailbox once. Never raises: a mailbox-level failure is
    recorded on the row and reported in the returned counters, because one
    mailbox being down must not stall the sweep.

    Counters: `processed`, `already_processed`, `skipped_claimed` (1 when
    another worker holds the claim, or the mailbox is inactive), `failed`
    (1 when the poll aborted with a mailbox-level failure), `flag_failed`
    (messages that were committed but whose `\\Seen` STORE was refused --
    NOT a mailbox-level failure, see the module docstring).

    At most `MAX_MESSAGES_PER_POLL` messages are handled; the rest stay
    UNSEEN and are picked up by the next interval.
    """
    result = {
        "processed": 0,
        "already_processed": 0,
        "skipped_claimed": 0,
        "failed": 0,
        "flag_failed": 0,
    }

    plan = await _claim(org_id, mailbox_id, engine=engine)
    if plan is None:
        result["skipped_claimed"] = 1
        return result

    last_error: str | None = None
    flag_error: str | None = None
    revoked: str | None = None
    # The `await` currently in flight, so a failure can say WHERE. An
    # `ImapError` refines it (`_failure_excerpt`); this is the fallback for
    # everything else, including an injected test transport.
    stage = "credential"
    try:
        # EVERY imaplib call goes through `asyncio.to_thread`, including the
        # factory -- it connects, logs in and SELECTs, all blocking. This
        # process also serves the agent gateway's WebSockets on this event
        # loop; an inline blocking call stalls every connected endpoint for
        # the length of someone else's network timeout.
        try:
            password, access_token = await _sign_in(
                plan, org_id, mailbox_id, engine=engine, vault=vault
            )
            stage = "connect"
            client: ImapClient = await asyncio.to_thread(
                client_factory,
                host=plan.host,
                port=plan.port,
                use_ssl=plan.use_ssl,
                folder=plan.folder,
                username=plan.username,
                password=password,
                access_token=access_token,
            )
        finally:
            # Held for one connection and no longer (invariant #3).
            plan.password = plan.access_token = password = ""
            access_token = None

        try:
            stage = "fetch"
            messages = await asyncio.to_thread(client.fetch_unseen, MAX_MESSAGES_PER_POLL)
            for uid, raw in messages:
                stage = "process"
                outcome = await _process_one(
                    org_id,
                    mailbox_id,
                    raw,
                    engine=engine,
                    store=store,
                    mailbox_address=plan.address,
                )
                result[outcome] += 1
                # --- SPEC §4 RULE 2. ---------------------------------------
                # The transaction inside `_process_one` has COMMITTED by the
                # time this line runs, and only now is the message flagged.
                # Moving this above the commit is the one regression in this
                # module that no other assertion in the suite would catch --
                # `test_seen_is_set_only_after_the_commit` exists for exactly
                # that, and observes the committed ticket count from inside
                # `mark_seen`.
                try:
                    await asyncio.to_thread(client.mark_seen, uid)
                except Exception as exc:  # noqa: BLE001
                    # Per-message, never per-mailbox: the row is committed, so
                    # the redelivery this causes is an `AlreadyProcessed`, and
                    # aborting here instead would wedge the mailbox forever
                    # (module docstring).
                    result["flag_failed"] += 1
                    flag_error = _flag_failure_excerpt(result["flag_failed"], exc)
        finally:
            await asyncio.to_thread(client.close)
    except TokenRevoked as exc:
        # Not retried: `_mark_needs_reconsent` below takes it out of the claim.
        result["failed"] = 1
        revoked = exc.cause
    except Exception as exc:  # noqa: BLE001
        # Mailbox-level (module docstring): the credential checkout, connect,
        # TLS, login, select, fetch, or an exception escaping
        # `process_message`. Recorded as a stage and a classification --
        # never the exception's text, which can carry the credential.
        result["failed"] = 1
        last_error = _describe_stored(stage, exc)
    else:
        # The poll itself succeeded. A refused STORE is still worth showing
        # in §10's surface -- it is the signature of a read-only folder --
        # but it did not stop the mailbox, so it is not `failed`.
        last_error = flag_error
    finally:
        if revoked is not None:
            await _mark_needs_reconsent(org_id, mailbox_id, revoked, engine=engine)
        else:
            await _release(org_id, mailbox_id, engine=engine, last_error=last_error)

    return result


async def test_mailbox(
    org_id: UUID,
    mailbox_id: UUID,
    *,
    actor_id: UUID,
    engine: AsyncEngine,
    vault: VaultService,
    client_factory: ImapClientFactory = imap_client_factory,
) -> MailboxTestResult:
    """Validate a mailbox's configuration by CONNECTING AND LISTING THE
    FOLDER (spec §10). Raises `MailboxNotFoundError` if the mailbox is not
    this org's; every other failure is a `MailboxTestResult(ok=False)`, never
    an exception -- "your credentials are wrong" is an answer, not a 500.

    IT LIVES HERE, NEXT TO `poll_mailbox`, BECAUSE IT IS A DEGENERATE POLL.
    It reuses this module's credential checkout, its `asyncio.to_thread`
    discipline and -- the point -- its failure classification, so a test and
    a real poll report a broken mailbox with the SAME words. A second,
    route-local IMAP conversation would drift from the one that matters.

    FOUR DELIBERATE DIFFERENCES FROM `poll_mailbox`:

    * **It does not claim the mailbox.** A claim is mutual exclusion for
      WRITERS; an operator pressing "test" must not be blocked by (or block)
      the scheduled poll, and this writes nothing a concurrent poll could
      race with.
    * **It does not touch `last_polled_at`/`last_error`.** Those describe the
      scheduled poll's health. A manual test overwriting them would erase the
      failure the operator is trying to diagnose, and a successful test would
      make a still-broken mailbox look healthy on §10's list.
    * **It reads no message.** `fetch_unseen(0)` issues the `UID SEARCH
      UNSEEN` -- which is the folder listing, and the operation that proves
      SELECT and the credential really work -- and returns before any `UID
      FETCH`. Pulling a message body to test a connection would be gratuitous
      handling of someone's mail, and `MAX_MESSAGES_PER_POLL` worth of it if
      the bound were forgotten.

    * **It is DEADLINED, because it runs on the API process.** See
      `TEST_CONVERSATION_DEADLINE_S` / `TEST_REQUEST_DEADLINE_S` above for
      the reasoning and the numbers. `poll_mailbox` passes no deadline and
      is unchanged.

    THE WORST CASE, EXPLICITLY. The REQUEST returns within
    `TEST_REQUEST_DEADLINE_S` (20s) plus the time the two short database
    round trips around it take -- `asyncio.wait_for` does not depend on the
    IMAP thread finishing. The WORKER THREAD's own worst case is
    `TEST_CONVERSATION_DEADLINE_S` (15s), after which its socket is closed
    underneath it, PLUS the platform resolver's own bound on `getaddrinfo`
    for a host that neither resolves nor fails fast: name resolution happens
    before any socket exists, `getaddrinfo` is not interruptible, and
    `asyncio.to_thread` cannot be cancelled. That residue is the same one
    every outbound-connecting call site in this process carries, and it is
    the reason the request deadline exists as well as the socket one.

    `detail` is `stage failed: reason`, both from `imap.py`'s fixed
    vocabularies, bounded -- never `str(exc)`, never a server response line
    (invariant #3).
    """
    async with org_session(org_id, engine=engine) as session:
        mailbox = (
            await session.execute(
                select(EmailMailbox).where(
                    EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
                )
            )
        ).scalar_one_or_none()
        if mailbox is None:
            # The SAME error a cross-org id produces (`mailboxes.get_mailbox`),
            # so the route's 404 cannot become an org-membership oracle.
            raise MailboxNotFoundError(tr("mailbox {mailbox_id} not found", mailbox_id=mailbox_id))
        plan = _MailboxPlan(mailbox)

    stage = "credential"
    ok = True
    host, port, folder = plan.host, plan.port, repr(plan.folder)
    detail = tr(
        "connected to {host}:{port} and listed {folder}", host=host, port=port, folder=folder
    )
    with use_locale("en"):
        stored_detail = tr(
            "connected to {host}:{port} and listed {folder}", host=host, port=port, folder=folder
        )

    async def _conversation() -> None:
        nonlocal stage
        password, access_token = "", None
        try:
            password, access_token = await _sign_in(
                plan, org_id, mailbox_id, engine=engine, vault=vault, actor_id=actor_id
            )
            stage = "connect"
            client: ImapClient = await asyncio.to_thread(
                client_factory,
                host=plan.host,
                port=plan.port,
                use_ssl=plan.use_ssl,
                folder=plan.folder,
                username=plan.username,
                password=password,
                deadline_s=TEST_CONVERSATION_DEADLINE_S,
                access_token=access_token,
            )
        finally:
            # Held for one connection and no longer (invariant #3).
            plan.password = plan.access_token = password = ""
            access_token = None
        try:
            stage = "fetch"
            await asyncio.to_thread(client.fetch_unseen, 0)
        finally:
            # Inside the same watchdog budget, so a server that trickles its
            # LOGOUT response cannot hold the cleanup open either.
            await asyncio.to_thread(client.close)

    try:
        await asyncio.wait_for(_conversation(), TEST_REQUEST_DEADLINE_S)
    except Exception as exc:  # noqa: BLE001
        # `asyncio.wait_for`'s timeout IS `TimeoutError` on 3.11+, so it
        # classifies as `timeout` through the same fixed vocabulary as every
        # other failure -- no separate branch, no separate wording.
        ok = False
        detail = _describe(stage, exc)
        stored_detail = _describe_stored(stage, exc)

    async with org_session(org_id, engine=engine) as session:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="email_intake.mailbox_tested",
            target_type="email_mailbox",
            target_id=str(mailbox_id),
            # The outcome and the constructed classification. Never the
            # password, never the credential id.
            metadata={"ok": ok, "detail": stored_detail[:MAX_TEST_DETAIL]},
        )

    return MailboxTestResult(ok=ok, detail=detail[:MAX_TEST_DETAIL])


async def poll_all_mailboxes(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    vault: VaultService,
    store: ObjectStore,
    client_factory: ImapClientFactory = imap_client_factory,
) -> int:
    """Poll every active mailbox in every org. Returns the number actually
    polled -- claimed and finished without a mailbox-level failure.

    Orgs come off the admin (RLS-bypassing) engine, as every other sweep in
    `worker.py` does; everything else runs through `org_session`, so each
    mailbox and each message is read and written under its own tenant
    (invariant #5).

    EVERY MAILBOX IS WRAPPED. `poll_mailbox` already absorbs its own
    failures, so this is defense in depth against the case it cannot absorb
    (a failure in its own claim or release transaction) -- exactly the
    convention `deliver_pending_all_orgs` and
    `backup_verification_sweep_all_orgs` follow. One mailbox, or one org,
    being unreachable must never skip every mailbox queued behind it.

    BOTH swallowing handlers LOG. These are the only two failures in the
    module that reach neither `last_error` (nothing was claimed, or the
    release itself is what broke) nor the return value beyond a smaller
    count, so without a log line an org whose mailboxes stopped being listed
    is completely silent. `exc_info` is safe here and only here: no
    credential has been checked out at either point -- the org-listing query
    runs before any claim, and a `poll_mailbox` escape is by construction
    outside the `plan.password` window, which `poll_mailbox` clears in a
    `finally` before any of its own error handling runs.
    """
    async with AsyncSession(admin_engine) as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())

    polled = 0
    for org_id in org_ids:
        try:
            async with org_session(org_id, engine=engine) as session:
                mailbox_ids = list(
                    (
                        await session.execute(
                            select(EmailMailbox.id).where(
                                EmailMailbox.org_id == org_id,
                                EmailMailbox.active.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:
            logger.warning(
                "email-intake: could not list mailboxes for org %s; skipping this interval",
                org_id,
                exc_info=True,
            )
            continue

        for mailbox_id in mailbox_ids:
            try:
                result = await poll_mailbox(
                    org_id,
                    mailbox_id,
                    engine=engine,
                    vault=vault,
                    store=store,
                    client_factory=client_factory,
                )
            except Exception:
                logger.error(
                    "email-intake: poll_mailbox escaped for mailbox %s in org %s",
                    mailbox_id,
                    org_id,
                    exc_info=True,
                )
                continue
            if not result["skipped_claimed"] and not result["failed"]:
                polled += 1
    return polled

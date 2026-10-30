"""Sender resolution and the reply token (spec §5, §0.2).

TWO INDEPENDENT CHECKS, AND BOTH MUST PASS. This module supplies both
halves, and neither is sufficient alone:

- `From:` is a trivially forgeable string. It is checked because an
  unrecognised sender must never create or comment on anything -- not
  because it proves identity.
- The token is NOT a bearer credential. It appears in the headers of every
  message in the thread, is forwarded wherever the requester forwards the
  thread, and outlives their employment. It identifies the TICKET. Whoever
  resolves a ticket by token must ALSO check that the sender is that
  ticket's requester -- see `intake.py`, which is the only caller that may
  put the two together.
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.identity import ACTIVE_STATUS
from app.core.tenancy import org_session
from app.models import User
from app.modules.email_intake.models import EmailMailbox
from app.modules.service_desk.models import Ticket

# `+t-` then EXACTLY 43 characters of the urlsafe-base64 alphabet -- the
# exact shape `secrets.token_urlsafe(32)` produces (see `mint_email_token`
# below) and `ticket.email_token`'s `String(43)`. Anything shorter or longer
# cannot possibly be a real minted token and is rejected here, before it
# ever reaches the database. (An earlier revision loosened this to
# `{1,64}` to fit short literal tokens in this module's own tests -- that
# was backwards: it is exactly what let a regex narrowed to `{43}` still
# pass the full suite, rejecting every real token in production. The tests
# were fixed to use realistic tokens instead; see
# `test_a_minted_token_survives_extraction` in
# `tests/test_email_intake_identity.py`, which round-trips a real
# `mint_email_token()` value and would catch this class of regression.)
#
# The leading `^[^\s"<>]*` requires everything before the `+t-` to look
# like a bare local part -- no quote, angle bracket, or whitespace. This is
# not exercised by any caller today (`parsing.py::_extract_recipients` uses
# `email.utils.getaddresses`, which already strips display names before
# this module sees anything), but it closes the boundary at this layer
# rather than relying on that caller's behaviour: without it, a raw
# recipient string like `"helpdesk+t-<token>@x" <attacker@evil.test>` would
# extract the token out of the quoted display name (see F5 in the Task 4
# review).
#
# NOTE on match semantics, corrected from an earlier, wrong comment here:
# within one address the LAST `+t-<43 chars>@` segment wins, not the
# first -- `helpdesk+t-AAA+t-BBB@acme.test` (with real 43-char tokens)
# yields the second token. A trailing `+suffix` after the token
# (`helpdesk+t-<token>+extra@acme.test`) suppresses extraction entirely
# rather than falling back to the first segment, because nothing in the
# string then matches "+t-<43 chars>@" at all. Neither case is a security
# problem -- an extracted token that fails to resolve to a ticket falls
# through to ordinary new-ticket handling, same as no token at all -- but
# callers must not assume "first segment" semantics.
_TOKEN_RE = re.compile(r'^[^\s"<>]*\+t-([A-Za-z0-9_-]{43})@')


@dataclass(frozen=True)
class SenderResolution:
    """`user_id is None` means QUARANTINE. `reason` is recorded on the
    `inbound_message` row so an admin can see what arrived and why it was
    refused -- silently dropping mail makes 'the helpdesk ignored my email'
    indistinguishable from 'the helpdesk never received it' (spec §5.1).

    `kind` and `client_id` are the resolved user's own (`staff`, or `contact`
    and the client that contact belongs to): the first step of the client
    rule (MSP clients spec §3.3, `services/client_routing.py`). Both are None
    when the sender did not resolve."""

    user_id: UUID | None
    reason: str
    kind: str | None = None
    client_id: UUID | None = None


async def resolve_sender(
    session: AsyncSession, org_id: UUID, from_address: str
) -> SenderResolution:
    # NOTE: Python's `.lower()` here and Postgres's `lower()` below can
    # disagree for some non-ASCII input (e.g. Python's `'İ'.lower()` yields
    # two code points, which Postgres's `lower()` does not). Left as-is
    # deliberately: the only consequence of the two disagreeing is a miss,
    # which routes to the `unknown sender` quarantine branch below --
    # fail-closed, not a match on the wrong user. It would surface as "this
    # user's mail is always quarantined", not as a security issue.
    address = from_address.strip().lower()
    if not address:
        return SenderResolution(None, "unknown sender: empty From address")

    # Uses ix_users_org_lower_email (migration 0032). LIMIT 2, not LIMIT 1:
    # the second row is what makes ambiguity detectable at all.
    #
    # ACTIVE USERS ONLY (MSP clients Task 9 review 1, concern 2). `users.status`
    # is the admission gate at every console door, and the mailbox is a door
    # too: a disabled or deleted technician, or a contact switched off (by
    # hand, or by archiving its client), no longer opens or comments on
    # tickets by email. Only active users count towards ambiguity either, so a
    # switched-off account sharing an address does not quarantine the active
    # one's mail.
    matches = list(
        (
            await session.execute(
                select(User.id, User.kind, User.client_id)
                .where(
                    User.org_id == org_id,
                    func.lower(User.email) == address,
                    User.status == ACTIVE_STATUS,
                )
                .limit(2)
            )
        ).all()
    )
    if not matches:
        inactive = (
            await session.execute(
                select(User.id)
                .where(User.org_id == org_id, func.lower(User.email) == address)
                .limit(1)
            )
        ).scalar_one_or_none()
        if inactive is not None:
            # Said apart from "unknown", so the intake log answers "why did
            # this person's mail stop becoming tickets".
            return SenderResolution(None, f"inactive sender: {address}")
        return SenderResolution(None, f"unknown sender: {address}")
    if len(matches) > 1:
        # NEVER guess which human sent it (spec §5.1). Two users in one org
        # may legitimately share an address.
        return SenderResolution(None, f"ambiguous sender: {address} matches multiple users")
    user_id, kind, client_id = matches[0]
    return SenderResolution(user_id, "", kind, client_id)


def extract_reply_token(recipients: Sequence[str]) -> str | None:
    """Find a `+t-<token>` in any recipient address (To, Cc, Delivered-To).

    Case is PRESERVED: `secrets.token_urlsafe` output is case-significant,
    and lower-casing would collapse the keyspace.
    """
    for recipient in recipients:
        match = _TOKEN_RE.search(recipient)
        if match:
            return match.group(1)
    return None


async def resolve_ticket_by_token(
    session: AsyncSession, org_id: UUID, token: str
) -> Ticket | None:
    """Resolve a token to its ticket, ORG-SCOPED (invariant #5).

    Returns the ticket, NOT an authorisation. The caller must still check
    the sender against `ticket.requester_id`.
    """
    return (
        await session.execute(
            select(Ticket).where(Ticket.org_id == org_id, Ticket.email_token == token)
        )
    ).scalar_one_or_none()


def mint_email_token() -> str:
    """256 bits, 43 urlsafe characters -- matches `ticket.email_token`'s
    String(43)."""
    return secrets.token_urlsafe(32)


def _is_header_safe(address: str) -> bool:
    """Printable ASCII, no whitespace -- the only shape that can appear in a
    `Reply-To` header at all.

    `email_mailbox.address` is a bare `String(320)` with no format
    constraint (neither `MailboxCreate` nor `MailboxUpdate` applies a
    pattern), so an org admin can store anything that fits, INCLUDING
    control characters. `email.policy.default` refuses to put a CR or LF in
    a header -- correct, that is header injection -- but it refuses by
    raising `ValueError` from deep inside `_send_smtp_sync`, which is not
    among the exceptions `notification/delivery.py::_send_email` classifies.
    It escapes to the worker's blanket `except Exception`, which marks the
    delivery `dead`: TERMINAL, no retry. One bad character in one mailbox
    address would therefore stop EVERY ticket-subject email for that org
    permanently, with a `last_error` naming neither the mailbox nor the
    address (Task 8 review F7).

    So the malformed address is refused HERE, by returning `None` (no
    `Reply-To`, mail still goes out) rather than by raising. The bound is
    `!`..`~`: it excludes CR, LF, NUL and every other control character,
    excludes space and tab, and excludes non-ASCII (this platform speaks
    plain SMTP with no SMTPUTF8 negotiation, so a non-ASCII address could
    not be replied to even if the header were built).
    """
    return all("\x21" <= ch <= "\x7e" for ch in address)


def _same_address(a: str, b: str) -> bool:
    """Case-insensitive equality of two email addresses.

    NOT a lookup, and deliberately not routed through `resolve_sender`:
    §5.1's warning that an address may match several users in an org is about
    going address -> user, which must never guess. Here the direction is the
    other way -- ticket -> `requester_id` -> exactly one `users` row -> that
    row's email -- and the question is only "is the address we are about to
    mail the same one?". Two users sharing an address makes this comparison
    true for both, which is the correct answer to that question: the address
    is the requester's.
    """
    return a.strip().lower() == b.strip().lower()


async def _mint_email_token_committed(
    session: AsyncSession, org_id: UUID, ticket_id: UUID
) -> str | None:
    """Mint (or read back) `ticket.email_token` in its OWN short, committed
    transaction -- never the caller's (Task 8 review F4).

    `reply_to_for_ticket`'s only caller is one step away from an SMTP
    conversation bounded by `_SMTP_TIMEOUT_S` (30 s), and the mint is a
    row-level WRITE on `ticket`. Done inside the delivery transaction it was
    wrong in both directions:

    * the ticket row's write lock was held across the whole SMTP round trip
      (measured at 1.66 s against a 2 s stub), so ANY other writer to that
      ticket -- a technician changing status, an assignment, a comment
      through the API -- blocked behind a background mail send; and, the
      decisive one,
    * a rollback AFTER a successful send discarded a token that had already
      gone out on the wire. The address in the requester's inbox would then
      resolve to no ticket, forever -- precisely the "the requester's reply
      vanishes" failure this whole feature exists to prevent.

    Committing before the send is strictly safer in both directions: a token
    committed but never published is harmless (the retry reuses it), while a
    token published but never committed is unrecoverable.

    The conditional `UPDATE ... WHERE email_token IS NULL ... RETURNING` is
    unchanged and still the concurrency guarantee: two deliveries for the
    same ticket racing each other cannot both win, because the loser's
    predicate is re-evaluated against the winner's committed row, matches
    nothing, and it reads back what the winner minted. (Reachable in
    production despite the worker's serial loop: the delivery claim uses `FOR
    UPDATE SKIP LOCKED`, so two workers can hold two different deliveries for
    the same ticket at once.)

    Returns `None` when the ticket does not exist in `org_id`, and when the
    caller's session is not bound to an engine this can open a second session
    on -- in that case no `Reply-To` is published (the mail still goes out,
    unrepliable) rather than falling back to minting inside the caller's
    transaction, which is the shape this function exists to avoid.

    CALLER CONSTRAINT: the caller's own transaction must not already hold a
    write lock on this ticket row, or this second connection would wait on it
    until the statement timeout. Every caller today (`deliver_pending`'s
    per-delivery transaction, which touches only `notification_deliveries`)
    satisfies that.
    """
    engine = session.bind
    if not isinstance(engine, AsyncEngine):
        return None

    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as mint_session:
        token = (
            await mint_session.execute(
                update(Ticket)
                .where(
                    Ticket.org_id == org_id,
                    Ticket.id == ticket_id,
                    Ticket.email_token.is_(None),
                )
                .values(email_token=mint_email_token())
                .returning(Ticket.email_token)
            )
        ).scalar_one_or_none()
        if token is None:
            # Either a concurrent delivery minted first, or there is no such
            # ticket in this org.
            token = (
                await mint_session.execute(
                    select(Ticket.email_token).where(
                        Ticket.org_id == org_id, Ticket.id == ticket_id
                    )
                )
            ).scalar_one_or_none()
    return token


async def reply_to_for_ticket(
    session: AsyncSession, org_id: UUID, ticket_id: UUID, recipient: str
) -> str | None:
    """The `Reply-To` for mail about this ticket, TO `recipient`, or None.

    THIS IS THE ONLY THING THAT MINTS A TOKEN IN PRODUCTION (spec correction
    C3). Before it existed, `mint_email_token` had no caller: every ticket
    carried `email_token = NULL`, no requester was ever given an address to
    reply to, and the entire inbound reply path -- token extraction,
    resolution, reopen, the "token is not authority" check -- was
    unreachable, while its tests all passed because each constructed its own
    token.

    Minting HERE, lazily, is deliberate: a token is only useful once it has
    been published to a requester, and a ticket that is never emailed about
    never needs one. `secrets.token_urlsafe` at ticket-create time would put
    a permanent 256-bit secret on every ticket the platform has ever
    created, for nothing.

    ONLY THE REQUESTER IS OFFERED A REPLY ADDRESS (Task 8 review F2).
    `recipient` is the address this particular message is going to
    (`NotificationDelivery.recipient`), and it must be the ticket
    requester's, or nothing is published and nothing is minted. Ticket
    notifications are routed to `assignee` and `queue_members` as well as
    `requester` (`notification/routing.py`), and spec §13 defers staff
    replying by email as a DESIGN: `intake.py` refuses a reply whose sender
    is not the ticket's requester, so a technician who hits Reply on
    "ticket resolved" has their message quarantined -- no comment, no
    bounce, nobody told, and they believe they updated the ticket. Handing
    someone an address that cannot work for them is the same defect as the
    no-mailbox case below, pointed at a person instead of an org.

    `NotificationDelivery` carries no `recipient_user_id`, so the check is
    by address: ticket -> `requester_id` -> that user's email, compared
    case-insensitively (`_same_address`). A ticket with no requester at all
    (`requester_id` is NULL -- an alert-origin ticket, permitted by
    `ck_ticket_requester_or_alert`) matches nobody and is never repliable.

    Returns None -- and mints NOTHING -- in each of these cases:

    * The org has no ACTIVE `email_mailbox`. Email intake is per-org
      configuration; a `Reply-To` pointing at an address nobody polls is
      worse than none, because the requester's reply vanishes silently
      rather than bouncing. This is checked BEFORE any write, so an org that
      does not run email intake never accumulates tokens.
    * The mailbox address's local part already contains a `+`. That is a
      plus-address being used as the mailbox address itself, and this module
      cannot nest a second tag inside it: `helpdesk+support+t-<token>@...`
      is not an address a mail system routes back to `helpdesk+support`, so
      the reply would be lost. Refusing is the honest outcome.
    * The mailbox address is not `local@domain` at all (no `@`, empty local
      part, or empty domain), or contains a character that cannot appear in
      a header at all -- see `_is_header_safe`. Nothing sensible can be
      built from either, and RAISING on the second would dead-letter the
      org's ticket mail.
    * `recipient` is not the ticket requester's address (above).
    * `ticket_id` names no ticket in `org_id` (invariant #5: the lookup is
      tenant-scoped, so another org's ticket is simply absent).

    ORG-SCOPED, and MULTI-MAILBOX-DETERMINISTIC. `email_mailbox` has a
    unique constraint on `(org_id, address)`, not on `org_id`, so an org may
    configure more than one. The oldest active one wins, ordered by
    `(created_at, id)` so the choice is stable across calls rather than
    whatever the planner returns first -- an unstable choice would put a
    different `Reply-To` domain on successive messages in one thread.

    THE TOKEN NEVER CHANGES ONCE MINTED. It appears in the headers of every
    message in the thread; minting a second one would orphan the first, so
    every reply to an older message in the thread would resolve to nothing.
    The conditional `UPDATE ... WHERE email_token IS NULL ... RETURNING` in
    `_mint_email_token_committed` is what guarantees that under concurrency
    too, and it runs in its own short transaction so the token is DURABLE
    before it is published -- see that function's docstring.
    """
    mailbox = (
        await session.execute(
            select(EmailMailbox)
            .where(EmailMailbox.org_id == org_id, EmailMailbox.active.is_(True))
            .order_by(EmailMailbox.created_at, EmailMailbox.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if mailbox is None:
        return None

    if not _is_header_safe(mailbox.address):
        return None
    local, at, domain = mailbox.address.rpartition("@")
    if not at or not local or not domain:
        return None
    if "+" in local:
        return None

    # One row: the ticket's token (usually already present -- every message
    # after the first in a thread) and its requester's address, joined here
    # so the recipient gate is decided BEFORE anything is written. The join
    # is an inner one, so a requester-less ticket yields no row and is never
    # repliable. Org-scoped on BOTH sides (invariant #5), in addition to RLS.
    row = (
        await session.execute(
            select(Ticket.email_token, User.email)
            .join(User, User.id == Ticket.requester_id)
            .where(
                Ticket.org_id == org_id,
                Ticket.id == ticket_id,
                User.org_id == org_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    token, requester_email = row

    if not _same_address(recipient, requester_email):
        return None

    if token is None:
        token = await _mint_email_token_committed(session, org_id, ticket_id)
    if token is None:
        return None

    return f"{local}+t-{token}@{domain}"

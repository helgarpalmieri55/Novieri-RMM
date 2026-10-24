"""Sender resolution and the reply token (spec §5, §0.2)."""

from app.core.client_scope import ClientScope
from app.core.tenancy import admin_session, org_session
from app.modules.email_intake.identity import (
    extract_reply_token,
    mint_email_token,
    resolve_sender,
    resolve_ticket_by_token,
)
from tests.test_email_intake_migration import _seed_user


async def test_exactly_one_matching_user_resolves(api_env) -> None:
    org_id = await api_env.create_org()
    user_id = await _seed_user(api_env, org_id, email="alice@acme.test")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "alice@acme.test")
    assert result.user_id == user_id


async def test_matching_is_case_insensitive(api_env) -> None:
    org_id = await api_env.create_org()
    user_id = await _seed_user(api_env, org_id, email="Alice@Acme.Test")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "ALICE@acme.TEST")
    assert result.user_id == user_id


async def test_an_unknown_sender_does_not_resolve(api_env) -> None:
    """Spec §11.3: quarantined, not dropped. The reason is recorded so an
    admin can tell 'the helpdesk ignored my email' from 'never received it'."""
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "stranger@elsewhere.test")
    assert result.user_id is None
    assert "unknown" in result.reason.lower()


async def test_an_empty_from_address_is_quarantined_without_a_query(api_env) -> None:
    """F6: the early-return branch for an empty/blank `From:` is
    behaviourally identical to falling through to the query (both
    quarantine with a reason containing "unknown"), but it saves a query
    and gives a more specific reason. Pin that reason so the branch is
    exercised rather than dead code."""
    org_id = await api_env.create_org()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "   ")
    assert result.user_id is None
    assert result.reason == "unknown sender: empty From address"


async def test_a_user_in_another_org_does_not_resolve(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    await _seed_user(api_env, org_b, email="alice@acme.test")
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_a, "alice@acme.test")
    assert result.user_id is None


async def test_the_sender_org_predicate_is_load_bearing(api_env) -> None:
    """Spec §9: "a behavioural cross-org test per table that fails if its
    predicate is removed." `test_a_user_in_another_org_does_not_resolve`
    above runs under `org_session`, where RLS rejects the cross-org row
    first -- the `User.org_id == org_id` predicate in `resolve_sender`
    could be deleted entirely and that test would stay green. This probe
    runs on the RLS-BYPASSING admin engine on purpose, so the
    application-level predicate is the only thing that can save it."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    await _seed_user(api_env, org_b, email="alice@acme.test")
    async with admin_session(engine=api_env.admin_engine) as session:
        result = await resolve_sender(session, org_a, "alice@acme.test")
    assert result.user_id is None, f"CROSS-ORG LEAK: org_a resolved org_b's user {result.user_id}"


async def test_an_ambiguous_sender_does_not_resolve(api_env) -> None:
    """Spec §5.1: `users` is unique on (org_id, external_subject), NOT on
    email, so two users in one org can legitimately share an address.
    Picking one would attribute a person's ticket to a colleague, and the
    resulting ticket would look entirely normal."""
    org_id = await api_env.create_org()
    await _seed_user(api_env, org_id, email="shared@acme.test")
    await _seed_user(api_env, org_id, email="shared@acme.test")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "shared@acme.test")
    assert result.user_id is None
    assert "ambiguous" in result.reason.lower()


def test_a_token_is_extracted_from_a_plus_address() -> None:
    token = mint_email_token()
    assert extract_reply_token([f"helpdesk+t-{token}@acme.test"]) == token


def test_a_token_is_found_on_any_recipient_header() -> None:
    token = mint_email_token()
    assert (
        extract_reply_token(["nobody@acme.test", f"helpdesk+t-{token}@acme.test"]) == token
    )


def test_a_plain_address_yields_no_token() -> None:
    assert extract_reply_token(["helpdesk@acme.test"]) is None


def test_a_plus_address_that_is_not_a_token_yields_none() -> None:
    assert extract_reply_token(["helpdesk+newsletter@acme.test"]) is None


def test_token_extraction_is_case_preserving() -> None:
    """token_urlsafe is case-SIGNIFICANT: lower-casing it would collapse the
    keyspace and could match the wrong ticket."""
    token = "Ab" * 21 + "C"  # 43 chars, mixed case, urlsafe-base64 alphabet
    assert len(token) == 43
    assert extract_reply_token([f"helpdesk+t-{token}@acme.test"]) == token


def test_a_minted_token_is_url_safe_and_long() -> None:
    token = mint_email_token()
    assert len(token) == 43  # secrets.token_urlsafe(32)
    assert token.isascii() and "@" not in token and "+" not in token


def test_a_minted_token_survives_extraction() -> None:
    """F2/F3: the literal tokens elsewhere in this file are illustrative
    fixtures; without a test that routes a REAL, `mint_email_token()`-shaped
    value through `extract_reply_token`, the regex could reject every real
    43-character token (e.g. if narrowed to `{1,6}`) and the rest of this
    suite would stay fully green. This is what actually closes that hole."""
    token = mint_email_token()
    recipient = f"helpdesk+t-{token}@acme.test"
    assert extract_reply_token([recipient]) == token
    assert extract_reply_token(["nobody@acme.test", recipient]) == token


def test_an_empty_token_yields_none() -> None:
    """The one genuinely dangerous case: `{0,...}` would extract `""`
    against a nullable `email_token` column. Verify the `{43}` bound still
    rejects it."""
    assert extract_reply_token(["helpdesk+t-@acme.test"]) is None


def test_the_last_token_segment_wins() -> None:
    """F4: documents actual behaviour -- the regex comment used to claim
    the opposite. A second `+t-` segment displaces the first."""
    first, second = mint_email_token(), mint_email_token()
    assert extract_reply_token([f"helpdesk+t-{first}+t-{second}@acme.test"]) == second


def test_a_trailing_suffix_after_the_token_suppresses_extraction() -> None:
    """F4: a forwarder or MTA that appends to the plus-address (rather than
    replacing it) silently downgrades a reply to a new ticket -- fail-closed,
    not a security problem (the sender check still gates every path), but
    worth pinning so the behaviour doesn't drift unnoticed."""
    token = mint_email_token()
    assert extract_reply_token([f"helpdesk+t-{token}+extra@acme.test"]) is None


def test_a_token_in_a_display_name_is_not_extracted() -> None:
    """F5: not reachable today -- `parsing.py::_extract_recipients` uses
    `email.utils.getaddresses`, which strips display names before this
    module ever sees a recipient string -- but close it at this layer
    rather than relying on that caller's behaviour."""
    token = mint_email_token()
    smuggled = f'"helpdesk+t-{token}@x" <real@evil.test>'
    assert extract_reply_token([smuggled]) is None


async def test_a_token_resolves_to_its_ticket_and_only_within_the_org(
    api_env, a_ticket_for
) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    ticket = await a_ticket_for(org_a, "requester")
    token = mint_email_token()

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # `a_ticket_for` returns an `ATicket` dataclass (id, requester_id),
        # not the ORM row -- load the real `Ticket` to set `email_token`.
        from app.models import Ticket as TicketModel

        ticket_row = await session.get(TicketModel, ticket.id)
        assert ticket_row is not None
        ticket_row.email_token = token
        session.add(ticket_row)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found_ticket = await resolve_ticket_by_token(session, org_a, token)
        assert found_ticket is not None
        assert found_ticket.id == ticket.id
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert await resolve_ticket_by_token(session, org_b, token) is None


async def test_the_ticket_org_predicate_is_load_bearing(api_env, a_ticket_for) -> None:
    """Spec §9, same shape as `test_the_sender_org_predicate_is_load_bearing`
    but worse: `uq_ticket_email_token` is GLOBALLY unique, so an unfiltered
    lookup returns exactly one row -- the other org's ticket -- with no
    `MultipleResultsFound` to signal anything is wrong. RLS is the only
    thing standing between a cross-org token lookup and a foreign ticket
    unless `Ticket.org_id == org_id` is also load-bearing at the
    application layer. This probe runs on the RLS-BYPASSING admin engine on
    purpose, so it is."""
    from app.models import Ticket as TicketModel

    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    ticket = await a_ticket_for(org_a, "requester")
    token = mint_email_token()
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket_row = await session.get(TicketModel, ticket.id)
        assert ticket_row is not None
        ticket_row.email_token = token
        session.add(ticket_row)

    async with admin_session(engine=api_env.admin_engine) as session:
        found = await resolve_ticket_by_token(session, org_b, token)
    assert found is None, (
        f"CROSS-ORG LEAK: org_b resolved org_a's ticket {found.id if found else None}"
    )


async def test_an_unknown_token_resolves_to_none(api_env, a_ticket_for) -> None:
    """F7: seed a REAL ticket with a REAL token first, then query a
    DIFFERENT valid-shaped token. Against an empty table this test would
    pass even against a `resolve_ticket_by_token` implemented as
    `return None` -- it has to distinguish "no match" from "no data"."""
    from app.models import Ticket as TicketModel

    org_id = await api_env.create_org()
    ticket = await a_ticket_for(org_id, "requester")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket_row = await session.get(TicketModel, ticket.id)
        assert ticket_row is not None
        ticket_row.email_token = mint_email_token()
        session.add(ticket_row)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        other_token = mint_email_token()
        assert await resolve_ticket_by_token(session, org_id, other_token) is None


async def _set_status(api_env, user_id, status: str) -> None:
    from sqlalchemy import update

    from app.models import User

    async with admin_session(engine=api_env.admin_engine) as session:
        await session.execute(update(User).where(User.id == user_id).values(status=status))


async def test_a_disabled_or_deleted_sender_does_not_resolve(api_env) -> None:
    """MSP clients Task 9 review (concern 2): `users.status` is the admission
    gate at every console door, and the mailbox is a door too. A switched-off
    contact or technician no longer opens or comments on tickets by email;
    the message is quarantined, with a reason that says why."""
    org_id = await api_env.create_org()
    disabled = await _seed_user(api_env, org_id, email="gone@acme.test")
    deleted = await _seed_user(api_env, org_id, email="left@acme.test")
    await _set_status(api_env, disabled, "disabled")
    await _set_status(api_env, deleted, "deleted")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        for address in ("gone@acme.test", "left@acme.test"):
            result = await resolve_sender(session, org_id, address)
            assert result.user_id is None, address
            assert result.reason == f"inactive sender: {address}"


async def test_an_active_user_is_not_shadowed_by_an_inactive_one(api_env) -> None:
    """Only active users take part, ambiguity included: a disabled account
    sharing an address with an active one does not quarantine the active
    one's mail."""
    org_id = await api_env.create_org()
    active = await _seed_user(api_env, org_id, email="shared@acme.test")
    inactive = await _seed_user(api_env, org_id, email="shared@acme.test")
    await _set_status(api_env, inactive, "disabled")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        result = await resolve_sender(session, org_id, "Shared@Acme.test")
    assert result.user_id == active

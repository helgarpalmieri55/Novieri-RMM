"""ServiceDeskChangeTicketAdapter tests (docs/modules/service-desk/spec.md
§7.1; docs/modules/change-mgmt/spec.md §5).

Covers: the port selection switch (`get_ticket_adapter` returns
`ServiceDeskChangeTicketAdapter` for `ITS_CHANGE_TICKET_BACKEND=service_desk`,
and `NullChangeTicketAdapter` remains the default -- an existing
deployment's behavior is unaffected until an operator opts in);
`push_change` opens a real `type="incident"` ticket in the org's default
queue (the `"other"` category / `"helpdesk"` queue seeded lazily), requested
by and attributed to the change's own author, and records exactly one
`change_external_ref` row keyed `system="service_desk"`; `update_change`
resolves the linked ticket both from a real `external_id` and, when that is
blank (the actual value every pinned `change_mgmt.service` call site passes
today), by falling back to the `change_external_ref` lookup, and is a no-op
when no ref exists at all; `close_change` drives the ticket toward
`resolved` (never straight to `closed` -- mirrors spec §7.2's alert-recovery
convention) when given a real `external_id`, and is a documented no-op when
`external_id` is blank (the inherited limitation of `change_mgmt.service`'s
pinned call sites, which this task does not touch); and the risk-to-
priority mapping (`low`/`medium`/`high` -> `low`/`normal`/`high`).
"""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeExternalRef,
    ChangeRequest,
    Organization,
    Ticket,
    TicketComment,
    TicketStatusHistory,
    User,
)
from app.modules.change_mgmt.ticketing import (
    ChangeTicketPort,
    NullChangeTicketAdapter,
    get_ticket_adapter,
)
from app.modules.service_desk.adapters import ServiceDeskChangeTicketAdapter

# Static conformance check: ServiceDeskChangeTicketAdapter structurally
# satisfies ChangeTicketPort (mypy verifies this line; no isinstance check
# needed), mirroring test_change_mgmt_ticketing.py's identical check for the
# null adapter.
_: ChangeTicketPort = ServiceDeskChangeTicketAdapter()


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://app_user:app@localhost/x",
        database_admin_url="postgresql+asyncpg://admin:a@localhost/x",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir="/tmp/ca",
        **overrides,
    )


def test_the_service_desk_is_the_default_and_null_opts_out() -> None:
    settings = _settings()
    assert settings.change_ticket_backend == "service_desk"
    assert isinstance(get_ticket_adapter(settings), ServiceDeskChangeTicketAdapter)
    opted_out = _settings(change_ticket_backend="null")
    assert isinstance(get_ticket_adapter(opted_out), NullChangeTicketAdapter)


def test_service_desk_backend_selects_the_real_adapter() -> None:
    settings = _settings(change_ticket_backend="service_desk")
    adapter = get_ticket_adapter(settings)
    assert isinstance(adapter, ServiceDeskChangeTicketAdapter)


async def _org_user_change(
    admin_engine: AsyncEngine, *, risk: str = "high"
) -> tuple[object, object, ChangeRequest]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="sda", slug=f"sda-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.flush()
        change = ChangeRequest(
            org_id=org.id,
            client_id=org.id,
            type="normal",
            title="Patch the mail relay",
            description="Apply the vendor security patch to the relay host.",
            risk=risk,
            created_by=usr.id,
        )
        s.add(change)
        await s.commit()
        return org.id, usr.id, change


async def test_push_change_opens_incident_ticket_in_default_queue(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        external_id = await adapter.push_change(s, org_id=org_id, change=change)

    assert external_id  # non-empty, unlike the null adapter's ""

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert str(ticket.id) == external_id
        assert ticket.type == "incident"
        assert ticket.status == "new"
        assert ticket.requester_id == user_id
        assert ticket.created_by == user_id
        # `origin="integration"`, not `"technician"` (whole-branch security
        # review, finding A3): this ticket was opened by the system reacting
        # to a change transition, not by `user_id` filing a helpdesk ticket
        # by hand -- see `test_push_change_uses_system_actor_attribution`
        # below for the resulting `actor_type` on the CREATE action.
        assert ticket.origin == "integration"
        assert "Patch the mail relay" in ticket.subject
        assert ticket.priority == "high"  # risk="high" -> priority="high"

        refs = (
            await s.execute(
                select(ChangeExternalRef).where(ChangeExternalRef.org_id == org_id)
            )
        ).scalars().all()
        assert len(refs) == 1
        assert refs[0].system == "service_desk"
        assert refs[0].change_id == change.id
        assert refs[0].external_id == external_id


async def test_push_change_uses_system_actor_attribution(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A3 regression. Before this fix, `push_change` created its ticket with
    `origin="technician"`, so the initial `ticket_status_history` row and the
    `ticket.created` audit event both read `actor_type="user"`,
    `actor_id=change.created_by` -- indistinguishable, in the immutable
    hash-chained log, from that person filing the ticket by hand, however
    much later the actual triggering change transition happened.
    `origin="integration"` fixes the attribution while leaving
    `requester_id`/`created_by` (asserted above) genuinely human."""
    org_id, user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        external_id = await adapter.push_change(s, org_id=org_id, change=change)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert str(ticket.id) == external_id

        creation_row = (
            await s.execute(
                select(TicketStatusHistory)
                .where(TicketStatusHistory.ticket_id == ticket.id)
                .order_by(TicketStatusHistory.id)
                .limit(1)
            )
        ).scalar_one()
        assert creation_row.actor_type == "system"
        assert creation_row.actor_id is None

        created_event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.created",
                    AuditEvent.target_id == str(ticket.id),
                )
            )
        ).scalar_one()
        assert created_event.actor_type == "system"
        assert created_event.actor_id is None
        # The human is not erased -- only the CREATE action's attribution is
        # system. The ticket itself still names a real requester/author
        # (checked in `test_push_change_opens_incident_ticket_in_default_queue`).
        assert ticket.requester_id == user_id
        assert ticket.created_by == user_id


async def test_push_change_risk_to_priority_mapping(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    adapter = ServiceDeskChangeTicketAdapter()
    for risk, expected_priority in (("low", "low"), ("medium", "normal"), ("high", "high")):
        org_id, _user_id, change = await _org_user_change(admin_engine, risk=risk)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
            await adapter.push_change(s, org_id=org_id, change=change)
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
            ticket = (
                await s.execute(select(Ticket).where(Ticket.org_id == org_id))
            ).scalar_one()
            assert ticket.priority == expected_priority, risk


async def test_update_change_resolves_via_real_external_id(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        external_id = await adapter.push_change(s, org_id=org_id, change=change)

    change.state = "implementing"
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.update_change(s, org_id=org_id, external_id=external_id, change=change)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        comments = (
            await s.execute(select(TicketComment).where(TicketComment.org_id == org_id))
        ).scalars().all()
        assert len(comments) == 1
        assert comments[0].author_type == "system"
        assert comments[0].author_id is None
        assert "implementing" in comments[0].body


async def test_update_change_falls_back_to_ref_lookup_when_external_id_blank(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Mirrors the ACTUAL call site in `change_mgmt.service.transition_change`/
    `decide_change`, both of which always pass `external_id=""` -- the
    adapter must still find the ticket via `change_external_ref` using
    `change.id`, which IS available at every real call site."""
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.push_change(s, org_id=org_id, change=change)

    change.state = "completed"
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.update_change(s, org_id=org_id, external_id="", change=change)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        comments = (
            await s.execute(select(TicketComment).where(TicketComment.org_id == org_id))
        ).scalars().all()
        assert len(comments) == 1
        assert "completed" in comments[0].body


async def test_update_change_is_noop_when_no_ref_was_ever_created(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    # push_change was never called -- no change_external_ref row exists.
    # B5 (whole-branch review, round B): `update_change` is declared `-> None`
    # with return-only exits, so `assert result is None` here asserted
    # nothing real (mypy already guarantees it). The actual behavior worth
    # pinning -- no comment gets written -- is the assertion below.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.update_change(s, org_id=org_id, external_id="", change=change)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        comments = (
            await s.execute(select(TicketComment).where(TicketComment.org_id == org_id))
        ).scalars().all()
        assert comments == []


async def test_close_change_drives_ticket_to_resolved_not_closed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """spec §7.2's alert-recovery convention, mirrored here: closing the
    LINKED change resolves the ticket, it never jumps straight to `closed`
    -- a human still confirms the work is genuinely done."""
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        external_id = await adapter.push_change(s, org_id=org_id, change=change)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket_before = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert ticket_before.status == "new"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.close_change(s, org_id=org_id, external_id=external_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket_after = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert ticket_after.status == "resolved"
        assert ticket_after.resolved_at is not None

        history = (
            await s.execute(
                select(TicketStatusHistory)
                .where(TicketStatusHistory.org_id == org_id)
                .order_by(TicketStatusHistory.created_at)
            )
        ).scalars().all()
        # new -> open -> resolved: two machine transitions after creation.
        assert [h.to_status for h in history[1:]] == ["open", "resolved"]
        assert all(h.actor_type == "system" and h.actor_id is None for h in history[1:])


async def test_close_change_is_noop_when_external_id_blank(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The inherited limitation, documented in adapters.py's module
    docstring: `change_mgmt.service`'s pinned `close_change` call site
    always passes `external_id=""`, with no `change` object to fall back
    on, so this is a genuine no-op through that wiring today."""
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.push_change(s, org_id=org_id, change=change)

    # B5 (whole-branch review, round B): `close_change` is likewise declared
    # `-> None` with return-only exits -- `assert result is None` asserted
    # nothing real. The actual behavior worth pinning is the ticket status
    # assertion below.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.close_change(s, org_id=org_id, external_id="")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert ticket.status == "new"  # untouched


async def test_close_change_is_idempotent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """B5 (whole-branch review, round B) -- KNOWN VACUOUS, documented rather
    than fixed (one of this round's two sanctioned documentation-only
    exceptions, alongside the upload pre-check note in
    `test_service_desk_api.py`).

    This test's idempotency observation comes entirely from a DIFFERENT
    branch than the guard whose comment claims it: `close_change`'s own
    `if ticket.status in FINAL_STATUSES or ticket.status == "closed": return`
    is checked BEFORE the second call ever runs, but by then the ticket is
    already `"resolved"` (the first call's result) -- neither final nor
    `"closed"` -- so that guard does not even fire here; the second call is a
    no-op only because `"resolved"` also fails BOTH of the two branches below
    the guard (`status == "new"` and `status in ("open", "pending")`).

    Verified empirically (delete the guard entirely, run the FULL file): all
    11 tests still pass, including this one, because the guard's condition
    set (`FINAL_STATUSES | {"closed"}` = `{"cancelled", "rejected", "closed"}`)
    is structurally DISJOINT from the two branches below it
    (`{"new"}` and `{"open", "pending"}`) -- a ticket cannot simultaneously be
    in both sets, so for EVERY status the guard could ever match, removing it
    changes nothing: the code falls through to the same two `if`s, neither of
    which fires either. This was reconfirmed directly on a ticket forced to
    `"cancelled"` (the guard's actual stated purpose: "never touch a
    cancelled/rejected/closed ticket") -- with the guard deleted, closing a
    cancelled ticket still leaves it `"cancelled"`, byte-for-byte the same
    outcome as with the guard present.

    In short: as currently shaped, this guard cannot be pinned by ANY test,
    because it is unreachable dead code given the two branches that follow
    it -- not merely under-tested. Writing a test that "fails when the guard
    is deleted" is not possible without either restructuring `close_change`
    itself (out of scope: this round is about tests, not production
    behavior) or asserting something the guard does not actually cause. Per
    this round's method note, this is left as one of the two documented
    exceptions rather than papered over with an assertion that would look
    load-bearing but isn't."""
    org_id, _user_id, change = await _org_user_change(admin_engine)
    adapter = ServiceDeskChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        external_id = await adapter.push_change(s, org_id=org_id, change=change)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.close_change(s, org_id=org_id, external_id=external_id)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await adapter.close_change(s, org_id=org_id, external_id=external_id)  # second call

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ticket = (
            await s.execute(select(Ticket).where(Ticket.org_id == org_id))
        ).scalar_one()
        assert ticket.status == "resolved"

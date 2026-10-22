"""Routing rules, webhook allowlist, and queue membership
(docs/modules/notification/spec.md §2.3, §2.4, §2.6, §6, §8).

Covers: `create_rule` re-verifies `channel_id`/`queue_id` org-scoped before
writing (invariant #5, the `default_queue_id` bug class) and rejects every
staff-only-event-to-requester pairing (`ticket.internal_note_added`,
`ticket.sla_breached`) at the SERVICE layer, unconditionally -- never
surfacing `ck_rule_no_internal_to_requester` /
`ck_rule_no_sla_breach_to_requester` as a raw `IntegrityError`;
`add_allowlist_entry` is race-safe against `uq_allowlist_org_host_port`, same
shape as `channels.create_channel`; `add_queue_member` is a deliberate,
documented exception to "CREATE, not get-or-create" -- idempotent, and race-safe
against `uq_queue_member` without ever raising a raw `IntegrityError`;
`allowlisted_hosts` returns `set[tuple[str, int]]`, the egress guard's exact
consumer shape; every list/read query used for a tenancy claim below is
verified, empirically, to go RED with its `org_id` predicate removed (see the
report for the exact output observed).

ENGINE CHOICE: every test uses a raw `AsyncSession(api_env.admin_engine, ...)`
(BYPASSRLS), mirroring `test_notification_channels.py` -- RLS would mask a
foreign org's row and prove nothing about this module's OWN `org_id`
predicates, which is what these tests must pin down.
"""

import pathlib
import re
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy import text as sqltext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.vault import LocalVaultService
from app.models import (
    AuditEvent,
    NotificationChannel,
    QueueMember,
    TicketQueue,
    User,
    WebhookAllowlistEntry,
)
from app.modules.notification.channels import create_channel
from app.modules.notification.errors import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from app.modules.notification.rules import (
    add_allowlist_entry,
    add_queue_member,
    allowlisted_hosts,
    create_rule,
    delete_allowlist_entry,
    delete_rule,
    list_allowlist,
    list_queue_members,
    list_rules,
    remove_queue_member,
)
from app.modules.notification.schema import AllowlistEntryCreate, ChannelCreate, RuleCreate
from app.modules.service_desk.refdata import create_queue


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors `test_notification_channels.py`'s helper of
    the same name/shape."""
    org_id = await api_env.create_org("notif-rules")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _events(session: AsyncSession, org_id: UUID, action: str) -> list[AuditEvent]:
    return (
        (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == action
                )
            )
        )
        .scalars()
        .all()
    )


async def _channel(
    session: AsyncSession, org_id: UUID, user_id: UUID, tmp_path: Path, name: str = "ops"
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(
            kind="webhook", name=name, config={"url": "https://h.example.com/x"}
        ),
        secret=None,
        vault=_vault(tmp_path),
    )


async def _queue(
    session: AsyncSession, org_id: UUID, user_id: UUID, key: str = "ops"
) -> TicketQueue:
    return await create_queue(session, org_id, user_id, key=key, name=key.title())


# ---------------------------------------------------------------------------
# create_rule -- cross-org id validation (invariant #5)
# ---------------------------------------------------------------------------


async def test_create_rule_rejects_a_cross_org_channel(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign_channel = await _channel(session, other_org_id, other_user_id, tmp_path)
        with pytest.raises(NotificationNotFoundError):
            await create_rule(
                session,
                org_id=org_id,
                actor_id=user_id,
                data=RuleCreate(
                    event_type="ticket.assigned",
                    channel_id=foreign_channel.id,
                    recipient_kind="assignee",
                ),
            )


async def test_create_rule_rejects_a_cross_org_queue(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        foreign_queue = await _queue(session, other_org_id, other_user_id)
        with pytest.raises(NotificationNotFoundError):
            await create_rule(
                session,
                org_id=org_id,
                actor_id=user_id,
                data=RuleCreate(
                    event_type="ticket.assigned",
                    channel_id=channel.id,
                    recipient_kind="queue_members",
                    queue_id=foreign_queue.id,
                ),
            )


async def test_create_rule_rejects_a_nonexistent_channel_identically(api_env) -> None:
    """The other half of invariant #5's "indistinguishable" requirement: a
    nonexistent id raises the SAME error type as a cross-org id."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        with pytest.raises(NotificationNotFoundError):
            await create_rule(
                session,
                org_id=org_id,
                actor_id=user_id,
                data=RuleCreate(
                    event_type="ticket.assigned",
                    channel_id=uuid7(),
                    recipient_kind="assignee",
                ),
            )


# ---------------------------------------------------------------------------
# create_rule -- the internal-note rule (spec §6.4)
# ---------------------------------------------------------------------------


async def test_rule_create_schema_rejects_internal_note_to_requester(tmp_path: Path) -> None:
    """The FIRST layer: `RuleCreate`'s own Pydantic validator (already shipped
    in schema.py) refuses to even construct the invalid pairing."""
    with pytest.raises(ValueError, match="internal notes cannot be routed"):
        RuleCreate(
            event_type="ticket.internal_note_added",
            channel_id=uuid7(),
            recipient_kind="requester",
        )


async def test_create_rule_rejects_internal_note_to_requester_at_the_service_layer(
    api_env, tmp_path: Path
) -> None:
    """The SECOND layer, and the one this task is actually responsible for:
    `create_rule` itself must refuse the pairing with a typed
    `NotificationValidationError`, independent of whatever `RuleCreate`'s own
    validator did. Built via `model_construct`, which skips Pydantic
    validation entirely -- this is the only way to get an "invalid" RuleCreate
    instance past the schema layer, so it is the only way to prove
    `create_rule`'s OWN guard is what is actually catching this, not just the
    validator that ran when the object was constructed normally."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        bad_data = RuleCreate.model_construct(
            event_type="ticket.internal_note_added",
            channel_id=channel.id,
            recipient_kind="requester",
            queue_id=None,
            active=True,
        )
        with pytest.raises(NotificationValidationError, match="internal notes cannot be routed"):
            await create_rule(session, org_id=org_id, actor_id=user_id, data=bad_data)

        # And nothing was written -- the DB's own CHECK constraint was never
        # even reached, let alone tripped as a raw IntegrityError.
        rules = await list_rules(session, org_id=org_id)
    assert rules == []


# ---------------------------------------------------------------------------
# create_rule -- the SLA-breach rule (SLA spec §6; review finding F2)
#
# `ticket.sla_breached` used to be representable with
# `recipient_kind="requester"` at all THREE layers: each was literal to
# `ticket.internal_note_added`. Its staff-only property rested entirely on the
# publisher omitting `requester_id` from the payload -- but
# `recipient_kind="requester"` resolves the recipient from the TICKET, not
# from the payload, so the mail would in fact have been delivered, telling the
# external customer their supplier missed a contractual deadline. The three
# tests below are one per layer.
# ---------------------------------------------------------------------------


async def test_rule_create_schema_rejects_sla_breach_to_requester() -> None:
    """LAYER 1: `RuleCreate`'s own Pydantic validator."""
    with pytest.raises(ValueError, match="SLA breach notifications cannot be routed"):
        RuleCreate(
            event_type="ticket.sla_breached",
            channel_id=uuid7(),
            recipient_kind="requester",
        )


async def test_create_rule_rejects_sla_breach_to_requester_at_the_service_layer(
    api_env, tmp_path: Path
) -> None:
    """LAYER 2: `create_rule`'s own guard, proven independent of the validator
    by building the `RuleCreate` with `model_construct` (which skips Pydantic
    validation entirely) -- same technique as the internal-note test above."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        bad_data = RuleCreate.model_construct(
            event_type="ticket.sla_breached",
            channel_id=channel.id,
            recipient_kind="requester",
            queue_id=None,
            active=True,
        )
        with pytest.raises(
            NotificationValidationError, match="SLA breach notifications cannot be routed"
        ):
            await create_rule(session, org_id=org_id, actor_id=user_id, data=bad_data)

        rules = await list_rules(session, org_id=org_id)
    assert rules == []


@pytest.mark.parametrize(
    ("event_type", "constraint"),
    [
        ("ticket.internal_note_added", "ck_rule_no_internal_to_requester"),
        ("ticket.sla_breached", "ck_rule_no_sla_breach_to_requester"),
    ],
)
async def test_the_db_refuses_a_staff_only_rule_for_the_requester(
    api_env, tmp_path: Path, event_type: str, constraint: str
) -> None:
    """LAYER 3: the DB CHECK constraints (0025 and 0034), reached by raw INSERT
    on the ADMIN (BYPASSRLS) engine so that neither application guard nor RLS
    can be what rejects. Before 0034 the `ticket.sla_breached` row committed
    clean."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        channel_id = channel.id
        await session.commit()

    async with AsyncSession(api_env.admin_engine) as session:
        with pytest.raises(IntegrityError, match=constraint):
            await session.execute(
                sqltext(
                    "INSERT INTO notification_rules"
                    " (id, org_id, event_type, channel_id, queue_id, recipient_kind,"
                    "  active, created_by, created_at)"
                    " VALUES (gen_random_uuid(), :org, :evt, :chan, NULL, 'requester',"
                    "  true, :actor, now())"
                ),
                {
                    "org": org_id,
                    "evt": event_type,
                    "chan": channel_id,
                    "actor": user_id,
                },
            )
            await session.commit()


# ---------------------------------------------------------------------------
# create_rule / list_rules / delete_rule -- happy path, audit, tenancy
# ---------------------------------------------------------------------------


async def test_create_rule_writes_an_audit_event(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        rule = await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned", channel_id=channel.id, recipient_kind="assignee"
            ),
        )
        events = await _events(session, org_id, "notification.rule_created")
    assert len(events) == 1
    assert events[0].target_id == str(rule.id)
    assert events[0].actor_id == user_id


async def test_delete_rule_removes_the_row_and_audits(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        rule = await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned", channel_id=channel.id, recipient_kind="assignee"
            ),
        )
        await delete_rule(session, org_id=org_id, actor_id=user_id, rule_id=rule.id)
        events = await _events(session, org_id, "notification.rule_deleted")
        assert (await list_rules(session, org_id=org_id)) == []
    assert len(events) == 1
    assert events[0].target_id == str(rule.id)


async def test_delete_rule_is_org_scoped(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign_channel = await _channel(session, other_org_id, other_user_id, tmp_path)
        foreign_rule = await create_rule(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=foreign_channel.id,
                recipient_kind="assignee",
            ),
        )
        with pytest.raises(NotificationNotFoundError):
            await delete_rule(session, org_id=org_id, actor_id=user_id, rule_id=foreign_rule.id)


async def test_list_rules_is_org_scoped(api_env, tmp_path: Path) -> None:
    """DISCRIMINATING: a real rule row exists (for `other_org_id`) that would
    be returned if `list_rules`'s `org_id` predicate were removed. See the
    report for the empirical red/green check against this exact test."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign_channel = await _channel(session, other_org_id, other_user_id, tmp_path)
        await create_rule(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=foreign_channel.id,
                recipient_kind="assignee",
            ),
        )
        assert await list_rules(session, org_id=org_id) == []


# ---------------------------------------------------------------------------
# add_allowlist_entry / list_allowlist / delete_allowlist_entry
# ---------------------------------------------------------------------------


async def test_add_allowlist_entry_writes_an_audit_event(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        entry = await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=8065, note="ops chat"),
        )
        events = await _events(session, org_id, "notification.allowlist_added")
    assert entry.host == "mattermost.lan"
    assert entry.port == 8065
    assert len(events) == 1
    assert events[0].target_id == str(entry.id)


def test_allowlist_entry_host_is_normalised_on_write() -> None:
    """Whole-branch review finding M10: stored verbatim (no lowercasing, no
    trailing-dot stripping), an entry named `Mattermost.LAN` would never
    match `app.core.egress.post_json`'s own normalised `host` (I1's fix:
    lowercase, trailing root dot stripped) -- fails CLOSED (a usability wart,
    not a hole), but both sides of the comparison must agree. Normalised at
    the schema layer so every write path gets it for free."""
    assert AllowlistEntryCreate(host="Mattermost.LAN", port=8065).host == "mattermost.lan"
    assert AllowlistEntryCreate(host="gw.example.com.", port=443).host == "gw.example.com"
    assert AllowlistEntryCreate(host="GW.Example.Com.", port=443).host == "gw.example.com"


async def test_allowlisted_hosts_matches_what_post_json_would_compare(api_env) -> None:
    """End-to-end version of the M10 fix: an entry written with mixed case
    and a trailing dot is what `allowlisted_hosts()` (the exact `(host,
    port)` shape `post_json` checks `(host, port) in allowlisted_hosts`
    against) returns -- lowercase, no trailing dot -- so a request naming
    the host in ANY of those forms (`post_json` normalises the SAME way)
    actually matches."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="Mattermost.LAN.", port=8065),
        )
        hosts = await allowlisted_hosts(session, org_id=org_id)
    assert ("mattermost.lan", 8065) in hosts
    assert ("Mattermost.LAN.", 8065) not in hosts


async def test_add_allowlist_entry_rejects_duplicate_host_port(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=8065),
        )
        with pytest.raises(NotificationValidationError, match="already exists"):
            await add_allowlist_entry(
                session,
                org_id=org_id,
                actor_id=user_id,
                data=AllowlistEntryCreate(host="mattermost.lan", port=8065),
            )


async def test_add_allowlist_entry_same_host_different_port_is_fine(api_env) -> None:
    """Migration 0026's whole point: the same host may legitimately need two
    rows for two different ports."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        a = await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=8065),
        )
        b = await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=3000),
        )
    assert a.id != b.id


async def test_add_allowlist_entry_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two concurrent `add_allowlist_entry` calls for the same
    `(org_id, host, port)` can both miss the pre-check before either has
    written. Deterministic interleave, following
    `test_notification_channels.py`'s own create-time race test: session A's
    pre-check SELECT is intercepted, and right after it returns (a miss),
    session B runs a full, independent `add_allowlist_entry` for the exact
    same `(host, port)` and commits before session A's own insert proceeds."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "webhook_allowlist_entries.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await add_allowlist_entry(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        data=AllowlistEntryCreate(
                            host="mattermost.lan", port=8065, note="winner"
                        ),
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(NotificationValidationError, match="already exists"):
            await add_allowlist_entry(
                session_a,
                org_id=org_id,
                actor_id=user_id,
                data=AllowlistEntryCreate(host="mattermost.lan", port=8065, note="loser"),
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(WebhookAllowlistEntry).where(
                        WebhookAllowlistEntry.org_id == org_id,
                        WebhookAllowlistEntry.host == "mattermost.lan",
                        WebhookAllowlistEntry.port == 8065,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].note == "winner"


async def test_delete_allowlist_entry_removes_the_row_and_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        entry = await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=8065),
        )
        await delete_allowlist_entry(session, org_id=org_id, actor_id=user_id, entry_id=entry.id)
        events = await _events(session, org_id, "notification.allowlist_removed")
        assert (await list_allowlist(session, org_id=org_id)) == []
    assert len(events) == 1


async def test_delete_allowlist_entry_is_org_scoped(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign = await add_allowlist_entry(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=AllowlistEntryCreate(host="theirs.lan", port=443),
        )
        with pytest.raises(NotificationNotFoundError):
            await delete_allowlist_entry(
                session, org_id=org_id, actor_id=user_id, entry_id=foreign.id
            )


# ---------------------------------------------------------------------------
# allowlisted_hosts -- shape and tenancy
# ---------------------------------------------------------------------------


async def test_allowlisted_hosts_returns_host_port_tuples(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="mattermost.lan", port=8065),
        )
        await add_allowlist_entry(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=AllowlistEntryCreate(host="gitea.lan", port=3000),
        )
        hosts = await allowlisted_hosts(session, org_id=org_id)
    assert hosts == {("mattermost.lan", 8065), ("gitea.lan", 3000)}
    for host, port in hosts:
        assert isinstance(host, str)
        assert isinstance(port, int)


async def test_allowlisted_hosts_is_org_scoped(api_env) -> None:
    """DISCRIMINATING: a real allowlist row exists in `other_org_id` that
    would be returned if `allowlisted_hosts`'s `org_id` predicate were
    removed. See the report for the empirical red/green check."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await add_allowlist_entry(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            data=AllowlistEntryCreate(host="internal.theirs", port=443),
        )
        assert await allowlisted_hosts(session, org_id=org_id) == set()


# ---------------------------------------------------------------------------
# add_queue_member / remove_queue_member / list_queue_members
# ---------------------------------------------------------------------------


async def test_add_queue_member_rejects_a_cross_org_queue(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign_queue = await _queue(session, other_org_id, other_user_id)
        with pytest.raises(NotificationNotFoundError):
            await add_queue_member(
                session,
                org_id=org_id,
                actor_id=user_id,
                queue_id=foreign_queue.id,
                user_id=user_id,
            )


async def test_add_queue_member_rejects_a_cross_org_user(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        with pytest.raises(NotificationNotFoundError):
            await add_queue_member(
                session,
                org_id=org_id,
                actor_id=user_id,
                queue_id=queue.id,
                user_id=other_user_id,
            )


async def test_add_queue_member_writes_an_audit_event(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        member = await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        events = await _events(session, org_id, "notification.queue_member_added")
    assert len(events) == 1
    assert events[0].target_id == str(member.id)


async def test_add_queue_member_is_idempotent_and_audits_only_once(api_env) -> None:
    """The documented idempotency decision: re-adding an existing member is a
    successful no-op, not an error, and does not write a second audit
    event."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        first = await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        second = await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        events = await _events(session, org_id, "notification.queue_member_added")
        members = await list_queue_members(session, org_id=org_id, queue_id=queue.id)
    assert first.id == second.id
    assert len(events) == 1
    assert len(members) == 1


async def test_add_queue_member_concurrent_duplicate_never_raises_and_leaves_one_row(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two concurrent `add_queue_member` calls for the same
    `(queue_id, user_id)` must both succeed (idempotent by design -- see
    `add_queue_member`'s docstring) and never surface a raw `IntegrityError`
    from `uq_queue_member`. Deterministic interleave keyed on `_resolve_user`'s
    plain SELECT (holds no lock under READ COMMITTED, unlike the INSERT
    itself): right after session A's user-resolution SELECT returns, session
    B runs a full, independent `add_queue_member` for the exact same pair and
    commits before session A's own `ON CONFLICT DO NOTHING` insert proceeds."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as warmup:
        queue = await _queue(warmup, org_id, user_id)
        await warmup.commit()
    queue_id = queue.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "from users" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await add_queue_member(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        queue_id=queue_id,
                        user_id=user_id,
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        # Must NOT raise -- this is the idempotent path, unlike the
        # allowlist/channel CREATE races above.
        await add_queue_member(
            session_a, org_id=org_id, actor_id=user_id, queue_id=queue_id, user_id=user_id
        )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(QueueMember).where(
                        QueueMember.org_id == org_id,
                        QueueMember.queue_id == queue_id,
                        QueueMember.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_remove_queue_member_deletes_and_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        await remove_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        events = await _events(session, org_id, "notification.queue_member_removed")
        members = await list_queue_members(session, org_id=org_id, queue_id=queue.id)
    assert len(events) == 1
    assert members == []


async def test_remove_queue_member_missing_raises_not_found(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        with pytest.raises(NotificationNotFoundError):
            await remove_queue_member(
                session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
            )


async def test_list_queue_members_is_org_scoped(api_env) -> None:
    """DISCRIMINATING (per the task brief's explicit warning): a naive version
    of this test creates a foreign queue and asserts an empty result, which
    would pass trivially even with `list_queue_members`'s `org_id` predicate
    deleted, because no membership row exists under that foreign `queue_id`
    at all -- `queue_id` alone already scopes correctly since one queue
    belongs to exactly one org. To actually discriminate, a REAL membership
    row is added under the foreign queue (in its own org, legitimately) so
    dropping the `org_id` predicate would demonstrably leak it. See the
    report for the empirical red/green check against this exact test."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        foreign_queue = await _queue(session, other_org_id, other_user_id)
        await add_queue_member(
            session,
            org_id=other_org_id,
            actor_id=other_user_id,
            queue_id=foreign_queue.id,
            user_id=other_user_id,
        )
        assert (
            await list_queue_members(session, org_id=org_id, queue_id=foreign_queue.id) == []
        )


# ---------------------------------------------------------------------------
# Module boundary (spec §1 / §12.12, whole-branch review finding M2). The
# reversed direction -- `app.modules.service_desk` importing nothing from
# `app.modules.notification` -- is already asserted by
# `test_service_desk_events.py::test_service_desk_does_not_import_notification`;
# this is the other half spec §12.12 names explicitly and that test's
# docstring does not cover.
# ---------------------------------------------------------------------------


def test_notification_does_not_import_service_desk() -> None:
    """Spec §1: "It imports nothing from `app.modules.service_desk`, and a
    test enforces that." The boundary already holds today -- this module
    reaches `TicketQueue` through the shared `app.models` registry instead
    (see this file's own `from app.models import ... TicketQueue` import, and
    `rules.py`'s module docstring) -- this test is what makes that hold a
    checked invariant rather than an incidental fact a future edit could
    silently break.

    Matches actual `import`/`from` STATEMENTS only (line-anchored), not the
    substring anywhere in the file -- `rules.py`'s/`channels.py`'s own module
    docstrings *discuss* `app.modules.service_desk` in prose (explaining
    exactly why this boundary holds and how `TicketQueue`/`Credential` are
    reached instead), and a bare substring match would false-positive on
    that prose forever."""
    import_pattern = re.compile(
        r"^\s*(?:from|import)\s+app\.modules\.service_desk\b", re.MULTILINE
    )
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "notification"
    assert root.is_dir(), root
    for path in root.rglob("*.py"):
        assert not import_pattern.search(path.read_text()), path

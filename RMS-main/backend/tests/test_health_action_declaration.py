"""§0.10's per-check action declaration, and the two behaviours it unlocked
(`docs/modules/endpoint-health-alerting/spec.md` §0.10, §2.2, §6, §7.1, §7.3).

WHY THIS FILE EXISTS. §0.10 says "a check may declare up to one of each action
kind" and §7.3 turns that into the difference between an alert that mails a team
and one that is console-only -- but until migration 0044 the schema had nowhere
to store it, so `declared_actions.py` derived a declaration in which every check
notified and no check remediated. §14's silent-by-design test and §6's
escalation-skip test were therefore not WRITABLE: both need a check that
declares no `notify`, and no such check could exist. They are the first two
tests below.

WHAT THESE TESTS VARY, stated because this project's defining defect is a guard
satisfied by a CONSTANT PARAMETER rather than by the guard:

  - **`declares_notify` is varied INSIDE each test, not across tests.** Every
    behavioural test below seeds a loud check and a silent check in the SAME
    org, on the SAME device, aged the SAME amount, and asserts on both. A test
    that only seeded the silent one passes against an implementation that
    publishes nothing at all.
  - **The check TYPE is swapped by parametrization.** `cpu.busy_pct` is silent
    and `memory.used_pct` loud in one run, and the reverse in the other. Two
    checks that differed in both `check_type` and `declares_notify` would let an
    implementation keyed on the type pass; covering all four
    `(check_type, declares_notify)` combinations leaves `declares_notify` as the
    only thing the outcome can track. The previous build's own warning is what
    this answers: every check in `test_health_ingest.py` and
    `test_health_sweep.py` is `cpu.busy_pct` with `os_family=NULL`, so nothing
    there can see a bug keyed on either.
  - **The observable for §7.3 is a DELIVERED-RECIPIENT COUNT and a
    `domain_events` row count, never a payload flag.** §7.3 forbids the flag
    form in terms: "a test asserting `payload["notify"] is False` would have
    passed against the defect this amendment fixes". The notification rule
    below resolves to exactly one recipient, so "zero recipients" is a number
    that moved rather than an absence that was always there.

REMEDIATION IS ASSERTED OFF, AND ASSERTED ON ONLY WHERE DECLARED. The last
section pairs the two: a check written with no `remediate_approved_action_id`
mints no `remediate` row at all, and the same check with one mints one carrying
the approved action's id. The negative alone would pass against a build where
remediation cannot be armed by anyone; the positive alone would not show that it
is off by default.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.health.errors import HealthConflictError, HealthValidationError
from app.modules.health.ingest import ingest_report
from app.modules.health.profiles import create_profile, list_checks, replace_checks
from app.modules.health.sweep import run_alert_sweep
from tests.health_action_fixtures import seed_approved_action, seed_ticketing
from tests.health_fixtures import (
    HealthOrg,
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)
from tests.test_health_ingest import _device_row, _line, _msg

pytestmark = pytest.mark.usefixtures("admin_engine")

# The two numeric, any-OS, selector-less check types the swap parametrization
# rotates between. Both are `tick` cadence and agent-evaluated, so nothing but
# the name differs -- which is the point: the name must not decide anything.
_SWAP = [("cpu.busy_pct", "memory.used_pct"), ("memory.used_pct", "cpu.busy_pct")]


class _Pair:
    """One org, one device, one profile, and two checks that differ ONLY in
    `declares_notify`."""

    def __init__(self, org: HealthOrg, profile_id: UUID, loud: UUID, silent: UUID) -> None:
        self.org = org
        self.org_id = org.org_id
        self.device_id = org.device_id
        self.profile_id = profile_id
        self.loud = loud
        self.silent = silent


async def _loud_and_silent(engine: Any, *, silent_type: str, loud_type: str) -> _Pair:
    org = await seed_org(engine, "decl")
    profile_id = await seed_profile(engine, org.org_id)
    loud = await seed_check(
        engine, org.org_id, profile_id, check_type=loud_type, declares_notify=True
    )
    silent = await seed_check(
        engine, org.org_id, profile_id, check_type=silent_type, declares_notify=False
    )
    await seed_assignment(engine, org.org_id, profile_id)
    return _Pair(org, profile_id, loud, silent)


async def _seed_notification_rule(engine: Any, org_id: UUID, user_id: UUID) -> None:
    """An org-wide `health.alert_raised` rule resolving to exactly one
    recipient, so §7.3's observable is a recipient count that MOVED.

    Seeded directly on the admin engine, `queue_id=NULL` so it matches whatever
    the alert payload carries: this file is testing the health module's decision
    to publish, not the notification module's rule selection.
    """
    from app.models import NotificationChannel, NotificationRule

    async with AsyncSession(engine, expire_on_commit=False) as session:
        channel = NotificationChannel(
            org_id=org_id,
            kind="webhook",
            name=f"ch-{uuid4().hex[:8]}",
            config={"url": "https://hooks.example.com/health"},
            created_by=user_id,
        )
        session.add(channel)
        await session.flush()
        session.add(
            NotificationRule(
                org_id=org_id,
                event_type="health.alert_raised",
                channel_id=channel.id,
                recipient_kind="channel_default",
                created_by=user_id,
            )
        )
        await session.commit()


async def _open(engine: Any, pair: _Pair, check_id: UUID, *, now: datetime) -> UUID:
    """Open one alert for `check_id` through the real ingest path."""
    from app.models import HealthAlert

    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = await _device_row(session, pair.device_id)
        outcome = await ingest_report(
            session, pair.org_id, device, _msg([_line(check_id)]), now=now
        )
        assert outcome.opened == 1, f"expected one alert for {check_id}, got {outcome}"
        await session.commit()
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(HealthAlert.id).where(
                    HealthAlert.org_id == pair.org_id, HealthAlert.check_id == check_id
                )
            )
        ).scalar_one()


async def _events_for(engine: Any, org_id: UUID, alert_id: UUID) -> list[str]:
    """Every `domain_events` row whose subject is this alert, by type."""
    from app.models import DomainEvent

    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent.event_type)
                    .where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.subject_type == "health_alert",
                        DomainEvent.subject_id == alert_id,
                    )
                    .order_by(DomainEvent.event_type)
                )
            )
            .scalars()
            .all()
        )


async def _recipients_for(engine: Any, org_id: UUID, alert_id: UUID) -> int:
    """How many recipients the notification module would actually deliver to
    for this alert's events -- §7.3's required observable.

    Resolved from the events that were PUBLISHED, so an alert with no event
    resolves to zero by having nothing to resolve. That is exactly §7.3's
    claim: "silence is the absence of an event, not a flag on one".
    """
    from app.models import DomainEvent
    from app.modules.notification.routing import resolve_recipients

    total = 0
    async with AsyncSession(engine) as session:
        events = list(
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.subject_type == "health_alert",
                        DomainEvent.subject_id == alert_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            total += len(await resolve_recipients(session, event=event))
    return total


# ---------------------------------------------------------------------------
# §7.3 -- silence is the absence of an event, and the observable is a
# delivered-recipient count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("silent_type", "loud_type"), _SWAP)
async def test_a_check_without_notify_publishes_no_event_and_reaches_no_recipient(
    admin_engine, silent_type: str, loud_type: str
) -> None:
    """§7.3 and §14's silent-by-design test, writable for the first time.

    Four assertions and each one fails against a different defect:

      - the LOUD alert has a `health.alert_raised` row and >= 1 recipient, so
        the rule and channel really do resolve -- without this, the silent
        half's zero is the zero of an org nobody could ever mail;
      - the SILENT alert has ZERO `domain_events` rows, which is the §7.3
        amendment itself (a `"notify": false` payload key would satisfy a
        weaker test and route identically loudly);
      - the SILENT alert therefore has ZERO recipients, the observable §7.3
        names and the one a flag cannot fake;
      - and the silent alert ROW EXISTS with its action rows, because §7.3 says
        silent means unrouted, never uncounted.

    GUARD OBSERVABILITY: with `declares_notify` dropped from
    `declared_action_kinds`, the silent half observes a stored
    `health.alert_raised` row and 1 delivered recipient where it expects 0.
    """
    from app.models import HealthAlert, HealthAlertAction

    pair = await _loud_and_silent(admin_engine, silent_type=silent_type, loud_type=loud_type)
    await _seed_notification_rule(admin_engine, pair.org_id, pair.org.user_id)
    now = datetime.now(UTC)

    loud_alert = await _open(admin_engine, pair, pair.loud, now=now)
    silent_alert = await _open(admin_engine, pair, pair.silent, now=now)

    assert await _events_for(admin_engine, pair.org_id, loud_alert) == ["health.alert_raised"]
    assert await _recipients_for(admin_engine, pair.org_id, loud_alert) >= 1

    # The RECIPIENT COUNT first, deliberately: §7.3 names it as the observable,
    # and asserting it before the event list makes a regression print the number
    # of people who were mailed rather than the name of an event type.
    assert await _recipients_for(admin_engine, pair.org_id, silent_alert) == 0
    assert await _events_for(admin_engine, pair.org_id, silent_alert) == []

    # Unrouted, never uncounted (§7.3, §0.9).
    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.check_type).where(
                    HealthAlert.id == silent_alert
                )
            )
        ).one()
        assert stored.state == "active"
        assert stored.check_type == silent_type
        kinds = list(
            (
                await session.execute(
                    select(HealthAlertAction.kind).where(
                        HealthAlertAction.alert_id == silent_alert
                    )
                )
            )
            .scalars()
            .all()
        )
    assert kinds == [], (
        "a silent check declares no notify, no ticket (no queue/category configured) "
        "and no remediation, so it mints no action rows -- but the ALERT row above "
        "still exists, which is the half that must not be lost"
    )


@pytest.mark.parametrize(("silent_type", "loud_type"), _SWAP)
async def test_a_silent_check_still_writes_its_action_rows_when_it_declares_them(
    admin_engine, silent_type: str, loud_type: str
) -> None:
    """The other half of "unrouted, never uncounted": a silent check that DOES
    declare `open_ticket` mints that row.

    Without this, the previous test's empty `kinds` list is ambiguous -- it
    would equally describe an implementation that suppressed every action for a
    check with no `notify`, which is §0.9's forbidden direction (suppression
    removes actions the operator did not ask for, never evidence and never the
    actions they did).
    """
    from app.models import HealthAlertAction

    pair = await _loud_and_silent(admin_engine, silent_type=silent_type, loud_type=loud_type)
    refs = await seed_ticketing(admin_engine, pair.org_id, pair.org.user_id)
    await seed_settings(
        admin_engine,
        pair.org_id,
        default_alert_queue_id=refs.queue_id,
        default_ticket_category_id=refs.category_id,
    )
    now = datetime.now(UTC)

    silent_alert = await _open(admin_engine, pair, pair.silent, now=now)
    loud_alert = await _open(admin_engine, pair, pair.loud, now=now)

    async with AsyncSession(admin_engine) as session:

        async def kinds(alert_id: UUID) -> list[str]:
            return list(
                (
                    await session.execute(
                        select(HealthAlertAction.kind)
                        .where(HealthAlertAction.alert_id == alert_id)
                        .order_by(HealthAlertAction.kind)
                    )
                )
                .scalars()
                .all()
            )

        assert await kinds(silent_alert) == ["open_ticket"]
        assert await kinds(loud_alert) == ["notify", "open_ticket"]


# ---------------------------------------------------------------------------
# §6 / §7.3 -- escalation SKIPS a silent check, and does not stamp escalated_at
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("silent_type", "loud_type"), _SWAP)
async def test_escalation_skips_a_silent_check_without_stamping_escalated_at(
    admin_engine, silent_type: str, loud_type: str
) -> None:
    """§6: "a silent-by-design check is skipped entirely and `escalated_at` is
    not stamped", and §7.3's reason -- "an alert not worth a mail when it opened
    does not become worth one by ageing".

    THE UNSTAMPED HALF IS THE LOAD-BEARING ONE and is why this is not just "the
    event count is 1". A sweep that skipped the publish but still stamped
    `escalated_at` would pass an event-count assertion, look correct forever,
    and have silently consumed the latch: the day someone gives that check a
    `notify` action, §12 believes it has already escalated and never mails.

    Both alerts are opened at the same `now` and aged past the same
    `escalate_after_s`, so the ONLY difference the sweep can be reacting to is
    `declares_notify`.

    GUARD OBSERVABILITY: with the `"notify" not in declared_action_kinds(...)`
    skip removed from `sweep._escalate`, this observes `escalated == 2`, a
    stamped `escalated_at` on the silent alert, and a
    `health.alert_escalated` row for it.
    """
    from app.models import HealthAlert

    pair = await _loud_and_silent(admin_engine, silent_type=silent_type, loud_type=loud_type)
    await seed_settings(admin_engine, pair.org_id, escalate_after_s=300)
    now = datetime.now(UTC)

    loud_alert = await _open(admin_engine, pair, pair.loud, now=now)
    silent_alert = await _open(admin_engine, pair, pair.silent, now=now)

    later = now + timedelta(seconds=400)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        escalated = (await run_alert_sweep(session, pair.org_id, now=later))["escalated"]
        await session.commit()

    assert escalated == 1, "the loud alert escalates; the silent one is skipped entirely"
    assert await _events_for(admin_engine, pair.org_id, silent_alert) == []
    assert "health.alert_escalated" in await _events_for(admin_engine, pair.org_id, loud_alert)

    async with AsyncSession(admin_engine) as session:
        stamps = {
            row.id: row.escalated_at
            for row in (
                await session.execute(
                    select(HealthAlert.id, HealthAlert.escalated_at).where(
                        HealthAlert.org_id == pair.org_id
                    )
                )
            ).all()
        }
    assert stamps[loud_alert] is not None
    assert stamps[silent_alert] is None, (
        "skipped ENTIRELY: stamping the latch for an alert nobody was told about "
        "consumes it, so a later `notify` declaration would never escalate"
    )


# ---------------------------------------------------------------------------
# §0.10 -- the declaration round-trips through the check CRUD
# ---------------------------------------------------------------------------


async def _profile_with(session: AsyncSession, org: HealthOrg, specs: list[dict[str, Any]]):
    profile = await create_profile(
        session, org.org_id, org.user_id, name=f"p-{uuid4().hex[:8]}"
    )
    await replace_checks(session, org.org_id, org.user_id, profile.id, specs)
    return profile


def _cpu(**kw: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "check_type": "cpu.busy_pct",
        "comparator": "gt",
        "threshold_num": 95,
        "severity": "warning",
    }
    spec.update(kw)
    return spec


async def test_replace_checks_defaults_both_booleans_true_and_remediation_to_none(
    admin_engine,
) -> None:
    """A spec that declares nothing keeps the behaviour every check had before
    migration 0044 -- and gets NO remediation.

    This is the fail-closed assertion for the one kind that puts a signed job on
    an endpoint (invariant #1): the default is not "the operator probably meant
    the safe action", it is `NULL`, and there is no boolean that could be
    defaulted true by a later edit.
    """
    org = await seed_org(admin_engine, "crud")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(session, org, [_cpu()])
        await session.commit()
        checks = await list_checks(session, org.org_id, profile.id)

    assert len(checks) == 1
    assert (
        checks[0].declares_notify,
        checks[0].declares_open_ticket,
        checks[0].remediate_approved_action_id,
    ) == (True, True, None)


async def test_a_check_can_be_declared_silent_and_read_back_silent(admin_engine) -> None:
    """The write path for §7.3. Asserted on the STORED ROW rather than on the
    return value, because a service that validated the field and dropped it
    would return an object carrying whatever the caller sent."""
    from app.models import HealthCheck

    org = await seed_org(admin_engine, "crud")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(
            session, org, [_cpu(declares_notify=False, declares_open_ticket=False)]
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthCheck.declares_notify, HealthCheck.declares_open_ticket).where(
                    HealthCheck.profile_id == profile.id
                )
            )
        ).one()
    assert (row.declares_notify, row.declares_open_ticket) == (False, False)


async def test_declaring_a_remediation_stores_the_approved_actions_id(admin_engine) -> None:
    """The id goes in and the SAME id comes back out. Symmetric on purpose:
    checks are replaced as a whole set (§10), so a UI that GETs the set and PUTs
    it back must not disarm a remediation by speaking a different identifier."""
    from app.models import HealthCheck

    org = await seed_org(admin_engine, "crud")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(session, org, [_cpu(remediate_approved_action_id=action_id)])
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthCheck.remediate_approved_action_id).where(
                    HealthCheck.profile_id == profile.id
                )
            )
        ).scalar_one()
    assert stored == action_id


async def test_a_check_cannot_declare_another_orgs_approved_action(admin_engine) -> None:
    """Invariant #5 on a third column. `remediate_approved_action_id` is a plain
    FK and a Postgres foreign-key check does not consult RLS, so the FK alone
    would let org A arm a check against org B's approved action -- a command
    path into a fleet that never approved it. The org-scoped key lookup is the
    guard, exactly as `_reject_cross_org_target` is for `device_id`.

    The outcome is asserted as the STORED ROW first: a bare `pytest.raises`
    prints "DID NOT RAISE" and says nothing about whether the check was armed.
    """
    from app.models import HealthCheck

    victim = await seed_org(admin_engine, "victim")
    attacker = await seed_org(admin_engine, "attacker")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(
        admin_engine, victim.org_id, victim.user_id, key=key
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await create_profile(
            session, attacker.org_id, attacker.user_id, name=f"p-{uuid4().hex[:8]}"
        )
        await session.commit()

    error: str | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await replace_checks(
                session,
                attacker.org_id,
                attacker.user_id,
                profile.id,
                [_cpu(remediate_approved_action_id=action_id)],
            )
            await session.commit()
        except HealthValidationError as exc:
            error = str(exc)

    async with AsyncSession(admin_engine) as session:
        armed = list(
            (
                await session.execute(
                    select(HealthCheck.remediate_approved_action_id).where(
                        HealthCheck.org_id == attacker.org_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert armed in ([], [None]), f"the attacker's check was armed: {armed}"
    assert error is not None and "does not resolve in this org" in error


async def test_a_check_cannot_declare_an_action_without_the_health_persona(
    admin_engine,
) -> None:
    """§8.2's posture: an approved action naming something impossible is a
    configuration bug that must surface when someone writes it. Without the
    persona, `_refuse_reason` would mark every remediate row this check ever
    minted `skipped`, and the operator would find out from a `last_error`.

    THE STORED ROW IS ASSERTED FIRST and the exception second. A
    `pytest.raises`-only version of this prints "DID NOT RAISE" when the guard
    goes, which does not say whether the check was armed; this prints the
    action id the database is now holding on the check.
    """
    from app.models import HealthCheck

    org = await seed_org(admin_engine, "persona")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(
        admin_engine, org.org_id, org.user_id, key=key, personas=["technician"]
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await create_profile(
            session, org.org_id, org.user_id, name=f"p-{uuid4().hex[:8]}"
        )
        await session.commit()

    error: str | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await replace_checks(
                session,
                org.org_id,
                org.user_id,
                profile.id,
                [_cpu(remediate_approved_action_id=action_id)],
            )
            await session.commit()
        except HealthValidationError as exc:
            error = str(exc)

    async with AsyncSession(admin_engine) as session:
        armed = list(
            (
                await session.execute(
                    select(HealthCheck.remediate_approved_action_id).where(
                        HealthCheck.profile_id == profile.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert armed in ([], [None]), (
        f"a check was armed with an approved action carrying no 'health' persona: {armed}; "
        f"every remediation it produced would be skipped at dispatch"
    )
    assert error is not None and "persona" in error


# ---------------------------------------------------------------------------
# §7.1 / invariant #1 -- remediation is off unless declared, and the declared
# row carries the action it must dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declare_remediation", [False, True])
async def test_a_remediate_row_is_minted_only_when_the_check_declares_one(
    admin_engine, declare_remediation: bool
) -> None:
    """The pair, run over both values of the ONE parameter that decides it.

    The `False` run is the fail-closed assertion invariant #1 needs: no
    `remediate` row exists, so nothing the action worker could pick up exists,
    so no signed job reaches an endpoint through this path. The `True` run is
    what makes that a fact about the DECLARATION rather than about a build in
    which remediation is simply unreachable -- and it asserts the row carries
    the approved action's id, because a `remediate` row without one is a row
    `_execute_remediate` can only mark `skipped`.
    """
    from app.models import HealthAlertAction

    org = await seed_org(admin_engine, "rem")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)
    spec = _cpu(remediate_approved_action_id=action_id) if declare_remediation else _cpu()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(session, org, [spec])
        await session.commit()
    await seed_assignment(admin_engine, org.org_id, profile.id)

    checks = None
    async with AsyncSession(admin_engine) as session:
        checks = await list_checks(session, org.org_id, profile.id)
    check_id = checks[0].id

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        outcome = await ingest_report(
            session, org.org_id, device, _msg([_line(check_id)]), now=now
        )
        assert outcome.opened == 1
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(HealthAlertAction.kind, HealthAlertAction.approved_action_id)
                    .where(HealthAlertAction.org_id == org.org_id)
                    .order_by(HealthAlertAction.kind)
                )
            ).all()
        )

    remediate = [r for r in rows if r.kind == "remediate"]
    if declare_remediation:
        assert [(r.kind, r.approved_action_id) for r in remediate] == [
            ("remediate", action_id)
        ]
    else:
        assert remediate == [], (
            "an undeclared check must mint NO remediate row: this is the only path "
            "by which a signed job reaches an endpoint without a human (invariant #1)"
        )
    # Both runs still notify, so the `False` run's empty list is not the empty
    # list of an ingest that wrote nothing at all.
    assert [r.kind for r in rows if r.kind == "notify"] == ["notify"]


async def test_cloning_a_profile_carries_the_declaration(admin_engine) -> None:
    """A clone that dropped the declaration would silently re-arm a
    silent-by-design check, and one that dropped only the remediation would read
    as "the same profile" while answering nothing automatically."""
    from app.modules.health.profiles import clone_profile

    org = await seed_org(admin_engine, "clone")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        source = await _profile_with(
            session,
            org,
            [_cpu(declares_notify=False, remediate_approved_action_id=action_id)],
        )
        clone = await clone_profile(
            session, org.org_id, org.user_id, source.id, name=f"copy-{uuid4().hex[:8]}"
        )
        await session.commit()
        copied = await list_checks(session, org.org_id, clone.id)

    assert len(copied) == 1
    assert copied[0].declares_notify is False
    assert copied[0].remediate_approved_action_id == action_id


# ---------------------------------------------------------------------------
# The FK, and the message in front of it
# ---------------------------------------------------------------------------


async def test_deleting_an_approved_action_a_check_declares_is_refused_by_name(
    admin_engine,
) -> None:
    """The service refusal names the blocking checks; `ON DELETE RESTRICT` is
    the fact underneath it. Both are asserted, because the service message
    survives no second caller and no direct SQL fixup.

    Each half asserts the STORED ROW before the exception: dropping the service
    refusal must print the approved action that is now gone (or the
    `IntegrityError` the caller would have seen instead of a message), not
    "DID NOT RAISE".
    """
    from app.models import ApprovedAction as _ApprovedAction
    from app.modules.health.approved_actions import delete_approved_action

    org = await seed_org(admin_engine, "del")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await _profile_with(session, org, [_cpu(remediate_approved_action_id=action_id)])
        await session.commit()

    refusal: str | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await delete_approved_action(session, org.org_id, action_id, org.user_id)
            await session.commit()
        except (HealthConflictError, IntegrityError, DBAPIError) as exc:
            refusal = f"{type(exc).__name__}: {str(getattr(exc, 'orig', exc)).splitlines()[0]}"

    async with AsyncSession(admin_engine) as session:
        survivors = list(
            (
                await session.execute(
                    select(_ApprovedAction.key).where(_ApprovedAction.id == action_id)
                )
            )
            .scalars()
            .all()
        )
    assert survivors == [key], (
        f"the approved action a live check declares was deleted; the check's "
        f"remediation now names nothing. survivors={survivors}, refusal={refusal}"
    )
    assert refusal is not None and "still declared as the remediation" in refusal, (
        f"expected the service to refuse BY NAME rather than let the FK produce a bare "
        f"IntegrityError; got {refusal!r}"
    )

    # Retiring the check DISARMS it, and that is what makes the action
    # deletable again -- see the next test.

    # The database backstop, reached by going around the service entirely.
    from sqlalchemy import delete as sql_delete

    from app.models import ApprovedAction

    outcome: str
    try:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await session.execute(sql_delete(ApprovedAction).where(ApprovedAction.id == action_id))
            await session.commit()
    except (IntegrityError, DBAPIError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        outcome = "ACCEPTED: the approved action a live check names was deleted"
    assert outcome.startswith("refused: "), outcome
    assert "foreign key" in outcome.lower(), outcome


async def test_retiring_a_check_disarms_it_so_the_action_becomes_deletable(
    admin_engine,
) -> None:
    """Retiring a check clears `remediate_approved_action_id`, and the approved
    action it named can then be deleted.

    WITHOUT THE DISARM THIS IS A PERMANENT DEADLOCK, which is why it is a test
    and not a comment. A retired check is excluded from resolution, from every
    push and from the effective-profile view (§2.2), so nothing can ever edit it
    again -- but the FK is `ON DELETE RESTRICT`, so its stale declaration would
    block the approved action's deletion forever, blamed on a row the operator
    cannot see. The alert it once opened is untouched, which is §2.4's whole
    reason for retiring rather than deleting.

    THE CHECK IS ARMED ONLY AFTER IT HAS ALARMED. Two reasons, and both are
    necessary: it must have an alert so the removal path takes the RETIRE branch
    rather than the hard-delete one (a hard delete would make this vacuous), and
    it must NOT have minted a `remediate` action row -- a dispatched history row
    blocks the delete on its own (see the next test), which would hide whether
    the disarm did anything.
    """
    from app.models import ApprovedAction as _AA
    from app.models import HealthAlert, HealthCheck
    from app.modules.health.approved_actions import delete_approved_action

    org = await seed_org(admin_engine, "retire")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(session, org, [_cpu()])
        await session.commit()
        checks = await list_checks(session, org.org_id, profile.id)
    check_id = checks[0].id
    await seed_assignment(admin_engine, org.org_id, profile.id)

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        assert (
            await ingest_report(session, org.org_id, device, _msg([_line(check_id)]), now=now)
        ).opened == 1
        await session.commit()

    # Arm it now, after the alert: the check is armed but nothing was dispatched.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(
            session,
            org.org_id,
            org.user_id,
            profile.id,
            [_cpu(remediate_approved_action_id=action_id)],
        )
        await session.commit()

    # Remove the check from the profile: it has alarmed, so this RETIRES it.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [])
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        retired = (
            await session.execute(
                select(
                    HealthCheck.retired_at, HealthCheck.remediate_approved_action_id
                ).where(HealthCheck.id == check_id)
            )
        ).one()
        surviving_alerts = int(
            len(
                (
                    await session.execute(
                        select(HealthAlert.id).where(HealthAlert.check_id == check_id)
                    )
                )
                .scalars()
                .all()
            )
        )
    assert retired.retired_at is not None, "the check alarmed, so it is retired, not deleted"
    assert retired.remediate_approved_action_id is None, (
        "a retired check must be disarmed: nothing can edit it again, so a stale "
        "declaration would block the approved action's deletion forever"
    )
    assert surviving_alerts == 1, "§2.4: retiring never destroys the alert record"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await delete_approved_action(session, org.org_id, action_id, org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        left = list(
            (await session.execute(select(_AA.key).where(_AA.id == action_id))).scalars().all()
        )
    assert left == [], f"the action should now be deletable; still present: {left}"


async def test_a_dispatched_approved_action_is_refused_by_name_not_by_integrityerror(
    admin_engine,
) -> None:
    """The second refusal (§2.4's rule that history is never destroyed because
    an admin deleted a config row).

    `health_alert_action.approved_action_id` predates the declaration columns
    and had no message in front of it, so this used to surface as a raw
    `IntegrityError` naming a constraint and no alerts. It is reachable far more
    easily now that a check can arm itself, which is why it is named here.

    The check is RETIRED first, so the OTHER refusal is out of the way and this
    test cannot pass on the wrong one.

    GUARD OBSERVABILITY: without the refusal this fails with the bare
    `ForeignKeyViolationError` on `health_alert_action_approved_action_id_fkey`,
    which is exactly what an operator would otherwise have seen.
    """
    from app.models import HealthAlertAction
    from app.modules.health.approved_actions import delete_approved_action

    org = await seed_org(admin_engine, "hist")
    key = f"act-{uuid4().hex[:8]}"
    action_id = await seed_approved_action(admin_engine, org.org_id, org.user_id, key=key)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = await _profile_with(
            session, org, [_cpu(remediate_approved_action_id=action_id)]
        )
        await session.commit()
        checks = await list_checks(session, org.org_id, profile.id)
    check_id = checks[0].id
    await seed_assignment(admin_engine, org.org_id, profile.id)

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        assert (
            await ingest_report(session, org.org_id, device, _msg([_line(check_id)]), now=now)
        ).opened == 1
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        dispatched = list(
            (
                await session.execute(
                    select(HealthAlertAction.approved_action_id).where(
                        HealthAlertAction.org_id == org.org_id,
                        HealthAlertAction.kind == "remediate",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert dispatched == [action_id], "the armed check must have minted the history row"

    # Retire the check so the FIRST refusal (a live check declares it) is gone.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [])
        await session.commit()

    refusal: str | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await delete_approved_action(session, org.org_id, action_id, org.user_id)
            await session.commit()
        except (HealthConflictError, IntegrityError, DBAPIError) as exc:
            refusal = f"{type(exc).__name__}: {str(getattr(exc, 'orig', exc)).splitlines()[0]}"

    assert refusal is not None and "has been dispatched" in refusal, (
        f"expected a named refusal pointing at the alert history; got {refusal!r}"
    )

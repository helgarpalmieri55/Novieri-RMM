"""Report ingestion: §0.8's OPEN predicate and three of its six CLEAR clauses
(`docs/modules/endpoint-health-alerting/spec.md` §6, §0.7, §0.8, §0.12).

THE TWO FAILURE MODES THIS FILE IS DESIGNED AGAINST, from §0.8: (a) an alert
that can be opened but never cleared, and (b) an alert that re-opens every tick.
(b) is closed structurally, by `uq_health_alert_open_key`, a partial unique
index Postgres enforces. (a) IS NOT CLOSED BY COUNTING THE CLAUSES, and the
docstring that used to say so ("six ways to clear rather than one") was wrong in
a way that cost a real defect: the six are not six ways for any ONE alert, and
which of them can reach a given alert depends on how its check is evaluated.

  - An AGENT-evaluated check reaches all six. This file drives clauses 1-3
    (`recovery`, `summary_ok`, `unavailable_timeout`) off lines the agent sends,
    the profile-write path drives clause 4, and §12's sweeps and the API drive
    5 and 6.
  - The one SERVER-evaluated check, `agent.offline` (§0.6), reaches clauses 4-6
    and ONE MORE THAT ONLY THE OFFLINE SWEEP CAN DRIVE. It reaches none of this
    file's three, and that is a property of `by_id` below: the lookup is keyed
    off `resolved.agent_checks`, so every line naming a server-evaluated check
    is discarded before `kind` is ever read. `sweep._clear_recovered_offline`
    is where its recovery lives, and its docstring states why.

Adding a second server-evaluated check type without a clear path in the same
change would re-introduce failure mode (a) for it, silently.

ONE STALE LINE MUST NEVER REJECT A BATCH. A `health_report` is at-least-once and
may carry a breach for a check an admin removed thirty seconds ago beside a
recovery for one that still exists. Discarding the first and applying the second
is the only behaviour that does not lose a real finding, so the per-line path
NEVER raises: it counts and continues. `HealthIngestError` exists for a whole
batch that cannot be attributed at all, which is the only thing §11.3's
`health.ingest_rejected` audit record is for.

WHERE THE DISCARD COUNT GOES, and why it is not a table (§0.8's amendment).
`ingest_report` RETURNS the counts; they ride the post-commit UI event on the
shape `_handle_security_events` already publishes; and a non-zero `discarded`
logs at WARNING with the device id and the offending `(check_id, selector)`
pairs, capped at the first 10 per batch. There is no `health_ingest_stats`
table and none will be added: §0.12's "exactly three tables retain anything" is
the monitoring-boundary guarantee this module exists to keep, and a per-batch
counter table is a time series wearing an ops-metrics hat.

THE MONITORING BOUNDARY IS A PROPERTY OF THIS FILE MORE THAN OF THE SCHEMA. A
hundred summaries for a stable device must leave every row count unchanged --
`device_health_check_state` is UPSERTed on `(org_id, device_id, check_id,
selector)` and nothing else here inserts. `tests/test_health_ingest.py::
test_no_health_table_grows_with_time_for_a_stable_device` is the property test
for "what was this device's CPU last Tuesday?" being unanswerable.

`unavailable` IS A THIRD STATE, NOT A SHADE OF EITHER OTHER ONE (§0.7). It never
opens an alert -- a "disk encryption is off" alert fired because the check could
not run trains operators to ignore posture alerts -- and it never clears one
except through §0.8 clause 3's time-boxed, separately attributed
`unavailable_timeout`. A recovery line that reports `unavailable` therefore
clears nothing: "the sensor stopped answering" is not "the problem went away".

AND `unavailable` IS NOT THE ONLY WAY A `state` CAN DISAGREE WITH ITS `kind`.
The two are independent enums on the wire (`gateway/protocol.py`) with no
cross-validation between them, so a line can assert an edge its own state
denies. The rule above handled ONE such pair and left the other: a
`recovery` reporting `breaching` cleared a live alert while
`_upsert_check_state` -- which runs first and trusts the line as data -- left
the key reading `breaching`, so the pair re-opened and re-cleared every tick.
`_contradicts_kind` now refuses BOTH (`breach` that is `ok`, `recovery` that is
`breaching`) from moving an alert, and deliberately does not touch
`unavailable`, whose separate, correct handling is the paragraph above.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.protocol import HealthReportLine, HealthReportMsg
from app.models import Device
from app.modules.health.alerts import (
    clear_alert,
    link_previous_alert,
    open_alert_for_key,
    publish_alert_event,
)
from app.modules.health.declared_actions import approved_action_id_for, declared_action_kinds
from app.modules.health.flap import AlertKey, note_open
from app.modules.health.models import (
    DeviceHealthCheckState,
    DeviceHealthPolicyAck,
    HealthAlert,
    HealthAlertAction,
    HealthCheck,
)
from app.modules.health.policy_push import record_policy_ack_from_summary
from app.modules.health.registry import CHECK_TYPES, render_threshold
from app.modules.health.resolution import ResolvedProfile, resolve_profile_for_device
from app.modules.health.settings import EffectiveHealthSettings, get_settings

__all__ = ["MAX_LOGGED_DISCARDS", "HealthIngestError", "IngestOutcome", "ingest_report"]

logger = logging.getLogger("its.health.ingest")

# §0.8's amendment: "capped at the first 10 per batch so a misconfigured fleet
# cannot turn the log into the metric store the table was rejected for being."
MAX_LOGGED_DISCARDS = 10


class HealthIngestError(Exception):
    """A whole batch that cannot be attributed. Audited once as
    `health.ingest_rejected` (§11.3), never per line -- per-report audit records
    would swamp a hash-chained log whose value is that a human can read it."""


@dataclass(frozen=True)
class IngestOutcome:
    """What one batch did, as values a caller can assert on.

    `applied`/`discarded` are §0.8's amendment's counters. `opened`/`cleared`
    are returned as well because a test that asserts "no exception was raised"
    passes against every broken implementation, and these are the two numbers
    that separate a batch that did the work from one that silently did nothing.
    """

    applied: int = 0
    discarded: int = 0
    opened: int = 0
    cleared: int = 0
    suppressed: int = 0
    # Lines whose `state` contradicts their `kind` (see `_contradicts_kind`).
    # Counted rather than only logged, because "the alert did not move" is
    # equally true of a correct implementation and of one that never looked.
    contradictory: int = 0
    discarded_keys: tuple[tuple[str, str], ...] = field(default=())


def _parse_observed_at(raw: str, *, fallback: datetime) -> datetime:
    """The agent's RFC3339 stamp, or the server clock.

    USED ONLY FOR `device_health_check_state.reported_at`, never for
    `opened_at`/`last_seen_at`. An agent with a skewed clock would otherwise put
    an alert's `opened_at` in the future, and `ck_health_alert_mute_bounded`
    (`muted_until <= opened_at + 30 days`) would then accept a mute nobody could
    have chosen. Server time is the authority for anything a constraint or an
    interval reads; agent time is data.
    """
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _acknowledged_version(session: AsyncSession, org_id: UUID, device_id: UUID) -> int:
    """§0.8 clause 2's "last acknowledged version".

    A MISSING ROW READS AS 0 (§2.11), which makes clause 2 vacuously true --
    deliberately, so a device that has never acknowledged a policy does not have
    its breaches discarded. Failing the other way would silence exactly the
    fleet nobody has finished configuring.
    """
    version = (
        await session.execute(
            select(DeviceHealthPolicyAck.policy_version).where(
                DeviceHealthPolicyAck.org_id == org_id,
                DeviceHealthPolicyAck.device_id == device_id,
            )
        )
    ).scalar_one_or_none()
    return int(version or 0)


async def _upsert_check_state(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    check: HealthCheck,
    line: HealthReportLine,
    *,
    policy_version: int,
    now: datetime,
) -> None:
    """§2.6. UPSERT ONLY -- one row per `(org, device, check, selector)`, ever.

    This is §0.12 clause 2 in one statement: `updated_at` moves and nothing
    accumulates, so cardinality is devices x 32 and CONSTANT IN TIME. An
    innocent "just keep the last ten values" here is the change that turns this
    module into the monitoring product.
    """
    values = {
        "org_id": org_id,
        "device_id": device_id,
        "check_id": check.id,
        "selector": line.selector,
        "state": line.state,
        "last_value": line.value,
        "policy_version": policy_version,
        "reported_at": _parse_observed_at(line.observed_at, fallback=now),
        "updated_at": now,
    }
    statement = pg_insert(DeviceHealthCheckState).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["org_id", "device_id", "check_id", "selector"],
            set_={
                "state": statement.excluded.state,
                "last_value": statement.excluded.last_value,
                "policy_version": statement.excluded.policy_version,
                "reported_at": statement.excluded.reported_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


def _threshold_display(check: HealthCheck) -> str:
    entry = CHECK_TYPES.get(check.check_type)
    if entry is None:  # pragma: no cover - resolution already excluded these
        return ""
    try:
        rendered = render_threshold(
            entry, threshold_num=check.threshold_num, threshold_bool=check.threshold_bool
        )
    except ValueError:  # pragma: no cover - a row the registry cannot render
        return ""
    return f"{check.comparator} {rendered}"


async def _open_alert(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    check: HealthCheck,
    line: HealthReportLine,
    *,
    profile_id: UUID,
    policy_version: int,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> tuple[bool, bool]:
    """§0.8's OPEN predicate for one breach line. Returns `(opened, suppressed)`.

    Clause 3 first, as an UPDATE: a duplicate breach for a key that already has
    a non-cleared alert bumps `last_seen_at` and `occurrence_count` and stops.
    `uq_health_alert_open_key` would refuse the insert anyway, but an
    `IntegrityError` aborts the whole ingest transaction and would take a
    recovery in the same batch down with it -- which is why §0.8 says the index
    is the guarantee and the in-code check is the ordinary path.

    Clause 4 (flap) is evaluated for EVERY open, including the first, because
    `note_open` is also what counts. Suppression removes the ACTIONS and never
    the evidence: the alert row is still written, with `flapping = true` and the
    count on it, and its action rows are `blocked_flapping` rather than absent.
    """
    existing = await open_alert_for_key(
        session, org_id, device_id=device_id, check_id=check.id, selector=line.selector
    )
    if existing is not None:
        existing.last_seen_at = now
        existing.occurrence_count += 1
        existing.observed_value = line.value
        existing.updated_at = now
        session.add(existing)
        await session.flush()
        return False, False

    decision = await note_open(
        session,
        org_id,
        AlertKey(device_id=device_id, check_id=check.id, selector=line.selector),
        now=now,
    )
    previous_id = await link_previous_alert(
        session,
        org_id,
        device_id=device_id,
        check_id=check.id,
        selector=line.selector,
        now=now,
        window_s=settings.flap_window_s,
    )
    alert = HealthAlert(
        org_id=org_id,
        device_id=device_id,
        check_id=check.id,
        profile_id=profile_id,
        selector=line.selector,
        check_type=check.check_type,
        severity=check.severity,
        state="active",
        observed_value=line.value,
        threshold_display=_threshold_display(check),
        opened_at=now,
        opened_policy_version=policy_version,
        last_seen_at=now,
        occurrence_count=1,
        flapping=decision.suppressed,
        flap_count=decision.flap_count,
        previous_alert_id=previous_id,
        created_at=now,
        updated_at=now,
    )
    session.add(alert)
    await session.flush()

    # §0.10: the action rows are written in the ALERT'S OWN TRANSACTION, in
    # state `pending`, and a worker executes them afterwards. Not inlined --
    # `deploy/scripts.py::run_script` states the reason in its own docstring: a
    # job published before the caller's tenant transaction commits could be
    # handed to an agent with no durable server record if the commit failed.
    state = "blocked_flapping" if decision.suppressed else "pending"
    for kind in declared_action_kinds(check, settings):
        session.add(
            HealthAlertAction(
                org_id=org_id,
                alert_id=alert.id,
                kind=kind,
                state=state,
                # §0.10/§7.1: a `remediate` row carries the approved action its
                # CHECK names. Without it `_execute_remediate` can only skip.
                approved_action_id=approved_action_id_for(check, kind),
                next_attempt_at=now,
                last_error=(
                    f"actions suppressed: this key opened {decision.flap_count} times inside "
                    f"the flap window; suppressed until {decision.suppressed_until}"
                    if decision.suppressed
                    else None
                ),
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()

    if not decision.suppressed:
        await publish_alert_event(
            session,
            org_id,
            alert,
            event_type="health.alert_raised",
            check=check,
            settings=settings,
            actor_user_id=None,
        )
    return True, decision.suppressed


def _contradicts_kind(line: HealthReportLine) -> bool:
    """Does this line's `state` contradict its own `kind`?

    `gateway/protocol.py` declares `kind` and `state` as two INDEPENDENT enums
    with no cross-validation, so `{"kind": "recovery", "state": "breaching"}`
    parses cleanly -- and every combination the agent can emit by accident, an
    old build can emit by version skew, or a compromised agent can emit on
    purpose, arrives here. The module already knew this: §0.7's rule that an
    `unavailable` recovery clears nothing IS a cross-validation, just of one
    pair out of four.

      - `breach` asserts the condition is TRUE, so `ok` contradicts it. Opening
        an alert on a line that reports the check as fine is failure mode (b)
        with no way out: nothing will ever send a `recovery` for a condition
        that was never breaching.
      - `recovery` asserts the condition ENDED, so `breaching` contradicts it.
        This is the one that cost a real defect: `_clear_for_line` excluded
        `unavailable` and nothing else, so a `recovery`+`breaching` line cleared
        the alert while `_upsert_check_state` -- which runs first and trusts the
        line as data -- left the key's CURRENT state reading `breaching`. The
        console then shows a breaching check with no alert, and because the
        state row still breaches, the next real edge re-opens it: the pair
        oscillates open/cleared at the tick interval, each cycle writing a
        `health.alert_raised` and a `health.alert_cleared`.
      - `summary` is a periodic RESTATEMENT and carries all three states
        legitimately (§0.7). It is never contradictory.

    `unavailable` IS NOT A CONTRADICTION, IT IS §0.7'S THIRD STATE. "The check
    could not be answered" is a legitimate thing for either edge to report -- a
    service was uninstalled, a volume unmounted -- and the module already has
    correct, separate handling for it: an `unavailable` breach opens nothing, an
    `unavailable` recovery clears nothing, and only clause 3's aged `summary`
    clears, under its own reason. Folding it in here would log a WARNING about a
    well-behaved agent and file a real removed-sensor signal as a protocol bug.

    A CONTRADICTORY LINE IS NOT A DISCARD. §0.8 clause 1's discard means "this
    check is not in the effective profile"; here the check is real and the
    observation is usable -- it is only the state TRANSITION that cannot be
    trusted. So `_upsert_check_state` still runs and the console still shows the
    latest value; what is refused is opening or clearing an alert on it.
    """
    if line.state == "unavailable":
        return False
    if line.kind == "breach":
        return line.state != "breaching"
    if line.kind == "recovery":
        return line.state != "ok"
    return False


async def _clear_for_line(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    check: HealthCheck,
    line: HealthReportLine,
    *,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> bool:
    """§0.8's CLEAR clauses 1-3, the three ingestion drives.

    1. `recovery` + `ok`                -> `recovery`
    2. `summary` + `ok`                 -> `summary_ok`
    3. `summary` + `unavailable`, aged  -> `unavailable_timeout`

    Clause 2 is the repair path for a LOST RECOVERY EDGE -- a device that
    rebooted between breach and recovery, an at-least-once buffer that dropped
    the message. Without it every dropped recovery is a permanent alert, which
    is §0.8's failure mode (a).

    Clause 3 is time-boxed and separately attributed because the disk was
    removed, the service was uninstalled, the volume was unmounted -- and an
    alert cleared that way is a REMOVED SENSOR, not a fixed problem, so it must
    never count as a resolution in an MTTR figure (§13). It is anchored to the
    alert's `last_seen_at` -- see the `age` line below.

    THE CALLER HAS ALREADY REJECTED A LINE WHOSE `state` CONTRADICTS ITS `kind`
    (`_contradicts_kind`), so clause 1 here reads the positive form, `state ==
    "ok"`, rather than "anything but `unavailable`". The negative form was the
    defect: it excluded one of the two contradictions and let the other --
    `recovery` reporting `breaching` -- clear a live alert.
    """
    alert = await open_alert_for_key(
        session, org_id, device_id=device_id, check_id=check.id, selector=line.selector
    )
    if alert is None:
        return False

    reason: str | None = None
    if line.kind == "recovery" and line.state == "ok":
        reason = "recovery"
    elif line.kind == "summary" and line.state == "ok":
        reason = "summary_ok"
    elif line.kind == "summary" and line.state == "unavailable":
        # ANCHORED TO `last_seen_at`, NOT `opened_at`. `last_seen_at` is the
        # last moment the condition was CONFIRMED still breaching -- stamped at
        # open and bumped by every duplicate breach -- so this measures "how
        # long since anything reconfirmed this finding", which is the question
        # clause 3 is actually asking. `opened_at` measured the alert's total
        # age, which meant a real breach alive for ten days cleared as
        # `unavailable_timeout` on its FIRST EVER unavailable summary: the
        # threshold had been passed nine days before the sensor went quiet.
        # That is the worst possible direction, because §13 requires reporting
        # to EXCLUDE this reason from resolution counts -- so the finding
        # vanished from the open list and from the resolved list at once.
        age = now - alert.last_seen_at
        if age > timedelta(seconds=settings.unavailable_clear_after_s):
            reason = "unavailable_timeout"

    if reason is None:
        return False
    return await clear_alert(
        session, org_id, alert, reason=reason, now=now, check=check, settings=settings
    )


async def ingest_report(
    session: AsyncSession,
    org_id: UUID,
    device: Device,
    msg: HealthReportMsg,
    *,
    now: datetime | None = None,
) -> IngestOutcome:
    """Apply one `health_report` batch for `device`, in the caller's transaction.

    `org_id` IS A PARAMETER AND IS COMPARED AGAINST `device.org_id`, matching
    `push_policy` and `resolve_profile_for_device`. The plan wrote the signature
    as `ingest_report(session, device, msg)`; taking the org from the device row
    alone would make the function unable to disagree with a caller that handed
    it the wrong device, and the org here comes from the CERTIFICATE-BOUND
    connection (invariant #2), never from message content. A mismatch writes
    nothing and returns an empty outcome -- the gateway audits it as an identity
    mismatch on its own path.

    `now` is injected so a whole batch shares one instant: two lines must not
    straddle a flap-window boundary because the second was evaluated a
    millisecond later.
    """
    now = now or datetime.now(UTC)
    if device.org_id != org_id:
        logger.error(
            "health: refusing to ingest a report for device %s (org %s) as org %s",
            device.id,
            device.org_id,
            org_id,
        )
        return IngestOutcome()

    settings = await get_settings(session, org_id)
    resolved: ResolvedProfile | None = await resolve_profile_for_device(session, org_id, device)
    # THE PUSH SET, NOT THE APPLICABLE SET. `agent_checks` excludes every
    # server-evaluated type (§0.6), and an agent reporting on a check it was
    # never sent -- `agent.offline` above all -- is reporting on something it
    # cannot observe. Discarding those is the same clause-1 discard as a removed
    # check, and it is counted the same way.
    by_id: dict[str, HealthCheck] = (
        {str(check.id): check for check in resolved.agent_checks} if resolved else {}
    )
    acknowledged = await _acknowledged_version(session, org_id, device.id)

    applied = discarded = opened = cleared = suppressed = contradictory = 0
    discarded_keys: list[tuple[str, str]] = []
    contradictory_keys: list[tuple[str, str, str]] = []

    for line in msg.reports:
        check = by_id.get(line.check_id)
        if check is None:
            # §0.8 clause 1. Discarded and COUNTED, never silently dropped, and
            # never raised: the next line in this batch may be the recovery that
            # closes a real alert.
            discarded += 1
            if len(discarded_keys) < MAX_LOGGED_DISCARDS:
                discarded_keys.append((line.check_id, line.selector))
            continue
        applied += 1

        await _upsert_check_state(
            session,
            org_id,
            device.id,
            check,
            line,
            policy_version=msg.policy_version,
            now=now,
        )

        if _contradicts_kind(line):
            # The observation was stored above -- the console still shows the
            # latest value -- but no alert TRANSITION is taken from a line that
            # disagrees with itself. `_contradicts_kind`'s docstring states the
            # four pairs and what each one costs.
            contradictory += 1
            if len(contradictory_keys) < MAX_LOGGED_DISCARDS:
                contradictory_keys.append((line.check_id, line.kind, line.state))
            continue

        if line.kind == "breach":
            if line.state == "unavailable":
                # §0.7, stated as code: a check that cannot be answered opens
                # nothing. An alert fired because the check could not run is
                # worse than no check.
                continue
            if msg.policy_version < acknowledged - 1:
                # §0.8 clause 2, with its one version of tolerance so a breach
                # in flight across a policy push is not lost.
                discarded += 1
                if len(discarded_keys) < MAX_LOGGED_DISCARDS:
                    discarded_keys.append((line.check_id, line.selector))
                applied -= 1
                continue
            was_opened, was_suppressed = await _open_alert(
                session,
                org_id,
                device.id,
                check,
                line,
                profile_id=check.profile_id,
                policy_version=msg.policy_version,
                settings=settings,
                now=now,
            )
            opened += int(was_opened)
            suppressed += int(was_suppressed)
        elif line.kind in ("recovery", "summary"):
            cleared += int(
                await _clear_for_line(
                    session, org_id, device.id, check, line, settings=settings, now=now
                )
            )

    if any(line.kind == "summary" for line in msg.reports):
        # §2.11's SECOND ack writer, "the channel that survives a lost result".
        # Outside the per-line loop and independent of `applied`: the version is
        # a property of the REPORT, and the case this repair exists for is the
        # one where every line was discarded because the checks were removed
        # from under the device. See `policy_push.record_policy_ack_from_summary`.
        await record_policy_ack_from_summary(
            session, org_id, device, policy_version=msg.policy_version, now=now
        )

    await session.flush()

    if discarded:
        logger.warning(
            "health: discarded %d of %d report line(s) from device %s (org %s); first %d "
            "offending (check_id, selector) pairs: %s. A device reporting stale check ids "
            "is a device whose device_health_policy_ack.policy_version is behind its "
            "profile's -- the reconciliation sweep is what fixes the cause.",
            discarded,
            len(msg.reports),
            device.id,
            org_id,
            len(discarded_keys),
            discarded_keys,
        )

    if contradictory:
        logger.warning(
            "health: %d of %d report line(s) from device %s (org %s) carry a state that "
            "contradicts their kind and were not allowed to move an alert; first %d "
            "offending (check_id, kind, state) triples: %s. An agent emitting these is "
            "running a build whose edge detection disagrees with its own state machine.",
            contradictory,
            len(msg.reports),
            device.id,
            org_id,
            len(contradictory_keys),
            contradictory_keys,
        )

    return IngestOutcome(
        applied=applied,
        discarded=discarded,
        opened=opened,
        cleared=cleared,
        suppressed=suppressed,
        contradictory=contradictory,
        discarded_keys=tuple(discarded_keys),
    )

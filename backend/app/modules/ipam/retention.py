"""The pass that bounds the three append-mostly IPAM tables
(`docs/modules/ipam/spec.md` §4.2, §2.8, §2.10).

**§4.2 NAMES THIS PASS AND NOTHING IMPLEMENTED IT.** `observations.py` says, at
the top of `current_observations`, that rows outside `W_obs` are excluded rather
than deleted because "the 365-day retention pass is what bounds the table". The
spec says the same thing in one line. Until this module existed that sentence
described nothing: `ipam_observation` had no delete path anywhere in the
repository, and a fleet sweeping a /22 weekly writes up to 1024 rows per sweep
that nothing ever removes. A comment promising a bound is not a bound.

THREE TABLES, THREE DIFFERENT RULES, because they hold three different kinds of
thing:

1. **`ipam_observation` -- 365 days on `last_seen_at`, per §4.2.** The upsert
   keeps this a current-state table rather than a probe log (§0.15), so what
   accumulates here is not sightings, it is *addresses and MACs that were seen
   once and never again*: a decommissioned printer, a laptop that visited the
   guest VLAN in March, every recycled address's previous occupant. Each is
   evidence for exactly as long as somebody might ask "what used to be at
   10.4.7.9", and 365 days is the spec's answer.

2. **`ipam_drift` -- 365 days on `resolved_at`, and RESOLVED ROWS ONLY.** An
   open finding is a finding, not a record; deleting one because it is old
   would be the module quietly forgetting a problem nobody fixed. That is
   `health.run_retention`'s rule for `health_alert` and it is the right one
   here for the same reason. §0.8 stamps `resolved_at` rather than deleting so
   acknowledgement and `first_detected_at` survive a flap -- which means the
   resolved rows are the ones that pile up, one per address per drift class per
   lifetime of the org, and nothing removes them.

3. **`ipam_sweep_run` -- TWO WINDOWS, and the newest run per subnet is never
   deleted.** See below; this is the interesting one.

### Why `ipam_sweep_run` gets a shorter window for `refused` and `no_agent`

`handle_sweep_result` is the ONLY writer of `ipam_subnet.last_swept_at`. So a
subnet that is `sweep_enabled` but has no authorization covering its prefix is
re-offered by `due_subnet_ids` on every hourly beat, forever, and every one of
those beats records a `refused` run: **8,760 rows per subnet per year, for a
subnet nobody ever authorized.** `no_agent` does the same for a segment with no
eligible sweeper on it. `set_sweep_settings` calls that refused run "the correct
and visible outcome", and it is -- but *visible* and *unbounded* are different
properties, and only the first was delivered.

These two states are not work. They are **the same configuration fact,
restated hourly**: "nobody has authorized this prefix", "no machine on this wire
can sweep it". The hundredth identical restatement carries no information the
first did not, so they age out after 30 days while real sweeps
(`succeeded`/`failed`/`dispatched`/`queued`) keep the full year of history an
operator would actually page back through.

**THE NEWEST RUN FOR A SUBNET IS NEVER DELETED, whatever its state or age.**
This is what keeps a persistently-unauthorized subnet from going *silent* rather
than merely quiet. What an operator sees, either way:

* **With this pass:** the subnet page's run list shows the most recent refusal
  -- `refused`, "not authorized", `started_at` from within the last hour --
  followed by 30 days of the same, and nothing older. The subnet is still
  visibly refusing, on every beat, and `GET /subnets/{id}/sweep-runs` still
  answers with a run rather than an empty list. An empty list means "never
  swept"; a refusal means "we tried and were told no", and those must not
  collapse into one answer.
* **Without the never-delete-the-newest rule:** a subnet whose only runs are
  refusals older than the window would read as *never swept* between the
  deletion and the next beat -- the module forgetting, once an hour, that it is
  being refused.

Rejected: suppressing the duplicate refusal at dispatch time (in `create_sweep`,
by not recording a `refused` run when the previous run is an identical refusal).
That bounds the table at the source and is tempting, but it moves a security
control's evidence trail into a de-duplication heuristic -- "no run was recorded"
and "no beat fired" become the same observation, and §2.10 exists precisely so
they are not. Retention deletes history it has already shown; suppression never
writes it.

Rejected also: deleting a run that is still referenced. `ipam_observation.
sweep_run_id` is a real FK with no `ON DELETE`, so a run with a surviving
observation pointing at it is skipped rather than having the pointer nulled --
"which sweep last saw this address" stays resolvable for as long as the
observation does. Observations are deleted FIRST for the same reason: a run and
the sightings it produced age out together, in one transaction, in the order the
foreign key requires.

**WHAT THIS PASS DOES TO §0.16's GATE H.** Gate H asks whether a subnet has
*ever* been looked at -- a `succeeded` run or any observation, with no window --
and this pass can delete both. §0.16 already accepts exactly that failure mode
("a subnet whose ... single ancient observation was dropped by the 365-day
retention pass reads as `assigned` when `dormant` is arguably more accurate ...
the misclassification costs a label in one cell of the address grid and nothing
else"). The never-delete-the-newest rule narrows it further: a subnet with any
sweep history at all keeps its most recent run.

EVERY STATEMENT CARRIES AN EXPLICIT `org_id` PREDICATE in addition to RLS
(invariant #5). `sweep_org` takes a session, and nothing stops a future caller
handing it an admin-engine one -- as `ai_admin.retention.sweep_org`'s docstring
records, an `org_session`-only test passes with the predicate deleted, so the
admin-engine test in `tests/test_ipam_retention.py` is what pins it.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Organization
from app.modules.ipam.models import IpamDrift, IpamObservation, IpamSweepRun

__all__ = [
    "DRIFT_RETENTION_DAYS",
    "OBSERVATION_RETENTION_DAYS",
    "RESTATED_STATES",
    "RESTATED_SWEEP_RUN_RETENTION_DAYS",
    "RETENTION_INTERVAL_S",
    "SWEEP_RUN_RETENTION_DAYS",
    "sweep_all_orgs",
    "sweep_org",
]

logger = logging.getLogger("its.ipam.retention")

#: Daily, matching every other retention beat in `app/worker.py`. Retention is a
#: clock, not a detector: running it more often costs writes and changes nothing.
RETENTION_INTERVAL_S = 86400.0

#: §4.2, verbatim: "A retention pass drops observations with
#: `last_seen_at < now() - 365 days` to keep the table bounded."
OBSERVATION_RETENTION_DAYS = 365

#: The same year, on `resolved_at`. An OPEN finding is never deleted.
DRIFT_RETENTION_DAYS = 365

#: A year of real sweep history -- what an operator paging back through a
#: segment's runs would actually want.
SWEEP_RUN_RETENTION_DAYS = 365

#: The two states that are a configuration fact restated hourly rather than work
#: performed. See this module's docstring for why they age out sooner.
RESTATED_STATES = ("refused", "no_agent")
RESTATED_SWEEP_RUN_RETENTION_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _deleted(result: object) -> int:
    """`rowcount` off a DELETE. `Session.execute` is typed as returning a
    `Result`, which has no `rowcount`; what a DML statement actually returns is a
    `CursorResult`, which does. Narrowed here rather than with a `cast` at each
    of the three call sites."""
    return int(getattr(result, "rowcount", 0) or 0)


async def sweep_org(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """Delete this org's expired observations, resolved drift and stale runs.

    Returns `{"observations": n, "drift": n, "sweep_runs": n}` -- three counts
    rather than one total, because a pass that stopped deleting one of the three
    would otherwise be invisible in the number.

    ORDER IS LOAD-BEARING. `ipam_observation.sweep_run_id` references
    `ipam_sweep_run` with no `ON DELETE`, so the observations go first and the
    runs are then filtered on "nothing still points at me". Reversing the two
    raises `ForeignKeyViolationError` and takes the org's whole pass down.

    BOUNDARY: STRICTLY OLDER THAN THE CUTOFF. A row exactly `days` old survives;
    the next tick takes it. `<=` at the boundary is a day of evidence an operator
    was told they had.
    """
    stamp = now or _utcnow()

    observations = await session.execute(
        delete(IpamObservation).where(
            IpamObservation.org_id == org_id,
            IpamObservation.last_seen_at
            < stamp - timedelta(days=OBSERVATION_RETENTION_DAYS),
        )
    )

    # RESOLVED ONLY. `resolved_at IS NOT NULL` is not redundant with the
    # comparison: an open finding has a NULL there, and `NULL < cutoff` is NULL,
    # so the row would not be deleted either way -- it is stated because the day
    # somebody adds an `OR` to this predicate, the two-clause version is the one
    # that still says what the rule is.
    drift = await session.execute(
        delete(IpamDrift).where(
            IpamDrift.org_id == org_id,
            IpamDrift.resolved_at.is_not(None),
            IpamDrift.resolved_at < stamp - timedelta(days=DRIFT_RETENTION_DAYS),
        )
    )

    newer = aliased(IpamSweepRun)
    # "Some other run for this subnet is newer than me" -- i.e. I am not the
    # newest, so deleting me cannot make the subnet's run list empty. Tie-broken
    # on `id` because `started_at` is a caller-supplied stamp and two runs in one
    # beat can share it; without the tie-break a tied pair would each see the
    # other as newer and BOTH would be deletable.
    not_newest_for_subnet = exists(
        select(newer.id).where(
            newer.org_id == IpamSweepRun.org_id,
            newer.subnet_id == IpamSweepRun.subnet_id,
            or_(
                newer.started_at > IpamSweepRun.started_at,
                and_(
                    newer.started_at == IpamSweepRun.started_at,
                    newer.id > IpamSweepRun.id,
                ),
            ),
        )
    )
    still_referenced = exists(
        select(IpamObservation.id).where(
            IpamObservation.org_id == IpamSweepRun.org_id,
            IpamObservation.sweep_run_id == IpamSweepRun.id,
        )
    )
    sweep_runs = await session.execute(
        delete(IpamSweepRun).where(
            IpamSweepRun.org_id == org_id,
            or_(
                and_(
                    IpamSweepRun.state.in_(RESTATED_STATES),
                    IpamSweepRun.started_at
                    < stamp - timedelta(days=RESTATED_SWEEP_RUN_RETENTION_DAYS),
                ),
                and_(
                    IpamSweepRun.state.not_in(RESTATED_STATES),
                    IpamSweepRun.started_at
                    < stamp - timedelta(days=SWEEP_RUN_RETENTION_DAYS),
                ),
            ),
            not_newest_for_subnet,
            ~still_referenced,
        )
    )

    return {
        "observations": _deleted(observations),
        "drift": _deleted(drift),
        "sweep_runs": _deleted(sweep_runs),
    }


async def sweep_all_orgs(
    *, engine: AsyncEngine, admin_engine: AsyncEngine, now: datetime | None = None
) -> int:
    """Every org, one transaction each. Returns the total rows deleted.

    **ONE BAD ORG MUST NEVER SKIP EVERY ORG QUEUED BEHIND IT** -- the containment
    `reconcile.reconcile_sweep_all_orgs`, `health.sweep` and
    `security_logs.retention` all use, and for the same reason: on a fleet where
    org #2 of 300 has a row that breaks the delete, the alternative is that 298
    orgs are never bounded again and the only symptom is a log line.

    Audited `ipam.retention_applied` on the org's own transaction, and ONLY when
    something was actually deleted -- a daily audit row per org saying "0" is the
    volume §11.3 excludes routine ingestion for. Deletion of evidence is a
    privileged action (invariant #4), which is why the pass that does it is
    audited at all; `security_logs.retention` sets that precedent.
    """
    stamp = now or _utcnow()
    async with AsyncSession(admin_engine) as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())

    total = 0
    for org_id in org_ids:
        try:
            async with org_session(org_id, engine=engine, clients=ClientScope.all()) as scoped:
                counts = await sweep_org(scoped, org_id, now=stamp)
                deleted = sum(counts.values())
                if deleted:
                    await audit_service.record(
                        scoped,
                        org_id=org_id,
                        actor_type="system",
                        actor_id=None,
                        action="ipam.retention_applied",
                        target_type="organization",
                        target_id=str(org_id),
                        metadata={"at": stamp.isoformat(), **counts},
                    )
            # `org_session` opens the transaction and commits on exit; a commit
            # inside the block would close it under the context manager.
            total += deleted
        except SQLAlchemyError:
            logger.exception(
                "ipam.retention: org %s failed; its pass is skipped and every "
                "other org is still bounded",
                org_id,
            )
    return total

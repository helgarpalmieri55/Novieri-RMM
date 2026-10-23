"""Celery worker: job expiry sweep and stuck-dispatch retry.

The logic lives in sweep_org() (async, engine-injected) so tests exercise it
directly; the Celery task is a thin scheduling shell around it.
"""

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from celery import Celery
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

# Imported as a module for the same reason as `health`, `ipam` and `sla` below:
# this file defines its own `*_all_orgs` shells, and the beat entry reads
# `ai_retention_module.RETENTION_INTERVAL_S` rather than a literal 86400.0 that
# can drift from the module's own constant.
from app.core.ai import retention as ai_retention_module
from app.core.ai import sweep as ai_action_sweep_module
from app.core.audit import MissingAuditKeyError, audit_service, load_audit_hmac_key
from app.core.config import get_settings
from app.core.errors.capture import capture_exception
from app.core.errors.store import DRAIN_INTERVAL_S, drain_once
from app.core.health import notify as platform_notify_module
from app.core.health.heartbeat import HEARTBEAT_INTERVAL_S, write_heartbeat
from app.core.signing import get_signer
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import VaultService, get_vault
from app.gateway.dispatch import publish_job, publish_revoke
from app.jobs.engine import JobEngine
from app.models import (
    Agent,
    BackupPoint,
    Deployment,
    Device,
    DomainEvent,
    Job,
    JobStatus,
    LicenseComplianceSnapshot,
    NotificationDelivery,
    Organization,
    PlatformHealthSample,
    ProvisionRun,
)
from app.modules.agent_push import dispatch as push_dispatch_module
from app.modules.agent_push import runs as push_runs_module
from app.modules.agent_update import reconcile as agent_update_reconcile
from app.modules.backup.dispatch import dispatch_due_backup
from app.modules.backup.gc import gc_repository, verify_point
from app.modules.backup.offsite import offsite_hold
from app.modules.backup.policy import resolve_backup_policy
from app.modules.backup.reconcile import reconcile_stuck
from app.modules.backup.retention import select_gfs
from app.modules.deploy.artifacts import LazyObjectStoreSigner
from app.modules.deploy.dispatch import dispatch_deployment
from app.modules.deploy.script_policies import run_policy_sweep
from app.modules.health import baseline as health_baseline_module
from app.modules.health import sweep as health_sweep_module

# Imported by NAME as well as through the module handle above, because this
# sweep calls it directly rather than delegating a whole `*_all_orgs` shell
# to the health module -- the transaction here is the device-group service's.
from app.modules.health.sweep import run_policy_reconcile
from app.modules.inventory import schedule as inventory_schedule_module

# Imported as a module for the same reason as `health` above: this file's own
# `*_all_orgs` shells would otherwise collide with the module's names, and the
# beat entry below reads `ipam_sweep_module.SWEEP_DISPATCH_INTERVAL_S` rather
# than a literal 3600.0 that can drift from §5.3.
from app.modules.ipam import reconcile as ipam_reconcile_module
from app.modules.ipam import retention as ipam_retention_module
from app.modules.ipam import sweep as ipam_sweep_module
from app.modules.ipam.errors import IpamError

# Imported as a module for the same reason as `health` and `ipam`: the beat
# entry below reads `keycloak_sweep_module.SWEEP_INTERVAL_S` rather than a
# literal 3600.0, so the schedule and the staleness threshold the
# `keycloak_events` probe measures against cannot drift apart.
from app.modules.keycloak_events import sweep as keycloak_sweep_module
from app.modules.licenses.reporting import compliance_summary
from app.modules.notification.delivery import attempt_delivery
from app.modules.notification.routing import resolve_recipients
from app.modules.patch.apply import dispatch_apply
from app.modules.patch.policy import resolve_patch_policy
from app.modules.patch.reboot import run_reboot_sweep
from app.modules.patch.reporting import last_successful_scan_at
from app.modules.patch.schema import PatchScanParams
from app.modules.provisioning.enroll import dispatch_provision_enroll
from app.modules.remote.recording import apply_recording_retention
from app.modules.security_logs import schedule as logs_ship_schedule_module

# Imported as a module, not as `from ... import sweep_all_orgs`: this file
# already defines a module-level `sweep_all_orgs` (the job-expiry sweep), and
# the SLA module's entry point has the same name by the plan's interface.
# Binding the module keeps both callable and unambiguous, and keeps
# `SWEEP_INTERVAL_SECONDS` at its single source of truth for the beat entry
# below rather than repeating 300.0 here.
from app.modules.sla import sweep as sla_sweep_module
from app.modules.subscriptions import alerts as subscription_alerts_module

# Imported as a module, not as `from ... import ...`, for the same reason the
# SLA sweep below is: this file defines its own `*_all_orgs` shells and a bare
# name would collide with them. It also keeps the beat entries' intervals
# readable as `health_sweep_module.ACTION_INTERVAL_S` rather than as literals
# that can drift from §12's table.
from app.services import device_groups as device_group_service
from app.services import device_review as device_review_module
from app.services import install_link as install_link_module
from app.services.device_groups import reevaluate_org_membership

STUCK_DISPATCH_AFTER = timedelta(minutes=10)

# Served only by the `backup-replicator` compose service.
BACKUP_REPLICATION_QUEUE = "backup_replication"

# Spec §7's "claim before send": a worker that dies between claiming a
# delivery row and recording its outcome leaves it claimed-but-unsent
# forever unless something returns it -- mirrors STUCK_DISPATCH_AFTER's
# precedent above for stuck job dispatch, same threshold.
STUCK_NOTIFICATION_CLAIM_AFTER = timedelta(minutes=10)

# `notification_deliveries.status` values that are still "in play" -- never
# attempted yet (`pending`, fresh out of `drain_outbox`) or scheduled for a
# retry (`failed`, per `delivery.py`'s backoff). `sent`/`dead` are terminal
# and never claimed again.
_NOTIFICATION_DUE_STATUSES = ("pending", "failed")

# docs/superpowers/specs/2026-08-15-platform-health-design.md §6.1: the page
# reads 48h of history and computes per-probe uptime over that window; the
# daily retention sweep below deletes anything older and leaves the rest.
RETENTION_HOURS = 48

logger = logging.getLogger("its.worker")


class _OrgCycleFailures:
    """Per-org containment bookkeeping, shared by every `*_all_orgs` shell here.

    **ONE UNSWEEPABLE ORG MUST NEVER SKIP EVERY ORG QUEUED BEHIND IT.** That is
    `app/modules/health/sweep.py::_for_each_org`'s reasoning, quoted verbatim by
    `ipam/reconcile.py`, `ipam/retention.py`, `sla/sweep.py` and
    `ai_admin/retention.py`; every `*_all_orgs` shell in this file now has it
    too. On a fleet where org #2 of 300 raises, the alternative is that 298 orgs
    are not swept that cycle and the only symptom is one traceback.

    WHAT THIS SHARES IS THE LOG LINE AND THE ALL-FAILED RULE, NOT THE LOOP.
    `health._for_each_org` takes the body as a callable and owns the
    `org_session`; that is right for five sweeps with one transaction shape and
    wrong for the fifteen here, which differ in exactly the way that matters --
    some open the tenant transaction themselves and publish signed jobs after it
    commits, some delegate the transaction to a per-org function, some hold an
    object store or a vault open across it. A helper that swallowed those
    differences would hide which sweeps are transactional. So the `try` stays
    inline and visible at every call site and only two things are centralized:

    - **`contain`**: every skipped org is reported at ERROR naming the org and
      the exception -- a contained failure that logs nothing is a silent skip,
      which is its own defect. EVERY org's line carries `exc_info` uniformly
      (fix wave 6, M-12 collapsed an `if`/`else` that used to save a
      traceback only for the first failure of a cycle and log the rest as one
      line each; that distinction no longer exists in the code, so it is not
      claimed here either). A cause that is really global (the signer is
      gone, Redis is down) fails every org, and 300 lines say nothing that
      the first one did not -- but each org still gets its own, so "which
      orgs were skipped" is always answerable.
    - **`raise_if_total`**: if EVERY org failed, the first exception is
      re-raised once the loop has finished. This is what keeps the fix from
      hiding a real bug: a globally broken sweep -- unavailable signer,
      unreachable Redis, a missing migration -- must not return a plausible `0`
      and a green Celery task. Note that this preserves today's behaviour
      exactly in the all-failed case: these shells propagate today, and they
      still propagate when the failure is universal. Only PARTIAL failure
      changes, which is the whole point.

    The genuinely fleet-wide preconditions are not contained at all, because
    they are resolved BEFORE the loop and outside its `try`: settings, the two
    engines, the Redis client, the vault, the object store, `get_signer`, and
    the admin-engine enumeration of orgs. A dead admin database or an
    unprovisioned signing key still fails the task immediately, with one
    traceback and no per-org noise.
    """

    def __init__(self, sweep: str) -> None:
        self._sweep = sweep
        self._first: BaseException | None = None
        self.count = 0

    def contain(self, org_id: UUID, exc: BaseException) -> None:
        self.count += 1
        if self._first is None:
            self._first = exc
        # Every skipped org gets its own ERROR line naming the org, via
        # exc_info rather than a %-arg -- %r rendered repr(exc), which
        # includes exc.args and is byte-identical to the str(exc) leak class
        # §2 exists to prevent. exc_info= does not reintroduce that leak
        # EITHER, but only as a consequence of this process's logger being
        # configured with `JsonFormatter` (app/core/logging.py), which reads
        # only exc_info[0].__name__ for `exc_type` and never calls
        # `formatException` -- see `create_celery`'s `setup_logging` wiring
        # (C-1), which is what makes that true here. A process logging
        # through this same call without that kernel installed -- the stdlib
        # default Formatter, or Celery's own before that wiring existed --
        # prints str(exc) in the traceback tail instead.
        #
        # NARROWED, fix wave 7: the paragraph above is true of THIS call --
        # every `logger.*()` site this codebase writes. It says nothing
        # about Celery's OWN task-failure record
        # (`celery.app.trace.TraceInfo._log_error`), which the same root
        # handler now also receives once `configure_logging()` is wired in,
        # and which carries a full traceback, `repr(exc)` and the task's raw
        # args/kwargs verbatim under field names this module's redaction has
        # no reason to flag. That record is sanitised separately, by
        # `_CeleryTraceSanitiser` (`app/core/logging.py`), not by anything
        # here -- `contain`'s own `exc_info=exc` call was never the leak;
        # Celery's independent, uncontrollable-from-here logging of the same
        # exception was.
        logger.error(
            "%s: sweeping org %s failed; its cycle is skipped and every other "
            "org is still swept",
            self._sweep,
            org_id,
            exc_info=exc,
        )
        # Task 3 (error monitoring, spec §3): this is the worker's OTHER
        # capture seam, alongside `task_failure` below -- a per-org failure
        # here is CONTAINED (the cycle continues for every other org), so it
        # never reaches Celery's own failure machinery and `task_failure`
        # never fires for it. Without this call, every contained sweep
        # failure in the fleet would be invisible to the error monitor
        # entirely, counted only in this ERROR line above.
        capture_exception(exc, where="worker")

    def raise_if_total(self, org_count: int) -> None:
        """Re-raise when the whole cycle failed -- see the class docstring."""
        if self._first is not None and self.count == org_count:
            raise self._first


async def sweep_org(
    org_id: UUID,
    job_engine: JobEngine,
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    *,
    max_attempts: int = 3,
) -> tuple[int, int]:
    """Expire overdue jobs; re-sign and republish dispatched jobs that never got
    a response. Returns (expired_count, redispatched_count).

    A JOB SOMEBODY ASKED TO CANCEL IS NEVER REPUBLISHED
    (2026-08-22-job-lifecycle-design.md §3.4). Without
    `cancel_requested_at IS NULL` this sweep would re-sign and re-send exactly
    the jobs an operator had just asked to stop -- ten minutes later, silently,
    with a fresh nonce and a valid signature. That is a signed command reaching
    a machine against an explicit instruction not to, and it is the quietest
    failure mode in the whole design: the console would still say a cancel was
    requested while the fleet ran the job anyway.

    The predicate costs nothing: it rides the existing `status = 'dispatched'`
    scan over a set bounded by ten minutes of dispatches, so no index moves.
    """
    redispatch: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(org_id, engine=app_engine) as session:
        expired = await job_engine.expire_due(session)
        cutoff = datetime.now(UTC) - STUCK_DISPATCH_AFTER
        stuck = (
            (
                await session.execute(
                    select(Job).where(
                        Job.status == JobStatus.dispatched,
                        Job.updated_at < cutoff,
                        Job.attempts < max_attempts,
                        Job.cancel_requested_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in stuck:
            await job_engine.resign_for_retry(session, job)
            redispatch.append((job.target_id, await job_engine.signed_envelope(job)))
    for device_id, envelope in redispatch:
        await publish_job(redis, device_id, envelope)
    return expired, len(redispatch)


async def sweep_all_orgs() -> tuple[int, int]:
    """Expire and re-dispatch across every org. One org's failure is contained;
    see `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("job.expiry")
    total_expired = total_redispatched = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                expired, redispatched = await sweep_org(
                    org_id, job_engine, app_engine, redis, max_attempts=settings.job_max_attempts
                )
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
                continue
            total_expired += expired
            total_redispatched += redispatched
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total_expired, total_redispatched


async def drain_outbox(org_id: UUID, app_engine: AsyncEngine) -> int:
    """Claim unprocessed `domain_events`, resolve routing rules for each,
    write the resulting `NotificationDelivery` rows, and mark the event
    processed (spec §6, §10).

    Every step here is a plain DB read/write -- resolving rules
    (`resolve_recipients`) and inserting delivery rows never opens a socket --
    so, unlike `deliver_pending` below, the whole operation safely runs
    inside ONE tenant transaction. `SELECT ... FOR UPDATE SKIP LOCKED`
    against `processed_at IS NULL` both claims these rows against a
    concurrent worker (two workers can never drain the same event) and holds
    them locked until this transaction commits. This is also why no
    stuck-claim sweep exists for `domain_events` the way one exists for
    `notification_deliveries` below: a worker that dies mid-transaction here
    rolls the whole transaction back -- releasing the lock along with it --
    rather than leaving a permanently claimed-but-unprocessed row the way a
    crash mid-NETWORK-CALL would (there is no network call in this
    function).

    Spec §6.2: zero matching rules is a valid state, not an error -- an event
    with no rules is still marked `processed_at` here exactly like one that
    resolved real recipients. Leaving it unprocessed would make the next
    drain re-resolve (and re-skip) it forever -- an invisible, permanent
    busy-loop that grows with every such event the org emits.

    Idempotent and org-scoped (invariant #5): every query carries an explicit
    `org_id` predicate, and a second call against the same org only ever sees
    `processed_at IS NULL` rows -- an event this call already processed is
    never revisited and no duplicate `NotificationDelivery` rows are ever
    written for it. Returns the number of events processed.
    """
    now = datetime.now(UTC)
    processed = 0
    async with org_session(org_id, engine=app_engine) as session:
        events = (
            (
                await session.execute(
                    select(DomainEvent)
                    .where(DomainEvent.org_id == org_id, DomainEvent.processed_at.is_(None))
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            event.claimed_at = now
            recipients = await resolve_recipients(session, event=event)
            for recipient in recipients:
                session.add(
                    NotificationDelivery(
                        org_id=org_id,
                        event_id=event.id,
                        channel_id=recipient.channel_id,
                        recipient=recipient.address,
                        status="pending",
                        attempts=0,
                        next_attempt_at=now,
                    )
                )
            event.processed_at = now
            session.add(event)
            processed += 1
        await session.flush()
    return processed


async def drain_outbox_all_orgs() -> int:
    """Drain every org's outbox. One org's failure is contained; see
    `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("notification.outbox")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await drain_outbox(org_id, app_engine)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def deliver_pending(
    org_id: UUID, app_engine: AsyncEngine, vault: VaultService
) -> tuple[int, int]:
    """Claim due `notification_deliveries` and attempt each (spec §7, §10).
    Returns `(delivered, returned)`: `delivered` is the number of deliveries
    actually attempted this call; `returned` is the number of stuck claims
    released before claiming (see below).

    A delivery is DUE when its status is `pending` (never attempted) or
    `failed` (scheduled for a retry -- `delivery.py`'s backoff never resets
    status back to `pending`) and `next_attempt_at <= now`. `sent`/`dead` are
    terminal and never reconsidered.

    CLAIM, THEN ATTEMPT OUTSIDE THE CLAIM TRANSACTION (spec §7's "claim
    before send", §10). The claim -- `SELECT ... FOR UPDATE SKIP LOCKED`
    restricted to `claimed_at IS NULL`, so two concurrent workers can never
    claim the same row -- happens in ONE short, DB-only transaction that
    stamps `claimed_at` and commits immediately (`org_session`'s block
    below). The actual attempt (an outbound HTTP POST or an SMTP
    conversation -- `attempt_delivery`, which can take up to its own
    `_SMTP_TIMEOUT_S`/egress timeout) happens AFTERWARDS, one delivery at a
    time, each in its OWN fresh transaction -- never inside the claim
    transaction. Holding a database connection open for the duration of an
    outbound call with someone else's timeout is how a handful of slow
    deliveries exhausts the connection pool and takes down the control
    plane; claim and attempt are two separate transactions specifically to
    avoid that.

    STUCK-CLAIM SWEEP (spec §7, mirrors `sweep_org`'s `STUCK_DISPATCH_AFTER`
    precedent for stuck job dispatch above). A worker that dies between
    claiming a row and recording its outcome leaves it `claimed_at`-set but
    still `pending`/`failed` -- forever, without this sweep: the row is
    claimed but never retried, and the notification silently vanishes.
    Before claiming anything new, this function releases (`claimed_at =
    NULL`) every due row whose `claimed_at` is older than
    `STUCK_NOTIFICATION_CLAIM_AFTER` -- in the SAME transaction as the claim
    step that follows, so a released row is immediately eligible to be
    reclaimed by that same claim query.

    `claimed_at` is cleared again, here, after each attempt completes
    (success, scheduled retry, or dead-letter) -- `attempt_delivery` itself
    never touches `claimed_at` (see its own module docstring, "CLAIM
    LIFECYCLE IS NOT THIS MODULE'S JOB"), so this function is the only thing
    that ever sets or clears it, in either direction.

    Org-scoped and idempotent (invariant #5): every query carries an
    explicit `org_id` predicate, and a delivery already `sent`/`dead` is
    excluded from every due query -- running this twice in a row never
    reattempts, and so never duplicates, a delivery the first call already
    finished.
    """
    now = datetime.now(UTC)
    claimed_ids: list[UUID] = []
    async with org_session(org_id, engine=app_engine) as session:
        cutoff = now - STUCK_NOTIFICATION_CLAIM_AFTER
        stuck = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.org_id == org_id,
                        NotificationDelivery.status.in_(_NOTIFICATION_DUE_STATUSES),
                        NotificationDelivery.claimed_at.is_not(None),
                        NotificationDelivery.claimed_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for delivery in stuck:
            delivery.claimed_at = None
            session.add(delivery)
        returned = len(stuck)

        due = (
            (
                await session.execute(
                    select(NotificationDelivery)
                    .where(
                        NotificationDelivery.org_id == org_id,
                        NotificationDelivery.status.in_(_NOTIFICATION_DUE_STATUSES),
                        NotificationDelivery.claimed_at.is_(None),
                        NotificationDelivery.next_attempt_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for delivery in due:
            delivery.claimed_at = now
            session.add(delivery)
            claimed_ids.append(delivery.id)
        await session.flush()

    delivered = 0
    for delivery_id in claimed_ids:
        async with org_session(org_id, engine=app_engine) as session:
            claimed_delivery = (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.org_id == org_id,
                        NotificationDelivery.id == delivery_id,
                    )
                )
            ).scalar_one_or_none()
            if claimed_delivery is None:
                continue  # deleted between claim and attempt; nothing to do
            try:
                await attempt_delivery(session, delivery=claimed_delivery, vault=vault)
            except Exception:  # noqa: BLE001
                # C1(b) fix (whole-branch review): `attempt_delivery` already
                # classifies every EXPECTED failure (egress rejection, SMTP
                # reject, transport error) into a terminal/retryable outcome
                # without raising. This catches what it does NOT expect --
                # e.g. a structurally broken channel `config` that slipped
                # past `schema.validate_channel_config` (a pre-existing row,
                # a future bug) and made `_send_email`/`_send_webhook` raise a
                # bare `KeyError`/`ValueError` instead of returning an
                # `_AttemptOutcome`. Before this fix that exception unwound
                # this whole function -- and, one level up,
                # `deliver_pending_all_orgs`'s per-org loop -- skipping every
                # org processed after the poisoned one on that sweep. Because
                # `claimed_at` was already committed in the claim transaction
                # above, `_apply_outcome` never ran to increment `attempts` or
                # set a terminal `status`, so `STUCK_NOTIFICATION_CLAIM_AFTER`
                # would reclaim and re-crash on this same row forever -- a
                # delivery that can never dead-letter is the actual defect.
                # Degrading it to `dead` here, directly (mirrors
                # `backup_verification_sweep_all_orgs`'s `except Exception:
                # continue` a few hundred lines below), makes THIS delivery
                # terminal instead, so it stops being reclaimed and every
                # other delivery -- this org's and every other org's -- keeps
                # being attempted normally.
                claimed_delivery.attempts += 1
                claimed_delivery.status = "dead"
                claimed_delivery.last_error = "internal delivery error"
                claimed_delivery.next_attempt_at = None
            claimed_delivery.claimed_at = None
            session.add(claimed_delivery)
            delivered += 1
    return delivered, returned


async def deliver_pending_all_orgs() -> tuple[int, int]:
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("notification.deliver")
    total_delivered = total_returned = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                delivered, returned = await deliver_pending(org_id, app_engine, vault)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                # C1(b) fix, defense in depth on top of `deliver_pending`'s
                # own per-delivery catch above: even a failure this function
                # itself did not anticipate (a bug in the claim query, a
                # transient DB error) must not skip every org queued after
                # this one in the same sweep run.
                #
                # THIS GUARD USED TO BE A BARE `continue`. It was the only
                # `*_all_orgs` shell in this file that contained anything, and
                # it contained it SILENTLY: an org whose deliveries stopped
                # being attempted produced no log line and no change in the
                # returned total, so the sweep reported a plausible number
                # forever. A silent skip is its own defect; `contain` names the
                # org and the exception.
                failures.contain(org_id, exc)
                continue
            total_delivered += delivered
            total_returned += returned
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total_delivered, total_returned


async def email_intake_poll_all_orgs() -> int:
    """Poll every org's active inbound mailboxes once (email-intake spec §4).

    A thin scheduling shell, like every other `*_all_orgs` here: the claim,
    the `\\Seen`-after-commit ordering and the per-mailbox error isolation
    all live in `email_intake/poller.py`, where they are tested directly.
    """
    from app.modules.email_intake.poller import poll_all_mailboxes

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await poll_all_mailboxes(
            engine=app_engine,
            admin_engine=admin_engine,
            vault=get_vault(settings),
            store=ObjectStore(settings),
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def retention_all_orgs() -> int:
    """Apply the security-event retention window to every org. One org's failure
    is contained; see `_OrgCycleFailures`.

    `apply_retention` resolves the org's `retention_days` from its policy row
    before it deletes anything, so the failures here are not only
    `SQLAlchemyError`: a policy `config` that is not a mapping, or an int that
    is not one, raises out of that resolution and used to take every remaining
    org's retention with it -- the same reason `ai_admin/retention.py` widened
    its own guard from `SQLAlchemyError` to `Exception`.
    """
    from app.modules.security_logs.retention import apply_retention

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("security_logs.retention")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await apply_retention(org_id, admin_engine, app_engine)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def ai_transcript_retention_all_orgs() -> int:
    """Hard-delete `ai_message`, `ai_injection_flag` and emptied `ai_session`
    rows past each org's `transcript_retention_days` (ai-admin spec §0.7, §3.2).

    A thin engine shell around `ai_admin.retention.sweep_all_orgs`, matching
    `health_retention_all_orgs`: the per-org loop, the transaction boundary and
    the one-bad-org containment live in the module, not here. Nothing is
    dispatched and nothing is published -- retention deletes, and the audit
    events the swept turns produced remain, because they are the record that it
    happened.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await ai_retention_module.sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def ai_action_expiry_all_orgs(now: datetime | None = None) -> int:
    """Expire this deployment's stale AI action previews
    (docs/modules/ai-automation.md §5.2, §6.4).

    **PER ORG, WITH ONE ORG'S FAILURE CONTAINED** -- see `_OrgCycleFailures`, and
    §9.1, which names this sweep specifically because the opposite shipped once
    (`3d3ac07`): a single org whose expiry pass raises must not leave every other
    org's previews confirmable past their own deadline. A stale preview is a
    confirmation token that outlived the situation that justified it, so "298 orgs
    were not swept and the only symptom is one traceback" is a security outcome
    here, not merely a missed cycle.

    Nothing is dispatched and nothing is published: this sweep only ever moves a
    row to `expired` and writes one audit event per row.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("ai.action_expiry")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as scoped:
                    total += await ai_action_sweep_module.expire_stale_actions(
                        scoped, org_id=org_id, now=now
                    )
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def remote_recording_retention_all_orgs() -> int:
    """Delete every org's expired session recordings. One org's failure is
    contained; see `_OrgCycleFailures`.

    This one talks to the object store per recording, so an S3/MinIO error --
    `ClientError`, a timeout, a bad bucket for one org's `recording_ref` -- is
    the expected failure and is not a `SQLAlchemyError` at all. Uncontained, one
    org's unreadable object left every other org's recordings past their
    retention window, which is a compliance problem and not merely a skipped
    cycle.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    store = ObjectStore(settings)
    failures = _OrgCycleFailures("remote.recording_retention")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await apply_recording_retention(org_id, admin_engine, app_engine, store)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def patch_scan_sweep_org(
    org_id: UUID,
    job_engine: JobEngine,
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    *,
    now: datetime,
) -> int:
    """Dispatch a signed `patch.scan` job to every device in this org that has
    never completed a scan, or whose last completed scan is older than the
    device's resolved policy `scan_interval_hours`.

    "Last completed scan" is `reporting.last_successful_scan_at` -- MAX
    `jobs.finished_at` over that device's `succeeded` `patch.scan` jobs, the
    same primitive `GET /patch/devices/{id}/status` reports, so the cadence and
    the console can never disagree about when a machine was last measured.

    Builds and publishes the job the same way `app.modules.patch.api`'s
    `POST /scan` handler does: create + sign inside the tenant transaction,
    publish after commit. Returns the count dispatched."""
    pending: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(org_id, engine=app_engine) as session:
        # ONLY DEVICES WHOSE AGENT IS CONNECTED, and this is the fix for a
        # storm observed on the operator's deployment on 2026-08-20: 29
        # `patch.scan` jobs, one an hour, every one `expired` with 0 attempts,
        # against a single machine that had been offline for two days.
        #
        # The cadence condition below is "the last SUCCESSFUL scan is older
        # than the policy interval". A device that cannot answer never produces
        # a successful scan, so that condition is permanently true and the
        # sweep re-dispatched for ever -- a signature, an audit-chain row and a
        # publish per pass, describing work nothing can do. On a fleet with 500
        # machines off overnight that is 500 of each, every hour.
        #
        # A device is passed over, not struck off: the sweep runs hourly, so an
        # agent that reconnects is scanned within one interval. An INNER join,
        # so a device with no `agents` row at all -- an asset record, a machine
        # that can never run one -- is skipped by the same rule, since it has
        # nothing that could execute a signed job either.
        device_ids = (
            await session.execute(
                select(Device.id)
                .join(Agent, Agent.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    Agent.status == "online",
                    # A device waiting for review is refused `patch.scan` by the
                    # gate; selecting it would abort this org's whole pass.
                    Device.review_state == "approved",
                )
            )
        ).scalars().all()

        # ...and none that already has a scan in flight. The cadence read below
        # moves only when a scan SUCCEEDS, so between dispatch and result the
        # cadence condition stays true and the next pass would queue a second
        # job for work already under way. UNEXPIRED is the load-bearing word:
        # "any scan job exists" would stop patch scanning across the fleet
        # permanently the first time one expired.
        outstanding = set(
            (
                await session.execute(
                    select(Job.target_id).where(
                        Job.org_id == org_id,
                        Job.type == "patch.scan",
                        Job.target_type == "device",
                        Job.status.in_(
                            (JobStatus.queued, JobStatus.dispatched, JobStatus.running)
                        ),
                        Job.expires_at > now,
                    )
                )
            ).scalars()
        )

        for device_id in device_ids:
            if device_id in outstanding:
                continue
            policy = await resolve_patch_policy(session, org_id, device_id)

            # WHEN THIS DEVICE LAST COMPLETED A SCAN, read off the `jobs`
            # table. This was `MAX(device_patch_status.detected_at)` until
            # 2026-08-21 and the comment above it asserted the property the
            # column does not have -- which is why the defect survived review.
            #
            # `detected_at` is stamped when a status ROW IS FIRST CREATED
            # (`patch/ingest.py` §3b) and is never rewritten by a later scan.
            # So it dates the discovery of a PATCH, not the running of a SCAN,
            # and the two diverge permanently on a settled machine: a device
            # whose patch set has not changed mints no new rows, its maximum
            # freezes at enrolment, the cadence condition is true for ever and
            # this sweep re-queued it on every hourly beat -- 24 signatures,
            # audit rows and publishes a day against a 24h policy, for exactly
            # the machines behaving best. The mirror image was as bad: a device
            # whose patch set churns restamps itself constantly, looks freshly
            # scanned, and was scanned LESS eagerly than a quiet one.
            #
            # A `succeeded` `patch.scan` job is the fact wanted, because
            # `JobEngine.complete` flips a job to `succeeded` only after this
            # module's ingest handler returns without raising: the job proves
            # both that the scan ran and that its document landed. A failed or
            # expired scan measured nothing and correctly moves no clock.
            last_scan = await last_successful_scan_at(session, org_id, device_id)
            cutoff = now - timedelta(hours=policy.scan_interval_hours)
            if last_scan is not None and last_scan >= cutoff:
                continue  # scanned recently enough; nothing to do

            job = await job_engine.create(
                session,
                org_id=org_id,
                type="patch.scan",
                target_device_id=device_id,
                params=PatchScanParams().model_dump(),
                requested_by=None,
            )
            envelope = await job_engine.signed_envelope(job)
            pending.append((device_id, envelope))
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="patch.scan_dispatched",
                target_type="device",
                target_id=str(device_id),
                metadata={"job_id": str(job.id)},
            )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for device_id, envelope in pending:
        await publish_job(redis, device_id, envelope)
    return len(pending)


async def patch_scan_sweep_all_orgs(now: datetime) -> int:
    """Fan `patch_scan_sweep_org` across every org. One org's failure is
    contained; see `_OrgCycleFailures`. `patch_scan_sweep_org` has NO internal
    containment at all -- one device whose policy will not resolve, one
    `job_engine.create`, one publish, and the org's whole scan cycle raises."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("patch.scan")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await patch_scan_sweep_org(
                    org_id, job_engine, app_engine, redis, now=now
                )
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def inventory_collect_sweep_org(
    org_id: UUID,
    job_engine: JobEngine,
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    *,
    now: datetime,
) -> int:
    """Dispatch the daily signed `inventory.collect` to this org's due devices
    (`app/modules/inventory/schedule.py`). Create + sign in the tenant
    transaction, publish after commit. Returns the count dispatched."""
    async with org_session(org_id, engine=app_engine) as session:
        pending = await inventory_schedule_module.dispatch_scheduled(
            session, job_engine, org_id, now=now
        )
    for device_id, envelope in pending:
        await publish_job(redis, device_id, envelope)
    return len(pending)


async def logs_ship_sweep_org(
    org_id: UUID,
    job_engine: JobEngine,
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    *,
    now: datetime,
) -> int:
    """Dispatch signed `logs.ship` jobs at the org policy's interval
    (`app/modules/security_logs/schedule.py`). Publishes after commit."""
    async with org_session(org_id, engine=app_engine) as session:
        pending = await logs_ship_schedule_module.dispatch_due(
            session, job_engine, org_id, now=now
        )
    for device_id, envelope in pending:
        await publish_job(redis, device_id, envelope)
    return len(pending)


_PerOrgJobSweep = Callable[..., Awaitable[int]]


async def _job_sweep_all_orgs(name: str, per_org: _PerOrgJobSweep, now: datetime) -> int:
    """Fan a job-creating per-org sweep across every org, with the engines,
    Redis client and signer built once. One org's failure is contained; see
    `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures(name)
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await per_org(org_id, job_engine, app_engine, redis, now=now)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def inventory_collect_sweep_all_orgs(now: datetime) -> int:
    return await _job_sweep_all_orgs("inventory.collect", inventory_collect_sweep_org, now)


async def logs_ship_sweep_all_orgs(now: datetime) -> int:
    return await _job_sweep_all_orgs("logs.ship", logs_ship_sweep_org, now)


async def ipam_sweep_dispatch_org(
    org_id: UUID,
    job_engine: JobEngine,
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    *,
    now: datetime,
) -> int:
    """Dispatch a signed `net.sweep` to every due subnet in this org
    (docs/modules/ipam/spec.md §5.3, §0.9).

    THE SCHEDULE LIVES HERE, NEVER IN THE AGENT'S LOCAL CRON (§0.9). The
    authorized target list is computed at dispatch time and carried inside the
    signed params, so a locally scheduled sweep would be a standing
    authorization: the agent would keep probing a scope signed once, weeks ago,
    and revoking it would not stop it. Signed-once-run-forever is precisely what
    invariant #1 exists to prevent.

    A subnet nobody has authorized comes back as `SweepDispatch.refusal` with
    the run already recorded `refused`, and this loop simply moves on: one
    unauthorized segment must not stop the org's other sweeps, and it must not
    roll back the evidence either. (`create_sweep` used to raise that one; the
    `except IpamError` below caught it here, which is why the worker's refusals
    reached the database and the API route's never did.) Every other `IpamError`
    -- a container, IPv6, a subnet deleted mid-pass -- is still caught, for the
    same containment reason.

    Publishes only AFTER the tenant transaction commits, mirroring
    `patch_scan_sweep_org`: a job must never reach an agent describing a run
    that rolled back. Returns the count dispatched.
    """
    pending: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(org_id, engine=app_engine) as session:
        subnet_ids = await ipam_sweep_module.due_subnet_ids(session, org_id, now=now)
        for subnet_id in subnet_ids:
            try:
                _run, envelope, _refusal = await ipam_sweep_module.create_sweep(
                    session,
                    job_engine,
                    org_id,
                    subnet_id=subnet_id,
                    actor_id=None,
                    trigger="scheduled",
                    now=now,
                )
            except IpamError:
                continue
            if envelope is not None:
                pending.append((UUID(envelope["job"]["device"]), envelope))
    # Transaction committed: safe to publish the signed jobs to the agents.
    for device_id, envelope in pending:
        await publish_job(redis, device_id, envelope)
    return len(pending)


async def ipam_sweep_dispatch_all_orgs(now: datetime) -> int:
    """Fan `ipam_sweep_dispatch_org` across every org, hourly.

    **ONE UNSWEEPABLE ORG MUST NEVER SKIP EVERY ORG QUEUED BEHIND IT** -- see
    `_OrgCycleFailures`. It belongs here for a reason the per-subnet
    `except IpamError` inside `ipam_sweep_dispatch_org` does NOT cover: that
    guard contains a bad SUBNET, while everything that makes an ORG unsweepable
    is raised outside it -- an unavailable signer (OpenBao down), a Redis
    publish failure, a database error, an unregistered job type. Uncontained,
    org #2 of 300 takes the other 298 with it and the only symptom is one
    traceback in the worker log.

    The engines, the Redis client and the JobEngine are built ONCE and shared,
    so a failure that is really "the signer is gone" still costs one attempt per
    org rather than one per subnet -- containment here is bounded by the org
    count, not by the fleet's subnet count.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("ipam.sweep_dispatch")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await ipam_sweep_dispatch_org(
                    org_id, job_engine, app_engine, redis, now=now
                )
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def patch_apply_sweep_all_orgs(now: datetime) -> int:
    """Per org, call `dispatch_apply` to build signed `patch.apply` jobs for
    every device with an approved, in-window patch, then publish them only
    after that org's tenant transaction commits (mirrors
    `patch_scan_sweep_org`; `dispatch_apply` itself no longer publishes —
    see its docstring). `dispatch_apply` already enforces the approval and
    maintenance-window gating; this sweep only fans it out across orgs on a
    timer. Returns the total dispatched.

    One org's failure is contained; see `_OrgCycleFailures`. THE GUARD SPANS
    BOTH PHASES -- the tenant transaction AND the publish loop after it -- so a
    Redis failure while handing this org's signed jobs to the gateway does not
    cost every org behind it its patch window either. An org whose publish loop
    dies part-way therefore contributes 0 to the total rather than a count of
    jobs that were only partly published: under-reporting dispatch is the safe
    direction, over-reporting it is not.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("patch.apply")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)
                # Transaction committed: safe to publish the signed jobs.
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def patch_reboot_sweep_all_orgs(now: datetime) -> int:
    """Per org, advance open restarts (`patch.reboot.run_reboot_sweep`) and
    publish the signed jobs after that org's transaction commits. One org's
    failure is contained, as in `patch_apply_sweep_all_orgs`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("patch.reboot")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    pending = await run_reboot_sweep(session, job_engine, org_id=org_id, now=now)
                for device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def script_policy_sweep_all_orgs(now: datetime) -> int:
    """Per org, dispatch every due scheduled script (`run_policy_sweep`) and
    publish after that org's transaction commits. One org's failure is
    contained, as in `patch_apply_sweep_all_orgs`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("script.policy")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    pending = await run_policy_sweep(session, job_engine, org_id=org_id, now=now)
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def deploy_window_sweep_all_orgs(now: datetime) -> int:
    """Per org, dispatch every scheduled (`window_cron IS NOT NULL`),
    not-yet-dispatched (`state == "pending"`) deployment via
    `dispatch_deployment`, which itself enforces the maintenance-window gate
    (`window_open`) and the per-device detection-skip logic -- this sweep
    only pre-filters to windowed+pending deployments and fans the dispatch
    out across orgs on a timer; it never re-implements the window check.
    Windowless deployments are dispatched elsewhere (at creation time), not
    by this sweep. `dispatch_deployment` no longer publishes (see its
    docstring); this sweep accumulates the pending publishes for an org's
    deployments and publishes them only after that org's tenant transaction
    commits. Returns the total number of signed `app.deploy` jobs dispatched,
    system-attributed (`actor_type="system"`, `actor_id=None`) since no user
    initiated this sweep.

    One org's failure is contained, across both the tenant transaction and the
    publish loop after it; see `_OrgCycleFailures` and
    `patch_apply_sweep_all_orgs`, whose two-phase reasoning is identical."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    # ONE signer for the whole sweep, and it opens nothing until a deployment
    # actually needs a presigned artifact URL -- a night on which every
    # windowed deployment is manager-sourced, already in the desired state, or
    # outside its window must not require object storage to be reachable.
    artifact_urls = LazyObjectStoreSigner(settings)
    failures = _OrgCycleFailures("deploy.window")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    deployment_ids = (
                        await session.execute(
                            select(Deployment.id).where(
                                Deployment.org_id == org_id,
                                Deployment.state == "pending",
                                Deployment.window_cron.is_not(None),
                            )
                        )
                    ).scalars().all()
                    for deployment_id in deployment_ids:
                        dispatched = await dispatch_deployment(
                            session,
                            job_engine,
                            org_id=org_id,
                            deployment_id=deployment_id,
                            now=now,
                            actor_type="system",
                            artifact_urls=artifact_urls,
                        )
                        pending.extend(dispatched)
                # Transaction committed: safe to publish the signed jobs.
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def backup_window_sweep_all_orgs(now: datetime) -> int:
    """Per org, dispatch a signed `backup.run` for every device whose effective
    backup policy has a scheduled time due at `now`, and record `missed` runs.

    Enumerates each org's devices and calls `dispatch_due_backup` once per
    device; it resolves the policy and applies the due rule
    (`app.modules.backup.schedule.decide`), so this sweep never re-implements
    it. `dispatch_due_backup` does not publish
    (see its docstring); this sweep accumulates an org's pending
    `(job_id, device_id, envelope)` triples and publishes them only AFTER that
    org's tenant transaction commits (publishing a signed job -- which carries a
    live repository key -- before its durable server record commits would be an
    unaudited privileged fleet execution). Returns the total number of signed
    `backup.run` jobs dispatched, system-attributed (`actor_type="system"`,
    `actor_id=None`) since no user initiated this sweep.

    One org's failure is contained, across both the tenant transaction and the
    publish loop after it; see `_OrgCycleFailures` and
    `patch_apply_sweep_all_orgs`. `dispatch_backup` resolves a REPOSITORY KEY
    out of the vault per device, so an org with one unreadable key used to cost
    every org behind it its nightly backup window -- an availability failure
    that only shows up when a restore is needed.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.window")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    device_ids = (
                        await session.execute(select(Device.id).where(Device.org_id == org_id))
                    ).scalars().all()
                    for device_id in device_ids:
                        dispatched = await dispatch_due_backup(
                            session,
                            job_engine,
                            vault,
                            org_id=org_id,
                            device_id=device_id,
                            now=now,
                        )
                        pending.extend(dispatched)
                # Transaction committed: safe to publish the signed jobs.
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def backup_reconcile_all_orgs(now: datetime) -> int:
    """Move backup runs and restores whose job is over out of `running`
    (`app.modules.backup.reconcile`). Returns rows moved, counted after each
    org's commit. One org's failure is contained; see `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("backup.reconcile")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    counts = await reconcile_stuck(session, org_id, now=now)
                total += counts["runs"] + counts["restores"]
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


# The replication sweep stops starting new steps after this long, so one run
# ends before the next beat (300 s) and a multi-hour seed spans many runs.
REPLICATION_RUN_BUDGET_S = 270.0


async def backup_replication_all_orgs(now: datetime) -> int:
    """Copy complete backup points to their repository's off-site target
    (`app.modules.backup.offsite`). Runs on the `backup_replication` queue, one
    run at a time (Redis lock), oldest point first, one transaction per step so
    a crash loses at most one batch. Returns copies completed."""
    from app.modules.backup.offsite import build_client, pending_copies, replicate_point

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    local_store = ObjectStore(settings)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.replication")
    deadline = time.monotonic() + REPLICATION_RUN_BUDGET_S
    lock_key = "its:backup:replication:lock"
    completed = 0
    if not await redis.set(lock_key, "1", nx=True, ex=int(REPLICATION_RUN_BUDGET_S) + 120):
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
        return 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                if time.monotonic() > deadline:
                    break
                async with org_session(org_id, engine=app_engine) as session:
                    work = await pending_copies(session, org_id)
                for item in work:
                    if time.monotonic() > deadline:
                        break
                    done = await replicate_point(
                        partial(org_session, org_id, engine=app_engine),
                        vault=vault,
                        local_store=local_store,
                        client_factory=build_client,
                        item=item,
                        org_id=org_id,
                        now=datetime.now(UTC),
                        deadline=deadline,
                    )
                    completed += int(done)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.delete(lock_key)
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return completed


async def backup_offsite_test_all_orgs(now: datetime) -> int:
    """Run the off-site target tests operators asked for. In the worker because
    only the worker may decrypt the target credential. Returns tests run."""
    from app.modules.backup.models import BackupOffsiteTarget
    from app.modules.backup.offsite import build_client, run_target_test

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.offsite_test")
    ran = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    targets = (
                        await session.execute(
                            select(BackupOffsiteTarget).where(
                                BackupOffsiteTarget.org_id == org_id,
                                BackupOffsiteTarget.test_requested_at.is_not(None),
                            )
                        )
                    ).scalars().all()
                    for target in targets:
                        await run_target_test(session, vault, target, build_client, now=now)
                ran += len(targets)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return ran


async def backup_offsite_gc_all_orgs(now: datetime) -> int:
    """Apply each off-site target's own retention and delete unreferenced,
    unlocked off-site objects. Returns objects deleted."""
    from app.modules.backup.offsite import build_client, offsite_gc

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    local_store = ObjectStore(settings)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.offsite_gc")
    deleted = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    counts = await offsite_gc(
                        session,
                        vault=vault,
                        local_store=local_store,
                        client_factory=build_client,
                        org_id=org_id,
                        now=now,
                    )
                deleted += counts["deleted"]
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return deleted


async def backup_test_restore_all_orgs(now: datetime) -> int:
    """Dispatch the scheduled test restores that are due
    (`app.modules.backup.test_restore`), publishing only after each org's
    commit. Returns jobs dispatched."""
    from app.modules.backup.test_restore import dispatch_due_test_restores

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    object_store = ObjectStore(settings)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.test_restore")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    pending = await dispatch_due_test_restores(
                        session, job_engine, vault, object_store, org_id=org_id, now=now
                    )
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def backup_health_all_orgs(now: datetime) -> int:
    """Evaluate the backup health checks (`app.modules.backup.health`).
    Returns alerts opened."""
    from app.modules.backup.health import run_backup_health_sweep

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("backup.health")
    opened = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    counts = await run_backup_health_sweep(session, org_id, now=now)
                opened += counts["opened"]
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return opened


async def backup_retention_gc_all_orgs(now: datetime) -> int:
    """Apply GFS retention PER DEVICE, then garbage-collect the chunks the newly
    pruned points orphan. Returns the total number of points marked `pruned`.

    Correctness hinges on GFS being computed over ONE DEVICE'S history at a
    time: mixing several devices' points into a single GFS bucket would prune
    device A's points merely because device B has newer ones (silent data
    loss). So, per org:

    - **Mark (per device):** enumerate the org's devices; for each, resolve its
      effective policy. Skip devices with no policy or an empty `retention_gfs`.
      Gather THAT device's non-pruned points IN THAT POLICY'S REPOSITORY, run
      `select_gfs` over just those points, and set `state = "pruned"` on the
      losers. Flush after each device.
    - **Sweep (per repository):** for each repository a device actually pruned
      into, call `gc_repository`, which deletes chunk objects no live
      (`state != "pruned"`) point still references. Repositories with no prune
      this cycle are skipped -- a chunk only becomes orphaned when the last
      point referencing it is pruned, so GC-on-prune reclaims every new orphan.

    All prune writes happen (and flush) before the sweep phase reads live points
    within the same `org_session`, so GC sees the post-mark state. Every query
    carries an explicit `org_id` predicate (invariant #5); the `backup.gc` audit
    event `gc_repository` writes is system-attributed (invariant #4).

    One org's failure is contained; see `_OrgCycleFailures`. `gc_repository`
    DELETES CHUNK OBJECTS from the store, so a store error mid-sweep is the
    expected failure, not a `SQLAlchemyError` -- and uncontained it left every
    org behind the bad one accumulating backup points forever.

    THE PRUNE COUNT IS ACCUMULATED PER ORG AND ADDED ONLY AFTER THE TENANT
    TRANSACTION HAS COMMITTED. It used to be added to the running total inside
    the `org_session` block, which was invisible while the block could not fail
    -- now that a later `gc_repository` failure rolls the whole org back, adding
    it early would report points as pruned that are still there.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    object_store = ObjectStore(settings)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.retention_gc")
    total_pruned = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            org_pruned = 0
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    device_ids = (
                        await session.execute(select(Device.id).where(Device.org_id == org_id))
                    ).scalars().all()
                    touched_repos: set[UUID] = set()
                    for device_id in device_ids:
                        policy = await resolve_backup_policy(session, org_id, device_id)
                        if policy is None or not policy.retention_gfs:
                            continue
                        points = (
                            (
                                await session.execute(
                                    select(BackupPoint).where(
                                        BackupPoint.org_id == org_id,
                                        BackupPoint.device_id == device_id,
                                        BackupPoint.repository_id == policy.repository_id,
                                        BackupPoint.state != "pruned",
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        _keep_ids, prune_ids = select_gfs(list(points), policy.retention_gfs, now)
                        # A point not yet copied off-site is never pruned locally.
                        prune_ids -= await offsite_hold(session, org_id, prune_ids)
                        if not prune_ids:
                            continue
                        for point in points:
                            if point.id in prune_ids:
                                point.state = "pruned"
                        org_pruned += len(prune_ids)
                        if policy.repository_id is not None:
                            touched_repos.add(policy.repository_id)
                        await session.flush()

                    for repository_id in touched_repos:
                        await gc_repository(
                            session,
                            object_store,
                            vault,
                            org_id=org_id,
                            repository_id=repository_id,
                            now=now,
                        )
                # Transaction committed: the prunes are real, so count them.
                total_pruned += org_pruned
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total_pruned


async def backup_verification_sweep_all_orgs(now: datetime) -> int:
    """Integrity-verify never-verified `complete` backup points. Returns the
    number of points that verified successfully (`verify_point` -> True).

    v1 scope: every `complete` point with `verified_at IS NULL` (never
    verified). A time-based "stale re-verification" threshold (re-checking
    points verified longer than N days ago) is a later optimization.

    Per org, each candidate point is verified via `verify_point`, which re-reads
    and BLAKE3-checks every referenced chunk, stamps `verified_at` and audits on
    success, and returns False (without stamping) on any corruption. A point
    whose repository key cannot even be opened -- or whose verification raises
    for any reason -- is skipped (counted as not-verified) and the sweep moves
    on, so one broken point never wedges verification of the rest of the fleet.
    Every query carries an explicit `org_id` predicate (invariant #5).

    THE PER-POINT GUARD BELOW CONTAINS A BROKEN POINT AND NOTHING ELSE. What
    makes an ORG unverifiable is raised outside it: the candidate query itself,
    and the `org_session` COMMIT that persists every `verified_at` stamp this
    org just made. Both were uncontained, so one org's failure skipped
    verification for every org behind it -- and backup verification that
    silently stops running is exactly the failure nobody notices until a
    restore. One org's failure is contained; see `_OrgCycleFailures`.

    The verified count is accumulated per org and added only after that commit,
    for `backup_retention_gc_all_orgs`'s reason: a stamp that rolled back is not
    a verification.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    object_store = ObjectStore(settings)
    vault = get_vault(settings)
    failures = _OrgCycleFailures("backup.verification")
    verified = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            org_verified = 0
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    point_ids = (
                        await session.execute(
                            select(BackupPoint.id).where(
                                BackupPoint.org_id == org_id,
                                BackupPoint.state == "complete",
                                BackupPoint.verified_at.is_(None),
                            )
                        )
                    ).scalars().all()
                    for point_id in point_ids:
                        try:
                            ok = await verify_point(
                                session,
                                object_store,
                                vault,
                                org_id=org_id,
                                backup_point_id=point_id,
                                now=now,
                            )
                        except Exception:  # noqa: BLE001
                            continue  # unopenable/broken point: skip, never wedge
                        if ok:
                            org_verified += 1
                # Transaction committed: the `verified_at` stamps are durable.
                verified += org_verified
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return verified


async def provision_enroll_dispatch_sweep_all_orgs(now: datetime) -> int:
    """Per org, dispatch a signed `provision.enroll` post-config job for every
    run that has actually enrolled (`state == "enrolled"`, i.e.
    `bind_provisioned_device` already bound its `device_id` inside the real
    `/enroll/v1` flow) but never had one dispatched yet (`job_id IS NULL`).

    Nothing else dispatches this job: `POST /profiles/{id}/run` deliberately
    doesn't (see `app.modules.provisioning.api`'s module docstring -- core
    enrollment must stay minimal), so this sweep is the only path by which an
    enrolled device ever receives its post-config job. `job_id IS NULL` makes
    the dispatch idempotent across sweep runs -- once `dispatch_provision_
    enroll` sets it, the run is never revisited.

    Mirrors `deploy_window_sweep_all_orgs` / `backup_window_sweep_all_orgs`'s
    publish-after-commit idiom: `dispatch_provision_enroll` itself does not
    publish (mirrors `dispatch_backup`), so this sweep accumulates an org's
    pending `(job_id, device_id, envelope)` triples INSIDE that org's
    `org_session` block and publishes them only after the block exits (i.e.
    after the org's tenant transaction commits) -- publishing a signed job
    before its durable server record commits would be an unaudited privileged
    fleet execution. Returns the total number of signed `provision.enroll`
    jobs dispatched, system-attributed (`actor_type="system"`) since no user
    initiated this sweep.

    One org's failure is contained, across both the tenant transaction and the
    publish loop after it; see `_OrgCycleFailures` and
    `patch_apply_sweep_all_orgs`. This sweep is the ONLY path by which an
    enrolled device ever gets its post-config job, so an uncontained failure
    here left every org behind the bad one with devices enrolled and never
    configured -- and `job_id IS NULL` means the next cycle simply retries, so
    skipping is genuinely recoverable and aborting is not.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    failures = _OrgCycleFailures("provisioning.enroll_dispatch")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    run_ids = (
                        await session.execute(
                            select(ProvisionRun.id).where(
                                ProvisionRun.org_id == org_id,
                                ProvisionRun.state == "enrolled",
                                ProvisionRun.job_id.is_(None),
                            )
                        )
                    ).scalars().all()
                    for run_id in run_ids:
                        dispatched = await dispatch_provision_enroll(
                            session,
                            job_engine,
                            org_id=org_id,
                            run_id=run_id,
                            now=now,
                            actor_type="system",
                        )
                        pending.extend(dispatched)
                # Transaction committed: safe to publish the signed jobs.
                for _job_id, device_id, envelope in pending:
                    await publish_job(redis, device_id, envelope)
                total += len(pending)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


PROVISION_ENROLLMENT_TIMEOUT = timedelta(hours=24)
_PROVISION_TERMINAL_STATES = ("complete", "failed")


async def provision_timeout_sweep_all_orgs(now: datetime) -> int:
    """Per org: (1) fail any run stuck in `awaiting_enrollment` past the 24h
    deadline (a target that never came up) with `error="enrollment timed
    out"`; (2) clean up the secret-bearing `answer_ref` object for any run
    that is now, or was already, terminal (`complete`/`failed`) -- one pass
    covers both a run this sweep just timed out and a run some earlier
    `provision.enroll` result already landed on `complete`/`failed` without
    its answer object being cleaned up yet.

    Per invariant #3, the rendered answer file can carry resolved domain-join
    / local-admin credentials, so it must never linger once the run it
    belongs to is done (successfully or not): `object_store.
    delete_provision_object` removes it and `answer_ref` is nulled in the
    same pass. Every query carries an explicit `org_id` predicate (invariant
    #5); the timeout transition is audited system-attributed (invariant #4).

    Returns the count of runs newly failed by the timeout (not the count of
    answer objects cleaned up).

    One org's failure is contained; see `_OrgCycleFailures`. The guard matters
    more here than almost anywhere else in this file: this pass is what DELETES
    THE SECRET-BEARING ANSWER OBJECTS (invariant #3), and `delete_provision_
    object` is an object-store call that can fail for one org's ref. Before this
    change that failure left every org behind it with rendered domain-join and
    local-admin credentials sitting in the store indefinitely.

    The timeout count is accumulated per org and added only after the tenant
    transaction commits, for `backup_retention_gc_all_orgs`'s reason -- it used
    to be added before the terminal-cleanup phase that can now roll the whole
    org back, which would report runs as failed that are still awaiting
    enrollment.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    object_store = ObjectStore(settings)
    cutoff = now - PROVISION_ENROLLMENT_TIMEOUT
    failures = _OrgCycleFailures("provisioning.timeout")
    total_failed = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            org_failed = 0
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    stale = (
                        await session.execute(
                            select(ProvisionRun).where(
                                ProvisionRun.org_id == org_id,
                                ProvisionRun.state == "awaiting_enrollment",
                                ProvisionRun.started_at < cutoff,
                            )
                        )
                    ).scalars().all()
                    for run in stale:
                        run.state = "failed"
                        run.error = "enrollment timed out"
                        run.finished_at = now
                        if run.answer_ref is not None:
                            await object_store.delete_provision_object(run.answer_ref)
                            run.answer_ref = None
                        session.add(run)
                        await audit_service.record(
                            session,
                            org_id=org_id,
                            actor_type="system",
                            actor_id=None,
                            action="provisioning.timed_out",
                            target_type="provision_run",
                            target_id=str(run.id),
                            metadata={"provision_run_id": str(run.id)},
                        )
                    org_failed += len(stale)

                    # Terminal-run answer cleanup: any run (whether it just
                    # timed out above, or reached complete/failed via the
                    # normal provision.enroll result path) with a
                    # still-lingering answer_ref gets its secret-bearing object
                    # deleted here. `session.execute` autoflushes the
                    # state/answer_ref writes above first, so a run just timed
                    # out (answer_ref already nulled) is not double-processed
                    # by this second query.
                    terminal = (
                        await session.execute(
                            select(ProvisionRun).where(
                                ProvisionRun.org_id == org_id,
                                ProvisionRun.state.in_(_PROVISION_TERMINAL_STATES),
                                ProvisionRun.answer_ref.is_not(None),
                            )
                        )
                    ).scalars().all()
                    for run in terminal:
                        ref = run.answer_ref
                        if ref is None:  # narrowed for the checker; the query filters non-null
                            continue
                        await object_store.delete_provision_object(ref)
                        run.answer_ref = None
                        session.add(run)
                # Transaction committed: the timeouts are real, so count them.
                total_failed += org_failed
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total_failed


async def licenses_compliance_sweep_all_orgs(now: datetime) -> int:
    """Daily Licenses/SAM compliance sweep (docs/modules/licenses/spec.md
    §7): per org, per `SoftwareProduct`, persist a `LicenseComplianceSnapshot`
    row capturing that product's `entitled`/`installed`/`delta` position as of
    `now`. Returns the total number of snapshot rows inserted across every
    org.

    Reuses `app.modules.licenses.reporting.compliance_summary` for the actual
    entitled/installed/delta computation rather than duplicating it -- that
    function already carries the explicit `org_id` predicates (invariant #5)
    on `SoftwareProduct`, `License`, and (via `product_installed_device_ids`)
    `InstalledSoftware`; this sweep only maps each of its rows onto a snapshot
    insert and flushes once per org.

    Snapshots are DATA writes, not privileged actions -- per spec §7/§9 the
    sweep records no audit event (alerts are computed at read time from the
    latest snapshot, not raised here).

    One org's failure is contained; see `_OrgCycleFailures`. A snapshot row
    rejected by a constraint, or a `compliance_summary` that trips over one
    org's product data, used to leave every org behind it with no compliance
    position recorded for that day -- and a gap in a compliance series is not
    recoverable by the next run, because the snapshot is `computed_at`-stamped.
    The count is added only after the org's transaction commits, so an org that
    failed at `flush`/commit contributes 0 rather than the rows it staged.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("licenses.compliance")
    total_snapshots = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    summary = await compliance_summary(session, org_id)
                    for row in summary:
                        session.add(
                            LicenseComplianceSnapshot(
                                org_id=org_id,
                                product_id=row["product_id"],
                                entitled=row["entitled"],
                                installed=row["installed"],
                                delta=row["delta"],
                                computed_at=now,
                            )
                        )
                    await session.flush()
                # Transaction committed: the snapshots are durable.
                total_snapshots += len(summary)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total_snapshots


async def sla_sweep_all_orgs() -> int:
    """Run the SLA breach sweep and auto-close for every org once
    (docs/modules/sla/spec.md §7, §12).

    A thin scheduling shell, like every other `*_all_orgs` here: the two
    passes, the per-ticket savepoint containment and the per-org isolation all
    live in `app/modules/sla/sweep.py`, where they are tested directly.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await sla_sweep_module.sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def agent_update_tick_all_orgs() -> int:
    """The self-update reconciler (spec-self-update.md §6.4). Continues rings a
    person started; never creates a rollout or advances a ring. The object
    store is built only when a dispatch needs a presigned URL, and envelopes
    are published after each org commits."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    stores: list[ObjectStore] = []

    def presign(version: str, os_name: str, arch: str, expires_in: int) -> str:
        if not stores:
            stores.append(ObjectStore(settings))
        return stores[0].presign_agent_release_get(version, os_name, arch, expires_in=expires_in)

    async def publish(_job_id: UUID, envelope: dict[str, Any]) -> None:
        await publish_job(redis, UUID(envelope["job"]["device"]), envelope)

    try:
        return await agent_update_reconcile.tick_all_orgs(
            engine=app_engine,
            admin_engine=admin_engine,
            job_engine=job_engine,
            presign=presign,
            publish=publish,
        )
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()


async def push_run_tick_all_orgs(now: datetime | None = None) -> int:
    """The push-run pacer's backstop, every `TICK_INTERVAL_S` (design
    2026-09-30 §1.4).

    It CONTINUES runs a person confirmed and never starts, resumes or widens one
    (CLAUDE.md 2a(e) as amended 2026-09-30). It settles in-flight targets whose
    jobs ended without reaching the gateway's result handler, and fills freed
    capacity -- which the gateway cannot do, because it holds no vault. The
    vault, signer and object store are fleet-wide preconditions, resolved
    before the loop. One org per transaction, and envelopes are published only
    after that org commits. One org's failure is contained; see
    `_OrgCycleFailures`.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    vault = get_vault(settings)
    stores: list[ObjectStore] = []

    def object_store() -> ObjectStore:
        if not stores:
            stores.append(ObjectStore(settings))
        return stores[0]

    dispatcher = push_dispatch_module.RunDispatcher(
        job_engine, object_store, vault, public_console_url=settings.public_console_url
    )
    failures = _OrgCycleFailures("push.run_tick")
    total = 0
    try:
        org_ids = await push_runs_module.org_ids_with_live_runs(admin_engine)
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    envelopes = await push_runs_module.tick(
                        session, org_id, dispatcher=dispatcher, now=now
                    )
                # Transaction committed: safe to hand the signed jobs to agents.
                for device_id, envelope in envelopes:
                    await publish_job(redis, device_id, envelope)
                total += len(envelopes)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def health_alert_actions_all_orgs() -> int:
    """Execute due `health_alert_action` rows for every org (endpoint-health
    spec §7, §12).

    The signed envelopes the action worker produced are published HERE, after
    each org's tenant transaction has committed -- `app/modules/health/sweep.py`
    takes `publish` as a parameter and never imports `publish_job` itself,
    because `tests/test_health_egress.py` asserts that no file under
    `app/modules/health/` names it. That is endpoint-health spec §8.1's
    single-egress property made structural rather than conventional: this
    module is the only place a health-originated job reaches Redis.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)

    async def publish(_job_id: UUID, envelope: dict[str, Any]) -> None:
        await publish_job(redis, UUID(envelope["job"]["device"]), envelope)

    try:
        return await health_sweep_module.sweep_actions_all_orgs(
            engine=app_engine,
            admin_engine=admin_engine,
            job_engine=job_engine,
            publish=publish,
        )
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()


async def health_alert_sweep_all_orgs() -> int:
    """Mute expiry, escalation, `unavailable_timeout` clears and approval
    expiry, per org (endpoint-health spec §12). Creates no jobs, so it needs
    neither a job engine nor Redis."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await health_sweep_module.sweep_alerts_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def health_offline_sweep_all_orgs() -> int:
    """The server-side `agent.offline` check (endpoint-health spec §0.6).

    It opens alerts and CLEARS NOTHING: an alert does not clear because the
    device went offline, or a fleet crashing under memory pressure would read as
    the healthiest it has ever been.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await health_sweep_module.sweep_offline_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def health_baseline_reconcile_all_orgs() -> int:
    """Seed the default "Workstation baseline" profile for any org never
    decided (`app/modules/health/baseline.py`). Creates no jobs: the policy
    reconcile pushes the new profile to devices on its own cadence."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await health_sweep_module.sweep_baseline_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def health_policy_reconcile_all_orgs() -> int:
    """Re-push `health.policy.apply` where the acknowledged `policy_version` is
    behind the device's effective profile, or where no ack row exists at all
    (endpoint-health spec §12, §2.11). Publishes after commit, like every other
    job-creating sweep in this file."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)

    async def publish(_job_id: UUID, envelope: dict[str, Any]) -> None:
        await publish_job(redis, UUID(envelope["job"]["device"]), envelope)

    try:
        return await health_sweep_module.sweep_policy_reconcile_all_orgs(
            engine=app_engine,
            admin_engine=admin_engine,
            job_engine=job_engine,
            publish=publish,
        )
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()


async def device_group_reevaluate_all_orgs() -> int:
    """Re-home every org's devices according to its live membership rules
    (device-group management design §5.3), and push health where that moved a
    device's effective profile.

    WHY A BEAT AND NOT THE WRITE ROUTE. Evaluating inside the `POST` would make
    one form submit re-read every device's latest snapshot and move an
    arbitrary number of machines onto different patch, backup and remote policy
    inside one HTTP transaction -- a fleet-wide policy change with an HTTP
    timeout on it. The route offers `POST /device-groups/preview` instead, so
    the matched set is inspectable BEFORE it is used, and this sweep applies it
    within `REEVALUATE_INTERVAL_S`.

    THE ADMIN ENGINE ENUMERATES ORGS AND NOTHING ELSE. Every read and write of
    tenant data happens inside that org's `org_session`, which is the shape
    `provision_enroll_dispatch` already has -- and it matters more here than
    there, because `evaluate_rules` is the one function in this codebase whose
    missing tenant predicate would cross-ASSIGN devices between tenants rather
    than merely disclose them. The predicate is in the query
    (`rule_bearing_groups_query`) as well, so this holds on both engines.

    HEALTH ONLY, AND ONLY WHEN SOMETHING MOVED. `patch/policy.py`,
    `backup/policy.py`, `remote/policy.py` and `deploy/dispatch.py` all resolve
    a policy at POINT OF USE by reading `Device.device_group_id` when the job is
    built, so a membership change needs no push at all. `health` is the
    exception -- `resolve_profile_for_device` walks the parent chain and
    `run_policy_reconcile` pushes `health.policy.apply` where a device's
    acknowledged version is behind. Guarding on `devices_moved` is not an
    optimisation: at a 300-second beat an unconditional reconcile is a
    fleet-wide profile comparison every five minutes for every org, forever.

    PUBLISHED AFTER THE COMMIT, like every other job-creating sweep in this
    file. A signed job published before its durable server record commits would
    be an unaudited privileged fleet execution if that commit then failed.

    One org's failure is contained (`_OrgCycleFailures`): a fleet where org #2
    of 300 has an unparseable snapshot must not leave 298 orgs unswept with one
    traceback as the only symptom. The next beat simply retries -- the sweep is
    idempotent, because a device already in the group its rule claims is not
    moved again.

    Returns the total number of devices re-homed across all orgs.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(
        signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s
    )
    failures = _OrgCycleFailures("device_groups.reevaluate")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            pending: list[tuple[UUID, dict[str, Any]]] = []
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    result = await reevaluate_org_membership(session, org_id=org_id)
                    if result.devices_moved:
                        pending = await run_policy_reconcile(
                            session, org_id, job_engine=job_engine
                        )
                # Transaction committed: safe to publish the signed jobs.
                for _job_id, envelope in pending:
                    await publish_job(
                        redis, UUID(envelope["job"]["device"]), envelope
                    )
                total += result.devices_moved
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def health_retention_all_orgs() -> int:
    """Delete cleared `health_alert` rows past `alert_retention_days`, prune
    quiet `health_flap_state` rows, and hard-delete retired `health_check` rows
    no alert still references (endpoint-health spec §12, §2.4's amendment --
    without the third step soft-deleted checks are immortal)."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await health_sweep_module.sweep_retention_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def ipam_reconcile_sweep_all_orgs(now: datetime) -> int:
    """Recompute `ipam_drift` for every org (docs/modules/ipam/spec.md §4.4).

    A thin engine shell around `reconcile.reconcile_sweep_all_orgs`, matching
    `health_retention_all_orgs` above: the per-org loop, the transaction
    boundary and the one-bad-org containment live in the module, not here.

    No job is dispatched and nothing is published to Redis -- reconciliation
    reads evidence and writes findings, and its only outbound effect is the
    `ipam.drift_detected` event on the kernel's transactional outbox.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await ipam_reconcile_module.reconcile_sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine, now=now
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def ipam_retention_all_orgs() -> int:
    """Bound `ipam_observation`, `ipam_drift` and `ipam_sweep_run`
    (docs/modules/ipam/spec.md §4.2).

    A thin engine shell around `ipam.retention.sweep_all_orgs`, matching
    `ipam_reconcile_sweep_all_orgs` above: the per-org loop, the transaction
    boundary and the one-bad-org containment live in the module, not here.

    §4.2 has always said this pass exists -- "a retention pass drops
    observations with `last_seen_at < now() - 365 days` to keep the table
    bounded" -- and until it did, `ipam_observation` had no delete path anywhere
    in the repository. Nothing is dispatched and nothing is published: retention
    deletes, and the audit row records what it removed.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    try:
        return await ipam_retention_module.sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def keycloak_event_sweep_once() -> dict[str, int]:
    """Forward Keycloak's credential events into the audit chain
    (`docs/modules/console-auth/spec.md` §7.1).

    NOT a `*_all_orgs` shell, and that is the shape of the feature rather than
    an omission: Keycloak's realm is shared by every tenant, so one fetch
    covers the whole deployment and the ORG is resolved per event from the
    Keycloak subject. A per-org loop here would fetch the same events once per
    tenant and still have to resolve each one.

    THE COUNTS ARE RECORDED, NOT ONLY RETURNED. Celery's result backend is not
    where an operator looks, so the run is written to Redis for the
    `keycloak_events` probe -- which is the only thing that can tell
    "configured and quiet" from "configured and not running".
    """
    from app.modules.keycloak_events.sweep import sweep_once, write_last_sweep

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        counts = await sweep_once(
            engine=app_engine,
            admin_engine=admin_engine,
            settings=settings,
            now=datetime.now(UTC),
        )
        # AFTER the sweep, deliberately. A record written up front would mark
        # the sweep fresh even when the fetch raised, which is the probe
        # reporting health on the strength of the beat having fired.
        await write_last_sweep(redis, counts, now=time.time())
        return {
            "fetched": counts.fetched,
            "written": counts.written,
            "unresolved": counts.unresolved,
            "skipped": counts.skipped,
        }
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
        await redis.aclose()


def register_worker_job_types() -> None:
    """Register every module's job types into the kernel registry, once, when
    the worker process's Celery app is built.

    The Celery worker+beat process runs its own module-level sweeps
    (``patch_scan_sweep``/``patch_apply_sweep``/``deploy_window_sweep``/
    ``provision_enroll_dispatch_sweep``) that call `job_engine.create()` (via
    `dispatch_apply`/`dispatch_deployment`/`dispatch_provision_enroll`)
    directly -- unlike the API and gateway processes, this process never
    imports a module's route/gateway registration path, so without this call
    the first sweep to fire would be the first code in this process to ask the
    registry for ``patch.scan``/``patch.apply``/``app.deploy``/``script.run``/
    ``provision.enroll`` and would raise `UnknownJobType`. `register()` is a
    plain idempotent dict assignment, so calling this alongside the
    API/gateway's own registration is always safe. This is also the natural
    home for future modules that dispatch jobs from the worker process.
    """
    from app.modules.agent_push.jobtypes import (
        register_job_types as register_agent_push_job_types,
    )
    from app.modules.agent_update.jobtypes import (
        register_job_types as register_agent_update_job_types,
    )
    from app.modules.backup.handler import register_job_types as register_backup_job_types
    from app.modules.deploy.handler import register_job_types as register_deploy_job_types
    from app.modules.health.jobtypes import register_job_types as register_health_job_types
    from app.modules.ipam.handler import register_job_types as register_ipam_job_types
    from app.modules.patch.handler import register_job_types as register_patch_job_types
    from app.modules.provisioning.handler import (
        register_job_types as register_provisioning_job_types,
    )
    from app.modules.site_server.handler import (
        register_job_types as register_site_server_job_types,
    )

    register_patch_job_types()
    register_deploy_job_types()
    register_backup_job_types()
    register_provisioning_job_types()
    register_health_job_types()
    # NO WORKER PATH CREATES A `site.wake` TODAY -- the only two creators are the
    # site-server wake route and the provisioning run-start route, both in the
    # API process. It is registered here anyway, and the reason is not
    # future-proofing: THIS FUNCTION IS WHAT THREE DENY-BY-DEFAULT PARTITIONS USE
    # AS THEIR DEFINITION OF "the live job-type registry"
    # (`tests/test_direct_dispatch.py`, `tests/test_dispatch_permissions.py`,
    # `tests/test_approved_actions_destructive.py`, each of which calls it and
    # then asserts its map covers exactly what is registered). A job type absent
    # here cannot be named in those maps without failing "these named job types
    # are not registered" -- while the process-global registry means an earlier
    # test importing the module makes it appear anyway, so the partition would
    # hold or fail depending on test ordering. That is the exact silence those
    # docstrings say they exist to break.
    # `ipam_sweep_dispatch` calls `JobEngine.create`, which refuses an
    # unregistered type: without this the FIRST scheduled sweep in production
    # raises `UnknownJobType` while every in-process test passes.
    register_ipam_job_types()
    register_site_server_job_types()
    # Agent push install, cycle B2 (docs/modules/agent-distribution/
    # spec-push-install.md §14). Registered here for the same reason as
    # `site.wake`: this function is what three deny-by-default partitions treat
    # as "the live job-type registry", so a push type absent here could not be
    # named in `direct_dispatch.MODULE_GOVERNED_REASONS` or
    # `approved_actions.NON_APPROVABLE_REASONS` without failing "these named
    # types are not registered". Both kernel gates DO name them (they must, per
    # clause (e)), so this registration is what makes the partition hold.
    register_agent_push_job_types()
    register_agent_update_job_types()


async def write_worker_heartbeat(redis: Any = None) -> None:
    """Writes this worker process's own heartbeat once (Task 6,
    `app.core.health.heartbeat`) -- the API cannot probe a Celery consumer, so
    the worker is the only one that can report it is alive. A failure here
    must never break the caller, so it is swallowed and logged, not raised.

    THIS IS THE SINGLE ITERATION, reused by the daemon thread
    `_worker_heartbeat_forever` starts (see `create_celery`'s
    `worker_process_init` receiver below) -- there is deliberately no queue,
    no beat entry and no second way to write this key. `redis` is normally
    omitted -- production builds and closes its own client every call, since
    this coroutine has no persistent connection of its own to reuse -- and is
    accepted here only so a test can inject a fake one and assert directly on
    it (see `tests/test_platform_heartbeat.py`), without that test also
    needing a configured `Settings` environment it has no other reason to
    depend on -- `get_settings()` is therefore only called when it is
    actually needed, to build the default client.

    NOT WRITTEN FROM A QUEUED CELERY TASK. It used to be: a beat entry fired
    this on its own clock, sharing the worker's task queue with every long
    sweep in this file (patch scans, backup verification, retention). A
    worker whose pool slots were all busy with one of those delayed the
    heartbeat behind them, and the status page and the Docker healthcheck
    both read the resulting staleness as `degraded`, then `down` -- reporting
    QUEUE SATURATION, not process death, while the process was entirely
    alive. The daemon thread below owns this call directly, independent of
    the task queue, matching how `app.main` and `app.gateway.main` already
    write their own heartbeats from a background task the process itself
    owns rather than from work it dispatches to itself."""
    owns_redis = redis is None
    if owns_redis:
        redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    instance = os.environ.get("HOSTNAME", uuid4().hex[:8])
    version = os.environ.get("ITS_IMAGE_TAG", "dev")
    try:
        await write_heartbeat(redis, "worker", instance, version, now=time.time())
    except Exception:  # noqa: BLE001 - a heartbeat failure must never take
        # down the process that reports it.
        logger.error("failed to write worker heartbeat", exc_info=True)
    finally:
        if owns_redis:
            await redis.aclose()


async def _worker_heartbeat_forever() -> None:
    """The daemon thread's async body: write one heartbeat, sleep
    `HEARTBEAT_INTERVAL_S`, repeat, for the life of the process. Runs inside
    ONE `asyncio.run()` call for the thread's entire lifetime (see
    `_run_worker_heartbeat_thread`), not one call per iteration -- the same
    `while True: ...; await asyncio.sleep(...)` shape `app.main`'s and
    `app.gateway.main`'s own heartbeat loops already use.

    THIS FUNCTION'S OWN `try`/`except` IS A SECOND GUARD, DELIBERATELY, NOT A
    DUPLICATE OF `write_worker_heartbeat`'s. That coroutine's guard covers
    exactly its Redis `.set()` call (`write_heartbeat`); it does NOT cover
    its own `get_settings()` / `aioredis.from_url()` call, which runs before
    its `try` -- building a client was never the "failure" a heartbeat writer
    swallows for the process it reports on. Under the OLD queued-task
    mechanism that gap was harmless: Celery beat re-enqueues the next attempt
    on its own clock regardless of whether the previous run raised. THIS LOOP
    HAS NO EXTERNAL SCHEDULER -- an exception that escaped `write_worker_
    heartbeat` here would not be retried by anything in fifteen seconds; it
    would end the only thread that writes this process's heartbeat, silently,
    for the rest of the process's life. That failure mode is worse than the
    queue-saturation defect this mechanism replaces, so every iteration is
    guarded here too."""
    while True:
        try:
            await write_worker_heartbeat()
        except Exception:  # noqa: BLE001 - see docstring: this loop is its
            # own scheduler, so an uncaught exception here silently ends
            # worker heartbeats for the rest of the process's life.
            logger.error("worker heartbeat loop iteration failed", exc_info=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


def _run_worker_heartbeat_thread() -> None:
    """Daemon-thread target (see the `worker_process_init` receiver in
    `create_celery`): one `asyncio.run()` call for the thread's whole life,
    running `_worker_heartbeat_forever` -- there is no persistent asyncio
    event loop already on this OS thread to reuse, since Celery's prefork
    pool starts it as a plain thread inside a forked child process, not as an
    asyncio task."""
    asyncio.run(_worker_heartbeat_forever())


async def sample_health_once(session: AsyncSession) -> int:
    """Write one row per probe (Task 4's `build_registry`, Task 7's
    `PlatformHealthSample`). Called by the beat task every 60 seconds.

    SAMPLED BY THE WORKER, NOT ON REQUEST. Request-driven sampling would be
    dense while somebody watches the page and empty overnight -- exactly
    inverted from what a history is for -- and several API replicas would
    each write their own view of the same instant.

    THE PROBE STATE IS BUILT HERE, NOT READ OFF AN `app.state`: this process
    is a Celery worker, not the FastAPI app the same probes also run
    under for `/readyz` -- there is no `app.state` to read them from.
    `app_engine` is `session`'s OWN bind, the same app-role connection this
    function writes through, rather than a second connection opened just to
    probe with. `admin_engine` and `redis` are opened for the duration of one
    probe run and disposed after, matching every other `*_all_orgs` shell in
    this file. `vault` and `signer` are the same local-backend constructions
    the rest of this module already builds per tick for real work (credential
    unwrap, job signing) -- local, no network, so building them again here
    costs nothing production is not already paying.

    ONLY `WORKER_SAMPLED_PROBES` ARE SAMPLED -- every probe but `jwks`. This
    process never verifies a token and constructs no `JwksCache`, so `state`
    below deliberately has no `jwks` attribute at all.

    CORRECTION: an earlier version of this docstring argued that running
    `probe_jwks` anyway and letting it read that absence as `down`/
    `unreachable` was "the honest answer, not a bug to work around". That was
    wrong -- it conflated two different facts ("the IdP is unreachable" and
    "this process does not use the IdP") into a single status. The status
    page computes per-probe uptime from exactly these rows, so writing
    `jwks / down / unreachable` every 60 seconds would show JWKS at a
    permanent 0% uptime next to the API's own live probe reporting `ok` on
    the same page -- an operator reading that during an incident would chase
    the IdP for nothing. The real fix is scope, not a fabricated reading:
    sample only the probes this process can meaningfully measure
    (`app.api.probes.WORKER_SAMPLED_PROBES`) and write nothing for the rest.
    Absence of a row is honest; a manufactured `down` is not. `probe_jwks`
    itself is unchanged -- in the API process, where a `JwksCache` genuinely
    exists, an absent or empty cache must still read as it does today.
    """
    from app.api.probes import WORKER_SAMPLED_PROBES, build_registry
    from app.core.ca import get_ca

    settings = get_settings()
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    state = SimpleNamespace(
        settings=settings,
        app_engine=session.bind,
        admin_engine=admin_engine,
        redis=redis,
        vault=get_vault(settings),
        signer=get_signer(settings),
        ca=get_ca(settings),
    )
    try:
        results = await build_registry(WORKER_SAMPLED_PROBES).run(state)
    finally:
        await redis.aclose()
        await admin_engine.dispose()

    now = datetime.now(UTC)
    for r in results:
        session.add(
            PlatformHealthSample(
                probe=r.name,
                status=r.status.value,
                duration_ms=r.duration_ms,
                # The reason CODE, never an exception's text -- the whole
                # design of `app.core.health.types.ReasonCode` turns on it.
                reason=r.reason.value if r.reason else None,
                sampled_at=now,
            )
        )
    await session.commit()
    return len(results)


async def sample_health_all() -> int:
    """Open the app-role (RLS-scoped) session `sample_health_once` writes
    through, and dispose it after. `platform_health_sample` table's only
    policy grants `app_user` (Task 7's migration 0055) -- this deliberately
    opens `settings.database_url`, the app-role DSN, not
    `database_admin_url`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(app_engine, expire_on_commit=False) as session:
            return await sample_health_once(session)
    finally:
        await app_engine.dispose()


async def prune_health_samples(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete `platform_health_sample` rows older than `RETENTION_HOURS`.
    Idempotent -- a row already beyond the window simply matches again."""
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=RETENTION_HOURS)
    result = await session.execute(
        delete(PlatformHealthSample).where(PlatformHealthSample.sampled_at < cutoff)
    )
    await session.commit()
    # `rowcount` off a DELETE. `Session.execute` is typed as returning a
    # `Result`, which has no `rowcount`; what a DML statement actually returns
    # is a `CursorResult`, which does -- narrowed here the same way
    # `app/modules/ipam/retention.py::_deleted` narrows it.
    return int(getattr(result, "rowcount", 0) or 0)


async def prune_health_samples_all() -> int:
    """Open the app-role session `prune_health_samples` writes through, and
    dispose it after -- same app-role rationale as `sample_health_all`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(app_engine, expire_on_commit=False) as session:
            return await prune_health_samples(session)
    finally:
        await app_engine.dispose()


async def platform_health_notify_all_orgs(now: datetime) -> int:
    """Publish platform probe changes into every org's notification outbox
    (`app/core/health/notify.py`). The verdicts are read once, on the app-role
    session the samples' only policy grants; each org's notice state is then
    applied in its own tenant transaction. One org's failure is contained; see
    `_OrgCycleFailures`."""
    notify = platform_notify_module
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("platform.health_notify")
    total = 0
    try:
        async with AsyncSession(app_engine) as session:
            verdicts = await notify.probe_verdicts(session, now=now)
        if not verdicts:
            return 0
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    total += await notify.notify_org(session, org_id, verdicts, now=now)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def subscription_alerts_all_orgs(now: datetime) -> int:
    """Publish due subscription notice-deadline and renewal alerts, per org
    (`app/modules/subscriptions/alerts.py`). One org's failure is contained;
    see `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("subscriptions.alerts")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    total += await subscription_alerts_module.emit_due_alerts(
                        session,
                        org_id,
                        today=now.date(),
                        window_days=settings.subscription_alert_window_days,
                    )
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def install_link_auto_off_all_orgs(now: datetime) -> int:
    """Record every install link whose automatic switch-off has passed
    (install-link spec §1.1): `enabled` false and one `enrollment.link_disabled`
    audit row with `reason: scheduled`. The link stopped ANSWERING at
    `disable_at` already -- `install_link.is_live` checks it on every request --
    so a late beat delays the record, never the switch-off. One org's failure is
    contained; see `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("install_link.auto_off")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    if await install_link_module.switch_off_due(session, org_id, now):
                        total += 1
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def review_rejections_all_orgs(now: datetime) -> int:
    """Complete every rejection whose agent confirmed its uninstall, or whose 24
    hours are up (install-link spec §1.5, plan D2): revoke the certificate, make
    the row a `decommissioned` tombstone, audit `device.rejection_completed`.

    THE ORDER IS `POST /agents/{id}/revoke`'s: the DB flag (the gateway's
    admission gate) commits inside the tenant transaction; only then the CRL
    and the live-connection drop. A CRL write that fails is retried by nothing
    automatically -- but the device is already refused at the gateway, which
    is the fail-safe direction."""
    from app.core.ca import get_ca

    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    ca = get_ca(settings)
    failures = _OrgCycleFailures("device_review.completion")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    done = await device_review_module.complete_due_rejections(
                        session, org_id, now=now
                    )
                # COMMITTED. Now the CRL, then the connection.
                for item in done:
                    if item.revoke_serial is not None:
                        await ca.revoke(item.revoke_serial)
                    await publish_revoke(redis, item.device_id)
                total += len(done)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total


async def drain_platform_errors_all() -> int:
    """Task 3's worker-side drain: open the app-role session `drain_once`
    writes through (`platform_error_group`/`_event`'s only RLS policy grants
    `app_user`, migration 0056 -- the identical rationale `sample_health_all`
    and `prune_health_samples_all` above already state for the same table
    family), run one drain, and dispose. Scheduled every `DRAIN_INTERVAL_S`
    by the `platform-error-drain` beat entry below, alongside the API's own
    background loop (`app.main.drain_platform_errors_once`) -- both drain the
    SAME in-process queue in whichever process they run in; a process's queue
    only ever holds what THAT process captured.

    UNLIKE THE API'S SEAM, THIS ONE DOES NOT SWALLOW. Every other beat task
    in this file lets a failure propagate to Celery's own task-failure
    handling rather than catching it locally -- and Task 3's `task_failure`
    receiver above is what turns that propagation into a capture, so a
    drain failure here becomes a counted error the NEXT drain cycle can
    pick up, instead of a silently swallowed line only an operator reading
    worker logs would ever see.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(app_engine, expire_on_commit=False) as session:
            return await drain_once(session)
    finally:
        await app_engine.dispose()


def _with_expiry(schedule: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Give every beat entry `options.expires`, defaulting to its own interval.

    **A SWEEP THAT OVERRUNS ITS INTERVAL MUST NOT BACKLOG WITHOUT BOUND.** Beat
    keeps publishing on the clock no matter how far behind the workers are, so
    an entry whose run takes longer than its schedule enqueues a second copy
    while the first is still going, then a third. Nothing here is unsafe when it
    doubles up -- the sweeps are idempotent by construction, and the two that
    could race (`health-alert-actions`, the delivery claim) are serialized by
    `SELECT ... FOR UPDATE SKIP LOCKED` plus a UNIQUE constraint -- so this is a
    CAPACITY bound, not a correctness one. `health-alert-actions` at 30s is the
    shortest-interval sweep that signs jobs, and every signature is an OpenBao
    round trip; a queue of them is how a slow vault turns into a worker doing
    nothing but stale work.

    ONE INTERVAL IS THE RIGHT WINDOW because a message still queued when the
    next one is published has already been superseded: these are sweeps over
    "what is due now", so the fresher message does strictly more work than the
    stale one. Dropping the stale copy loses nothing.

    Applied here, over the literal, rather than written into each entry:
    twenty-odd `"options": {"expires": ...}` lines are twenty-odd chances to
    forget one, and the one that gets forgotten is the one that backlogs.
    `setdefault` so an entry that ever needs a different window can say so and
    keep it.
    """
    for entry in schedule.values():
        entry.setdefault("options", {}).setdefault("expires", entry["schedule"])
    return schedule


def create_celery() -> Celery:
    settings = get_settings()
    # Authenticate this process's audit chain BEFORE anything else here, with
    # the same HMAC key the API and gateway load (invariant #4). Sweeps in this
    # file -- and the module code they call -- write audit events through the
    # process-wide `audit_service`; without this the chain links they append are
    # plain SHA-256, which is a checksum, not evidence: anyone who can write to
    # `audit_events` can recompute them and so forge or rewrite worker-written
    # entries undetected. It also makes `GET /api/v1/audit/verify` report the
    # first such event as `broken_at`, which reads like a failed restore.
    #
    # Placement: this is the worker's single process-wide bootstrap, and it
    # cannot be missed by a task added later -- every Celery task in this module
    # is defined inside the `try` block below whose FIRST statement is
    # `celery_app = create_celery()`, and each is decorated with
    # `@celery_app.task`, so no task can exist in this process without this
    # function having already run. It is strictly broader than a
    # `worker_process_init` handler, which celery only sends from the prefork
    # and solo pools (not threads/gevent/eventlet) and never for `celery beat`
    # or for a script that imports a `*_all_orgs()` sweep directly. Set
    # unconditionally -- passing None when unconfigured clears a key inherited
    # from another context, matching app/main.py and app/gateway/main.py.
    audit_service.set_hmac_key(load_audit_hmac_key(settings))
    register_worker_job_types()

    # C-1: Celery installs its OWN `logging.Formatter` subclass on worker
    # start, which -- like `basicConfig` -- renders `str(exc)` into the
    # traceback tail for any `exc_info=exc` call in this file (`contain`
    # below is exactly such a call). A plain `configure_logging()` here would
    # just get overwritten later; connecting a receiver to Celery's
    # `setup_logging` signal is what SUPPRESSES Celery's own configuration
    # and hands the process to us instead -- that suppression, not the call
    # itself, is why a signal is used rather than calling `configure_logging`
    # directly at the top of this function. `weak=False`: the receiver is a
    # closure local to this function, and celery's default weak reference to
    # a signal receiver is collected the instant `create_celery()` returns
    # and nothing else holds it -- verified, with the default the receiver
    # never fires when the signal is sent. `weak=False` keeps a strong
    # reference so the connection outlives this function call.
    #
    # WHAT INSTALLING `JsonFormatter` HERE COVERS, NARROWED (fix wave 7):
    # `contain` below is safe under `exc_info=exc` because THIS process now
    # installs `JsonFormatter`, which reads only `exc_info[0].__name__` --
    # that covers every log call THIS CODEBASE writes. It does NOT, by
    # itself, cover Celery's OWN task-failure record
    # (`celery.app.trace.TraceInfo._log_error`), which this same handler now
    # also receives and which carries a full traceback, `repr(exc)` and the
    # task's raw args/kwargs under field names redaction-by-name has no
    # reason to flag. `configure_logging()` (`app/core/logging.py`) installs
    # a second, separate defence for that record -- `_CeleryTraceSanitiser`,
    # on the same handler -- see its docstring. This receiver only wires
    # `JsonFormatter` in; it does not need its own copy of that defence.
    #
    # I-3: THIS RECEIVER BEING A CLOSURE IS LOAD-BEARING, NOT INCIDENTAL --
    # `test_the_setup_logging_receiver_still_configures_the_worker_after_gc`
    # (`tests/test_worker_sweep_containment.py`) exercises the mechanism
    # behaviourally (build the Celery app, force `gc.collect()`, fire the
    # signal, assert `JsonFormatter` is actually installed) rather than
    # merely checking that this module's SOURCE calls `configure_logging` --
    # a source-level check cannot see that the call only happens if this
    # closure survived, and deleting `weak=False` used to leave the AST check
    # green while the worker silently reverted to Celery's own formatter.
    #
    # I-4: `configure_logging()` used to hardcode `level="INFO"`, silently
    # ignoring the `loglevel` Celery's `setup_logging_subsystem` sends on
    # this signal (itself resolved from `--loglevel`,
    # `deploy/docker-compose.yml`'s `--loglevel=info`) -- changing that flag
    # to `debug` during an incident did nothing. `loglevel` is now forwarded.
    #
    # F-9: connecting `setup_logging` makes `-f`/`--logfile` inert too, for
    # the same reason `--loglevel` used to be silently dropped: suppressing
    # Celery's own `_configure_logger` (see the paragraph below) means the
    # `logfile` argument that call would otherwise have honoured is never
    # read at all. `configure_logging()` always writes to `sys.stdout` --
    # this receiver does not forward `logfile`, and there is no code path
    # left that would route worker output to a file even if it did, since
    # `JsonFormatter`'s handler is hardcoded to `StreamHandler(sys.stdout)`.
    # Not fixed here (this platform's deployment is `deploy/docker-compose.
    # yml`'s log driver, per spec §4 -- "no file handling" -- so a worker
    # started with `--logfile` was never a supported configuration), but
    # named here so the silent-ignore is a documented decision, not a
    # surprise the next person to pass `--logfile` discovers by hand.
    #
    # M-6: `dispatch_uid` makes this connection IDEMPOTENT. Without it, every
    # call to `create_celery()` -- one per process in production, but many
    # per test session, each from a different test module -- left a NEW
    # strong-referenced receiver connected forever (celery signals are never
    # disconnected automatically), and each one independently calls
    # `root.handlers[:] = [h]` on `setup_logging.send()`. A signal firing
    # mid-suite -- from an unrelated test that also builds a Celery app --
    # would therefore silently evict pytest's `caplog` handler from the root
    # logger, once per leaked receiver. `dispatch_uid` makes a repeat
    # `.connect()` call with the same id a no-op (verified:
    # `Signal.receivers` stays at length 1 across repeated `create_celery()`
    # calls), so there is always exactly one receiver regardless of how many
    # times this function runs in one process.
    #
    # UNDOCUMENTED LOAD-BEARING BEHAVIOUR, NAMED HERE (I-4): connecting
    # `setup_logging` makes Celery's `Logging.setup_logging_subsystem` see a
    # non-empty `receivers` return value, which is what makes it SKIP its own
    # `_configure_logger` call -- and that, not anything explicit in this
    # file, is what disables `worker_redirect_stdouts`'s stdout/stderr ->
    # logger redirection. Today that is benign (no bare `print()` under
    # `app/`) and it is load-bearing: without it, this handler's own
    # `StreamHandler(sys.stdout)` would receive Celery's redirected stdout
    # writes, including ITS OWN formatted output, an infinite `LoggingProxy`
    # <-> `StreamHandler` recursion. A future `print()` added under `app/`
    # would not silently reopen this -- it would simply not be captured by
    # the logger at all -- but the absence of that recursion is this
    # receiver's doing, not a property of Python logging in general.
    from celery.signals import setup_logging as _celery_setup_logging

    from app.core.logging import configure_logging

    @_celery_setup_logging.connect(
        weak=False, dispatch_uid="app.worker.configure_worker_logging"
    )  # type: ignore[untyped-decorator]
    def _configure_worker_logging(
        loglevel: int | str | None = None, **_kwargs: object
    ) -> None:
        configure_logging(level=loglevel if loglevel is not None else "INFO")

    # THE WORKER'S OWN LIVENESS, WRITTEN FROM A THREAD THE PROCESS OWNS, NOT
    # FROM A QUEUED TASK -- see `write_worker_heartbeat`'s docstring for the
    # defect this replaces (a beat-scheduled heartbeat shares the worker's
    # task queue with every long sweep in this file, so queue saturation read
    # as `degraded`/`down` while the process was entirely alive). `app.main`
    # and `app.gateway.main` already write their own heartbeats this way --
    # a background task/thread the process starts once and owns directly.
    #
    # `worker_process_init`, NOT `worker_ready`: it fires once PER CHILD
    # PROCESS under the prefork/solo pools (`create_celery`'s own comment
    # above, on why it cannot be relied on for the audit-key bootstrap,
    # documents this same scoping) -- AFTER the fork, which is exactly when a
    # new thread must be started, since a thread alive in the parent before
    # `fork()` does not survive into the child. `worker_ready` fires once for
    # the whole worker (the arbiter), which would leave every pool child
    # process's queue-bound task execution unrepresented by this heartbeat --
    # `worker_process_init` matches "this worker process is alive" more
    # directly than "the worker has finished booting" does.
    #
    # `weak=False` for the same reason as `_configure_worker_logging` just
    # above: this receiver is a closure local to this function, and Celery's
    # default WEAK reference to a signal receiver is collected the instant
    # `create_celery()` returns and nothing else holds it -- this project
    # already lost a fix to exactly that (see `_configure_worker_logging`'s
    # own comment, I-3). `dispatch_uid` keeps repeated `create_celery()`
    # calls (one per test module that builds this app, many per test
    # session) from leaving a new receiver connected every time -- without it
    # a single `worker_process_init` firing late in a test session would
    # start one thread per leaked receiver.
    from celery.signals import worker_process_init as _worker_process_init

    @_worker_process_init.connect(
        weak=False, dispatch_uid="app.worker.start_heartbeat_thread"
    )  # type: ignore[untyped-decorator]
    def _start_worker_heartbeat_thread(**_kwargs: object) -> None:
        threading.Thread(
            target=_run_worker_heartbeat_thread,
            name="worker-heartbeat",
            daemon=True,  # so it can never keep a shutting-down worker alive
        ).start()

    # Task 3 (error monitoring, spec §3): this process's OTHER capture seam,
    # alongside `_OrgCycleFailures.contain` above. `contain` covers a
    # CONTAINED per-org sweep failure; `task_failure` covers the opposite
    # case -- a task that raised all the way out (every `*_all_orgs` shell
    # when EVERY org failed, per `raise_if_total`, and any task in this file
    # that has no per-org containment at all) and that Celery's own failure
    # machinery is about to log and mark failed. Without this, that whole
    # class of worker failure would be invisible to the error monitor.
    #
    # `weak=False`, FOR THE SAME MEASURED REASON as `_configure_worker_
    # logging` and `_start_worker_heartbeat_thread` above: this receiver is
    # a closure local to `create_celery`, and Celery's default WEAK
    # reference to a signal receiver is collected the instant this function
    # returns and nothing else holds it -- this project has already lost a
    # fix to exactly that once (see `_configure_worker_logging`'s own
    # comment, I-3). `dispatch_uid` keeps repeated `create_celery()` calls
    # (many per test session) from leaving one new receiver connected per
    # call, each independently capturing the same failure.
    #
    # `exception` carries its own `__traceback__` at the point this signal
    # fires -- `celery/app/trace.py::TraceInfo.handle_failure` builds it via
    # `ExceptionInfo(exc_info=(exc_type, exc, exc.__traceback__))` and sends
    # the signal BEFORE its own `traceback_clear(exc)` call, so
    # `capture_exception`'s `_raise_site` walk (which reads `exc.
    # __traceback__`) sees the real raise site, not a cleared one.
    from celery.signals import task_failure as _celery_task_failure

    @_celery_task_failure.connect(
        weak=False, dispatch_uid="app.worker.capture_task_failures"
    )  # type: ignore[untyped-decorator]
    def _capture_task_failure(
        exception: BaseException | None = None, **_kwargs: object
    ) -> None:
        if exception is not None:
            capture_exception(exception, where="worker")

    celery = Celery("its", broker=settings.redis_url, backend=settings.redis_url)
    celery.conf.beat_schedule = _with_expiry({
        # Task 6's worker liveness heartbeat is NOT a beat entry -- see the
        # `worker_process_init` receiver and `write_worker_heartbeat`'s
        # docstring above for why a queued heartbeat measures queue depth,
        # not liveness, and is not scheduled here for that reason.
        #
        # docs/superpowers/specs/2026-08-15-platform-health-design.md §6.1:
        # same 60s cadence as `sweep-expired-jobs` below, one row per probe.
        # Task 3 (error monitoring, spec §3.1): drains the SAME bounded
        # in-process queue `app.core.errors.capture` fills, on this
        # process's own clock -- `DRAIN_INTERVAL_S` is the module's own
        # constant (`app.core.errors.store`), not a literal, so this entry
        # and `app.main`'s API-side loop cannot drift apart on how often
        # they drain.
        "platform-error-drain": {
            "task": "app.worker.platform_error_drain",
            "schedule": DRAIN_INTERVAL_S,
        },
        "platform-health-sample": {
            "task": "app.worker.platform_health_sample",
            "schedule": 60.0,
        },
        # Probe changes to a person, through `notification` (health/notify.py).
        "platform-health-notify": {
            "task": "app.worker.platform_health_notify",
            "schedule": platform_notify_module.NOTIFY_INTERVAL_S,
        },
        # §6.1's daily retention sweep. `RETENTION_HOURS` is the module's own
        # constant (not a literal here) for the same reason the health-alert
        # sweeps below read `health_sweep_module.RETENTION_INTERVAL_S`.
        "platform-health-retention": {
            "task": "app.worker.platform_health_retention",
            "schedule": 86400.0,
        },
        "sweep-expired-jobs": {"task": "app.worker.sweep_expired_jobs", "schedule": 60.0},
        "security-log-retention": {
            "task": "app.worker.apply_security_log_retention",
            "schedule": 86400.0,
        },
        "remote-recording-retention": {
            "task": "app.worker.apply_remote_recording_retention",
            "schedule": 86400.0,
        },
        "patch-scan-sweep": {"task": "app.worker.patch_scan_sweep", "schedule": 3600.0},
        # Hourly passes; each device is due about once a day and a pass is
        # capped so a fleet's first collect is spread out.
        "inventory-collect-sweep": {
            "task": "app.worker.inventory_collect_sweep",
            "schedule": inventory_schedule_module.SWEEP_INTERVAL_S,
        },
        # The org's `ship_interval_s` decides who is due; this is only how
        # often the beat asks.
        "logs-ship-sweep": {
            "task": "app.worker.logs_ship_sweep",
            "schedule": logs_ship_schedule_module.SWEEP_INTERVAL_S,
        },
        "patch-apply-sweep": {"task": "app.worker.patch_apply_sweep", "schedule": 900.0},
        "patch-reboot-sweep": {"task": "app.worker.patch_reboot_sweep", "schedule": 900.0},
        "script-policy-sweep": {"task": "app.worker.script_policy_sweep", "schedule": 900.0},
        "deploy-window-sweep": {"task": "app.worker.deploy_window_sweep", "schedule": 900.0},
        "backup-window-sweep": {"task": "app.worker.backup_window_sweep", "schedule": 900.0},
        "backup-retention-gc": {"task": "app.worker.backup_retention_gc", "schedule": 86400.0},
        "backup-reconcile": {"task": "app.worker.backup_reconcile", "schedule": 300.0},
        "backup-test-restore": {"task": "app.worker.backup_test_restore", "schedule": 86400.0},
        "backup-health": {"task": "app.worker.backup_health", "schedule": 900.0},
        # The off-site copy runs on its own queue, served by the
        # `backup-replicator` service, so a multi-hour first seed never delays
        # the sweeps above.
        "backup-replication": {
            "task": "app.worker.backup_replication",
            "schedule": 300.0,
            "options": {"queue": BACKUP_REPLICATION_QUEUE},
        },
        "backup-offsite-test": {
            "task": "app.worker.backup_offsite_test",
            "schedule": 60.0,
            "options": {"queue": BACKUP_REPLICATION_QUEUE},
        },
        "backup-offsite-gc": {
            "task": "app.worker.backup_offsite_gc",
            "schedule": 86400.0,
            "options": {"queue": BACKUP_REPLICATION_QUEUE},
        },
        "backup-verification": {"task": "app.worker.backup_verification", "schedule": 86400.0},
        "provision-enroll-dispatch": {
            "task": "app.worker.provision_enroll_dispatch",
            "schedule": 300.0,
        },
        "provision-timeout-sweep": {
            "task": "app.worker.provision_timeout_sweep",
            "schedule": 3600.0,
        },
        "licenses-compliance-sweep": {
            "task": "app.worker.licenses_compliance_sweep",
            "schedule": 86400.0,
        },
        "subscription-alerts": {
            "task": "app.worker.subscription_alerts",
            "schedule": subscription_alerts_module.SWEEP_INTERVAL_S,
        },
        # The install link (spec 2026-09-28 §1.1, §1.5). Both every minute:
        # the switch-off is already enforced per request, and a rejection's
        # certificate should not outlive the agent's confirmation by long.
        "install-link-auto-off": {
            "task": "app.worker.install_link_auto_off",
            "schedule": install_link_module.AUTO_OFF_INTERVAL_S,
        },
        "device-review-completion": {
            "task": "app.worker.device_review_completion",
            "schedule": device_review_module.COMPLETION_INTERVAL_S,
        },
        "notification-drain-outbox": {
            "task": "app.worker.notification_drain_outbox",
            "schedule": 30.0,
        },
        "notification-deliver-pending": {
            "task": "app.worker.notification_deliver_pending",
            "schedule": 30.0,
        },
        "email-intake-poll": {
            "task": "app.worker.email_intake_poll",
            "schedule": 120.0,
        },
        "sla-sweep": {
            "task": "app.worker.sla_sweep",
            "schedule": sla_sweep_module.SWEEP_INTERVAL_SECONDS,
        },
        # Endpoint health, spec §12. The intervals are PROJECT CONSTANTS read
        # from the module rather than literals here: detection granularity is an
        # operational property of this deployment, and a literal in this file
        # can drift from §12's table without anything noticing.
        "health-alert-actions": {
            "task": "app.worker.health_alert_actions",
            "schedule": health_sweep_module.ACTION_INTERVAL_S,
        },
        "health-alert-sweep": {
            "task": "app.worker.health_alert_sweep",
            "schedule": health_sweep_module.ALERT_SWEEP_INTERVAL_S,
        },
        "health-offline-sweep": {
            "task": "app.worker.health_offline_sweep",
            "schedule": health_sweep_module.OFFLINE_SWEEP_INTERVAL_S,
        },
        "health-policy-reconcile": {
            "task": "app.worker.health_policy_reconcile",
            "schedule": health_sweep_module.POLICY_RECONCILE_INTERVAL_S,
        },
        "health-retention": {
            "task": "app.worker.health_retention",
            "schedule": health_sweep_module.RETENTION_INTERVAL_S,
        },
        # Once per org ever; the hourly pass only finds orgs not yet decided.
        "health-baseline-reconcile": {
            "task": "app.worker.health_baseline_reconcile",
            "schedule": health_baseline_module.RECONCILE_INTERVAL_S,
        },
        # Device-group membership, design §5.3. The interval is the SERVICE's
        # own constant rather than a literal, for the reason the health and
        # IPAM entries state: this number IS the lag a membership change costs,
        # the console tells the operator what it is, and a literal here can
        # drift from what the screen says with nothing noticing.
        #
        # It is a beat and not a request-time enqueue because no route in this
        # codebase enqueues a Celery task -- everything is beat-driven -- and
        # introducing request-to-broker enqueueing to shave a bounded five
        # minutes off a membership update is a larger change than the feature
        # it would serve.
        "device-group-reevaluate": {
            "task": "app.worker.device_group_reevaluate",
            "schedule": device_group_service.REEVALUATE_INTERVAL_S,
        },
        # IPAM, spec §5.3. Hourly, because DUENESS is per subnet and clamped to
        # >= 1 hour (§0.10 point 6) -- the beat asks "what is due" every hour,
        # it does not sweep every subnet every hour.
        "ipam-sweep-dispatch": {
            "task": "app.worker.ipam_sweep_dispatch",
            "schedule": ipam_sweep_module.SWEEP_DISPATCH_INTERVAL_S,
        },
        # Daily, per §4.4 and §0.8: drift needs two consecutive detections to
        # speak, so the cadence IS the deduplication window. An hourly
        # reconcile would make "two consecutive detections" mean two hours and
        # turn a switch reboot into an event.
        "ipam-reconcile-sweep": {
            "task": "app.worker.ipam_reconcile_sweep",
            "schedule": ipam_reconcile_module.RECONCILE_INTERVAL_SECONDS,
        },
        # console-auth §7.1. Hourly, matching the module's own constant rather
        # than a literal: this is the LAG the sweep costs versus a push
        # listener, and it is stated in the spec as the price of not shipping a
        # Java plugin into a Keycloak image this project pins and does not
        # build. A faster beat would buy nothing -- Keycloak's own event
        # filters are day-granular.
        "keycloak-event-sweep": {
            "task": "app.worker.keycloak_event_sweep",
            "schedule": keycloak_sweep_module.SWEEP_INTERVAL_S,
        },
        # §4.2's retention pass. Daily: retention is a clock, and it is the ONLY
        # thing that bounds `ipam_observation` -- the upsert keeps that table
        # out of the monitoring business, but nothing kept it out of the
        # forever business until this entry existed.
        "ipam-retention": {
            "task": "app.worker.ipam_retention",
            "schedule": ipam_retention_module.RETENTION_INTERVAL_S,
        },
        # AI admin agent, spec §0.7/§3.2. Daily: retention is a clock, and the
        # window is a PER-ORG setting the sweep resolves itself.
        "ai-transcript-retention": {
            "task": "app.worker.ai_transcript_retention",
            "schedule": ai_retention_module.RETENTION_INTERVAL_S,
        },
        # docs/modules/ai-automation.md §6.4. The interval is the module's own
        # constant, not a literal, because it is tied to `ai_settings.
        # action_expiry_s`'s LOWER BOUND (60 s) -- a beat slower than the
        # shortest expiry an org may configure leaves rows confirmable past
        # their stated deadline.
        # spec-self-update.md §6.4. Returns at once unless some org has a
        # rollout running, paused or with jobs in flight.
        "agent-update-tick": {
            "task": "app.worker.agent_update_tick",
            "schedule": agent_update_reconcile.TICK_INTERVAL_S,
        },
        # Push runs (design 2026-09-30 §1.4): the pacer's backstop. Returns at
        # once unless some org has a run running, paused, or with installs in
        # flight.
        "push-run-tick": {
            "task": "app.worker.push_run_tick",
            "schedule": push_runs_module.TICK_INTERVAL_S,
        },
        "ai-action-expiry": {
            "task": "app.worker.ai_action_expiry",
            "schedule": ai_action_sweep_module.EXPIRY_INTERVAL_S,
        },
    })
    celery.conf.timezone = "UTC"
    return celery


try:  # pragma: no cover - the worker process needs settings; tests may not
    celery_app = create_celery()

    @celery_app.task(name="app.worker.platform_error_drain")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def platform_error_drain() -> dict[str, int]:
        return {"drained": asyncio.run(drain_platform_errors_all())}

    @celery_app.task(name="app.worker.platform_health_sample")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def platform_health_sample() -> dict[str, int]:
        return {"written": asyncio.run(sample_health_all())}

    @celery_app.task(name="app.worker.platform_health_notify")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def platform_health_notify() -> dict[str, int]:
        return {"published": asyncio.run(platform_health_notify_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.platform_health_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def platform_health_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(prune_health_samples_all())}

    @celery_app.task(name="app.worker.sweep_expired_jobs")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def sweep_expired_jobs() -> dict[str, int]:
        expired, redispatched = asyncio.run(sweep_all_orgs())
        return {"expired": expired, "redispatched": redispatched}

    @celery_app.task(name="app.worker.apply_security_log_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def apply_security_log_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(retention_all_orgs())}

    @celery_app.task(name="app.worker.apply_remote_recording_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def apply_remote_recording_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(remote_recording_retention_all_orgs())}

    @celery_app.task(name="app.worker.patch_scan_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def patch_scan_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(patch_scan_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.inventory_collect_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def inventory_collect_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(inventory_collect_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.logs_ship_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def logs_ship_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(logs_ship_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.ipam_sweep_dispatch")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def ipam_sweep_dispatch() -> dict[str, int]:
        return {"dispatched": asyncio.run(ipam_sweep_dispatch_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.ipam_reconcile_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def ipam_reconcile_sweep() -> dict[str, int]:
        return {"detected": asyncio.run(ipam_reconcile_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.keycloak_event_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def keycloak_event_sweep() -> dict[str, int]:
        return asyncio.run(keycloak_event_sweep_once())

    @celery_app.task(name="app.worker.ipam_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def ipam_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(ipam_retention_all_orgs())}

    @celery_app.task(name="app.worker.ai_transcript_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def ai_transcript_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(ai_transcript_retention_all_orgs())}

    @celery_app.task(name="app.worker.ai_action_expiry")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def ai_action_expiry() -> dict[str, int]:
        return {"expired": asyncio.run(ai_action_expiry_all_orgs())}

    @celery_app.task(name="app.worker.patch_apply_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def patch_apply_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(patch_apply_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.patch_reboot_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def patch_reboot_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(patch_reboot_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.script_policy_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def script_policy_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(script_policy_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.deploy_window_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def deploy_window_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(deploy_window_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_window_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_window_sweep() -> dict[str, int]:
        return {"dispatched": asyncio.run(backup_window_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_reconcile")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_reconcile() -> dict[str, int]:
        return {"reconciled": asyncio.run(backup_reconcile_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_replication")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_replication() -> dict[str, int]:
        return {"copied": asyncio.run(backup_replication_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_offsite_test")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_offsite_test() -> dict[str, int]:
        return {"tested": asyncio.run(backup_offsite_test_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_offsite_gc")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_offsite_gc() -> dict[str, int]:
        return {"deleted": asyncio.run(backup_offsite_gc_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_test_restore")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_test_restore() -> dict[str, int]:
        return {"dispatched": asyncio.run(backup_test_restore_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_health")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_health() -> dict[str, int]:
        return {"opened": asyncio.run(backup_health_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_retention_gc")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_retention_gc() -> dict[str, int]:
        return {"pruned": asyncio.run(backup_retention_gc_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.backup_verification")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def backup_verification() -> dict[str, int]:
        return {"verified": asyncio.run(backup_verification_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.provision_enroll_dispatch")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def provision_enroll_dispatch() -> dict[str, int]:
        return {
            "dispatched": asyncio.run(provision_enroll_dispatch_sweep_all_orgs(datetime.now(UTC)))
        }

    @celery_app.task(name="app.worker.provision_timeout_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def provision_timeout_sweep() -> dict[str, int]:
        return {"failed": asyncio.run(provision_timeout_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.licenses_compliance_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def licenses_compliance_sweep() -> dict[str, int]:
        return {"snapshots": asyncio.run(licenses_compliance_sweep_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.subscription_alerts")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def subscription_alerts() -> dict[str, int]:
        return {"published": asyncio.run(subscription_alerts_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.install_link_auto_off")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def install_link_auto_off() -> dict[str, int]:
        return {"switched_off": asyncio.run(install_link_auto_off_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.device_review_completion")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def device_review_completion() -> dict[str, int]:
        return {"completed": asyncio.run(review_rejections_all_orgs(datetime.now(UTC)))}

    @celery_app.task(name="app.worker.notification_drain_outbox")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def notification_drain_outbox() -> dict[str, int]:
        return {"processed": asyncio.run(drain_outbox_all_orgs())}

    @celery_app.task(name="app.worker.notification_deliver_pending")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def notification_deliver_pending() -> dict[str, int]:
        delivered, returned = asyncio.run(deliver_pending_all_orgs())
        return {"delivered": delivered, "returned": returned}

    @celery_app.task(name="app.worker.email_intake_poll")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def email_intake_poll() -> dict[str, int]:
        return {"polled": asyncio.run(email_intake_poll_all_orgs())}

    @celery_app.task(name="app.worker.sla_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def sla_sweep() -> dict[str, int]:
        return {"actions": asyncio.run(sla_sweep_all_orgs())}

    @celery_app.task(name="app.worker.agent_update_tick")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def agent_update_tick() -> dict[str, int]:
        return {"dispatched": asyncio.run(agent_update_tick_all_orgs())}

    @celery_app.task(name="app.worker.push_run_tick")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def push_run_tick() -> dict[str, int]:
        return {"dispatched": asyncio.run(push_run_tick_all_orgs())}

    @celery_app.task(name="app.worker.health_alert_actions")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_alert_actions() -> dict[str, int]:
        return {"actions": asyncio.run(health_alert_actions_all_orgs())}

    @celery_app.task(name="app.worker.health_alert_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_alert_sweep() -> dict[str, int]:
        return {"actions": asyncio.run(health_alert_sweep_all_orgs())}

    @celery_app.task(name="app.worker.health_offline_sweep")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_offline_sweep() -> dict[str, int]:
        return {"opened": asyncio.run(health_offline_sweep_all_orgs())}

    @celery_app.task(name="app.worker.health_baseline_reconcile")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_baseline_reconcile() -> dict[str, int]:
        return {"seeded": asyncio.run(health_baseline_reconcile_all_orgs())}

    @celery_app.task(name="app.worker.health_policy_reconcile")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_policy_reconcile() -> dict[str, int]:
        return {"pushed": asyncio.run(health_policy_reconcile_all_orgs())}

    @celery_app.task(name="app.worker.device_group_reevaluate")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def device_group_reevaluate() -> dict[str, int]:
        return {"moved": asyncio.run(device_group_reevaluate_all_orgs())}

    @celery_app.task(name="app.worker.health_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def health_retention() -> dict[str, int]:
        return {"rows": asyncio.run(health_retention_all_orgs())}

except MissingAuditKeyError:
    # Fail closed *loudly*. Swallowing this would leave `celery_app = None`, so
    # the worker would refuse to start anyway -- but with an opaque "app is
    # None" error from the celery CLI rather than the real cause (a production
    # deployment with no ITS_AUDIT_HMAC_KEY_PATH). Same posture as create_app()
    # and run_gateway(), which let this propagate.
    raise
except Exception:  # noqa: BLE001
    celery_app = None

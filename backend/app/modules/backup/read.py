"""The READ half of the repository exchange (`backup_read_query` ->
`backup_read_grant`).

`dedup.py` authorizes WRITES for a `backup.run`; this module authorizes READS
for a `backup.restore`. The two are deliberately separate modules over the same
discipline rather than one parameterised path: they resolve different job types
against different run tables, and a shared resolver would put the capability
that TAKES backups on the same code as the one that returns them.

THE QUERY CARRIES SUFFIXES, NEVER KEYS. Every key is derived here, by joining a
suffix onto the prefix computed from ids this process is already authorized for.
An agent cannot express a location outside its own repository, so the boundary
is a property of the construction rather than of remembering to validate. A
suffix whose shape could escape anyway is refused a layer earlier, by
`BackupReadQueryMsg`, and a third time by `_safe_object_suffix` inside
`presign_backup_read`.

EVERY REFUSAL IS SILENCE PLUS AN AUDIT RECORD. `BackupChunkGrantMsg`'s reasoning
applies unchanged: a reply that said "no such job" and a reply that said "not
your job" would together be an oracle a compromised endpoint could walk. The
agent sees a refusal as `readreply`'s bounded timeout.

Per-grant auditing is deliberately NOT done, for the reason `dedup.py` states:
one restore is many batches, and a hash-chained log whose value is that a human
can read it must not be swamped by them. The authorization these grants rest on
is already audited once, at `backup.restore_dispatched`, and it names the same
backup point this grant's repository is reached through. What IS audited is
every REFUSAL, with a reason code.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.protocol import (
    BackupReadGrantItem,
    BackupReadGrantMsg,
    BackupReadQueryMsg,
)
from app.models import BackupPoint, BackupRepository, Job, RestoreJob

# The live-job set is the WRITE side's, imported rather than restated. "Which
# job states may still be doing work" is one platform-wide answer, and a second
# copy of it is a second thing to drift: a state added to one exchange and not
# the other would refuse a legitimate restore that a legitimate backup accepts.
from app.modules.backup.dedup import _LIVE_JOB_STATUSES, refusal_client
from app.modules.backup.models import REPOSITORY_ACTIVE, REPOSITORY_RETIRED
from app.modules.backup.schedule import RESTORE_START_DEADLINE, grant_window_end


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

# THE READ WINDOW. 256 suffixes is ~256 MiB at the pinned 1 MiB average chunk
# size -- about 3.5 minutes on a 10 Mbps link, comfortably inside this window,
# so a batch does not expire mid-fetch. Erring short is safe because a read is
# idempotent and a re-request is cheap; erring long widens what a leaked URL is
# worth. Deliberately NOT tied to the job's `expires_at`: a job with a generous
# lifetime would silently widen the signing window, which is the property being
# controlled. The job's lifetime is still a CEILING, applied below.
READ_GRANT_TTL = 900

#: The repository states a restore may read (phase 3 design §5.5): a retired
#: repository takes no new backup, and its points stay restorable. `dedup.py`
#: still grants writes on `active` only.
READABLE_STATES = frozenset({REPOSITORY_ACTIVE, REPOSITORY_RETIRED})


@dataclass(frozen=True)
class _ResolvedRestore:
    """Everything the answer needs, all of it read off server-side rows."""

    job: Job
    repository_id: UUID
    storage_ref: str
    grant_until: datetime


async def _audit_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    reason: str,
) -> None:
    """The twin of `dedup.py::_audit_refusal`, and it must stay in step with it.

    `reason` is a word from the fixed vocabulary used below -- never a suffix,
    never a key, never an exception message. This row is hash-chained and
    permanent. Its client is `dedup.refusal_client`'s (phase 3 design §5.5).
    """
    client_id = await refusal_client(
        session, org_id=org_id, device_id=device_id, job_id=job_id
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="backup.read_refused",
        target_type="device",
        target_id=str(device_id),
        # `job_id` is agent-supplied and may be anything the wire model allowed;
        # bounded here so a hostile value cannot bloat an audit row.
        metadata={"reason": reason, "job_id": job_id[:64]},
        client_id=client_id,
    )


async def _resolve_restore(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    now: datetime,
) -> tuple[_ResolvedRestore | None, str]:
    """(what the job authorizes, or None and a reason code).

    `dedup.py::_resolve`'s structure and reason vocabulary, over the RESTORE
    side's rows: a `backup.restore` job with a live `RestoreJob`, whose
    repository is reached through the backup point it is restoring
    (`RestoreJob.backup_point_id -> BackupPoint.repository_id`) -- there is no
    `repository_id` on `restore_jobs`, and inventing one here would be a second
    statement of which repository a restore reads.

    EVERY lookup carries `org_id` explicitly (invariant #5) and the device
    binding is checked against the certificate-resolved `device_id`, never
    against anything in the message.
    """
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None, "malformed_job_id"

    job = (
        await session.execute(select(Job).where(Job.org_id == org_id, Job.id == parsed))
    ).scalar_one_or_none()
    if job is None:
        return None, "no_such_job"
    if job.type != "backup.restore":
        # A read grant over a repository must come from the job type that READS
        # one. A `backup.run` reaching here is either a bug or an endpoint
        # trying to borrow authorization from a job it does hold.
        return None, "wrong_job_type"
    if job.target_type != "device" or job.target_id != device_id:
        return None, "wrong_device"
    if job.status not in _LIVE_JOB_STATUSES:
        return None, "job_not_live"
    if job.expires_at <= now:
        # The grant's lifetime is clamped by this; an expired job would
        # otherwise mint a zero- or negative-lifetime URL, or (worse, if the
        # clamp were the other way) one that outlives its job.
        return None, "job_expired"
    # The restore params carry no start deadline (the wire section does not
    # define one), so it is derived from when the server issued the job.
    grant_until, lifetime_reason = grant_window_end(
        start_deadline=_as_utc(job.issued_at) + RESTORE_START_DEADLINE,
        started_at=job.started_at,
        expires_at=job.expires_at,
        now=now,
    )
    if grant_until is None:
        return None, lifetime_reason

    restore = (
        await session.execute(
            select(RestoreJob).where(RestoreJob.org_id == org_id, RestoreJob.job_id == job.id)
        )
    ).scalar_one_or_none()
    if restore is None:
        return None, "no_such_restore"
    if restore.state != "running":
        # A finished restore must not still mint grants: the window closes when
        # the work does, not when the job's `expires_at` happens to pass.
        return None, "restore_not_running"

    point = (
        await session.execute(
            select(BackupPoint).where(
                BackupPoint.org_id == org_id, BackupPoint.id == restore.backup_point_id
            )
        )
    ).scalar_one_or_none()
    if point is None:
        return None, "no_such_point"

    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id,
                BackupRepository.id == point.repository_id,
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        return None, "no_such_repository"
    if repo.state not in READABLE_STATES:
        return None, "repository_not_active"

    return (
        _ResolvedRestore(
            job=job,
            repository_id=repo.id,
            storage_ref=repo.storage_ref,
            grant_until=grant_until,
        ),
        "",
    )


async def answer_read_query(
    session: AsyncSession,
    object_store: ObjectStore,
    msg: BackupReadQueryMsg,
    *,
    org_id: UUID,
    device_id: UUID,
    now: datetime | None = None,
) -> BackupReadGrantMsg | None:
    """Answer one read query, or refuse it silently.

    `org_id`/`device_id` come from the gateway's mTLS binding. Returns the reply
    the caller should send to this device, or `None` -- in which case an audit
    record has been written and nothing must be sent.
    """
    now = now or datetime.now(UTC)

    resolved, reason = await _resolve_restore(
        session, org_id=org_id, device_id=device_id, job_id=msg.job_id, now=now
    )
    if resolved is None:
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason=reason
        )
        return None

    # THE PREFIX IS DERIVED AND THE STORED VALUE IS ONLY CHECKED AGAINST IT --
    # `answer_chunk_query`'s rule, unchanged. A `storage_ref` column somehow
    # made to name another tenant's prefix yields NO grant at all, rather than a
    # signed one over whatever it names.
    derived_prefix = object_store.backup_prefix(org_id, resolved.repository_id)
    if resolved.storage_ref != derived_prefix:
        await _audit_refusal(
            session,
            org_id=org_id,
            device_id=device_id,
            job_id=msg.job_id,
            reason="storage_ref_mismatch",
        )
        return None

    # The grant dies with the job that authorized it, and never later. The job's
    # remaining life is a CEILING on the window, not the window itself.
    expires_in = min(READ_GRANT_TTL, int((resolved.grant_until - now).total_seconds()))
    if expires_in < 1:
        # REACHABLE: a job with well under a second of remaining life (e.g.
        # 500ms) is NOT expired, so `_resolve_restore`'s `job.expires_at <= now`
        # check passes it -- and `int(...)` above then truncates that remainder
        # to 0. `test_a_sub_second_remainder_is_refused_rather_than_signed`
        # exercises exactly this.
        #
        # Kept as its own guard because it is what makes THIS
        # `presign_backup_read` call safe on its own, rather than dependent on
        # a check twenty lines up: without it, a sub-second remainder would
        # reach `presign_backup_read` and be refused by a raised `ValueError`
        # instead of this auditable `job_expired` silence.
        #
        # The resolver's own `job.expires_at <= now` check is still
        # load-bearing despite this guard existing:
        # `test_the_resolvers_expiry_guard_is_load_bearing` deletes it and
        # shows the audited reason for a job that is BOTH expired AND carrying
        # a corrupted `storage_ref` changes from `job_expired` to
        # `storage_ref_mismatch` -- execution reaches the `storage_ref` check
        # first once the resolver stops refusing the job outright. The audit
        # reason is permanent, hash-chained, observable behaviour, so that
        # is a real difference, not a redundant one.
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason="job_expired"
        )
        return None

    try:
        signed = object_store.presign_backup_read(
            org_id, resolved.repository_id, list(msg.suffixes), expires_in=expires_in
        )
    except ValueError:
        # `presign_backup_read` raises `ValueError` from three places, and this
        # branch audits all three as `unsafe_suffix`. Only one is reachable
        # here today, and this comment names it rather than the other two.
        #
        # REACHABLE: `_safe_object_suffix` rejects a suffix that got past
        # `BackupReadQueryMsg`'s own `ObjectSuffix` validation -- a drifted
        # validator, or a manifest from a compromised endpoint.
        #
        # NOT reachable today: the empty-batch guard, because
        # `BackupReadQueryMsg.suffixes` is `Field(min_length=1, ...)` and an
        # empty batch cannot arrive through `parse_agent_message` (only
        # through a `model_construct` bypass, which is not agent-supplied wire
        # data). And `_check_ttl`, because `expires_in` is bounded to
        # `1..READ_GRANT_TTL` (900) by the guard above, well under
        # `MAX_PRESIGNED_TTL_S` (43200) -- verified by that arithmetic, not
        # assumed. A future `READ_GRANT_TTL` raised past that ceiling WOULD
        # reach `_check_ttl`, and this reason word would then log it as "a
        # compromised manifest", which is a false accusation in a permanent,
        # hash-chained row: treat that as the signal to split the vocabulary,
        # not as license to trust this comment forever.
        #
        # THE EXCEPTION MESSAGE IS NOT RECORDED: it echoes the offending
        # value, and this row is permanent.
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason="unsafe_suffix"
        )
        return None

    return BackupReadGrantMsg(
        v=1,
        kind="backup_read_grant",
        job_id=str(resolved.job.id),
        query_id=msg.query_id,
        grants=[
            BackupReadGrantItem(suffix=str(g["suffix"]), url=str(g["url"])) for g in signed
        ],
        expires_at=(now + timedelta(seconds=expires_in))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    )


async def handle_read_query(
    app_engine: AsyncEngine,
    redis: "aioredis.Redis",
    msg: BackupReadQueryMsg,
    *,
    device_id: UUID,
    org_id: UUID,
    object_store: ObjectStore | None = None,
) -> BackupReadGrantMsg | None:
    """Answer one query and send the reply back down the device's channel.

    THE REPLY IS PUBLISHED ONLY AFTER THE TENANT TRANSACTION COMMITS, matching
    `handle_chunk_query`. Nothing here writes application rows, but the audit
    record for a refusal does, and a reply that raced its own audit row would
    put a grant on the wire that the log did not yet know about.

    It travels on `dispatch:{device_id}` -- the channel that already carries
    signed job envelopes to whichever gateway replica holds this device's
    connection. It is NOT a signed job and does not pretend to be one: it
    commands nothing, and invariant #1 governs what the agent EXECUTES.

    Returns the reply for a caller that wants to assert on it; `None` means
    refused, audited, and nothing sent.
    """
    import json

    # Imported HERE, not at module scope, for two reasons. `dispatch_channel`
    # would be a cycle (`dispatch.py` routes into this module). And
    # `gateway_object_store` must be looked up on the module at CALL time: a
    # module-scope `from ... import gateway_object_store` binds the function
    # object, so a test that patches `dedup.gateway_object_store` would patch
    # something this module no longer consults -- and would then build a real
    # `ObjectStore`, which reaches the network.
    from app.gateway.dispatch import dispatch_channel
    from app.modules.backup import dedup

    store = object_store or dedup.gateway_object_store()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        reply = await answer_read_query(
            session, store, msg, org_id=org_id, device_id=device_id
        )
    if reply is None:
        return None
    await redis.publish(
        dispatch_channel(device_id), json.dumps(reply.model_dump(mode="json"))
    )
    return reply

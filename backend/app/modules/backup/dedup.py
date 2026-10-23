"""The server-side dedup exchange (`docs/modules/backup/spec.md` §5.2).

# What this replaces, and why it had to move

`agent/internal/capabilities/backup` asked the object store itself whether a
chunk already existed (`ObjectStore.Has`), once per chunk. That needed a
credential on the endpoint, which invariant #3 forbids and §6.1 explains at
length, so `backup.run` refused in production and every byte above the
transport went unused.

**The check never needed endpoint storage access.** The server already reasons
about chunk references — `gc.py` reads manifests to decide what nothing points
at, `verify_point` re-reads and BLAKE3-checks them. What it lacked was a cheap
way to answer for a six-figure hash list, and `backup_chunks` (migration 0053)
is that. So the agent sends hashes, the server answers which are NEW, and the
same reply carries the write grant for exactly the run those new keys belong
to. That collapses the worst round-trip case from one per chunk to one per
`MAX_CHUNK_BATCH`, and it leaves nothing on the endpoint that outlives the job.

# The authorization, and why it takes NOTHING from the message

`answer_chunk_query` is handed `org_id` and `device_id` resolved from the mTLS
CERTIFICATE by the gateway, never from message content. The message names one
thing: a `job_id`. Everything else — the org, the device, the repository, the
storage prefix, the chunker bounds — is read off the job THIS server signed and
the rows it wrote when it dispatched it.

That is `app.core.storage`'s "the key is derived, never accepted" rule applied
to the tenancy question. An exchange that accepted a `repository_id` would be
answering "does repository R hold chunk X" for whatever R an endpoint named,
which in a deduplicating store is a cross-tenant existence oracle: a device
that can enumerate content hashes could ask another org's repository whether it
holds a known file. Here it cannot phrase the question.

Every query carries an explicit `org_id` predicate as well (invariant #5).
`org_session` sets RLS, which would ALSO refuse a foreign row — and that is
exactly why the predicate is written out and why `test_backup_dedup.py` runs
these functions on the ADMIN engine: RLS masks a missing application-level
predicate, so a test that only went through `org_session` would pass with the
predicate deleted.

# Silence is the only failure mode

There is no error reply. Unknown job, another device's job, a job that is not a
`backup.run`, an expired job, a finished run, a repository that is not active —
all produce `None`, an audit record, and nothing on the wire. Distinguishable
refusals would be an oracle a compromised endpoint could walk to learn which
job ids exist.

# Audit volume

Per-grant auditing is deliberately NOT done, following
`_handle_health_report`'s reasoning in `app/gateway/dispatch.py`: a terabyte
protected is ~1000 queries, and a hash-chained log whose value is that a human
can read it must not be swamped by them. The authorization the grant rests on
is already audited once, at `backup.dispatched`, and it names the same
repository this grant is scoped to. What IS audited is every REFUSAL, with a
reason code — those are rare, and each one is either a bug or an endpoint
asking a question it has no business asking.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.storage import MAX_PRESIGNED_TTL_S, ObjectStore
from app.core.tenancy import org_session
from app.gateway.protocol import (
    BackupChunkGrantMsg,
    BackupChunkQueryMsg,
    BackupUploadGrant,
)
from app.models import BackupChunk, BackupRepository, BackupRun, Job
from app.models.core import JobStatus
from app.modules.backup.schedule import grant_window_end, run_start_deadline

logger = logging.getLogger("its.backup.dedup")

# The job states a running backup may still be uploading under. A job the
# server considers finished must not be able to mint a fresh write grant: the
# run is over, and a grant issued after it is a standing credential attached to
# nothing.
_LIVE_JOB_STATUSES = frozenset(
    {JobStatus.queued, JobStatus.dispatched, JobStatus.running}
)

# Per-object ceiling written into the POST policy's `content-length-range`.
#
# A chunk is at most `dedup_params.max` plaintext bytes, plus AES-256-GCM's
# 12-byte nonce and 16-byte tag. The slack above that is deliberate but small:
# the point of the condition is that a compromised endpoint holding this grant
# cannot fill the tenant's storage with one object, and a ceiling of "whatever
# the repository row says, times some comfort factor" would give that up. The
# MANIFEST is written under the same grant and is also bounded by it — a
# manifest for a very large tree can be big, so the floor below is what keeps a
# repository whose `max` is tiny from being unable to write its own manifest.
_GRANT_SLACK_BYTES = 4096
_GRANT_FLOOR_BYTES = 64 << 20  # 64 MiB: enough for a large sealed manifest


@dataclass(frozen=True)
class _Resolved:
    """Everything the answer needs, all of it read off server-side rows."""

    job: Job
    repository_id: UUID
    storage_ref: str
    max_object_bytes: int
    grant_until: datetime


def _max_object_bytes(dedup_params: dict[str, Any]) -> int:
    raw = dedup_params.get("max")
    chunk_max = raw if isinstance(raw, int) and raw > 0 else 0
    return max(chunk_max + _GRANT_SLACK_BYTES, _GRANT_FLOOR_BYTES)


async def _audit_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    reason: str,
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="backup.dedup_refused",
        target_type="device",
        target_id=str(device_id),
        # `job_id` is agent-supplied and may be anything the wire model allowed;
        # it is bounded here so a hostile value cannot bloat an audit row.
        metadata={"reason": reason, "job_id": job_id[:64]},
    )


async def _resolve(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    job_id: str,
    now: datetime,
) -> tuple[_Resolved | None, str]:
    """(what the job authorizes, or None and a reason code).

    EVERY lookup carries `org_id` explicitly (invariant #5) and the device
    binding is checked against the certificate-resolved `device_id`, not
    against anything in the message.
    """
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None, "malformed_job_id"

    job = (
        await session.execute(
            select(Job).where(Job.org_id == org_id, Job.id == parsed)
        )
    ).scalar_one_or_none()
    if job is None:
        return None, "no_such_job"
    if job.type != "backup.run":
        # A grant scoped to a repository must come from the job type that
        # writes to one. Any other job type reaching here is either a bug or an
        # endpoint trying to borrow authorization from a job it does hold.
        return None, "wrong_job_type"
    if job.target_type != "device" or job.target_id != device_id:
        return None, "wrong_device"
    if job.status not in _LIVE_JOB_STATUSES:
        return None, "job_not_live"
    if job.expires_at <= now:
        # The grant's lifetime is derived from this; an expired job would
        # otherwise mint a zero- or negative-lifetime URL, or (worse, if the
        # clamp were the other way) one that outlives its job.
        return None, "job_expired"
    grant_until, lifetime_reason = grant_window_end(
        start_deadline=run_start_deadline(job.params or {}),
        started_at=job.started_at,
        expires_at=job.expires_at,
        now=now,
    )
    if grant_until is None:
        return None, lifetime_reason

    run = (
        await session.execute(
            select(BackupRun).where(BackupRun.org_id == org_id, BackupRun.job_id == job.id)
        )
    ).scalar_one_or_none()
    if run is None:
        return None, "no_such_run"
    if run.state != "running":
        return None, "run_not_running"

    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id,
                BackupRepository.id == run.repository_id,
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        return None, "no_such_repository"
    if repo.state != "active":
        return None, "repository_not_active"

    return (
        _Resolved(
            job=job,
            repository_id=repo.id,
            storage_ref=repo.storage_ref,
            max_object_bytes=_max_object_bytes(repo.dedup_params),
            grant_until=grant_until,
        ),
        "",
    )


async def _record_stored(
    session: AsyncSession, *, org_id: UUID, repository_id: UUID, chunk_ids: list[str]
) -> int:
    """Index the chunks the agent reports it has written. Returns how many rows
    were new.

    `org_id` and `repository_id` are the SERVER's, resolved from the job. The
    agent supplies content ids and nothing else, so no report can place a row
    in another tenant's repository however the message is shaped.

    `ON CONFLICT DO NOTHING` on `(repository_id, chunk_id)`: two devices backing
    the same file into the same repository at the same time both report the
    same id, and that is dedup working, not a conflict to raise on.
    """
    unique = list(dict.fromkeys(chunk_ids))
    if not unique:
        return 0
    stmt = (
        pg_insert(BackupChunk)
        .values(
            [
                {
                    "id": uuid7(),
                    "org_id": org_id,
                    "repository_id": repository_id,
                    "chunk_id": cid,
                }
                for cid in unique
            ]
        )
        .on_conflict_do_nothing(constraint="uq_backup_chunks_repo_chunk")
        .returning(BackupChunk.chunk_id)
    )
    inserted = (await session.execute(stmt)).scalars().all()
    return len(inserted)


async def _partition(
    session: AsyncSession, *, org_id: UUID, repository_id: UUID, chunk_ids: list[str]
) -> list[str]:
    """The subset of `chunk_ids` this repository does NOT already hold, in the
    order asked and without repeats."""
    unique = list(dict.fromkeys(chunk_ids))
    if not unique:
        return []
    held = set(
        (
            await session.execute(
                select(BackupChunk.chunk_id).where(
                    BackupChunk.org_id == org_id,
                    BackupChunk.repository_id == repository_id,
                    BackupChunk.chunk_id.in_(unique),
                )
            )
        )
        .scalars()
        .all()
    )
    return [cid for cid in unique if cid not in held]


async def answer_chunk_query(
    session: AsyncSession,
    object_store: ObjectStore,
    msg: BackupChunkQueryMsg,
    *,
    org_id: UUID,
    device_id: UUID,
    now: datetime | None = None,
) -> BackupChunkGrantMsg | None:
    """Answer one dedup query, or refuse it silently.

    `org_id`/`device_id` come from the gateway's mTLS binding. Returns the
    reply the caller should send to this device, or `None` — in which case an
    audit record has been written and nothing must be sent.
    """
    now = now or datetime.now(UTC)

    resolved, reason = await _resolve(
        session, org_id=org_id, device_id=device_id, job_id=msg.job_id, now=now
    )
    if resolved is None:
        await _audit_refusal(
            session, org_id=org_id, device_id=device_id, job_id=msg.job_id, reason=reason
        )
        return None

    # THE PREFIX IS DERIVED AND THE STORED VALUE IS ONLY CHECKED AGAINST IT —
    # `presign_package_artifact`'s reasoning exactly. A `storage_ref` column
    # somehow made to name another tenant's prefix yields no grant at all
    # rather than a signed one over whatever it names.
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

    recorded = await _record_stored(
        session,
        org_id=org_id,
        repository_id=resolved.repository_id,
        chunk_ids=msg.stored,
    )
    new = await _partition(
        session,
        org_id=org_id,
        repository_id=resolved.repository_id,
        chunk_ids=msg.check,
    )

    # The grant dies with the run that authorized it, and never later. A URL
    # that outlived its run would be a standing write grant over a tenant's
    # backup prefix, attached to work that is already over.
    expires_in = min(
        max(int((resolved.grant_until - now).total_seconds()), 1), MAX_PRESIGNED_TTL_S
    )
    post = object_store.presign_backup_write(
        org_id,
        resolved.repository_id,
        expires_in=expires_in,
        max_bytes=resolved.max_object_bytes,
    )

    return BackupChunkGrantMsg(
        v=1,
        kind="backup_chunk_grant",
        job_id=str(resolved.job.id),
        query_id=msg.query_id,
        new=new,
        recorded=recorded,
        upload=BackupUploadGrant(
            url=str(post["url"]),
            fields={str(k): str(v) for k, v in dict(post["fields"]).items()},
            key_prefix=f"{derived_prefix}/",
            max_bytes=resolved.max_object_bytes,
        ),
        expires_at=(now + timedelta(seconds=expires_in))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    )


async def forget_chunks(
    session: AsyncSession, *, org_id: UUID, repository_id: UUID, chunk_ids: list[str]
) -> int:
    """Drop index rows for chunks that no longer exist in the repository.

    GC deletes chunk OBJECTS; the index must lose the same ids in the same
    breath or it keeps answering "you already have this" for a chunk that was
    swept, and the next run's manifest references an object nobody will ever
    write. Returns the number of rows removed.
    """
    if not chunk_ids:
        return 0
    rows = (
        (
            await session.execute(
                select(BackupChunk).where(
                    BackupChunk.org_id == org_id,
                    BackupChunk.repository_id == repository_id,
                    BackupChunk.chunk_id.in_(list(dict.fromkeys(chunk_ids))),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


# --------------------------------------------------------------------------- #
# The gateway hookup
# --------------------------------------------------------------------------- #

_gateway_store: ObjectStore | None = None


def gateway_object_store() -> ObjectStore:
    """The gateway process's object store, built on FIRST USE.

    Construction reaches the object store to ensure the bucket exists, so it
    stays out of process startup for the same reason
    `app.modules.deploy.api._object_store` does: a gateway that cannot reach
    MinIO must still admit agents and carry every other message kind.

    "First use" is no longer only the first dedup query: `app.modules.backup.
    handler.handle_backup_result` also calls this (as a lazily-invoked
    factory, `ingest_backup_result`'s `object_store_factory`) to resolve a
    successful `backup.run` report's `manifest_ref`. A deployment with no
    object store loses backup dedup AND the ability to reconcile a
    *successful* backup run to a `BackupPoint` -- a `state="failed"` report
    is unaffected, since `ingest_backup_result` never calls this factory on
    that path. It is still lost at first use rather than at boot.
    """
    global _gateway_store
    if _gateway_store is None:
        from app.core.config import get_settings

        _gateway_store = ObjectStore(get_settings())
    return _gateway_store


async def handle_chunk_query(
    app_engine: AsyncEngine,
    redis: "aioredis.Redis",
    msg: BackupChunkQueryMsg,
    *,
    device_id: UUID,
    org_id: UUID,
    object_store: ObjectStore | None = None,
) -> BackupChunkGrantMsg | None:
    """Answer one query and send the reply back down the device's channel.

    THE REPLY IS PUBLISHED ONLY AFTER THE TENANT TRANSACTION COMMITS, for the
    same reason `dispatch_backup` defers `publish_job`: the answer says "these
    chunks are already stored, do not upload them", and an agent that acted on
    that before the index rows were durable would write a manifest referencing
    chunks the database later forgot.

    It travels on `dispatch:{device_id}` — the channel that already carries
    signed job envelopes to whichever gateway replica holds this device's
    connection. `DispatchSubscriber` forwards it verbatim and
    `GatewayServer._on_delivered` no-ops on a frame with no `job` member, so
    no new fan-out path exists. It is NOT a signed job and does not pretend to
    be one: it commands nothing, and invariant #1 governs what the agent
    EXECUTES.

    Returns the reply for a caller that wants to assert on it; `None` means
    refused, audited, and nothing sent.
    """
    import json

    from app.gateway.dispatch import dispatch_channel

    store = object_store or gateway_object_store()
    async with org_session(org_id, engine=app_engine) as session:
        reply = await answer_chunk_query(
            session, store, msg, org_id=org_id, device_id=device_id
        )
    if reply is None:
        return None
    await redis.publish(
        dispatch_channel(device_id), json.dumps(reply.model_dump(mode="json"))
    )
    return reply


async def reconcile_chunk_index(
    session: AsyncSession, *, org_id: UUID, repository_id: UUID, present: set[str]
) -> int:
    """Make the index agree with `present`, the chunk ids that actually survive
    in the repository's storage. Returns the number of rows dropped.

    Called by GC, and it is what closes the one soft spot in this design. A row
    is created on the agent's WORD that it uploaded a chunk (see migration
    0053's third note): the server does not re-HEAD the object, because that
    would restore the per-chunk round trip the exchange exists to remove. So a
    buggy or compromised agent can leave the index claiming a chunk that was
    never written, and the next run would skip uploading it and produce a
    manifest referencing nothing.

    GC already enumerates every stored chunk object to do its sweep, so it can
    repair that for free: an id the index holds and the bucket does not is
    dropped, and the next run re-uploads it. The direction is fail-safe — the
    worst outcome of dropping a row is one redundant upload.
    """
    held = set(
        (
            await session.execute(
                select(BackupChunk.chunk_id).where(
                    BackupChunk.org_id == org_id,
                    BackupChunk.repository_id == repository_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return await forget_chunks(
        session, org_id=org_id, repository_id=repository_id, chunk_ids=sorted(held - present)
    )

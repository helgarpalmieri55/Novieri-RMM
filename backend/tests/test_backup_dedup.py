"""The server-side dedup exchange (`docs/modules/backup/spec.md` §5.2).

**EVERY TENANCY TEST HERE RUNS ON THE ADMIN ENGINE.** `answer_chunk_query` is
called in production inside `org_session`, where RLS would refuse a foreign row
even if the application-level `org_id` predicate were deleted — so a test that
went through `org_session` would pass with the predicate gone and prove
nothing. On the admin engine the predicate is the ONLY thing standing there,
and deleting it produces a named wrong value in each of the tests below.

**THE PARTITION FIXTURE VARIES.** A dedup test in which every chunk is new, or
every chunk is already held, passes against an implementation that returns the
whole list or the empty list. Every partition assertion here mixes held and new
ids and asserts exactly WHICH came back, in order.
"""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import MAX_PRESIGNED_TTL_S, ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupChunkQueryMsg, parse_agent_message
from app.models import AuditEvent, BackupRun, Job
from app.models.core import JobStatus
from app.modules.backup import dedup
from app.modules.backup.dedup import answer_chunk_query, forget_chunks
from tests.backup_dedup_fixtures import (
    SEED_NOW,
    all_index_rows,
    chunk_id,
    index_rows,
    seed_dedup_org,
    seed_index_rows,
)
from tests.client_scope_support import drop_client_keys

_NOW = SEED_NOW


@pytest.fixture()
def object_store(tmp_path: Path):
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


@pytest.fixture()
def vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


def _query(job_id: UUID | str, *, check: list[str] | None = None,
           stored: list[str] | None = None, query_id: str = "q1") -> BackupChunkQueryMsg:
    return BackupChunkQueryMsg(
        v=1,
        kind="backup_chunk_query",
        job_id=str(job_id),
        query_id=query_id,
        check=check or [],
        stored=stored or [],
    )


async def _refusal_reasons(admin_engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "backup.dedup_refused",
                    )
                )
            )
            .scalars()
            .all()
        )
    return [str(r.meta.get("reason")) for r in rows]


# --------------------------------------------------------------------------- #
# The partition — the whole point of the exchange
# --------------------------------------------------------------------------- #


async def test_it_answers_exactly_the_chunks_the_repository_does_not_hold(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Held and new are BOTH present in the fixture and the assertion names the
    ids: an implementation returning `check` unchanged, or `[]`, is red here.
    Order is the order asked, and a repeated id appears once."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddp-a")
    held = [chunk_id(1), chunk_id(3)]
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, held)

    asked = [chunk_id(3), chunk_id(2), chunk_id(1), chunk_id(4), chunk_id(2)]
    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session,
            object_store,
            _query(a.job_id, check=asked),
            org_id=a.org_id,
            device_id=a.device_id,
            now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert reply.new == [chunk_id(2), chunk_id(4)]
    assert (reply.job_id, reply.query_id, reply.recorded) == (str(a.job_id), "q1", 0)


async def test_reported_chunks_are_indexed_and_then_dedupe_on_the_next_query(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The two halves of the exchange, in sequence: what the agent says it
    stored stops being `new`, and what it never mentioned stays `new`."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddp-b")

    async with AsyncSession(admin_engine) as session:
        first = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(10), chunk_id(11)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()
    assert first is not None and first.new == [chunk_id(10), chunk_id(11)]

    async with AsyncSession(admin_engine) as session:
        second = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(10), chunk_id(11)], stored=[chunk_id(10)],
                   query_id="q2"),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert second is not None
    # `recorded` counts the newly indexed ids, and `new` is computed AFTER the
    # report — so 10 is gone from `new` in the same reply that recorded it.
    assert (second.recorded, second.new) == (1, [chunk_id(11)])
    assert await index_rows(admin_engine, a.repository_id) == [(a.org_id, chunk_id(10))]


async def test_a_repeated_report_is_idempotent_and_counts_zero(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Two devices backing the same file up into one repository both report the
    same id. That is dedup working, not a conflict."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddp-c")
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, [chunk_id(20)])

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, stored=[chunk_id(20), chunk_id(20)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None and reply.recorded == 0
    assert await index_rows(admin_engine, a.repository_id) == [(a.org_id, chunk_id(20))]


# --------------------------------------------------------------------------- #
# Tenancy (invariant #5) — every one of these is pinned by mutation
# --------------------------------------------------------------------------- #


async def test_another_orgs_job_id_yields_no_answer(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `Job.org_id == org_id` from `_resolve`'s job lookup.
    Observed: `['wrong_device'] == ['no_such_job']`.

    Org B's device names org A's `backup.run` job id, and the assertion is on
    WHICH layer refuses. The org predicate is the OUTERMOST of three — the
    device binding and the derived-prefix check stand behind it — so deleting
    it does not leak a grant here; it moves the refusal one layer in, and the
    reason code says so. That is the honest claim, and it is the reason the
    assertion pins the reason code rather than only `None`: a test that
    asserted `reply is None` alone would pass with this predicate deleted.

    Runs on the ADMIN engine on purpose: inside `org_session` RLS refuses the
    foreign `jobs` row on its own and the mutation would be invisible even to
    the reason code.
    """
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-a")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-b")
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, [chunk_id(30)])

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(30), chunk_id(31)]),
            org_id=b.org_id, device_id=b.device_id, now=_NOW,
        )
        await session.commit()

    prefix = None if reply is None else reply.upload.key_prefix
    assert (reply, prefix) == (None, None)
    assert await _refusal_reasons(admin_engine, b.org_id) == ["no_such_job"]


async def test_another_devices_job_in_the_same_org_yields_no_answer(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `job.target_id != device_id` from `_resolve`.

    Same org, so RLS is no help at all here and the check is the only guard
    even in production. Under the mutation device 2 receives a grant and the
    dedup answer for device 1's run — this asserts `(None, None)` and the
    mutation reports `(<grant>, ['...1f'])`.
    """
    a, devices = await seed_dedup_org(
        admin_engine, app_engine, vault, "ddt-c", device_count=2
    )
    other_device = devices[1]
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, [chunk_id(0x1E)])

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x1E), chunk_id(0x1F)]),
            org_id=a.org_id, device_id=other_device, now=_NOW,
        )
        await session.commit()

    new = None if reply is None else reply.new
    assert (reply, new) == (None, None)
    assert await _refusal_reasons(admin_engine, a.org_id) == ["wrong_device"]


async def test_a_chunk_row_carrying_another_orgs_id_does_not_dedupe(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `BackupChunk.org_id == org_id` from `_partition`.

    `backup_chunks.org_id` has no FK tying it to the repository's org, so a
    row can exist that names repository R and some OTHER org. The predicate is
    what makes such a row invisible to R's owner — and the direction matters:
    ignoring it means the chunk is reported NEW and gets re-uploaded, which is
    safe, while honouring it means the agent skips a chunk on the strength of a
    row that does not belong to it and the manifest ends up referencing an
    object nobody wrote.

    Guarded, `new == ['...28']`. Under the mutation, `new == []` — the chunk
    silently vanishes from the upload set.
    """
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-d")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-e")
    # A mis-scoped row: org B's id, org A's repository.
    await seed_index_rows(admin_engine, b.org_id, a.repository_id, [chunk_id(0x28)])

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x28)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert reply.new == [chunk_id(0x28)]


async def test_a_report_is_indexed_under_the_servers_org_not_the_callers_claim(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The message carries no org and no repository, so there is nothing to
    forge; this pins that the row lands under the pair the SERVER resolved,
    read back on the admin engine where a mis-scoped row would be visible."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-f")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-g")

    async with AsyncSession(admin_engine) as session:
        await answer_chunk_query(
            session, object_store,
            _query(a.job_id, stored=[chunk_id(0x33)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    rows = [r for r in await all_index_rows(admin_engine) if r[2] == chunk_id(0x33)]
    assert rows == [(a.org_id, a.repository_id, chunk_id(0x33))]
    assert await index_rows(admin_engine, b.repository_id) == []


async def test_a_storage_ref_pointing_outside_the_derived_prefix_yields_no_grant(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete the `resolved.storage_ref != derived_prefix` check.

    The stored column is repointed at org B's prefix. Guarded this is `None`;
    under the mutation the reply still carries org A's DERIVED prefix (the
    grant is built from ids, not from the column) — so the mutation's damage
    here is that a corrupted column no longer fails closed. This asserts the
    refusal AND the reason, so deleting the check reports
    `(<grant>, [])` instead of `(None, ['storage_ref_mismatch'])`.
    """
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-h")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-i")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE backup_repositories SET storage_ref = :ref WHERE id = :id"),
            {"ref": b.storage_ref, "id": a.repository_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x40)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["storage_ref_mismatch"],
    )


# --------------------------------------------------------------------------- #
# Every other way an answer is refused — all silent, all audited
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("job_type", "wrong_job_type"),
        ("job_status", "job_not_live"),
        ("job_expiry", "job_expired"),
        ("run_state", "run_not_running"),
        ("repo_state", "repository_not_active"),
        ("no_run", "no_such_run"),
    ],
)
async def test_a_job_that_does_not_authorize_a_write_gets_silence(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    object_store,
    vault,
    mutate: str,
    reason: str,
) -> None:
    """Each of these would otherwise mint a write grant over a tenant's backup
    prefix on the strength of a job that is not, or is no longer, a live
    backup run."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, f"ddr-{mutate[:4]}")
    statements = {
        "job_type": ("UPDATE jobs SET type = 'script.run' WHERE id = :job", {}),
        "job_status": ("UPDATE jobs SET status = 'succeeded' WHERE id = :job", {}),
        "job_expiry": (
            "UPDATE jobs SET expires_at = :past WHERE id = :job",
            {"past": _NOW - timedelta(minutes=1)},
        ),
        "run_state": ("UPDATE backup_runs SET state = 'success' WHERE job_id = :job", {}),
        "repo_state": (
            "UPDATE backup_repositories SET state = 'archived' WHERE id = :repo",
            {},
        ),
        "no_run": ("DELETE FROM backup_runs WHERE job_id = :job", {}),
    }
    sql, extra = statements[mutate]
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(sql), {"job": a.job_id, "repo": a.repository_id, **extra}
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x50)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (None, [reason])


async def test_a_malformed_job_id_is_refused_without_a_database_lookup(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddr-m")
    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query("not-a-uuid", check=[chunk_id(0x51)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()
    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["malformed_job_id"],
    )


# --------------------------------------------------------------------------- #
# The grant itself
# --------------------------------------------------------------------------- #


async def test_the_grant_is_scoped_to_the_prefix_and_dies_with_its_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The signed POST policy is what S3 enforces, so the assertions are on the
    signed DOCUMENT: the `starts-with $key` condition names this repository's
    prefix and nothing wider, the `content-length-range` is bounded, and
    `X-Amz-Expires` (readable back out of the URL) is the job's remaining
    lifetime rather than whatever was asked for.

    What this CANNOT prove is that MinIO/S3 enforces the policy — `moto`
    accepts an upload that violates it (spec §6.1's measured note). The signed
    document is the part this codebase can get wrong.
    """
    import base64
    import json as _json

    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddg-a")
    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.id == a.job_id))
        ).scalar_one()
        expires_at = job.expires_at

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x60)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert reply.upload.key_prefix == f"{a.org_id}/backup/{a.repository_id}/"

    policy = _json.loads(base64.b64decode(reply.upload.fields["policy"]))
    conditions = [c for c in policy["conditions"] if isinstance(c, list)]
    assert ["starts-with", "$key", reply.upload.key_prefix] in conditions
    assert ["content-length-range", 0, reply.upload.max_bytes] in conditions

    # `dedup.answer_chunk_query` clamps the grant to `MAX_PRESIGNED_TTL_S`
    # (12h): the seeded job's TTL is 24h (`backup_dedup_fixtures.py`) so that a
    # slow/parallel suite run keeps a comfortable margin against `job_expired`,
    # which puts the job's raw remaining lifetime OVER the cap this grant is
    # actually bounded by. Mirror that clamp here rather than assert the raw
    # `expires_at - _NOW`, which the grant no longer equals.
    remaining = min(int((expires_at - _NOW).total_seconds()), MAX_PRESIGNED_TTL_S)
    assert f"X-Amz-Expires={remaining}" in reply.upload.url or reply.upload.fields.get(
        "x-amz-expires"
    ) in (str(remaining), None)
    assert reply.expires_at == (
        (_NOW + timedelta(seconds=remaining)).replace(microsecond=0).isoformat()
    ).replace("+00:00", "Z")


async def test_the_grant_carries_no_bucket_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The endpoint must never hold a credential (invariant #3). A POST policy
    carries the ACCESS KEY ID — which names the credential — and a SIGNATURE
    over one bounded document; it must never carry the secret."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddg-b")
    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store, _query(a.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    blob = reply.upload.url + " " + " ".join(
        f"{k}={v}" for k, v in reply.upload.fields.items()
    )
    assert "test" not in blob.split("credential=")[0].replace("it-suite-test", "")


# --------------------------------------------------------------------------- #
# The batch bound is at the parse layer, not in the service
# --------------------------------------------------------------------------- #


async def test_an_oversized_batch_never_reaches_the_service() -> None:
    """`MAX_CHUNK_BATCH` is refused where every other wire bound is refused —
    at `parse_agent_message`, before business logic. 1024 is accepted so the
    bound is the stated one and not "anything smaller"."""
    import json as _json

    from app.gateway.protocol import MAX_CHUNK_BATCH

    def frame(n: int) -> str:
        return _json.dumps(
            {
                "v": 1,
                "kind": "backup_chunk_query",
                "job_id": str(uuid4()),
                "query_id": "q",
                "check": [chunk_id(i) for i in range(n)],
            }
        )

    parsed = parse_agent_message(frame(MAX_CHUNK_BATCH))
    assert len(parsed.check) == MAX_CHUNK_BATCH
    with pytest.raises(ValidationError):
        parse_agent_message(frame(MAX_CHUNK_BATCH + 1))


# --------------------------------------------------------------------------- #
# forget_chunks — the GC side of the index
# --------------------------------------------------------------------------- #


async def test_forget_chunks_removes_only_the_named_ids_in_the_named_repository(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault
) -> None:
    """Held and untouched ids both present: a `forget` that deleted everything,
    or nothing, passes a single-id fixture."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddf-a")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddf-b")
    await seed_index_rows(
        admin_engine, a.org_id, a.repository_id, [chunk_id(70), chunk_id(71)]
    )
    await seed_index_rows(admin_engine, b.org_id, b.repository_id, [chunk_id(70)])

    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        removed = await forget_chunks(
            session, org_id=a.org_id, repository_id=a.repository_id,
            chunk_ids=[chunk_id(70), chunk_id(99)],
        )

    assert removed == 1
    assert await index_rows(admin_engine, a.repository_id) == [(a.org_id, chunk_id(71))]
    assert await index_rows(admin_engine, b.repository_id) == [(b.org_id, chunk_id(70))]


async def test_a_run_row_belonging_to_another_org_is_not_reachable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `BackupRun.org_id == org_id` from `_resolve`'s run
    lookup.

    Observed: `['no_such_repository'] == ['no_such_run']`.

    Constructed the only way it can be: a `backup_runs` row carrying org B's
    id against org A's job. Guarded, the run does not resolve. Under the
    mutation it DOES resolve, and the refusal falls through to the repository
    lookup one layer in — which is exactly what a layered defence should do,
    and exactly why the assertion is on the reason code. Without it this test
    would pass with the predicate gone.
    """
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-j")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-k")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM backup_runs WHERE job_id = :job"), {"job": a.job_id}
        )
        session.add(
            BackupRun(
                org_id=b.org_id,
                device_id=b.device_id,
                repository_id=b.repository_id,
                job_id=a.job_id,
                state="running",
                actor_type="user",
                started_at=_NOW,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x80)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    prefix = None if reply is None else reply.upload.key_prefix
    assert (reply, prefix) == (None, None)
    assert await _refusal_reasons(admin_engine, a.org_id) == ["no_such_run"]


async def test_a_repository_row_belonging_to_another_org_is_not_reachable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `BackupRepository.org_id == org_id` from `_resolve`.

    Observed: `['storage_ref_mismatch'] == ['no_such_repository']`.

    Org A's run is repointed at org B's repository — the shape a mis-wired
    dispatch or a tampered row would produce. Guarded, the repository does not
    resolve. Under the mutation it resolves, the derived prefix becomes
    `'<org A>/backup/<org B repo>/'` — a prefix belonging to neither tenant's
    real layout — and the derived-vs-stored check catches it one layer in.
    Two layers, and the reason code is what distinguishes them.

    Since migration 0242 the database refuses that row too: a run names a
    repository of its own client (`fk_backup_runs_repository_client`), and B's
    is B's. So it is planted with that key dropped, and the query answered and
    its refusal read, inside one transaction that is rolled back (plan
    2026-10-26, lesson 2): the org predicate is still what is under test.
    """
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-l")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddt-m")

    async with AsyncSession(admin_engine) as session:
        await drop_client_keys(session, "backup_runs", "fk_backup_runs_repository_client")
        await session.execute(
            text("UPDATE backup_runs SET repository_id = :repo WHERE job_id = :job"),
            {"repo": b.repository_id, "job": a.job_id},
        )
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x81)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        reasons = [
            str(meta.get("reason"))
            for meta in (
                await session.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == a.org_id,
                        AuditEvent.action == "backup.dedup_refused",
                    )
                )
            ).scalars()
        ]
        await session.rollback()

    assert (reply, reasons) == (None, ["no_such_repository"])


async def test_a_queued_job_may_already_query(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The live-status set is three states wide on purpose. An agent that
    reconnects, drains a queued envelope and starts uploading before its
    `job_status` message has been processed must not be refused."""
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddq-a")
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == a.job_id))).scalar_one()
        job.status = JobStatus.queued
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_chunk_query(
            session, object_store,
            _query(a.job_id, check=[chunk_id(0x90)]),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None and reply.new == [chunk_id(0x90)]


# --------------------------------------------------------------------------- #
# The gateway hookup
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))


async def test_the_reply_goes_down_the_devices_own_channel(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault, monkeypatch
) -> None:
    """Routed through `handle_agent_message`, exactly as the gateway does it.

    The reply travels on `dispatch:{device_id}` — the channel that already
    carries signed job envelopes to whichever replica holds the connection —
    so the assertion is on the channel NAME as well as the payload. Publishing
    to any other device's channel would hand one tenant's write grant to
    another endpoint.
    """
    import json as _json

    from app.gateway.dispatch import handle_agent_message

    monkeypatch.setattr(dedup, "gateway_object_store", lambda: object_store)
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddw-a")
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, [chunk_id(0xA0)])
    redis = _FakeRedis()

    await handle_agent_message(
        app_engine,
        None,  # type: ignore[arg-type]  # a chunk query never reaches the job engine
        redis,  # type: ignore[arg-type]
        _query(a.job_id, check=[chunk_id(0xA0), chunk_id(0xA1)]),
        device_id=a.device_id,
        org_id=a.org_id,
    )

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    frame = _json.loads(payload)
    assert channel == f"dispatch:{a.device_id}"
    assert (frame["v"], frame["kind"], frame["new"]) == (
        1,
        "backup_chunk_grant",
        [chunk_id(0xA1)],
    )
    # `DispatchSubscriber` forwards this verbatim and `_on_delivered` reads
    # `envelope["job"]["id"]`; a reply carrying a `job` member would flip a job
    # to dispatched on every dedup round trip.
    assert "job" not in frame


async def test_a_refused_query_publishes_nothing_at_all(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault, monkeypatch
) -> None:
    """Silence is the contract, and "silence" means no frame — not an empty
    one, and not one on another channel."""
    from app.gateway.dispatch import handle_agent_message

    monkeypatch.setattr(dedup, "gateway_object_store", lambda: object_store)
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddw-b")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddw-c")
    redis = _FakeRedis()

    await handle_agent_message(
        app_engine,
        None,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        _query(a.job_id, check=[chunk_id(0xA2)]),
        device_id=b.device_id,
        org_id=b.org_id,
    )

    assert redis.published == []
    assert await _refusal_reasons(admin_engine, b.org_id) == ["no_such_job"]


# --------------------------------------------------------------------------- #
# GC keeps the index honest (the loop dedup's trust decision leaves open)
# --------------------------------------------------------------------------- #


async def test_gc_drops_index_rows_for_chunks_it_swept_and_for_chunks_never_written(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Three ids, three fates, one fixture — because a reconcile that dropped
    everything, or nothing, passes a one-id test.

    * `live`   — object present AND referenced by a live point: row survives.
    * `swept`  — object present, referenced by nothing: object deleted, row goes.
    * `phantom`— NO object at all, row only: this is the agent's word being
      repaired, and it is the case the whole reconcile exists for.
    """
    from blake3 import blake3

    from app.models import BackupPoint
    from app.modules.backup.gc import gc_repository, seal_chunk
    from app.modules.backup.repository import open_repo_key
    from app.modules.backup.restore import seal_manifest
    from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "ddg-c")
    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo_key = await open_repo_key(
            session, vault, org_id=a.org_id, repository_id=a.repository_id
        )

    live_plain, swept_plain = b"live-chunk", b"swept-chunk"
    live = blake3(live_plain).hexdigest()
    swept = blake3(swept_plain).hexdigest()
    phantom = chunk_id(0xDEAD)
    for cid, plain in ((live, live_plain), (swept, swept_plain)):
        await object_store.put_backup_chunk(
            a.org_id, a.repository_id, cid, seal_chunk(repo_key, plain)
        )
    await seed_index_rows(admin_engine, a.org_id, a.repository_id, [live, swept, phantom])

    manifest_ref = await object_store.put_backup_object(
        a.org_id,
        a.repository_id,
        f"manifests/{uuid4().hex}",
        seal_manifest(
            repo_key,
            BackupManifest(
                schema_version=1,
                streams=[
                    FiletreeStream(
                        type="filetree",
                        entries=[
                            ManifestEntry(
                                path="/data/a", kind="file", mode=0o644, size=10,
                                mtime=1.0, chunk_ids=[live],
                            )
                        ],
                    )
                ],
            ),
        ),
    )
    async with AsyncSession(admin_engine) as session:
        session.add(
            BackupPoint(
                org_id=a.org_id, device_id=a.device_id, repository_id=a.repository_id,
                backup_run_id=a.run_id, taken_at=_NOW, manifest_ref=manifest_ref,
                size=10, dedup_stats={}, state="complete",
            )
        )
        await session.commit()

    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        deleted = await gc_repository(
            session, object_store, vault,
            org_id=a.org_id, repository_id=a.repository_id, now=_NOW,
        )

    assert deleted == 1
    assert await index_rows(admin_engine, a.repository_id) == [(a.org_id, live)]

"""The server-side READ exchange (`backup_read_query` -> `backup_read_grant`).

The read-side twin of `test_backup_dedup.py`, and it inherits that file's two
standing rules verbatim:

**EVERY TENANCY TEST HERE RUNS ON THE ADMIN ENGINE.** `answer_read_query` is
called in production inside `org_session`, where RLS would refuse a foreign row
even if the application-level `org_id` predicate were deleted -- so a test that
went through `org_session` would pass with the predicate gone and prove
nothing. On the admin engine the predicate is the ONLY thing standing there,
and deleting it produces a named wrong value in each of the tests below.

**EVERY REFUSAL ASSERTS THE REASON CODE, NOT ONLY `None`.** The refusals are
layered: the org predicate stands outside the device binding, which stands
outside the derived-prefix check. Deleting an outer layer usually does not leak
a grant -- it moves the refusal one layer in. A test asserting `reply is None`
alone would pass against the deleted check it claims to guard, so each one pins
which layer refused.

Fixtures are `backup_dedup_fixtures.seed_dedup_org` with the restore rows laid
on top: one construction path for org/user/device/repository, shared with the
dedup suite, and no second way to build one.
"""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupReadQueryMsg, parse_agent_message
from app.models import AuditEvent, BackupPoint, Job, RestoreJob
from app.models.core import JobStatus
from app.modules.backup import dedup
from app.modules.backup.read import READ_GRANT_TTL, answer_read_query
from app.modules.backup.repository import create_repository
from tests.backup_dedup_fixtures import SEED_NOW, DedupOrg, seed_dedup_org
from tests.client_scope_support import drop_client_keys

_NOW = SEED_NOW
_SUFFIXES = ["chunks/ab/abcd", "manifests/m1"]
_ONE = ["chunks/ab/abcd"]


@pytest.fixture()
def object_store(tmp_path: Path):
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test-secret-must-not-leak",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


@pytest.fixture()
def vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


@dataclass(frozen=True)
class RestoreBundle:
    """The `backup.restore` job laid over a `seed_dedup_org` bundle."""

    point_id: UUID
    job_id: UUID
    restore_id: UUID


async def seed_restore(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    tag: str,
    *,
    device_count: int = 1,
) -> tuple[DedupOrg, RestoreBundle, list[UUID]]:
    """`seed_dedup_org` plus a running `backup.restore` job over a backup point.

    Shaped exactly as `dispatch_restore` leaves the database: a
    `type="backup.restore"` job targeting the point's device, a `RestoreJob` in
    state `running` naming that job, and a `BackupPoint` carrying the
    repository. Inserted directly on the admin engine -- nothing here goes
    through `app.modules.backup.read`, so no test is seeded through the code it
    exercises.

    The `backup.run` job `seed_dedup_org` builds is left in place on purpose:
    `test_a_backup_run_job_cannot_borrow_authorization_for_a_read` needs a real,
    live, same-device job of the wrong type to name.
    """
    base, device_ids = await seed_dedup_org(
        admin_engine, app_engine, vault, tag, device_count=device_count
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        point = BackupPoint(
            org_id=base.org_id,
            device_id=base.device_id,
            repository_id=base.repository_id,
            backup_run_id=base.run_id,
            taken_at=_NOW,
            manifest_ref=f"{base.storage_ref}/manifests/m1",
            size=1024,
            dedup_stats={},
            state="complete",
        )
        session.add(point)
        await session.flush()
        job = Job(
            org_id=base.org_id,
            type="backup.restore",
            target_type="device",
            target_id=base.device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=_NOW,
            # 24h for the reason `backup_dedup_fixtures` states at length: a
            # slow or parallel run must not burn through the margin and report
            # `job_expired` for a test about something else.
            expires_at=_NOW + timedelta(hours=24),
            status=JobStatus.running,
        )
        session.add(job)
        await session.flush()
        restore = RestoreJob(
            org_id=base.org_id,
            device_id=base.device_id,
            backup_point_id=point.id,
            job_id=job.id,
            selection={"paths": ["/data"]},
            target={"kind": "in_place"},
            mode="restore",
            state="running",
            actor_type="user",
            requested_by=base.user_id,
        )
        session.add(restore)
        await session.commit()
        bundle = RestoreBundle(point_id=point.id, job_id=job.id, restore_id=restore.id)
    return base, bundle, device_ids


async def _second_repository(
    app_engine: AsyncEngine, vault: LocalVaultService, a: DedupOrg
) -> UUID:
    """A second, real repository in org `a` -- built the one way repositories
    are built, so its `storage_ref` is genuine rather than hand-written."""
    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as inner:
        repo = await create_repository(
            inner, vault, org_id=a.org_id, actor_id=a.user_id,
            client_id=a.org_id, name=f"second-{uuid4().hex[:8]}"
        )
        await inner.flush()
        return repo.id


def _query(
    job_id: UUID | str, suffixes: list[str] | None = None, *, query_id: str = "q1"
) -> BackupReadQueryMsg:
    return BackupReadQueryMsg(
        v=1,
        kind="backup_read_query",
        job_id=str(job_id),
        query_id=query_id,
        suffixes=list(suffixes if suffixes is not None else _ONE),
    )


async def _refusal_reasons(admin_engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "backup.read_refused",
                    )
                )
            )
            .scalars()
            .all()
        )
    return [str(r.meta.get("reason")) for r in rows]


async def _audit_rows(admin_engine: AsyncEngine, org_id: UUID) -> list[tuple[str, str]]:
    """(action, rendered metadata) for every audit row in this org, on the
    admin engine -- so a row written under the wrong org would still be seen."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
    return [(str(r.action), repr(r.meta)) for r in rows]


def _key_of(url: str) -> str:
    """The object key a signed GET addresses, path-decoded."""
    return unquote(urlparse(url).path)


def _expires_in(url: str) -> str:
    return parse_qs(urlparse(url).query)["X-Amz-Expires"][0]


# --------------------------------------------------------------------------- #
# The grant itself
# --------------------------------------------------------------------------- #


async def test_a_live_restore_job_gets_one_grant_per_suffix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Two DIFFERENT suffixes, and the assertion names both in the order asked:
    an implementation that signed only the first, returned them sorted, or
    returned `[]`, is red here. The keys are read back out of the signed URLs,
    so the `suffix` echo cannot pass on its own.

    FALSIFIER: narrow `list(msg.suffixes)` to `msg.suffixes[:1]` in the
    `presign_backup_read` call.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-a")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id, _SUFFIXES),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert (reply.v, reply.kind) == (1, "backup_read_grant")
    assert (reply.job_id, reply.query_id) == (str(r.job_id), "q1")
    assert [g.suffix for g in reply.grants] == _SUFFIXES
    prefix = object_store.backup_prefix(a.org_id, a.repository_id)
    assert [_key_of(g.url).split(f"{prefix}/")[-1] for g in reply.grants] == _SUFFIXES


async def test_the_repository_comes_from_the_job_never_the_payload(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """`BackupReadQueryMsg` carries no repository id at all, so this asserts the
    property that model enforces: the signed keys land under the repository the
    JOB resolves to, reached through `RestoreJob.backup_point_id ->
    BackupPoint.repository_id`.

    FALSIFIER (first half): sign against any repository other than
    `resolved.repository_id`.
    FALSIFIER (second half): resolve the repository from anything but the
    restore's own backup point -- the device's latest run, say. The second
    repository is in the SAME org, so the org id is identical on both sides and
    only the point->repository traversal can tell them apart.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-b")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-c")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    mine = object_store.backup_prefix(a.org_id, a.repository_id)
    theirs = object_store.backup_prefix(b.org_id, b.repository_id)
    assert f"{mine}/" in _key_of(reply.grants[0].url)
    assert theirs not in _key_of(reply.grants[0].url)

    second_repo = await _second_repository(app_engine, vault, a)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        point = BackupPoint(
            org_id=a.org_id, device_id=a.device_id, repository_id=second_repo,
            backup_run_id=a.run_id, taken_at=_NOW, manifest_ref="m", size=1,
            dedup_stats={}, state="complete",
        )
        session.add(point)
        await session.flush()
        await session.execute(
            text("UPDATE restore_jobs SET backup_point_id = :p WHERE id = :id"),
            {"p": point.id, "id": r.restore_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        moved = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert moved is not None
    assert f"{object_store.backup_prefix(a.org_id, second_repo)}/" in _key_of(
        moved.grants[0].url
    )
    assert f"{mine}/" not in _key_of(moved.grants[0].url)


async def test_the_grant_never_outlives_the_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The job's remaining life is a CEILING on the signing window.

    30 seconds is far below `READ_GRANT_TTL`, so `min(...)` must pick the job.
    The assertion is on `X-Amz-Expires`, the lifetime SIGNED INTO the URL, not
    merely the one this module reports in `expires_at`.

    FALSIFIER: replace the `min(...)` with `READ_GRANT_TTL` -- the URL then
    carries 900 and outlives the job that authorized it by 870 seconds.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-d")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE jobs SET expires_at = :exp WHERE id = :job"),
            {"exp": _NOW + timedelta(seconds=30), "job": r.job_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert _expires_in(reply.grants[0].url) == "30"
    assert reply.expires_at == (
        (_NOW + timedelta(seconds=30)).replace(microsecond=0).isoformat()
    ).replace("+00:00", "Z")


async def test_a_generous_job_lifetime_does_not_widen_the_signing_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The other side of the clamp, and the reason `READ_GRANT_TTL` exists at
    all: the seeded job lives 24 hours and the grant must still be 900 seconds.

    FALSIFIER (measured): replace the `min(...)` with the job's raw remaining
    lifetime. Observed `assert None is not None` -- 86400s exceeds
    `MAX_PRESIGNED_TTL_S`, so `presign_backup_read` raises and the batch is
    refused outright. A job TTL between 900s and 12h would instead mint a URL
    wider than the window; the assertion below catches both, because it names
    `READ_GRANT_TTL` rather than "not too big".
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-e")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    assert READ_GRANT_TTL == 900
    assert _expires_in(reply.grants[0].url) == str(READ_GRANT_TTL)


async def test_the_grant_carries_no_bucket_credential(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """The endpoint must never hold a credential (invariant #3). A presigned GET
    names the ACCESS KEY ID and carries a signature over one bounded request; it
    must never carry the secret.

    The `object_store` fixture's secret (`test-secret-must-not-leak`) is
    deliberately distinct from its access key id (`test`): a shared literal for
    both cannot fail, because it cannot tell "the secret leaked" apart from
    "the key id is present, as SigV4 requires". With a distinct secret, this
    assertion is falsifiable.

    FALSIFIER: sign with a form that embeds the secret (or log it into the URL).
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdq-f")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id, _SUFFIXES),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    for grant in reply.grants:
        parts = urlparse(grant.url)
        assert "X-Amz-Signature" in parse_qs(parts.query)
        assert "test-secret-must-not-leak" not in grant.url


# --------------------------------------------------------------------------- #
# Tenancy (invariant #5) -- every one of these is pinned by mutation
# --------------------------------------------------------------------------- #


async def test_another_orgs_restore_job_id_yields_no_answer(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `Job.org_id == org_id` from `_resolve_restore`'s job
    lookup. Observed: `['wrong_device'] == ['no_such_job']`.

    Org B's device names org A's `backup.restore` job id. The org predicate is
    the OUTERMOST of three, so deleting it does not leak a grant here -- it
    moves the refusal one layer in, and the reason code says so. Runs on the
    ADMIN engine: inside `org_session` RLS refuses the foreign `jobs` row on its
    own and the mutation would be invisible even to the reason code.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-a")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-b")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=b.org_id, device_id=b.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, b.org_id)) == (None, ["no_such_job"])


async def test_a_job_belonging_to_another_device_is_refused_in_silence(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `job.target_id != device_id` from `_resolve_restore`.

    Same org, so RLS is no help at all and this check is the only guard even in
    production. Under the mutation device 2 receives signed GETs over device 1's
    restore; guarded, the answer is silence.
    """
    a, r, devices = await seed_restore(
        admin_engine, app_engine, vault, "rdt-c", device_count=2
    )
    other_device = devices[1]

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=other_device, now=_NOW,
        )
        await session.commit()

    grants = None if reply is None else [g.url for g in reply.grants]
    assert (reply, grants) == (None, None), (
        "an authorization refusal must be silence, never a distinguishable reply"
    )
    assert await _refusal_reasons(admin_engine, a.org_id) == ["wrong_device"]


async def test_a_restore_row_belonging_to_another_org_is_not_reachable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `RestoreJob.org_id == org_id` from the restore lookup.
    Observed: `['no_such_point'] == ['no_such_restore']`.

    Constructed the only way it can be: a `restore_jobs` row carrying org B's id
    against org A's job. Guarded, the restore does not resolve; under the
    mutation it DOES, and the refusal falls through to the point lookup one
    layer in -- which is why the assertion is on the reason code.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-d")
    b, rb, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-e")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM restore_jobs WHERE job_id = :job"), {"job": r.job_id}
        )
        await session.execute(
            text("UPDATE restore_jobs SET job_id = :job WHERE id = :id"),
            {"job": r.job_id, "id": rb.restore_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["no_such_restore"],
    )


async def test_a_backup_point_belonging_to_another_org_is_not_reachable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `BackupPoint.org_id == org_id` from the point lookup.
    Observed: `['no_such_repository'] == ['no_such_point']`.

    Org A's restore is repointed at org B's backup point -- the shape a
    mis-wired dispatch or a tampered row would produce. Guarded, the point does
    not resolve. Under the mutation it does, and the repository lookup one layer
    in refuses instead. Two layers, and the reason code distinguishes them.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-f")
    _, rb, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-g")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE restore_jobs SET backup_point_id = :p WHERE id = :id"),
            {"p": rb.point_id, "id": r.restore_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["no_such_point"],
    )


async def test_a_repository_row_belonging_to_another_org_is_not_reachable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete `BackupRepository.org_id == org_id` from the repository
    lookup. Observed: `['storage_ref_mismatch'] == ['no_such_repository']`.

    Org A's point is repointed at org B's repository. Guarded, the repository
    does not resolve. Under the mutation it does, the derived prefix becomes
    `<org A>/backup/<org B repo>` -- a prefix belonging to neither tenant's real
    layout -- and the derived-vs-stored check catches it one layer in.

    Since migration 0242 the database refuses that row too: a point names a
    repository of its own client (`fk_backup_points_repository_client`), and
    B's is B's. So it is planted with that key dropped, and the query answered
    and its refusal read, inside one transaction that is rolled back (plan
    2026-10-26, lesson 2): the org predicate is still what is under test.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-h")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-i")

    async with AsyncSession(admin_engine) as session:
        await drop_client_keys(session, "backup_points", "fk_backup_points_repository_client")
        await session.execute(
            text("UPDATE backup_points SET repository_id = :repo WHERE id = :id"),
            {"repo": b.repository_id, "id": r.point_id},
        )
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        reasons = [
            str(meta.get("reason"))
            for meta in (
                await session.execute(
                    select(AuditEvent.meta).where(
                        AuditEvent.org_id == a.org_id,
                        AuditEvent.action == "backup.read_refused",
                    )
                )
            ).scalars()
        ]
        await session.rollback()

    assert (reply, reasons) == (None, ["no_such_repository"])


async def test_a_storage_ref_that_does_not_match_the_derived_prefix_yields_no_grant(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """MUTATION: delete the `resolved.storage_ref != derived_prefix` check.

    `answer_chunk_query`'s rule applied to the read side. The stored column is
    repointed at org B's prefix; guarded this is `None`, and under the mutation
    the reply still carries org A's DERIVED prefix (the grant is built from ids,
    not from the column) -- so the mutation's damage is that a corrupted column
    no longer fails closed. Asserting the reason as well as the refusal is what
    makes deleting the check report `(<grant>, [])`.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-j")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdt-k")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE backup_repositories SET storage_ref = :ref WHERE id = :id"),
            {"ref": b.storage_ref, "id": a.repository_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["storage_ref_mismatch"],
    )


# --------------------------------------------------------------------------- #
# Every other way an answer is refused -- all silent, all audited
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("job_type", "wrong_job_type"),
        ("job_status", "job_not_live"),
        ("job_expiry", "job_expired"),
        ("restore_state", "restore_not_running"),
        ("repo_state", "repository_not_active"),
        ("no_restore", "no_such_restore"),
    ],
)
async def test_a_job_that_does_not_authorize_a_read_gets_silence(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    object_store,
    vault,
    mutate: str,
    reason: str,
) -> None:
    """Each of these would otherwise mint signed GETs over a tenant's backup
    prefix on the strength of a job that is not, or is no longer, a live
    restore.

    `restore_state` is the one the design calls out by name: a finished restore
    must not still mint grants -- the window closes when the WORK does, not when
    the job's `expires_at` happens to pass. `job_type` here flips the job to
    `backup.run`, the type that authorizes WRITES to this same repository.

    MEASURED, AND ONE CASE IS HONESTLY WEAKER THAN THE REST. Deleting
    `job.expires_at <= now` from `_resolve_restore` leaves `job_expiry` GREEN:
    a job that expired a minute ago clamps to a negative `expires_in`, and
    `answer_read_query`'s own `expires_in < 1` guard audits the same
    `job_expired` and returns the same silence. The two layers are genuinely
    interchangeable here, so no assertion on observable behaviour can tell them
    apart -- which is the point of having both, and is stated rather than
    papered over. Every other case in this table dies to the deletion of its
    own check (`restore_state`, `repo_state`, `job_status` and `job_type` each
    hand back a signed grant; `no_restore` blows up).
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, f"rdr-{mutate[:4]}")
    statements = {
        "job_type": ("UPDATE jobs SET type = 'backup.run' WHERE id = :job", {}),
        "job_status": ("UPDATE jobs SET status = 'succeeded' WHERE id = :job", {}),
        "job_expiry": (
            "UPDATE jobs SET expires_at = :past WHERE id = :job",
            {"past": _NOW - timedelta(minutes=1)},
        ),
        "restore_state": (
            "UPDATE restore_jobs SET state = 'success' WHERE job_id = :job",
            {},
        ),
        "repo_state": (
            "UPDATE backup_repositories SET state = 'archived' WHERE id = :repo",
            {},
        ),
        "no_restore": ("DELETE FROM restore_jobs WHERE job_id = :job", {}),
    }
    sql, extra = statements[mutate]
    async with AsyncSession(admin_engine) as session:
        await session.execute(text(sql), {"job": r.job_id, "repo": a.repository_id, **extra})
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (None, [reason])


async def test_a_sub_second_remainder_is_refused_rather_than_signed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """A job with well under a second of life left is NOT expired --
    `_resolve_restore`'s `job.expires_at <= now` check passes it -- but
    `int((resolved.job.expires_at - now).total_seconds())` in
    `answer_read_query` truncates that remainder to 0, and the `expires_in < 1`
    guard there refuses it before `presign_backup_read` is ever called.

    FALSIFIER: delete the `expires_in < 1` guard in `answer_read_query`. The
    call then reaches `presign_backup_read` with `expires_in=0`, which raises
    `ValueError` from `_check_ttl` -- caught by the `except ValueError` branch,
    so the audited reason would read `unsafe_suffix` instead of `job_expired`.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdr-sub")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE jobs SET expires_at = :exp WHERE id = :job"),
            {"exp": _NOW + timedelta(milliseconds=500), "job": r.job_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["job_expired"],
    )


async def test_the_resolvers_expiry_guard_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Pins the claim `answer_read_query`'s `expires_in < 1` guard now makes:
    the resolver's own `job.expires_at <= now` check is NOT redundant with it.

    A job that IS expired AND whose repository `storage_ref` no longer matches
    the derived prefix: guarded, `_resolve_restore` refuses first on the
    expiry and the audit reason is `job_expired`.

    FALSIFIER: delete `if job.expires_at <= now: return None, "job_expired"`
    from `_resolve_restore`. Execution then falls through the resolver to the
    `resolved.storage_ref != derived_prefix` check in `answer_read_query`,
    changing the permanent, hash-chained audit reason to
    `storage_ref_mismatch` -- a different, observable outcome, not the "same
    silence" a truly redundant guard would produce.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdr-lb")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdr-lb2")

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE jobs SET expires_at = :past WHERE id = :job"),
            {"past": _NOW - timedelta(minutes=1), "job": r.job_id},
        )
        await session.execute(
            text("UPDATE backup_repositories SET storage_ref = :ref WHERE id = :id"),
            {"ref": b.storage_ref, "id": a.repository_id},
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["job_expired"],
    )


async def test_a_backup_run_job_cannot_borrow_authorization_for_a_read(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """A read grant must come from the job type that READS a repository.

    The `backup.run` job named here is real, live, and targets the same device
    -- it authorizes WRITES to this very repository, and its `BackupRun` row is
    in state `running`. `_resolve_restore` must still refuse it: everything
    except the job TYPE lines up.

    FALSIFIER: delete the `job.type != "backup.restore"` check. The `RestoreJob`
    lookup then refuses instead (`no_such_restore`), which is why this asserts
    the reason code and not only `None`.
    """
    a, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdr-run")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(a.job_id),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["wrong_job_type"],
    )


async def test_a_malformed_job_id_is_refused_without_a_database_lookup(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    a, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdr-m")
    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query("not-a-uuid"),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()
    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (
        None,
        ["malformed_job_id"],
    )


# --------------------------------------------------------------------------- #
# What the permanent, hash-chained audit row is allowed to say
# --------------------------------------------------------------------------- #


async def test_an_unsafe_suffix_refuses_the_batch_without_echoing_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """A suffix that got past the wire model (a drifted validator, a manifest
    from a compromised endpoint) refuses the WHOLE batch, and the audit row
    names a vocabulary word rather than the suffix.

    Constructed with `model_construct` because `BackupReadQueryMsg` refuses this
    value -- which is the point: the layer under test is the one that has to
    hold when the layer above it has drifted.

    FALSIFIER: interpolate the `ValueError` into `reason` (e.g.
    `reason=f"unsafe_suffix: {exc}"`). `_safe_object_suffix`'s message wording
    then lands in a permanent, hash-chained row, and a variant of it that
    echoed the value would put the suffix there too.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rda-a")
    hostile = "../../secret-org/backup/plunder"
    msg = BackupReadQueryMsg.model_construct(
        v=1,
        kind="backup_read_query",
        job_id=str(r.job_id),
        query_id="q1",
        suffixes=["chunks/ab/abcd", hostile],
    )

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, msg, org_id=a.org_id, device_id=a.device_id, now=_NOW
        )
        await session.commit()

    assert (reply, await _refusal_reasons(admin_engine, a.org_id)) == (None, ["unsafe_suffix"])
    rows = await _audit_rows(admin_engine, a.org_id)
    blob = " ".join(f"{action} {meta}" for action, meta in rows)
    assert hostile not in blob
    assert "secret-org" not in blob
    assert "object suffix" not in blob  # the ValueError's own wording


async def test_a_hostile_job_id_cannot_bloat_the_audit_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """`job_id` is agent-supplied and unbounded by the wire model, and it lands
    in a permanent row. It is truncated to 64 characters.

    FALSIFIER: drop the `[:64]` slice in `_audit_refusal`.
    """
    a, _, _ = await seed_restore(admin_engine, app_engine, vault, "rda-b")
    async with AsyncSession(admin_engine) as session:
        await answer_read_query(
            session, object_store, _query("z" * 4096),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == a.org_id,
                    AuditEvent.action == "backup.read_refused",
                )
            )
        ).scalar_one()
    assert row.meta["job_id"] == "z" * 64


async def test_a_granted_read_writes_no_audit_row_and_no_url_anywhere(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault
) -> None:
    """Per-grant auditing is deliberately not done (`dedup.py`'s reasoning: one
    restore is many batches, and a hash-chained log whose value is that a human
    can read it must not be swamped), and a signed URL is a bearer credential
    that must never be written to one.

    FALSIFIER: add a success-path `audit_service.record` -- with or without the
    grants, either lands a `backup.read_*` action in the log.
    """
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rda-c")

    async with AsyncSession(admin_engine) as session:
        reply = await answer_read_query(
            session, object_store, _query(r.job_id, _SUFFIXES),
            org_id=a.org_id, device_id=a.device_id, now=_NOW,
        )
        await session.commit()

    assert reply is not None
    rows = await _audit_rows(admin_engine, a.org_id)
    assert [action for action, _ in rows if action.startswith("backup.read")] == []
    assert [meta for _, meta in rows if "X-Amz-Signature" in meta] == []


# --------------------------------------------------------------------------- #
# The batch bound is at the parse layer, not in the service
# --------------------------------------------------------------------------- #


async def test_an_oversized_batch_never_reaches_the_service() -> None:
    """`READ_BATCH_MAX` is refused where every other wire bound is refused -- at
    `parse_agent_message`, before business logic. 256 is accepted so the bound
    is the stated one and not "anything smaller"."""
    import json as _json

    from app.gateway.protocol import READ_BATCH_MAX

    def frame(n: int) -> str:
        return _json.dumps(
            {
                "v": 1,
                "kind": "backup_read_query",
                "job_id": str(uuid4()),
                "query_id": "q",
                "suffixes": [f"chunks/ab/{i:04x}" for i in range(n)],
            }
        )

    parsed = parse_agent_message(frame(READ_BATCH_MAX))
    assert isinstance(parsed, BackupReadQueryMsg)
    assert len(parsed.suffixes) == READ_BATCH_MAX
    with pytest.raises(ValidationError):
        parse_agent_message(frame(READ_BATCH_MAX + 1))


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

    FALSIFIER: delete the `BackupReadQueryMsg` branch in `dispatch.py` -- the
    message falls through to `_load_job_for_device` and nothing is published.
    Publishing to any other device's channel would hand one tenant's read grants
    to another endpoint, so the channel NAME is asserted too.

    This test reads the REAL clock (`handle_agent_message` offers no `now`
    seam), which is why the seeded job's TTL is 24 hours.
    """
    import json as _json

    from app.gateway.dispatch import handle_agent_message

    monkeypatch.setattr(dedup, "gateway_object_store", lambda: object_store)
    a, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdw-a")
    redis = _FakeRedis()

    await handle_agent_message(
        app_engine,
        None,  # type: ignore[arg-type]  # a read query never reaches the job engine
        redis,  # type: ignore[arg-type]
        _query(r.job_id, _SUFFIXES),
        device_id=a.device_id,
        org_id=a.org_id,
    )

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    frame = _json.loads(payload)
    assert channel == f"dispatch:{a.device_id}"
    assert (frame["v"], frame["kind"]) == (1, "backup_read_grant")
    assert [g["suffix"] for g in frame["grants"]] == _SUFFIXES
    # `DispatchSubscriber` forwards this verbatim and `_on_delivered` reads
    # `envelope["job"]["id"]`; a reply carrying a `job` member would flip a job
    # to dispatched on every read round trip.
    assert "job" not in frame


async def test_a_refused_query_publishes_nothing_at_all(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, vault, monkeypatch
) -> None:
    """Silence is the contract, and "silence" means no frame -- not an empty
    one, and not one on another channel."""
    from app.gateway.dispatch import handle_agent_message

    monkeypatch.setattr(dedup, "gateway_object_store", lambda: object_store)
    _, r, _ = await seed_restore(admin_engine, app_engine, vault, "rdw-b")
    b, _, _ = await seed_restore(admin_engine, app_engine, vault, "rdw-c")
    redis = _FakeRedis()

    await handle_agent_message(
        app_engine,
        None,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        _query(r.job_id),
        device_id=b.device_id,
        org_id=b.org_id,
    )

    assert redis.published == []
    assert await _refusal_reasons(admin_engine, b.org_id) == ["no_such_job"]

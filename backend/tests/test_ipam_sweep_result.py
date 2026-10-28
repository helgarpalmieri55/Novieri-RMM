"""`net.sweep` results: the whole-result rejection and the untrusted-input
controls (`docs/modules/ipam/spec.md` §5.4, §6.4, §0.6, §0.10, §11.3).

**THE AGENT'S REPORT IS UNTRUSTED INPUT.** Everything here is written on the
assumption that the device on the other end may be buggy or controlled by
somebody else, and that the interesting failure is not "the sweep returned
nothing" but "the sweep returned something it was never authorized to see".

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Every rejection fixture is a result that is otherwise perfectly valid.**
  One field is wrong; the rest would succeed. So each test observes the
  difference between `succeeded` with N observations and `failed` with ZERO --
  never the difference between a valid document and a broken one.
* **The out-of-scope result carries THREE good hosts and one bad one.** A
  handler that filtered the bad row would leave three observations and a
  succeeded job; rejecting whole leaves none. Those are different observable
  values, which is the point: filtering optimises for keeping data, rejecting
  optimises for detecting the compromise.
* **The scope check is exercised against the SIGNED `params.targets`, with a
  live authorization that covers the offending address.** Recomputing from the
  current database would let a scope granted after dispatch retroactively bless
  the result; with the authorization present, a handler that recomputed observes
  `succeeded`.
* **The identity tests supply a real, resolvable foreign `device_id` and a real
  foreign `org_id` in the payload.** A payload carrying a nonexistent UUID would
  be rejected by a foreign key even under the bug, so the test would pass while
  proving nothing.
* **`max_targets` is lowered in the params rather than the host list inflated**,
  so the over-cap fixture is four hosts and a cap of three -- small enough to
  read and still a real breach of the signed ceiling.
* **The address-budget fixtures send MORE HOST ENTRIES THAN THE SIGNED SCOPE HAS
  ADDRESSES**, which no other fixture in this file does. Everywhere else
  `len(hosts) <= max_targets` and "one record per address" are the same
  assertion, and that is exactly why an entry-count bound could pass every test
  here while letting 500 records land at one address.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    DomainEvent,
    IpamObservation,
    IpamScanAuthorization,
    IpamSweepRun,
    Job,
    JobStatus,
)
from app.modules.ipam.errors import IpamSweepRejected
from app.modules.ipam.handler import handle_sweep_result, register_job_types
from app.modules.ipam.net import scope_digest
from app.modules.ipam.schema import NetSweepDocument, NetSweepParams
from app.modules.ipam.sweep import SWEEP_TTL_S
from tests.ipam_fixtures import seed_device, seed_org, seed_subnet, seed_vrf

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TARGETS = ["10.70.0.0/24"]
DIGEST = scope_digest(TARGETS)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _Run:
    def __init__(
        self, org_id: UUID, vrf_id: UUID, subnet_id: UUID, device_id: UUID,
        run_id: UUID, job_id: UUID, user_id: UUID,
    ) -> None:
        self.org_id = org_id
        self.vrf_id = vrf_id
        self.subnet_id = subnet_id
        self.device_id = device_id
        self.run_id = run_id
        self.job_id = job_id
        self.user_id = user_id


async def _dispatched(
    engine: AsyncEngine,
    tag: str,
    *,
    prefix: str = "10.70.0.0/24",
    targets: list[str] | None = None,
    max_targets: int = 4096,
) -> _Run:
    """A subnet with a dispatched `net.sweep` job and its `ipam_sweep_run`.

    Built directly rather than through `create_sweep` so a result test is never
    set up by the dispatcher whose output it is checking -- and so the signed
    params can be given deliberately wrong values that the dispatcher would never
    produce.
    """
    register_job_types()
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    # At the internal client's Principal site (id = org id): a sweep files
    # what it finds at its subnet's site, and records nothing without one
    # (MSP clients plan 2026-10-26 Task 11, C7).
    subnet_id = await seed_subnet(
        engine, org.org_id, vrf_id=vrf_id, prefix=prefix, site_id=org.org_id
    )
    device_id = org.device_id
    signed = targets if targets is not None else TARGETS
    async with AsyncSession(engine, expire_on_commit=False) as session:
        run = IpamSweepRun(
            org_id=org.org_id,
            subnet_id=subnet_id,
            state="queued",
            trigger="manual",
            target_count=256,
            scope_digest=scope_digest(signed),
            started_at=NOW,
            requested_by=org.user_id,
        )
        session.add(run)
        await session.flush()
        job = Job(
            org_id=org.org_id,
            type="net.sweep",
            target_type="device",
            target_id=device_id,
            params=NetSweepParams(
                subnet_id=str(subnet_id),
                sweep_run_id=str(run.id),
                targets=signed,
                scope_digest=scope_digest(signed),
                method="icmp+arp",
                max_targets=max_targets,
            ).model_dump(),
            nonce=uuid4().hex,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=SWEEP_TTL_S),
            status=JobStatus.dispatched,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        run.state = "dispatched"
        await session.commit()
        return _Run(
            org.org_id, vrf_id, subnet_id, device_id, run.id, job.id, org.user_id
        )


def _document(fx: _Run, **kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "subnet_id": str(fx.subnet_id),
        "sweep_run_id": str(fx.run_id),
        "scope_digest": DIGEST,
        "method": "icmp+arp",
        "started_at": "2026-08-02T12:00:00Z",
        "finished_at": "2026-08-02T12:01:00Z",
        "probed_count": 256,
        "truncated": False,
        "hosts": [
            {
                "address": "10.70.0.10",
                "mac": "a4:bb:6d:11:22:33",
                "responded_via": "arp",
                "hostname": "printer",
            },
            {
                "address": "10.70.0.11",
                "mac": None,
                "responded_via": "icmp",
                "hostname": None,
            },
            {
                "address": "10.70.0.12",
                "mac": "A4-BB-6D-44-55-66",
                "responded_via": "both",
                "hostname": None,
            },
        ],
        "errors": [],
    }
    doc.update(kw)
    return doc


async def _complete(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    fx: _Run,
    doc: dict[str, Any],
    tmp_path: Path,
) -> Job:
    """Run the result through `JobEngine.complete`, the real extension point.

    Calling the handler directly would test the handler; going through
    `complete` also proves the raise lands where §6.4 says it does -- job
    `failed`, and the audit rows written before the raise still committed,
    because `complete` catches rather than rolls back.
    """
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(fx.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (
            await session.execute(select(Job).where(Job.id == fx.job_id))
        ).scalar_one()
        await engine.complete(session, job, state="succeeded", result=doc)
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(select(Job).where(Job.id == fx.job_id))
        ).scalar_one()


async def _observations(engine: AsyncEngine, org_id: UUID) -> list[IpamObservation]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(IpamObservation)
                    .where(IpamObservation.org_id == org_id)
                    .order_by(IpamObservation.address)
                )
            )
            .scalars()
            .all()
        )


async def _observation_count(engine: AsyncEngine, org_id: UUID) -> int:
    """SCOPED, like `_observations` above. This counted every observation IN THE
    DATABASE, on the admin engine, with no `org_id` predicate -- the same shape
    that made `test_ipam_import.py`'s subnet read-back a collection-order flake.
    It happens to be unused today, which is exactly why it was worth fixing
    rather than leaving: the next caller would have inherited an assertion that
    passes when another org's fixture writes a row and can never detect one that
    leaks."""
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(IpamObservation)
                    .where(IpamObservation.org_id == org_id)
                )
            ).scalar_one()
        )


async def _audit(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _events(engine: AsyncEngine, org_id: UUID) -> list[DomainEvent]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent).where(DomainEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


async def _run_row(engine: AsyncEngine, run_id: UUID) -> IpamSweepRun:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(IpamSweepRun).where(IpamSweepRun.id == run_id))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The happy path -- so every rejection below is a DIFFERENCE, not a baseline
# --------------------------------------------------------------------------- #


async def test_a_valid_result_writes_one_observation_per_host(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4's success path, and the baseline every rejection test differs from.

    `arp` and `both` are `medium` confidence (authoritative for address<->MAC
    on-segment, not for identity); `icmp` is `low` (something answered; that is
    all it means). §4.3's `mac_mismatch` reads that column, which is why it is a
    column and not a comment.
    """
    fx = await _dispatched(admin_engine, "ok")
    job = await _complete(admin_engine, app_engine, fx, _document(fx), tmp_path)

    assert job.status is JobStatus.succeeded, job.result
    rows = await _observations(admin_engine, fx.org_id)
    assert [str(r.address) for r in rows] == ["10.70.0.10", "10.70.0.11", "10.70.0.12"]
    assert [r.source for r in rows] == ["sweep"] * 3
    assert [r.method for r in rows] == ["arp", "icmp", "arp"]
    assert [r.confidence for r in rows] == ["medium", "low", "medium"]
    assert [r.mac for r in rows] == [
        "a4:bb:6d:11:22:33",
        None,
        "a4:bb:6d:44:55:66",  # normalised from the Windows dash form
    ]
    assert [r.sweep_run_id for r in rows] == [fx.run_id] * 3
    assert [r.subnet_id for r in rows] == [fx.subnet_id] * 3

    run = await _run_row(admin_engine, fx.run_id)
    assert (run.state, run.hosts_found, run.truncated) == ("succeeded", 3, False)
    assert run.finished_at is not None


async def test_a_successful_sweep_stamps_last_swept_at_and_audits_only_what_it_created(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§11.3: routine successful INGESTION is still not audited -- volume, and
    `jobs.succeeded` already records that the job happened. A REJECTED ingest
    always is, which the tests below assert.

    WHAT CHANGED ON 2026-08-26, and why it does not contradict §11.3. This
    asserted `actions == ["jobs.succeeded"]` when a sweep wrote observations and
    nothing else. On the operator's ruling a sweep now also creates `devices`
    rows, and an asset appearing in the inventory is a privileged action under
    invariant 4 -- every later report, sweep and licence count reads it -- so
    each creation writes one event. The observations themselves are still
    silent: three hosts, three `asset.auto_created` events, not six.

    `last_swept_at` is what makes the subnet stop being due (§5.3): *if it were
    never stamped*, the hourly beat would re-dispatch the same segment forever.
    """
    from app.modules.ipam.models import IpamSubnet

    fx = await _dispatched(admin_engine, "stamp")
    await _complete(admin_engine, app_engine, fx, _document(fx), tmp_path)

    async with AsyncSession(admin_engine) as session:
        subnet = (
            await session.execute(
                select(IpamSubnet).where(IpamSubnet.id == fx.subnet_id)
            )
        ).scalar_one()
    assert subnet.last_swept_at is not None

    actions = [row.action for row in await _audit(admin_engine, fx.org_id)]
    assert "ipam.sweep_rejected" not in actions
    assert "ipam.sweep_out_of_scope" not in actions
    assert actions == ["asset.auto_created"] * 3 + ["jobs.succeeded"]


async def test_a_result_with_no_hosts_succeeds_and_is_evidence(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A segment where nothing answered is a SUCCESSFUL sweep, not a failure --
    and Gate E (§4.3) reads exactly that: the run succeeded, so absence in this
    segment now means something."""
    fx = await _dispatched(admin_engine, "empty")
    job = await _complete(
        admin_engine, app_engine, fx, _document(fx, hosts=[], probed_count=256), tmp_path
    )
    assert job.status is JobStatus.succeeded
    assert await _observations(admin_engine, fx.org_id) == []
    assert (await _run_row(admin_engine, fx.run_id)).state == "succeeded"


# --------------------------------------------------------------------------- #
# The whole-result rejection (§6.4)
# --------------------------------------------------------------------------- #


async def test_one_out_of_scope_address_rejects_the_whole_result(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE SECURITY TEST. Three good hosts and one outside the signed scope.

    *If the handler filtered the bad row instead of rejecting* this observes a
    `succeeded` job and THREE observations -- the agent's good rows keep flowing
    and the tell is hidden. Rejecting whole leaves ZERO, which is the difference
    the assertion below is written to see.

    THE OFFENDING ADDRESS IS COVERED BY A LIVE AUTHORIZATION: the check is
    against what the job was SIGNED for, not against what is authorized now, so
    a handler that recomputed from the database observes `succeeded`.
    """
    fx = await _dispatched(admin_engine, "scope")
    async with AsyncSession(admin_engine) as session:
        session.add(
            IpamScanAuthorization(
                org_id=fx.org_id,
                vrf_id=fx.vrf_id,
                prefix="10.0.0.0/8",  # granted AFTER dispatch: blesses nothing
                authorized_by=fx.user_id,
                authorized_at=NOW,
            )
        )
        await session.commit()

    doc = _document(fx)
    doc["hosts"].append(
        {
            "address": "10.99.99.99",
            "mac": None,
            "responded_via": "icmp",
            "hostname": None,
        }
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []

    actions = [row.action for row in await _audit(admin_engine, fx.org_id)]
    assert "ipam.sweep_rejected" in actions
    assert "ipam.sweep_out_of_scope" in actions

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == fx.org_id,
                    AuditEvent.action == "ipam.sweep_out_of_scope",
                )
            )
        ).scalar_one()
    assert row.actor_type == "agent"
    assert row.target_type == "device"
    assert row.target_id == str(fx.device_id)
    assert row.meta["offending_count"] == 1
    assert row.meta["sample"] == ["10.99.99.99"]

    events = await _events(admin_engine, fx.org_id)
    assert [e.event_type for e in events] == ["ipam.sweep_rejected"]
    assert events[0].subject_type == "ipam_sweep_run"
    assert events[0].subject_id == fx.run_id
    # Phase 2 Task 1: the event names the client of the job that was rejected.
    assert events[0].client_id == job.client_id
    assert events[0].payload["device_id"] == str(fx.device_id)
    assert events[0].payload["offending_count"] == 1


async def test_the_out_of_scope_sample_is_bounded_to_ten_addresses(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 bounds the sample. The payload comes from a device that has just been
    caught reporting outside its authorization, so it is bounded here rather than
    trusted to be small -- and the COUNT is reported in full, so the bound
    truncates the evidence and never the finding."""
    fx = await _dispatched(admin_engine, "sample")
    doc = _document(fx, hosts=[])
    doc["hosts"] = [
        {"address": f"10.99.0.{i}", "mac": None, "responded_via": "icmp",
         "hostname": None}
        for i in range(1, 15)
    ]
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.failed
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == fx.org_id,
                    AuditEvent.action == "ipam.sweep_out_of_scope",
                )
            )
        ).scalar_one()
    assert row.meta["offending_count"] == 14
    assert len(row.meta["sample"]) == 10


async def test_a_mismatched_scope_digest_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The digest binds what the server authorized to what the agent claims it
    probed. Every host below is INSIDE the signed scope, so the only thing wrong
    is the digest.

    *If the digest check were skipped* this observes `succeeded` and three
    observations -- and the one value tying the result to the signed scope would
    be decorative.
    """
    fx = await _dispatched(admin_engine, "digest")
    job = await _complete(
        admin_engine, app_engine, fx, _document(fx, scope_digest="0" * 64), tmp_path
    )
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []
    assert "ipam.sweep_rejected" in [r.action for r in await _audit(admin_engine, fx.org_id)]


async def test_the_handler_recomputes_the_digest_from_the_signed_targets(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """BOTH SIDES CALL `net.scope_digest` (§6.4 step 1-2).

    The job here is signed with `targets` and a `scope_digest` that DISAGREE, and
    the agent's document echoes the params' digest faithfully. A handler that
    only compared `doc.scope_digest == params.scope_digest` observes `succeeded`;
    one that recomputes over `params.targets` -- with the same single
    implementation the dispatcher used -- observes the disagreement.

    That is the whole reason `scope_digest` has exactly one implementation: two
    that drift let both sides agree while checking nothing.
    """
    fx = await _dispatched(admin_engine, "recompute")
    async with AsyncSession(admin_engine) as session:
        job_row = (
            await session.execute(select(Job).where(Job.id == fx.job_id))
        ).scalar_one()
        params = dict(job_row.params)
        params["scope_digest"] = scope_digest(["10.0.0.0/8"])  # not over `targets`
        job_row.params = params
        await session.commit()

    doc = _document(fx, scope_digest=scope_digest(["10.0.0.0/8"]))
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


async def test_the_recompute_check_fires_on_its_own_when_the_document_is_honest(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**STEP 1 IS A SEPARATE CHECK FROM STEP 2, AND NOTHING OBSERVED IT.**

    `test_the_handler_recomputes_the_digest_from_the_signed_targets` above
    tampers `params.scope_digest` AND has the document echo the tampered value,
    so step 2 (`doc.scope_digest != recomputed`) rejects it first: deleting
    step 1 outright left 53 tests passing.

    Here the params are tampered the same way and **the agent's document echoes
    the CORRECT digest** -- the one recomputed over `params.targets`, which is
    what an honest agent that hashes what it was actually told to probe would
    report. Every later check then passes: the document names the right subnet
    and run, the hosts are inside the signed targets, the count is under the
    signed cap. Step 1 is the only thing left standing between this result and
    the database.

    *If step 1 were deleted* this observes `succeeded` and three observations
    from a job whose own signed fields contradict each other -- which is the
    exact condition "the digest has one implementation" exists to detect, since
    a params blob whose digest does not cover its targets is a params blob that
    was edited after signing or built by something that is not this dispatcher.
    """
    fx = await _dispatched(admin_engine, "step1")
    async with AsyncSession(admin_engine) as session:
        job_row = (
            await session.execute(select(Job).where(Job.id == fx.job_id))
        ).scalar_one()
        params = dict(job_row.params)
        params["scope_digest"] = scope_digest(["10.0.0.0/8"])  # not over `targets`
        job_row.params = params
        await session.commit()

    # The document is HONEST: `DIGEST` is `scope_digest(TARGETS)`, exactly what
    # the handler recomputes. Only the signed params disagree with themselves.
    job = await _complete(admin_engine, app_engine, fx, _document(fx), tmp_path)

    assert job.status is JobStatus.failed, job.result
    assert await _observations(admin_engine, fx.org_id) == []
    assert (await _run_row(admin_engine, fx.run_id)).state == "failed"
    assert "ipam.sweep_rejected" in [r.action for r in await _audit(admin_engine, fx.org_id)]


async def test_a_run_whose_subnet_disagrees_with_its_jobs_params_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**§6.4's THIRD IDENTITY CHECK, which no fixture ever contradicted.**

    Every check above compares the DOCUMENT against the PARAMS. This one
    compares the stored `ipam_sweep_run` against the params -- "the run this job
    says it belongs to is a run for a different subnet" -- and deleting it left
    24 tests passing, because no fixture ever built that disagreement.

    Here run and job are wired the way they always are, and then the run is
    re-pointed at a real second subnet in the same org. The document echoes the
    params faithfully, so every document-vs-params check passes.

    *If the check were deleted* this observes the sweep of `10.70.0.0/24`
    landing on the run that belongs to `10.71.0.0/24`: that run is stamped
    `succeeded` with `hosts_found=3`, so §4.3's Gate E and §0.16's Gate H both
    start reading "somebody successfully swept 10.71.0.0/24" -- evidence for a
    segment nothing ever probed, manufactured by a result about a different one.
    """
    fx = await _dispatched(admin_engine, "runsubnet")
    other_subnet = await seed_subnet(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, prefix="10.71.0.0/24"
    )
    async with AsyncSession(admin_engine) as session:
        run_row = (
            await session.execute(
                select(IpamSweepRun).where(IpamSweepRun.id == fx.run_id)
            )
        ).scalar_one()
        run_row.subnet_id = other_subnet
        await session.commit()

    job = await _complete(admin_engine, app_engine, fx, _document(fx), tmp_path)

    assert job.status is JobStatus.failed, job.result
    assert await _observations(admin_engine, fx.org_id) == []
    run = await _run_row(admin_engine, fx.run_id)
    assert (run.state, run.hosts_found) == ("failed", 0), (
        "a run belonging to another subnet was credited with a successful sweep"
    )
    from app.modules.ipam.models import IpamSubnet

    async with AsyncSession(admin_engine) as session:
        subnet = (
            await session.execute(
                select(IpamSubnet).where(
                    IpamSubnet.org_id == fx.org_id, IpamSubnet.id == other_subnet
                )
            )
        ).scalar_one()
    assert subnet.last_swept_at is None


async def test_a_result_naming_another_subnet_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 2: `subnet_id` must ECHO the params. A real, in-org second
    subnet is used, so the rejection is not a foreign-key accident."""
    fx = await _dispatched(admin_engine, "othersub")
    other = await seed_subnet(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, prefix="10.71.0.0/24"
    )
    job = await _complete(
        admin_engine, app_engine, fx, _document(fx, subnet_id=str(other)), tmp_path
    )
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


async def test_a_result_naming_another_sweep_run_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 2 again, for the run id. The other run is REAL and in this org
    and on this subnet -- so the check is "does it echo the params", not "does
    the id resolve"."""
    fx = await _dispatched(admin_engine, "otherrun")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        other = IpamSweepRun(
            org_id=fx.org_id,
            subnet_id=fx.subnet_id,
            state="dispatched",
            trigger="manual",
            started_at=NOW,
        )
        session.add(other)
        await session.commit()
        other_id = other.id

    job = await _complete(
        admin_engine, app_engine, fx, _document(fx, sweep_run_id=str(other_id)), tmp_path
    )
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []
    assert (await _run_row(admin_engine, other_id)).state == "dispatched"


async def test_more_hosts_than_max_targets_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 3. `max_targets` is inside the SIGNED params, so the ceiling on
    what a sweep can cost is covered by the signature -- and a result that
    exceeds it is a result from an agent that ignored it.

    Four hosts against a signed cap of three: every address is in scope, so the
    cap is the only thing rejecting.
    """
    fx = await _dispatched(admin_engine, "cap", max_targets=3)
    doc = _document(fx)
    doc["hosts"].append(
        {"address": "10.70.0.13", "mac": None, "responded_via": "icmp", "hostname": None}
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


# --------------------------------------------------------------------------- #
# `max_targets` IS AN ADDRESS BUDGET -- the fixtures above cannot see it
#
# Every fixture in this file sends FEWER host entries than the signed scope has
# addresses, so `len(hosts) <= max_targets` and "one record per address" are the
# same assertion in all of them. These two vary exactly that: many entries, few
# addresses.
# --------------------------------------------------------------------------- #


def _repeated(address: str, count: int) -> list[dict[str, Any]]:
    """`count` host records at ONE address, each with its own MAC.

    Otherwise a perfectly valid document: the address is inside the signed
    targets, every MAC parses, `responded_via` is in the enum. The MAC is the
    agent's to choose and it is what `mac_key` (`coalesce(mac::text,'')`) keys
    the observation upsert on, so each record lands as its own row.
    """
    return [
        {
            "address": address,
            "mac": f"a4:bb:6d:{index // 65536 % 256:02x}:{index // 256 % 256:02x}"
                   f":{index % 256:02x}",
            "responded_via": "arp",
            "hostname": None,
        }
        for index in range(count)
    ]


async def test_five_hundred_records_at_one_address_are_rejected_whole(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§0.10, §0.15, §7: `ipam_observation` is a CURRENT-STATE table, and one
    signed sweep must not be able to turn it into a time series.

    The signed scope is a single `/32` -- one address, one legitimate host
    record -- and the document reports that address 500 times with 500
    attacker-chosen MACs. Every record is inside the scope, so §6.4's
    containment check passes on all 500.

    *If the bound were `len(hosts) <= max_targets`* this observes
    `succeeded` with **500** observation rows at **1** distinct address, no
    schema change required, and 4096 rows per sweep per subnet at the default
    cap. Those 500 rows are all current, so `_evaluate`'s `macs` set gives
    `len(macs) > 1` -- a high-severity `conflict`, which §3.2 then turns into a
    block on assigning that address.
    """
    fx = await _dispatched(
        admin_engine, "flood", targets=["10.70.0.10/32"], max_targets=4096
    )
    doc = _document(
        fx,
        scope_digest=scope_digest(["10.70.0.10/32"]),
        hosts=_repeated("10.70.0.10", 500),
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.failed, job.result
    rows = await _observations(admin_engine, fx.org_id)
    assert rows == []
    run = await _run_row(admin_engine, fx.run_id)
    assert run.state == "failed"
    assert "500" in (run.error or ""), run.error
    assert "ipam.sweep_rejected" in [row.action for row in await _audit(admin_engine, fx.org_id)]


async def test_one_address_reported_twice_is_rejected_whole(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The distinctness rule, at its smallest, and NOT reachable by the count
    bound: four records at three addresses, against a `/24` of 256 and a signed
    `max_targets` of 4096. `len(hosts)` is comfortably under every ceiling; the
    only thing wrong is that `10.70.0.10` appears twice, the second time with a
    different MAC -- which is a different `mac_key`, and so a second row.

    Rejected WHOLE, like every other §6.4 failure: filtering the repeat would
    keep the good rows flowing and hide the tell.
    """
    fx = await _dispatched(admin_engine, "dupe")
    doc = _document(fx)
    doc["hosts"].append(
        {
            "address": "10.70.0.10",
            "mac": "a4:bb:6d:99:88:77",
            "responded_via": "arp",
            "hostname": None,
        }
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.failed, job.result
    assert await _observations(admin_engine, fx.org_id) == []
    run = await _run_row(admin_engine, fx.run_id)
    assert "10.70.0.10" in (run.error or ""), run.error


async def test_more_records_than_the_signed_scope_has_addresses_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The budget is `min(max_targets, addresses in the signed targets)`.

    A sweep of a `/30` cannot legitimately report five hosts however generous
    `max_targets` is, and this document's five records are at five DISTINCT
    addresses -- so the distinctness rule does not reject it and the scope size
    is the only thing that can. Two of those addresses are outside the `/30`,
    which is why the assertion is on the run's error rather than only on the
    status: the count bound must fire BEFORE the loop that would report them as
    a scope violation.
    """
    targets = ["10.70.0.0/30"]
    fx = await _dispatched(
        admin_engine, "scopecap", targets=targets, max_targets=4096
    )
    doc = _document(
        fx,
        scope_digest=scope_digest(targets),
        hosts=[
            {
                "address": f"10.70.0.{index}",
                "mac": None,
                "responded_via": "icmp",
                "hostname": None,
            }
            for index in range(5)
        ],
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.failed, job.result
    assert await _observations(admin_engine, fx.org_id) == []
    run = await _run_row(admin_engine, fx.run_id)
    assert "scope 4" in (run.error or ""), run.error


async def test_the_full_signed_scope_reported_once_each_still_succeeds(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The permitted twin, so the three refusals above cannot pass by refusing
    everything: a `/29`'s eight addresses, each reported exactly once, at
    exactly the budget. `hosts_found` is 8 and eight observation rows exist.
    """
    targets = ["10.70.0.0/29"]
    fx = await _dispatched(admin_engine, "atcap", targets=targets, max_targets=4096)
    doc = _document(
        fx,
        scope_digest=scope_digest(targets),
        hosts=[
            {
                "address": f"10.70.0.{index}",
                "mac": None,
                "responded_via": "icmp",
                "hostname": None,
            }
            for index in range(8)
        ],
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.succeeded, job.result
    rows = await _observations(admin_engine, fx.org_id)
    assert len(rows) == 8
    assert len({str(row.address) for row in rows}) == 8
    run = await _run_row(admin_engine, fx.run_id)
    assert (run.state, run.hosts_found) == ("succeeded", 8)


async def test_an_unparseable_address_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 3. Three good hosts and one that is not an address at all."""
    fx = await _dispatched(admin_engine, "badaddr")
    doc = _document(fx)
    doc["hosts"].append(
        {"address": "not-an-address", "mac": None, "responded_via": "icmp",
         "hostname": None}
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


async def test_an_address_of_the_wrong_family_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 3: the family must match the subnet. An IPv6 address in an IPv4
    subnet is not merely out of scope -- it is a result from a capability that
    was never asked to look there (§0.5)."""
    fx = await _dispatched(admin_engine, "family")
    doc = _document(fx)
    doc["hosts"].append(
        {"address": "2001:db8::9", "mac": None, "responded_via": "icmp",
         "hostname": None}
    )
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


async def test_an_unparseable_mac_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 step 3. `macaddr` would raise a DATABASE error on this value, which
    poisons the transaction rather than rejecting the result -- so it is caught
    in Python, before any statement runs."""
    fx = await _dispatched(admin_engine, "badmac")
    doc = _document(fx)
    doc["hosts"][0]["mac"] = "zz:zz:zz:zz:zz:zz"
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []


async def test_a_rejected_result_stamps_the_run_as_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The run row is the subnet page's evidence: a rejected sweep must not read
    as "still dispatched" forever. The reason is stored on the row, bounded to
    the column width."""
    fx = await _dispatched(admin_engine, "runfail")
    doc = _document(fx)
    doc["hosts"].append(
        {"address": "10.99.9.9", "mac": None, "responded_via": "icmp", "hostname": None}
    )
    await _complete(admin_engine, app_engine, fx, doc, tmp_path)
    run = await _run_row(admin_engine, fx.run_id)
    assert run.state == "failed"
    assert run.error
    assert len(run.error) <= 500
    assert run.finished_at is not None


# --------------------------------------------------------------------------- #
# Untrusted input: identity comes from the JOB (§6.4, §0.6)
# --------------------------------------------------------------------------- #


async def test_a_sweep_observation_never_carries_a_device_id(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4: a sweep sees addresses and MACs, not identity. Letting it assert one
    would be a compromised agent's cheapest way to RE-POINT AN ASSIGNMENT.

    The payload names a REAL, in-org device, so a handler that trusted it would
    succeed at writing rather than trip a foreign key. *If it read the payload*
    this observes that device id on every written row.
    """
    fx = await _dispatched(admin_engine, "noident")
    victim = await seed_device(admin_engine, fx.org_id)
    doc = _document(fx, device_id=str(victim))
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.succeeded, job.result
    rows = await _observations(admin_engine, fx.org_id)
    assert len(rows) == 3
    assert [r.device_id for r in rows] == [None, None, None]


async def test_the_org_id_comes_from_the_job_not_from_the_payload(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§11.1: the result documents have no org field to be believed.

    The payload names a REAL second org, so *if the handler read it* the write
    lands in that org -- and RLS then rejects it, which is why this asserts THE
    WRITTEN ROW'S `org_id` rather than the absence of an error. A test that only
    asserted "no exception" passes against a handler that wrote nothing at all.
    """
    fx = await _dispatched(admin_engine, "orgfrom")
    other = await seed_org(admin_engine, "orgfrom-b")
    doc = _document(fx, org_id=str(other.org_id))
    job = await _complete(admin_engine, app_engine, fx, doc, tmp_path)

    assert job.status is JobStatus.succeeded, job.result
    rows = await _observations(admin_engine, fx.org_id)
    assert len(rows) == 3
    assert {r.org_id for r in rows} == {fx.org_id}
    assert await _observations(admin_engine, other.org_id) == []


async def test_an_ingested_sweep_result_cannot_change_a_documented_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§0.6, stated as a test rather than as a modelling preference: the agent's
    report creates an `ipam_observation` row and can NEVER change
    `ipam_address.state`, its claimant, or `assigned_by`.

    The address at `10.70.0.10` is documented as RESERVED to an asset label. A
    successful sweep of it must leave every one of those columns exactly as the
    operator left them -- "the IPAM changed my reservation by itself" is what the
    alternative means.
    """
    from app.modules.ipam.models import IpamAddress

    fx = await _dispatched(admin_engine, "nostate")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = IpamAddress(
            org_id=fx.org_id,
            subnet_id=fx.subnet_id,
            vrf_id=fx.vrf_id,
            address="10.70.0.10",
            state="reserved",
            origin="manual",
            asset_label="core-switch",
            assigned_by=fx.user_id,
        )
        session.add(row)
        await session.commit()
        address_id = row.id

    await _complete(admin_engine, app_engine, fx, _document(fx), tmp_path)

    async with AsyncSession(admin_engine) as session:
        after = (
            await session.execute(
                select(IpamAddress).where(IpamAddress.id == address_id)
            )
        ).scalar_one()
    assert (after.state, after.asset_label, after.device_id) == (
        "reserved",
        "core-switch",
        None,
    )
    assert after.assigned_by == fx.user_id
    assert after.mac is None


# --------------------------------------------------------------------------- #
# The schema is where §0.10 lives (§5.4)
# --------------------------------------------------------------------------- #


def test_a_host_entry_carrying_rtt_ms_is_refused_by_the_schema() -> None:
    """§0.10 point 5 AS A SCHEMA PROPERTY rather than a matter of restraint.

    There is no RTT field, no per-probe timing, no retry count and no
    `reachable: false` entry, because if latency were present someone would
    chart it and the chart would be the monitoring product. *If the host model
    were `extra="allow"`* this observes acceptance -- and the boundary in §7
    would be one field weaker with nothing failing.
    """
    from pydantic import ValidationError

    base = {"address": "10.70.0.10", "mac": None, "responded_via": "icmp",
            "hostname": None}
    assert NetSweepDocument(
        subnet_id=str(uuid4()),
        sweep_run_id=str(uuid4()),
        scope_digest=DIGEST,
        method="icmp",
        started_at="2026-08-02T12:00:00Z",
        finished_at="2026-08-02T12:01:00Z",
        probed_count=1,
        truncated=False,
        hosts=[base],
        errors=[],
    ).hosts[0].address == "10.70.0.10"

    for creeping in ("rtt_ms", "latency_ms", "reachable", "retries"):
        with pytest.raises(ValidationError):
            NetSweepDocument(
                subnet_id=str(uuid4()),
                sweep_run_id=str(uuid4()),
                scope_digest=DIGEST,
                method="icmp",
                started_at="2026-08-02T12:00:00Z",
                finished_at="2026-08-02T12:01:00Z",
                probed_count=1,
                truncated=False,
                hosts=[{**base, creeping: 1}],
                errors=[],
            )


def test_the_params_model_forbids_extras_and_bounds_every_cost() -> None:
    """§5.2: `rate_pps`, `max_targets` and `budget_s` are inside the SIGNED
    params, so the ceiling on what a sweep can cost is set by the server and
    covered by the signature. Each bound is asserted at both ends -- a `ge=`
    without a `le=` is how a `rate_pps` of 200000 gets signed."""
    from pydantic import ValidationError

    base = {
        "subnet_id": str(uuid4()),
        "sweep_run_id": str(uuid4()),
        "targets": ["10.70.0.0/24"],
        "scope_digest": DIGEST,
    }
    assert NetSweepParams(**base).method == "icmp+arp"
    with pytest.raises(ValidationError):
        NetSweepParams(**base, subnet="10.70.0.0/24")  # extra="forbid"
    for field, bad in (
        ("rate_pps", 0), ("rate_pps", 201),
        ("probe_timeout_ms", 99), ("probe_timeout_ms", 5001),
        ("max_targets", 0), ("max_targets", 65537),
        ("budget_s", 9), ("budget_s", 901),
        ("method", "syn"),
    ):
        with pytest.raises(ValidationError):
            NetSweepParams(**base, **{field: bad})


def test_the_errors_list_is_bounded_in_both_dimensions() -> None:
    """§5.4: 20 entries, 200 characters each. An agent's error strings are
    unbounded free text landing in a JSONB column that the whole result is
    stored in; the bound is in the schema so no caller has to remember it."""
    from pydantic import ValidationError

    base = {
        "subnet_id": str(uuid4()),
        "sweep_run_id": str(uuid4()),
        "scope_digest": DIGEST,
        "method": "icmp",
        "started_at": "2026-08-02T12:00:00Z",
        "finished_at": "2026-08-02T12:01:00Z",
        "probed_count": 0,
        "truncated": False,
        "hosts": [],
    }
    assert NetSweepDocument(**base, errors=["x" * 200] * 20).errors[0] == "x" * 200
    with pytest.raises(ValidationError):
        NetSweepDocument(**base, errors=["x"] * 21)
    with pytest.raises(ValidationError):
        NetSweepDocument(**base, errors=["x" * 201])


def test_a_malformed_document_is_rejected_rather_than_partially_read(
    admin_engine: AsyncEngine
) -> None:
    """`handle_sweep_result` never reads a raw dict key. A document that fails
    validation raises `IpamSweepRejected`, which is a rejection and not a
    `KeyError` from a handler halfway through writing."""
    assert issubclass(IpamSweepRejected, Exception)


async def test_a_document_that_fails_validation_writes_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The garbage case, end to end: `probed_count` is a string, so the model
    refuses. Zero observations, the job failed, and the rejection is audited --
    the same treatment as a scope violation, because a document this module
    cannot parse is a document it cannot check."""
    fx = await _dispatched(admin_engine, "garbage")
    job = await _complete(
        admin_engine, app_engine, fx, _document(fx, probed_count="lots"), tmp_path
    )
    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, fx.org_id) == []
    assert "ipam.sweep_rejected" in [r.action for r in await _audit(admin_engine, fx.org_id)]


# --------------------------------------------------------------------------- #
# Cross-org
# --------------------------------------------------------------------------- #


async def test_a_result_for_a_run_in_another_org_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The run is loaded with the JOB's `org_id`, so a signed job for org A can
    never complete a run belonging to org B -- even though `ipam_sweep_run.id` is
    a bare UUID that org A could name.

    Pinned by asserting org B's run is untouched, which is the observable
    difference; the exception alone would be produced by a missing row too.
    """
    a = await _dispatched(admin_engine, "xa")
    b = await _dispatched(admin_engine, "xb")
    doc = _document(a, sweep_run_id=str(b.run_id))
    job = await _complete(admin_engine, app_engine, a, doc, tmp_path)

    assert job.status is JobStatus.failed
    assert await _observations(admin_engine, a.org_id) == []
    assert (await _run_row(admin_engine, b.run_id)).state == "dispatched"
    assert (await _run_row(admin_engine, b.run_id)).hosts_found == 0


async def test_the_handler_is_the_registered_result_handler_for_net_sweep() -> None:
    """The job type carries the handler: `JobEngine.complete` looks it up by name
    and a `net.sweep` registered without one would silently accept every result
    it was ever sent."""
    from app.jobs.registry import get_job_type

    register_job_types()
    job_type = get_job_type("net.sweep")
    assert job_type.params_model is NetSweepParams
    assert job_type.result_handler is handle_sweep_result

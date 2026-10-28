"""Seed helpers shared by every `test_ipam_*.py` module.

WHY THESE LIVE IN ONE PLACE. Every task from the registry onward needs an org
with a user, a device and a `global` VRF before it can assert anything, and a
per-file copy of that setup is how two tests come to disagree about what a
"default VRF" is. These are deliberately THIN -- they insert rows on the admin
(RLS-bypassing) engine and return ids, with no service-layer call anywhere, so a
test that exercises a service is never seeded by the same code path it is
testing. Same shape and same reasoning as `tests/health_fixtures.py`.

Every helper accepts either an `AsyncEngine` or an object carrying one as
`.admin_engine` (the `api_env` fixture), because some tests hold one and some
the other.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope


def admin_engine_of(env_or_engine: Any) -> AsyncEngine:
    """`api_env` or a bare engine -> the admin engine."""
    engine = getattr(env_or_engine, "admin_engine", env_or_engine)
    assert isinstance(engine, AsyncEngine)
    return engine


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IpamOrg:
    """One org with the kernel rows every IPAM test needs."""

    org_id: UUID
    user_id: UUID
    device_id: UUID


async def seed_org(env_or_engine: Any, tag: str = "ip") -> IpamOrg:
    from app.models import Device, Organization, User

    engine = admin_engine_of(env_or_engine)
    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        device = Device(
            org_id=org.id,
            hostname=f"host-{slug}",
            os_family="linux",
            client_id=org.id,
            site_id=org.id,
        )
        session.add(device)
        await session.commit()
        return IpamOrg(org_id=org.id, user_id=user.id, device_id=device.id)


async def seed_user(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    from app.models import User

    engine = admin_engine_of(env_or_engine)
    tag = uuid4().hex[:10]
    values: dict[str, Any] = {
        "external_subject": f"sub-{tag}",
        "email": f"{tag}@test.local",
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, **values)
        session.add(user)
        await session.commit()
        return user.id


async def seed_device(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    from app.models import Device

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {
        "hostname": f"host-{uuid4().hex[:8]}",
        "os_family": "linux",
    }
    values.update(kw)
    # The internal client and its Principal site (id = org id) unless a test
    # names another client's site.
    values.setdefault("client_id", org_id)
    values.setdefault("site_id", org_id)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, **values)
        session.add(device)
        await session.commit()
        return device.id


async def seed_agent(
    env_or_engine: Any,
    org_id: UUID,
    device_id: UUID,
    *,
    status: str = "online",
    last_seen_at: datetime | None = None,
    **kw: Any,
) -> UUID:
    """An `agents` row -- the CONNECTION-LIVENESS signal, which is what §0.11's
    gates read.

    `Device.last_seen_at` is written only by inventory ingestion
    (`app/modules/inventory/ingest.py:85`) and by enrollment, so it means "last
    inventory sweep", not "last contact". A gate reading `Device` would mark
    every connected-but-not-recently-swept machine dormant, which is the exact
    noise §0.11 exists to eliminate.
    """
    from app.models import Agent

    engine = admin_engine_of(env_or_engine)
    now = utcnow()
    values: dict[str, Any] = {
        "cert_fingerprint": uuid4().hex,
        "cert_serial": uuid4().hex[:16],
        "cert_not_after": now + timedelta(days=365),
        "enrolled_at": now,
        "status": status,
        "last_seen_at": now if last_seen_at is None else last_seen_at,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        agent = Agent(org_id=org_id, device_id=device_id, **values)
        session.add(agent)
        await session.commit()
        return agent.id


async def seed_vrf(env_or_engine: Any, org_id: UUID, name: str = "global", **kw: Any) -> UUID:
    """A VRF row, defaulting to `global` -- the one §0.7 creates lazily -- of
    the internal client (whose id is the organisation's) unless `client_id`
    names another (migration 0240: a VRF is a client's).

    Seeded directly rather than through `registry.ensure_default_vrf`, so a test
    of that function is never set up by that function.
    """
    from app.models import IpamVrf

    engine = admin_engine_of(env_or_engine)
    kw.setdefault("client_id", org_id)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        vrf = IpamVrf(org_id=org_id, name=name, **kw)
        session.add(vrf)
        await session.commit()
        return vrf.id


async def seed_site(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    """A `sites` row -- what a VLAN, a subnet or a site server names since
    migration 0240 merged `ipam_site` into it -- of the internal client unless
    `client_id` names another."""
    from app.models import Site

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {"name": f"site-{uuid4().hex[:8]}", "client_id": org_id}
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        site = Site(org_id=org_id, **values)
        session.add(site)
        await session.commit()
        return site.id


async def seed_vlan(env_or_engine: Any, org_id: UUID, vid: int = 10, **kw: Any) -> UUID:
    """A VLAN of the internal client unless `client_id` (or a `site_id` of
    another client's, with that `client_id`) names another."""
    from app.models import IpamVlan

    engine = admin_engine_of(env_or_engine)
    kw.setdefault("client_id", org_id)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        vlan = IpamVlan(org_id=org_id, vid=vid, **kw)
        session.add(vlan)
        await session.commit()
        return vlan.id


async def seed_subnet(
    env_or_engine: Any,
    org_id: UUID,
    *,
    vrf_id: UUID,
    prefix: str,
    kind: str = "assignable",
    **kw: Any,
) -> UUID:
    from app.models import IpamSubnet

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        subnet = IpamSubnet(org_id=org_id, vrf_id=vrf_id, prefix=prefix, kind=kind, **kw)
        session.add(subnet)
        await session.commit()
        return subnet.id


async def upsert_observation_row(
    env_or_engine: Any,
    org_id: UUID,
    *,
    vrf_id: UUID,
    address: str,
    source: str = "sweep",
    method: str = "icmp",
    confidence: str = "low",
    mac: str | None = None,
    **kw: Any,
) -> None:
    """§2.7's upsert, written the ONE way that is correct (§0.15).

    Inference is on `uq_ipam_obs_key`'s five columns, the fifth of which is the
    GENERATED `mac_key` and not the nullable `mac`. `mac_key` is never in
    `values()`: setting a generated column is a Postgres error, which is exactly
    the property that makes the guarantee unforgettable rather than remembered.

    This lives here rather than in a test file because the migration test and
    the observation-service test must exercise the SAME statement shape -- two
    copies is how one of them ends up inferring on `mac` and passing anyway.
    """
    from app.models import IpamObservation

    engine = admin_engine_of(env_or_engine)
    now = kw.pop("now", None) or utcnow()
    values: dict[str, Any] = {
        "org_id": org_id,
        "vrf_id": vrf_id,
        "address": address,
        "mac": mac,
        "source": source,
        "method": method,
        "confidence": confidence,
        "first_seen_at": now,
        "last_seen_at": now,
        "sighting_count": 1,
    }
    values.update(kw)
    statement = pg_insert(IpamObservation).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["org_id", "vrf_id", "address", "source", "mac_key"],
        set_={
            "last_seen_at": statement.excluded.last_seen_at,
            "sighting_count": IpamObservation.sighting_count + 1,
            "hostname": statement.excluded.hostname,
        },
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(statement)
        await session.commit()


async def observations_at(
    env_or_engine: Any, org_id: UUID, address: str
) -> list[tuple[str, str | None, int]]:
    """`(source, mac, sighting_count)` per stored observation, ordered.

    Returned as VALUES rather than as a count, because §0.15's whole point is
    that the broken key produces two rows each with `sighting_count = 1` where
    the correct one produces one row with `sighting_count = 2`. A test asserting
    "no exception was raised" cannot tell those apart.
    """
    from app.models import IpamObservation

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                select(
                    IpamObservation.source,
                    IpamObservation.mac,
                    IpamObservation.sighting_count,
                )
                .where(
                    IpamObservation.org_id == org_id,
                    IpamObservation.address == address,
                )
                .order_by(IpamObservation.source, IpamObservation.mac)
            )
        ).all()
    return [(r.source, r.mac, r.sighting_count) for r in rows]


async def seed_address(
    env_or_engine: Any,
    org_id: UUID,
    *,
    vrf_id: UUID,
    address: str,
    subnet_id: UUID | None = None,
    state: str = "assigned",
    **kw: Any,
) -> UUID:
    """An `ipam_address` row -- what a HUMAN asserted (§0.6).

    Seeded directly rather than through `addresses.assign` so a reconciliation
    test is never set up by the transition workflow it is independent of, and so
    a fixture can express states (`deprecated` at an address nobody assigned)
    that the workflow would refuse to reach in one step.
    """
    from app.models import IpamAddress

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = IpamAddress(
            org_id=org_id,
            vrf_id=vrf_id,
            subnet_id=subnet_id,
            address=address,
            state=state,
            **kw,
        )
        session.add(row)
        await session.commit()
        return row.id


async def seed_range(
    env_or_engine: Any,
    org_id: UUID,
    *,
    subnet_id: UUID,
    kind: str,
    start_address: str,
    end_address: str,
    **kw: Any,
) -> UUID:
    from app.models import IpamRange

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = IpamRange(
            org_id=org_id,
            subnet_id=subnet_id,
            kind=kind,
            start_address=start_address,
            end_address=end_address,
            **kw,
        )
        session.add(row)
        await session.commit()
        return row.id


async def seed_sweep_run(
    env_or_engine: Any,
    org_id: UUID,
    *,
    subnet_id: UUID,
    state: str = "succeeded",
    started_at: datetime | None = None,
    **kw: Any,
) -> UUID:
    """An `ipam_sweep_run` -- the evidence source §4.3's Gate E and §0.16's Gate
    H both read.

    `state` and `started_at` are the two fields those gates turn on, so they are
    parameters rather than constants: a fixture that always seeds a succeeded,
    recent run cannot tell Gate E from Gate H.
    """
    from app.models import IpamSweepRun

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = IpamSweepRun(
            org_id=org_id,
            subnet_id=subnet_id,
            state=state,
            trigger=kw.pop("trigger", "scheduled"),
            started_at=started_at or utcnow(),
            **kw,
        )
        session.add(row)
        await session.commit()
        return row.id


# --------------------------------------------------------------------------- #
# A whole sweep, dispatched and completed
#
# THESE EXIST BECAUSE A SWEEP NOW WRITES OUTSIDE IPAM. Since the operator's
# ruling of 2026-08-26 a completed `net.sweep` creates `devices` rows
# (`docs/modules/asset-inventory/spec.md` §3), so the asset tests have to be
# able to run a real sweep result through the real extension point rather than
# inserting observations by hand -- an observation written directly into the
# table is not a sweep and never triggers the path under test.
#
# Built directly rather than through `create_sweep` for the reason
# `test_ipam_sweep_result.py` gives: a result test must never be set up by the
# dispatcher whose output it is checking.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DispatchedSweep:
    """One `net.sweep` job, signed-params and all, ready to be completed."""

    org_id: UUID
    vrf_id: UUID
    subnet_id: UUID
    run_id: UUID
    job_id: UUID
    #: The device the job targets: the agent that will do the sweeping.
    device_id: UUID
    targets: list[str]


async def dispatch_sweep(
    env_or_engine: Any,
    org_id: UUID,
    *,
    vrf_id: UUID,
    device_id: UUID,
    prefix: str = "10.70.0.0/24",
    targets: list[str] | None = None,
    max_targets: int = 4096,
    subnet_id: UUID | None = None,
) -> DispatchedSweep:
    from app.models import Job, JobStatus
    from app.modules.ipam.handler import register_job_types
    from app.modules.ipam.models import IpamSweepRun
    from app.modules.ipam.net import scope_digest
    from app.modules.ipam.schema import NetSweepParams
    from app.modules.ipam.sweep import SWEEP_TTL_S

    register_job_types()
    engine = admin_engine_of(env_or_engine)
    signed = targets if targets is not None else [prefix]
    if subnet_id is None:
        subnet_id = await seed_subnet(engine, org_id, vrf_id=vrf_id, prefix=prefix)
    now = utcnow()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        run = IpamSweepRun(
            org_id=org_id,
            subnet_id=subnet_id,
            state="queued",
            trigger="manual",
            target_count=256,
            scope_digest=scope_digest(signed),
            started_at=now,
        )
        session.add(run)
        await session.flush()
        job = Job(
            org_id=org_id,
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
            issued_at=now,
            expires_at=now + timedelta(seconds=SWEEP_TTL_S),
            status=JobStatus.dispatched,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        run.state = "dispatched"
        await session.commit()
        return DispatchedSweep(
            org_id=org_id,
            vrf_id=vrf_id,
            subnet_id=subnet_id,
            run_id=run.id,
            job_id=job.id,
            device_id=device_id,
            targets=list(signed),
        )


def sweep_document(fx: DispatchedSweep, hosts: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """The agent's report for `fx`, echoing the scope it was signed for."""
    from app.modules.ipam.net import scope_digest

    doc: dict[str, Any] = {
        "subnet_id": str(fx.subnet_id),
        "sweep_run_id": str(fx.run_id),
        "scope_digest": scope_digest(fx.targets),
        "method": "icmp+arp",
        "started_at": "2026-08-26T12:00:00Z",
        "finished_at": "2026-08-26T12:01:00Z",
        "probed_count": 256,
        "truncated": False,
        "hosts": hosts,
        "errors": [],
    }
    doc.update(kw)
    return doc


def sweep_host(address: str, *, mac: str | None = None, hostname: str | None = None,
               responded_via: str | None = None) -> dict[str, Any]:
    """One host record. `responded_via` follows the MAC unless overridden: ARP
    is what carries one, and a fixture claiming an ICMP-only sighting that
    somehow knows a MAC would be describing something the agent cannot do."""
    return {
        "address": address,
        "mac": mac,
        "responded_via": responded_via or ("arp" if mac else "icmp"),
        "hostname": hostname,
    }


async def complete_sweep(
    app_engine: Any,
    admin_engine: Any,
    fx: DispatchedSweep,
    doc: dict[str, Any],
    tmp_path: Any,
) -> Any:
    """Run `doc` through `JobEngine.complete` -- the real extension point.

    Through the TENANT session, like production: the handler's writes are
    therefore subject to RLS exactly as they are in the gateway, so a tenancy
    defect in what it writes cannot hide behind an admin connection.
    """
    from app.core.signing import LocalEd25519Signer
    from app.core.tenancy import org_session
    from app.jobs.engine import JobEngine
    from app.models import Job

    app = getattr(app_engine, "app_engine", app_engine)
    admin = admin_engine_of(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(Path(tmp_path) / "sweep.key"))
    async with org_session(fx.org_id, engine=app, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == fx.job_id))).scalar_one()
        await engine.complete(session, job, state="succeeded", result=doc)
    async with AsyncSession(admin) as session:
        return (await session.execute(select(Job).where(Job.id == fx.job_id))).scalar_one()

"""Seed helpers shared by every `test_reporting_*.py` module.

Deliberately THIN: they insert rows on the admin (RLS-bypassing) engine and
return ids, with no service-layer call anywhere, so a test that exercises the
reporting service is never seeded by the code path it is testing.

`reporting_user` exists because this module's whole subject is the CONJUNCTION
of permission codes, and the seeded global roles cannot express "holds
`reporting.read` and `devices.read` but NOT `health.read`". Every grant set here
is a freshly-named org-scoped role, so two callers in one test never share one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def utcnow() -> datetime:
    return datetime.now(UTC)


async def reporting_user(
    api_env: Any,
    org_id: UUID,
    *codes: str,
    client_id: UUID | None = None,
) -> tuple[UUID, str]:
    """A user in `org_id` holding exactly `codes` and nothing else.

    `client_id` scopes the grant to one client -- the §0.6 case (MSP clients
    spec §2.1): `user_permission_codes` counts such a row ONLY when asked
    about that same client, and every reporting gate asks with no client, so a
    client-scoped grant holds nothing on every client.
    """
    from app.models import Permission, Role, RolePermission, User, UserRole

    engine: AsyncEngine = api_env.admin_engine
    sub = f"rep-{uuid4().hex[:10]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        session.add(user)
        role = Role(org_id=org_id, name=f"rep-role-{uuid4().hex[:8]}", description="test")
        session.add(role)
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(
            UserRole(
                org_id=org_id,
                user_id=user.id,
                role_id=role.id,
                client_id=client_id,
            )
        )
        await session.commit()
        return user.id, sub


# --------------------------------------------------------------------------- #
# Making a report RUNNABLE and NON-EMPTY, for the sweeps in
# `test_reporting_runner.py` that are parametrized over the whole REGISTRY.
#
# `sla-attainment` is what forced these into existence: it is the first report
# with REQUIRED params (§5.4's closed window) and the first whose rows are not
# per-device, so a sweep that requested `/api/v1/reports/{key}` bare got a 422
# for it, and a sweep seeded with two devices got a 200 with NO ROWS -- which
# passes every "the entitled caller is not refused" assertion while asserting
# nothing at all.
# --------------------------------------------------------------------------- #


def required_query(key: str) -> dict[str, str]:
    """The query parameters a report cannot be requested without.

    `uncovered_required_params` below is the guard that keeps this matching the
    DEFINITIONS rather than the reports somebody remembered: a future report
    with a required parameter fails THERE, naming it, instead of appearing as a
    422 in six unrelated sweeps.
    """
    if key == "sla-attainment":
        now = utcnow()
        return {
            "from": (now - timedelta(days=30)).isoformat(),
            "to": (now + timedelta(days=1)).isoformat(),
        }
    return {}


def report_query(key: str, **extra: Any) -> str:
    """`?from=...&to=...&limit=...` -- the required params plus whatever the
    caller adds. Empty string when a report needs nothing."""
    params = {**required_query(key), **{name: str(value) for name, value in extra.items()}}
    return f"?{urlencode(params)}" if params else ""


def uncovered_required_params() -> dict[str, set[str]]:
    """Definitions whose required params `required_query` does not supply."""
    from app.modules.reporting.registry import REGISTRY

    uncovered: dict[str, set[str]] = {}
    for key, definition in REGISTRY.items():
        required = {
            info.alias or name
            for name, info in definition.params.model_fields.items()
            if info.is_required()
        }
        missing = required - set(required_query(key))
        if missing:
            uncovered[key] = missing
    return uncovered


async def seed_reportable_org(api_env: Any, prefix: str = "rep") -> UUID:
    """An org with enough seeded data that EVERY report in the registry returns
    at least one row.

    The fixture-blindness guard the conjunction sweeps rest on: a 200 with no
    rows and a 403 are equally "not a leak", and only one of them is the
    behaviour under test. `test_withholding_exactly_one_source_code_is_a_403`
    asserts non-empty rows per key, so a future report this does not seed goes
    red there, naming the key.
    """
    org_id = await api_env.create_org(prefix)
    # devices / agents / health -> fleet-posture, and the per-device rows of
    # patch-compliance and backup-coverage (both report a device with no module
    # data at all, which is the point of them).
    first = await seed_device(api_env.admin_engine, org_id, hostname="alpha")
    await seed_device(api_env.admin_engine, org_id, hostname="beta")
    await seed_agent(api_env.admin_engine, org_id, first.device_id, last_seen_days_ago=0.1)
    # ticket / ticket_queue -> sla-attainment, whose rows are per (queue,
    # priority) and which is empty for an org with no tickets.
    anchor = await seed_service_desk_anchor(api_env.admin_engine, org_id)
    await seed_ticket(api_env.admin_engine, org_id, anchor)
    return org_id


@dataclass(frozen=True)
class SeededDevice:
    device_id: UUID
    hostname: str


async def seed_device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    hostname: str,
    os_family: str = "windows",
    os_version: str = "10.0.19045",
    device_group_id: UUID | None = None,
) -> SeededDevice:
    from app.models import Device

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family=os_family,
            os_version=os_version,
            device_group_id=device_group_id,
            client_id=org_id,
            site_id=org_id,
        )
        session.add(device)
        await session.commit()
        return SeededDevice(device_id=device.id, hostname=hostname)


async def seed_agent(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    *,
    last_seen_days_ago: float,
    status: str = "online",
) -> UUID:
    from app.models import Agent

    now = utcnow()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        agent = Agent(
            org_id=org_id,
            device_id=device_id,
            cert_fingerprint=uuid4().hex,
            cert_serial=uuid4().hex[:16],
            cert_not_after=now + timedelta(days=365),
            enrolled_at=now - timedelta(days=30),
            status=status,
            last_seen_at=now - timedelta(days=last_seen_days_ago),
        )
        session.add(agent)
        await session.commit()
        return agent.id


async def seed_device_group(admin_engine: AsyncEngine, org_id: UUID, *, name: str) -> UUID:
    from app.models import DeviceGroup

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name=name, client_id=org_id)
        session.add(group)
        await session.commit()
        return group.id


# --------------------------------------------------------------------------- #
# Patch module rows (`patch-compliance`, spec §5.2).
# --------------------------------------------------------------------------- #


async def seed_patch(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    identifier: str,
    severity: str = "critical",
    os_family: str = "windows",
    source: str = "wsus",
) -> UUID:
    from app.models import Patch

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        patch = Patch(
            org_id=org_id,
            os_family=os_family,
            source=source,
            identifier=identifier,
            title=f"patch {identifier}",
            severity=severity,
        )
        session.add(patch)
        await session.commit()
        return patch.id


async def seed_patch_status(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    patch_id: UUID,
    *,
    state: str,
    detected_days_ago: float = 1,
    applied_days_ago: float | None = None,
    updated_days_ago: float | None = None,
) -> UUID:
    """One `device_patch_status` row.

    `updated_days_ago` is settable because `failed_in_window` has no timestamp
    of its own to read: `apply.py` sets `state = "failed"` and `last_error` and
    NOTHING else, so `updated_at` (server default, `onupdate`) is the only
    record of when the failure landed. Passing it explicitly at INSERT is how a
    test puts a failure inside or outside a window.
    """
    from app.models import DevicePatchStatus

    now = utcnow()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        status = DevicePatchStatus(
            org_id=org_id,
            device_id=device_id,
            patch_id=patch_id,
            state=state,
            detected_at=now - timedelta(days=detected_days_ago),
            applied_at=(
                None if applied_days_ago is None else now - timedelta(days=applied_days_ago)
            ),
        )
        if updated_days_ago is not None:
            status.updated_at = now - timedelta(days=updated_days_ago)
        session.add(status)
        await session.commit()
        return status.id


async def seed_patch_approval(
    admin_engine: AsyncEngine,
    org_id: UUID,
    patch_id: UUID,
    *,
    ring_name: str,
    revoked: bool = False,
) -> UUID:
    from app.models import PatchApproval

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        approval = PatchApproval(
            org_id=org_id, patch_id=patch_id, ring_name=ring_name, revoked=revoked
        )
        session.add(approval)
        await session.commit()
        return approval.id


async def seed_patch_policy(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    device_group_id: UUID | None = None,
    ring_name: str = "broad",
    scan_interval_hours: int = 24,
) -> UUID:
    from app.models import PatchPolicy

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        policy = PatchPolicy(
            org_id=org_id,
            device_group_id=device_group_id,
            ring_name=ring_name,
            scan_interval_hours=scan_interval_hours,
        )
        session.add(policy)
        await session.commit()
        return policy.id


async def seed_patch_scan_job(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    *,
    hours_ago: float = 1,
    succeeded: bool = True,
) -> UUID:
    """A terminal `patch.scan` job against `device_id`.

    THIS, AND NOT A `device_patch_status` ROW, IS THE RECORD THAT A SCAN
    HAPPENED. `JobEngine.complete` flips a job to `succeeded` only after the
    patch module's ingest handler has returned without raising, so a succeeded
    row proves both that the scan ran and that its document landed. A
    `device_patch_status` row proves only that a PATCH was once discovered:
    `detected_at` is stamped at insert and no later scan rewrites it, so a
    device whose patch set has not changed since enrolment carries rows that
    are as old as the device.

    `org_id` and `device_id` are seeded independently on purpose. `jobs.
    target_id` is a bare UUID with no foreign key, so a job in one org CAN
    name another org's device id, and a test that could not seed that pair
    could not pin the tenancy predicate.
    """
    from app.models import Job, JobStatus

    now = utcnow()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type="patch.scan",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=now - timedelta(hours=hours_ago),
            expires_at=now - timedelta(hours=hours_ago) + timedelta(hours=1),
            status=JobStatus.succeeded if succeeded else JobStatus.failed,
            finished_at=now - timedelta(hours=hours_ago),
        )
        session.add(job)
        await session.commit()
        return job.id


# --------------------------------------------------------------------------- #
# Backup module rows (`backup-coverage`, spec §5.3).
# --------------------------------------------------------------------------- #


async def seed_backup_repository(
    admin_engine: AsyncEngine, org_id: UUID, *, name: str, client_id: UUID | None = None
) -> UUID:
    from app.models import BackupRepository

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        repository = BackupRepository(
            org_id=org_id,
            # One client's (migration 0242): the internal client's by default.
            client_id=client_id or org_id,
            name=name,
            # Neither of these may ever appear in a report; they are on
            # `_NEVER_A_COLUMN`. Seeded with recognisable values so a test can
            # assert their ABSENCE from a response body by substring.
            storage_ref="s3://secret-bucket/never-in-a-report",
            encryption_key_ref="vault://never-in-a-report",
        )
        session.add(repository)
        await session.commit()
        return repository.id


async def seed_backup_policy(
    admin_engine: AsyncEngine,
    org_id: UUID,
    repository_id: UUID,
    *,
    device_group_id: UUID | None = None,
    enabled: bool = True,
    client_id: UUID | None = None,
) -> UUID:
    """A group's policy, or -- with no group -- a client's default: the
    internal client's unless `client_id` names another (a policy names only a
    repository of its own client, migration 0242)."""
    from app.models import BackupPolicy

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        policy = BackupPolicy(
            org_id=org_id,
            repository_id=repository_id,
            device_group_id=device_group_id,
            enabled=enabled,
        )
        if device_group_id is None:
            policy.client_id = client_id or org_id
        session.add(policy)
        await session.commit()
        return policy.id


async def seed_backup_run(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    repository_id: UUID,
    *,
    state: str,
    started_days_ago: float = 1,
) -> UUID:
    from app.models import BackupRun

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        run = BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repository_id,
            state=state,
            actor_type="system",
            started_at=utcnow() - timedelta(days=started_days_ago),
        )
        session.add(run)
        await session.commit()
        return run.id


async def seed_backup_point(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    repository_id: UUID,
    run_id: UUID,
    *,
    state: str = "complete",
    taken_days_ago: float = 1,
    verified_days_ago: float | None = None,
    restore_tested_days_ago: float | None = None,
) -> UUID:
    from app.models import BackupPoint

    now = utcnow()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        point = BackupPoint(
            org_id=org_id,
            device_id=device_id,
            repository_id=repository_id,
            backup_run_id=run_id,
            taken_at=now - timedelta(days=taken_days_ago),
            manifest_ref=f"manifest-{uuid4().hex[:8]}",
            size=1024,
            state=state,
            verified_at=(
                None if verified_days_ago is None else now - timedelta(days=verified_days_ago)
            ),
            restore_tested_at=(
                None
                if restore_tested_days_ago is None
                else now - timedelta(days=restore_tested_days_ago)
            ),
        )
        session.add(point)
        await session.commit()
        return point.id


# --------------------------------------------------------------------------- #
# Service desk + SLA rows (`sla-attainment`, spec §5.4).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServiceDeskAnchor:
    """Everything a `ticket` + `ticket_sla` pair needs to exist: a user (queues
    and categories carry a NOT NULL `created_by`), a queue, a category, a
    calendar and a policy."""

    user_id: UUID
    queue_id: UUID
    queue_name: str
    category_id: UUID
    calendar_id: UUID
    policy_id: UUID


async def seed_service_desk_anchor(
    admin_engine: AsyncEngine, org_id: UUID, *, queue_name: str = "Support"
) -> ServiceDeskAnchor:
    """One queue, and one SLA policy scoped to it.

    The policy is scoped to the QUEUE rather than being a `(NULL, NULL)`
    wildcard on purpose: `uq_sla_policy_client_wildcard_both` is a partial unique
    index on `(org_id, client_id)` with NULLs not distinct, so two global
    wildcard policies in one org is an IntegrityError -- and a test that wanted
    two queues in one org would hit it.
    """
    from app.models import SlaCalendar, SlaPolicy, TicketCategory, TicketQueue, User

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        user = User(
            org_id=org_id,
            external_subject=f"sd-{uuid4().hex[:10]}",
            email=f"sd-{uuid4().hex[:6]}@test.local",
        )
        session.add(user)
        await session.flush()

        queue = TicketQueue(
            org_id=org_id, key=f"q-{uuid4().hex[:10]}", name=queue_name, created_by=user.id
        )
        session.add(queue)
        await session.flush()

        category = TicketCategory(
            org_id=org_id,
            key=f"c-{uuid4().hex[:10]}",
            name="Category",
            type="incident",
            default_queue_id=queue.id,
            created_by=user.id,
        )
        session.add(category)

        calendar = SlaCalendar(
            org_id=org_id,
            name=f"cal-{uuid4().hex[:8]}",
            timezone="UTC",
            created_by=user.id,
        )
        session.add(calendar)
        await session.flush()

        policy = SlaPolicy(
            org_id=org_id,
            queue_id=queue.id,
            priority=None,
            calendar_id=calendar.id,
            response_target_minutes=60,
            resolution_target_minutes=480,
            created_by=user.id,
        )
        session.add(policy)
        await session.commit()

        return ServiceDeskAnchor(
            user_id=user.id,
            queue_id=queue.id,
            queue_name=queue_name,
            category_id=category.id,
            calendar_id=calendar.id,
            policy_id=policy.id,
        )


async def seed_ticket(
    admin_engine: AsyncEngine,
    org_id: UUID,
    anchor: ServiceDeskAnchor,
    *,
    priority: str = "normal",
    status: str = "open",
    created_days_ago: float = 1,
    queue_id: UUID | None = None,
) -> UUID:
    """One ticket.

    `queue_id` overrides the anchor's queue and exists for ONE test: a ticket in
    org B pointed at org A's queue. `ticket.queue_id` is a plain foreign key with
    no cross-org check, so that row is storable -- and it is the only shape in
    which `Ticket.org_id == org_id` is individually observable, because the join
    condition `TicketQueue.org_id == org_id` otherwise makes the two predicates
    mutually redundant.
    """
    from app.models import Ticket

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = Ticket(
            org_id=org_id,
            client_id=org_id,
            number=f"INC-{uuid4().hex[:10]}",
            type="incident",
            category_id=anchor.category_id,
            queue_id=anchor.queue_id if queue_id is None else queue_id,
            status=status,
            priority=priority,
            subject="never appears in a report",
            description="never appears in a report",
            requester_id=anchor.user_id,
            created_by=anchor.user_id,
            origin="technician",
            created_at=utcnow() - timedelta(days=created_days_ago),
        )
        session.add(ticket)
        await session.commit()
        return ticket.id


async def seed_ticket_sla(
    admin_engine: AsyncEngine,
    org_id: UUID,
    anchor: ServiceDeskAnchor,
    ticket_id: UUID,
    *,
    policy_source: str = "initial",
    response_met: bool = False,
    response_breached: bool = False,
    resolution_met: bool = False,
    resolution_breached: bool = False,
    voided: bool = False,
) -> UUID:
    """A `ticket_sla` row with the four stop columns set independently.

    They are independent because they genuinely co-occur: a reply that lands
    after the deadline stamps `met_at` on a row whose `breached_at` is already
    set, which is the case `views.target_state` resolves in favour of
    `breached` and the one this module's precedence has to reproduce.
    """
    from app.models import TicketSla

    now = utcnow()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = TicketSla(
            org_id=org_id,
            ticket_id=ticket_id,
            policy_id=anchor.policy_id,
            calendar_id=anchor.calendar_id,
            response_due_at=now - timedelta(hours=2),
            resolution_due_at=now - timedelta(hours=1),
            response_met_at=now if response_met else None,
            resolution_met_at=now if resolution_met else None,
            response_breached_at=now if response_breached else None,
            resolution_breached_at=now if resolution_breached else None,
            voided_at=now if voided else None,
            policy_source=policy_source,
        )
        session.add(row)
        await session.commit()
        return row.id


@dataclass(frozen=True)
class AlertAnchor:
    """The `health_profile` + `health_check` rows a `health_alert` needs, since
    `health_alert.check_id` is `ON DELETE RESTRICT` against a real check."""

    profile_id: UUID
    check_id: UUID


async def seed_alert_anchor(admin_engine: AsyncEngine, org_id: UUID) -> AlertAnchor:
    from app.models import HealthCheck, HealthProfile

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = HealthProfile(
            org_id=org_id, name=f"profile-{uuid4().hex[:8]}", description=""
        )
        session.add(profile)
        await session.flush()
        check = HealthCheck(
            org_id=org_id,
            profile_id=profile.id,
            check_type="cpu.busy_pct",
            selector="",
            comparator="gt",
            threshold_num=95,
            threshold_bool=None,
            for_ticks=3,
            severity="warning",
            os_family=None,
        )
        session.add(check)
        await session.commit()
        return AlertAnchor(profile_id=profile.id, check_id=check.id)


async def seed_alert(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    anchor: AlertAnchor,
    *,
    severity: str = "warning",
    state: str = "active",
    opened_days_ago: float = 1,
    selector: str = "",
) -> UUID:
    from app.models import HealthAlert

    now = utcnow()
    opened = now - timedelta(days=opened_days_ago)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = HealthAlert(
            org_id=org_id,
            device_id=device_id,
            check_id=anchor.check_id,
            profile_id=anchor.profile_id,
            selector=selector,
            check_type="cpu.busy_pct",
            severity=severity,
            state=state,
            observed_value="97.4",
            threshold_display="> 95",
            opened_at=opened,
            opened_policy_version=1,
            last_seen_at=now,
            cleared_at=now if state == "cleared" else None,
            cleared_reason="recovery" if state == "cleared" else None,
            muted_until=(opened + timedelta(days=1)) if state == "muted" else None,
        )
        session.add(alert)
        await session.commit()
        return alert.id

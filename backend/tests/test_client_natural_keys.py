"""Task 8b: a natural key on a per-client table is unique per CLIENT.

Unique per organisation, such a key is a rule a technician limited to client X
cannot see: the application's duplicate check runs under X's RLS, misses Y's
row, and the INSERT dies on the index -- a 500 where an every-client admin
gets the refusal, which tells X's technician Y's values one guess at a time.
And two clients cannot both have "Servers". Migration 0231 makes each such
key per client (`tests/test_client_catalog.py`'s guard keeps it so):

- C/H tables: `(org_id, client_id, ...)`;
- O tables: `UNIQUE NULLS NOT DISTINCT (org_id, client_id, ...)`, so global
  rows (no client: every client's configuration) stay unique among
  themselves and each client has a namespace of its own. A global row and a
  client's row may share a value.

Per key, at the database: two clients share a value, one client cannot hold
it twice, and (O) a global row sits beside them while two global rows
collide. Then each application path that checks the key first: it looks in
the namespace of the row it writes -- the narrowed technician is not refused
over another client's value, and the every-client admin is not refused over
a client's value when writing a global one -- and the index's own 23505
answers as the path's existing refusal, never a 500.

In phase 1 only `script_policies` (a group's client) and `push_host_key` (a
device's client) are written into a client by the application; the other O
tables' rows are global, written under scope all, so a narrowed technician
reaches none of them (403, `tests/test_global_rows_api.py`). Their client
namespace is pinned here at the database and on the global path.
"""

import base64
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    BackupOffsiteTarget,
    BackupRecoveryKey,
    ChangeGate,
    Client,
    Credential,
    Device,
    DeviceGroup,
    HealthProfile,
    HealthProfileAssignment,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    Script,
    ScriptPolicy,
    Site,
    SlaCalendar,
    SlaPolicy,
    SoftwareIgnore,
    TicketQueue,
    User,
)
from app.modules.agent_push import host_key as host_key_service
from app.modules.agent_push.models import PushHostKey
from app.modules.backup import offsite_api
from app.modules.backup.recovery import RecoveryKeyError, register_recovery_key
from app.modules.change_mgmt.gates import set_gate
from app.modules.deploy import script_policies
from app.modules.health.errors import HealthValidationError
from app.modules.health.profiles import create_assignment
from app.modules.licenses.service import ignore_software
from app.modules.patch.approve import approve_patch
from app.modules.patch.ingest import AUTO_APPROVE_RING, ingest_scan
from app.modules.patch.schema import PatchScanDocument
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.policies import create_policy
from tests.client_scope_support import everything_on

ADDRESS = "10.88.0.5"
SCHEDULES = "/api/v1/deploy/script-policies"
OFFSITE = "/api/v1/backup/offsite-targets"
CONFIRM = "/api/v1/agent-push/host-keys/confirm"


@dataclass(frozen=True)
class Side:
    """One client's rows. The two sides are alike in everything but the
    client: same group name, same device address."""

    client: UUID
    group: UUID
    device: UUID


@dataclass(frozen=True)
class World:
    org: UUID
    actor: UUID
    script: UUID
    patch: UUID
    calendar: UUID
    queue: UUID
    profile: UUID
    credential: UUID
    x: Side
    y: Side


def _now() -> datetime:
    return datetime.now(UTC)


async def _world(admin_engine: AsyncEngine, org: UUID | None = None) -> World:
    tag = uuid4().hex[:8]
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        if org is None:
            organization = Organization(name=f"keys-{tag}", slug=f"keys-{tag}")
            s.add(organization)
            await s.flush()
            org = organization.id
        actor = User(org_id=org, external_subject=f"sub-keys-{tag}", email=f"k-{tag}@test.local")
        s.add(actor)
        sides: list[Side] = []
        for name in ("X", "Y"):
            client = Client(org_id=org, name=f"{name} {tag}", kind="customer")
            s.add(client)
            await s.flush()
            site = Site(org_id=org, client_id=client.id, name="Principal")
            s.add(site)
            await s.flush()
            group = DeviceGroup(org_id=org, client_id=client.id, name="Servers")
            device = Device(
                org_id=org,
                client_id=client.id,
                site_id=site.id,
                hostname="h",
                primary_ip=ADDRESS,
            )
            s.add_all([group, device])
            await s.flush()
            sides.append(Side(client=client.id, group=group.id, device=device.id))
        await s.flush()
        script = Script(
            org_id=org, name="s", os_family="windows", interpreter="powershell", body="exit 0"
        )
        patch = Patch(
            org_id=org,
            os_family="windows",
            source="wu",
            identifier=f"KB{tag}",
            title="a patch",
            severity="important",
        )
        calendar = SlaCalendar(org_id=org, name=f"cal-{tag}", timezone="UTC", created_by=actor.id)
        queue = TicketQueue(org_id=org, key=f"q-{tag}", name="queue", created_by=actor.id)
        profile = HealthProfile(org_id=org, name=f"profile-{tag}", description="")
        credential = Credential(org_id=org, name="s3", type="s3_offsite", vault_ref="v")
        s.add_all([script, patch, calendar, queue, profile, credential])
        await s.commit()
        return World(
            org=org,
            actor=actor.id,
            script=script.id,
            patch=patch.id,
            calendar=calendar.id,
            queue=queue.id,
            profile=profile.id,
            credential=credential.id,
            x=sides[0],
            y=sides[1],
        )


def _client(side: Side | None) -> UUID | None:
    return None if side is None else side.client


def _sla(queue: bool, priority: bool) -> Callable[[World, Side | None], Any]:
    def make(w: World, side: Side | None) -> SlaPolicy:
        return SlaPolicy(
            org_id=w.org,
            client_id=_client(side),
            queue_id=w.queue if queue else None,
            priority="high" if priority else None,
            calendar_id=w.calendar,
            response_target_minutes=60,
            resolution_target_minutes=480,
            created_by=w.actor,
        )

    return make


#: Each per-client key, and a row holding the SAME natural key in `side`'s
#: client (None: a global row). Rows of an H table always have a client.
ROWS: dict[str, Callable[[World, Side | None], Any]] = {
    "uq_script_policies_client_name": lambda w, side: ScriptPolicy(
        org_id=w.org,
        name="Nightly",
        script_id=w.script,
        device_group_id=side.group,  # type: ignore[union-attr]
    ),
    "uq_push_host_key_client_address": lambda w, side: PushHostKey(
        org_id=w.org,
        device_id=None if side is None else side.device,
        address=ADDRESS,
        transport="ssh",
        observed_fingerprint="SHA256:any",
        observed_at=_now(),
    ),
    "uq_backup_offsite_targets_client_name": lambda w, side: BackupOffsiteTarget(
        org_id=w.org,
        client_id=_client(side),
        name="Offsite",
        bucket="b",
        credential_id=w.credential,
    ),
    "uq_software_ignore_client_name": lambda w, side: SoftwareIgnore(
        org_id=w.org, client_id=_client(side), name="Foo", created_by=w.actor
    ),
    "uq_backup_recovery_keys_client_active": lambda w, side: BackupRecoveryKey(
        org_id=w.org, client_id=_client(side), public_key="k", fingerprint="f" * 32
    ),
    "uq_patch_approvals_client_patch_ring": lambda w, side: PatchApproval(
        org_id=w.org, client_id=_client(side), patch_id=w.patch, ring_name="pilot"
    ),
    "uq_sla_policy_client_queue_and_priority": _sla(queue=True, priority=True),
    "uq_sla_policy_client_queue_wildcard_priority": _sla(queue=True, priority=False),
    "uq_sla_policy_client_wildcard_queue_priority": _sla(queue=False, priority=True),
    "uq_sla_policy_client_wildcard_both": _sla(queue=False, priority=False),
    "uq_health_assignment_client_org": lambda w, side: HealthProfileAssignment(
        org_id=w.org, client_id=_client(side), profile_id=w.profile, created_at=_now()
    ),
    "uq_change_gates_client_group": lambda w, side: ChangeGate(
        org_id=w.org,
        client_id=_client(side),
        device_group_id=None,
        applies_to_patches=True,
        applies_to_deployments=False,
    ),
}

H_KEYS = frozenset({"uq_script_policies_client_name"})


async def _attempt(engine: AsyncEngine, row: Any) -> str | None:
    """None when `row` is written, else the unique key that refused it."""
    async with AsyncSession(engine) as s:
        s.add(row)
        try:
            await s.commit()
        except IntegrityError as exc:
            return str(getattr(getattr(exc.orig, "__cause__", None), "constraint_name", exc))
    return None


@pytest.mark.parametrize("key", sorted(ROWS))
async def test_two_clients_share_a_value_and_one_client_does_not(
    admin_engine: AsyncEngine, key: str
) -> None:
    w = await _world(admin_engine)
    row = ROWS[key]
    assert await _attempt(admin_engine, row(w, w.x)) is None
    assert await _attempt(admin_engine, row(w, w.y)) is None, "two clients share a value"
    assert await _attempt(admin_engine, row(w, w.x)) == key, "one client holds it once"
    if key not in H_KEYS:
        assert await _attempt(admin_engine, row(w, None)) is None, "a global row beside them"
        assert await _attempt(admin_engine, row(w, None)) == key, "global rows collide"


# --------------------------------------------------------------------------- #
# script_policies (H, through the group): the API, every leg
# --------------------------------------------------------------------------- #


def _schedule(w: World, side: Side, name: str) -> dict[str, str]:
    return {"name": name, "script_id": str(w.script), "device_group_id": str(side.group)}


async def test_schedules_are_named_per_client(api_env: Any) -> None:
    org = await api_env.create_org("keys")
    w = await _world(api_env.admin_engine, org)
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    post = api_env.client.post

    x = await post(SCHEDULES, json=_schedule(w, w.x, "Nightly"), headers=admin)
    y = await post(SCHEDULES, json=_schedule(w, w.y, "Nightly"), headers=admin)
    assert (x.status_code, y.status_code) == (201, 201), (x.text, y.text)

    # The same client: the route's existing refusal (422), as before 8b.
    again = await post(SCHEDULES, json=_schedule(w, w.x, "Nightly"), headers=admin)
    assert again.status_code == 422, again.text
    assert again.json()["detail"] == "a schedule with that name already exists"

    # A technician limited to X, a name only Y uses: created, not a 500.
    theirs = await post(SCHEDULES, json=_schedule(w, w.y, "Weekly"), headers=admin)
    assert theirs.status_code == 201, theirs.text
    tech = await everything_on(api_env, org, w.x.client)
    mine = await post(SCHEDULES, json=_schedule(w, w.x, "Weekly"), headers=tech)
    assert mine.status_code == 201, mine.text

    # Renaming: into a name X holds is refused; into one only Y holds is not.
    other = await post(SCHEDULES, json=_schedule(w, w.x, "Hourly"), headers=tech)
    assert other.status_code == 201, other.text
    path = f"{SCHEDULES}/{other.json()['id']}"
    taken = await api_env.client.patch(path, json={"name": "Nightly"}, headers=tech)
    assert taken.status_code == 422, taken.text
    only_y = await post(SCHEDULES, json=_schedule(w, w.y, "Daily"), headers=admin)
    assert only_y.status_code == 201, only_y.text
    renamed = await api_env.client.patch(path, json={"name": "Daily"}, headers=tech)
    assert renamed.status_code == 200, renamed.text


async def test_a_schedule_name_lost_to_a_race_is_the_routes_refusal(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is what holds when two creates race past the check: with the
    check passing (as the race loser's does), its 23505 answers 422 with the
    check's sentence -- not a 500."""
    org = await api_env.create_org("keys")
    w = await _world(api_env.admin_engine, org)
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    first = await api_env.client.post(SCHEDULES, json=_schedule(w, w.x, "Race"), headers=admin)
    assert first.status_code == 201, first.text

    async def never_taken(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(script_policies, "_name_taken", never_taken)
    lost = await api_env.client.post(SCHEDULES, json=_schedule(w, w.x, "Race"), headers=admin)
    assert lost.status_code == 422, lost.text
    assert lost.json()["detail"] == "a schedule with that name already exists"
    other = await api_env.client.post(SCHEDULES, json=_schedule(w, w.x, "Other"), headers=admin)
    path = f"{SCHEDULES}/{other.json()['id']}"
    renamed = await api_env.client.patch(path, json={"name": "Race"}, headers=admin)
    assert renamed.status_code == 422, renamed.text


# --------------------------------------------------------------------------- #
# push_host_key (O, through the device): an address names a host only within
# one client's network
# --------------------------------------------------------------------------- #


async def _observe(app_engine: AsyncEngine, w: World, device: UUID, fingerprint: str) -> None:
    """What the gateway does with a probe's result, under scope all."""
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        await host_key_service.record_observation(
            session,
            w.org,
            address=ADDRESS,
            transport="ssh",
            fingerprint=fingerprint,
            key_type="ssh-ed25519",
            device_id=device,
            job_id=None,
        )


async def _host_keys(admin_engine: AsyncEngine, org: UUID) -> dict[UUID | None, PushHostKey]:
    async with AsyncSession(admin_engine) as s:
        rows = (
            (await s.execute(select(PushHostKey).where(PushHostKey.org_id == org)))
            .scalars()
            .all()
        )
    return {row.client_id: row for row in rows}


async def test_two_clients_hosts_at_one_address_keep_their_own_keys(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """X's and Y's machines both answer 10.88.0.5 on their own networks. Y's
    probe used to land on X's row: overwrite X's observation and move the row
    -- X's confirmation with it -- into Y. Each is its own row now, and each
    client's push reads its own."""
    w = await _world(admin_engine)
    await _observe(app_engine, w, w.x.device, "SHA256:x")
    await _observe(app_engine, w, w.y.device, "SHA256:y")
    await _observe(app_engine, w, w.x.device, "SHA256:x")  # re-probed: updated, not doubled

    rows = await _host_keys(admin_engine, w.org)
    assert set(rows) == {w.x.client, w.y.client}
    assert rows[w.x.client].observed_fingerprint == "SHA256:x"
    assert rows[w.x.client].device_id == w.x.device
    assert rows[w.y.client].observed_fingerprint == "SHA256:y"

    x_only = ClientScope.only([w.x.client])
    async with org_session(w.org, engine=app_engine, clients=x_only) as session:
        await host_key_service.confirm_host_key(
            session, w.org, actor_id=w.actor, address=ADDRESS, fingerprint="SHA256:x"
        )
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        x = await host_key_service.confirmed_key_for(
            session, w.org, ADDRESS, device_id=w.x.device
        )
        y = await host_key_service.confirmed_key_for(
            session, w.org, ADDRESS, device_id=w.y.device
        )
    assert x == ("SHA256:x", None)
    assert y == (None, "host_key_unconfirmed")


async def test_a_narrowed_technician_confirms_their_clients_key(api_env: Any) -> None:
    """X's technician confirms the key at an address Y's machine also has: 201,
    X's row, and Y's is untouched. An every-client admin must say which
    client's key it is (`X-Client-Id`): the address alone names two hosts."""
    org = await api_env.create_org("keys")
    w = await _world(api_env.admin_engine, org)
    await _observe(api_env.app_engine, w, w.x.device, "SHA256:x")
    await _observe(api_env.app_engine, w, w.y.device, "SHA256:y")

    tech = await everything_on(api_env, org, w.x.client)
    body = {"address": ADDRESS, "fingerprint": "SHA256:x"}
    mine = await api_env.client.post(CONFIRM, json=body, headers=tech)
    assert mine.status_code == 201, mine.text
    rows = await _host_keys(api_env.admin_engine, org)
    assert rows[w.x.client].confirmed_fingerprint == "SHA256:x"
    assert rows[w.y.client].confirmed_fingerprint is None

    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    theirs = {"address": ADDRESS, "fingerprint": "SHA256:y"}
    which = await api_env.client.post(CONFIRM, json=theirs, headers=admin)
    assert which.status_code == 409, which.text
    assert "more than one client" in which.text
    spanish = await api_env.client.post(
        CONFIRM, json=theirs, headers={**admin, "Accept-Language": "es"}
    )
    assert "más de un cliente" in spanish.text, spanish.text
    named = await api_env.client.post(
        CONFIRM, json=theirs, headers={**admin, "X-Client-Id": str(w.y.client)}
    )
    assert named.status_code == 201, named.text
    rows = await _host_keys(api_env.admin_engine, org)
    assert rows[w.y.client].confirmed_fingerprint == "SHA256:y"


def _global_key(w: World) -> PushHostKey:
    """A host key with no device: a global row (O), confirmed by an admin --
    legacy data, since every probe now records its device."""
    return PushHostKey(
        org_id=w.org,
        address=ADDRESS,
        transport="ssh",
        observed_fingerprint="SHA256:g",
        observed_key_type="ssh-ed25519",
        observed_at=_now(),
        confirmed_fingerprint="SHA256:g",
        confirmed_at=_now(),
        confirmed_by=w.actor,
    )


async def _served(app_engine: AsyncEngine, w: World, device: UUID) -> tuple[Any, Any]:
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        return await host_key_service.confirmed_key_for(
            session, w.org, ADDRESS, device_id=device
        )


async def test_a_global_host_key_serves_a_client_without_its_own(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A row with no device is global: every client's configuration (O). It
    answers a push to any client that has no row of its own at the address,
    and a client's own row -- even unconfirmed, even observing the same key --
    takes precedence over it for that client and no other."""
    w = await _world(admin_engine)
    await _add(admin_engine, _global_key(w))
    assert await _served(app_engine, w, w.x.device) == ("SHA256:g", None)
    assert await _served(app_engine, w, w.y.device) == ("SHA256:g", None)

    await _observe(app_engine, w, w.x.device, "SHA256:g")
    assert await _served(app_engine, w, w.x.device) == (None, "host_key_unconfirmed")
    assert await _served(app_engine, w, w.y.device) == ("SHA256:g", None)


_KEY_COLUMNS = (
    "device_id",
    "client_id",
    "transport",
    "observed_fingerprint",
    "observed_key_type",
    "observed_at",
    "observed_job_id",
    "readiness",
    "confirmed_fingerprint",
    "confirmed_at",
    "confirmed_by",
)


@pytest.mark.parametrize("transport", ["ssh", "winrm"])
async def test_a_clients_probe_leaves_the_global_host_key_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, transport: str
) -> None:
    """Task 8b review I1. A probe of X's machine at an address that has a
    global row writes X's OWN row, twice over (created, then updated), and
    never claims, rewrites or moves the global one: claiming it took it from
    every other client it served, told them some other client's machine at
    that address had been probed, and handed X a confirmation nobody made for
    X. A global row changes only under scope all, by a person."""
    w = await _world(admin_engine)
    shared = _global_key(w)
    await _add(admin_engine, shared)
    before = await _host_keys(admin_engine, w.org)
    snapshot = tuple(getattr(before[None], c) for c in _KEY_COLUMNS)

    for fingerprint in ("SHA256:x1", "SHA256:x2"):
        async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
            if transport == "ssh":
                await host_key_service.record_observation(
                    session,
                    w.org,
                    address=ADDRESS,
                    transport="ssh",
                    fingerprint=fingerprint,
                    key_type="ssh-ed25519",
                    device_id=w.x.device,
                    job_id=None,
                )
            else:
                await host_key_service.record_readiness(
                    session,
                    w.org,
                    address=ADDRESS,
                    device_id=w.x.device,
                    job_id=None,
                    readiness={"winrm_https": True, "winrm_http": False, "kdc_reachable": True},
                )

    rows = await _host_keys(admin_engine, w.org)
    assert set(rows) == {None, w.x.client}
    assert rows[None].id == shared.id
    assert tuple(getattr(rows[None], c) for c in _KEY_COLUMNS) == snapshot
    mine = rows[w.x.client]
    assert mine.device_id == w.x.device and mine.confirmed_fingerprint is None
    observed = "SHA256:x2" if transport == "ssh" else host_key_service.WINRM_OBSERVATION
    assert (mine.transport, mine.observed_fingerprint) == (transport, observed)


def test_a_push_says_whose_host_key_it_reads() -> None:
    """Task 8b review M2: `confirmed_key_for` has no default device. None is
    the global row only; a caller that forgets the device must not silently
    read it."""
    parameter = inspect.signature(host_key_service.confirmed_key_for).parameters["device_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


async def test_the_host_key_audit_names_the_row_and_its_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Task 8b review M3. Two clients' rows share an address, and the events'
    target is the address: each event names the row it wrote and its client."""
    w = await _world(admin_engine)
    await _observe(app_engine, w, w.x.device, "SHA256:x")
    async with org_session(
        w.org, engine=app_engine, clients=ClientScope.only([w.x.client])
    ) as session:
        await host_key_service.confirm_host_key(
            session, w.org, actor_id=w.actor, address=ADDRESS, fingerprint="SHA256:x"
        )
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        await host_key_service.record_readiness(
            session,
            w.org,
            address=ADDRESS,
            device_id=w.y.device,
            job_id=None,
            readiness={"winrm_https": True, "winrm_http": True, "kdc_reachable": True},
        )
    rows = await _host_keys(admin_engine, w.org)
    async with AsyncSession(admin_engine) as s:
        events = {
            e.action: e.meta
            for e in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == w.org))
            ).scalars()
        }
    x = {"host_key_id": str(rows[w.x.client].id), "client_id": str(w.x.client)}
    y = {"host_key_id": str(rows[w.y.client].id), "client_id": str(w.y.client)}
    assert x.items() <= events["push.host_key_observed"].items()
    assert x.items() <= events["push.host_key_confirmed"].items()
    assert y.items() <= events["push.winrm_readiness_observed"].items()


# --------------------------------------------------------------------------- #
# The O tables written as global rows in phase 1: a client's row does not
# answer for the global one
# --------------------------------------------------------------------------- #


async def _add(admin_engine: AsyncEngine, *rows: Any) -> None:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        s.add_all(rows)
        await s.commit()


async def test_a_clients_offsite_target_does_not_take_a_global_name(api_env: Any) -> None:
    org = await api_env.create_org("keys")
    w = await _world(api_env.admin_engine, org)
    await _add(api_env.admin_engine, ROWS["uq_backup_offsite_targets_client_name"](w, w.x))
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    body = {
        "name": "Offsite",
        "bucket": "bkt",
        "access_key_id": "AKIA",
        "secret_access_key": "secret",
    }
    made = await api_env.client.post(OFFSITE, json=body, headers=admin)
    assert made.status_code == 201, made.text
    again = await api_env.client.post(OFFSITE, json=body, headers=admin)
    assert again.status_code == 409, again.text


async def test_an_offsite_target_name_lost_to_a_race_is_a_409(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await api_env.create_org("keys")
    admin = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    body = {"name": "Race", "bucket": "bkt", "access_key_id": "AKIA", "secret_access_key": "s"}
    first = await api_env.client.post(OFFSITE, json=body, headers=admin)
    assert first.status_code == 201, first.text
    other = await api_env.client.post(OFFSITE, json={**body, "name": "Other"}, headers=admin)
    assert other.status_code == 201, other.text

    async def never_taken(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(offsite_api, "target_name_taken", never_taken)
    lost = await api_env.client.post(OFFSITE, json=body, headers=admin)
    assert lost.status_code == 409, lost.text
    renamed = await api_env.client.put(
        f"{OFFSITE}/{other.json()['id']}",
        json={**{k: v for k, v in body.items() if "key" not in k}, "name": "Race"},
        headers=admin,
    )
    assert renamed.status_code == 409, renamed.text


async def test_a_clients_ignore_is_not_the_global_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    theirs = ROWS["uq_software_ignore_client_name"](w, w.x)
    await _add(admin_engine, theirs)
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        made = await ignore_software(session, org_id=w.org, actor_id=w.actor, name="Foo")
        again = await ignore_software(session, org_id=w.org, actor_id=w.actor, name="Foo")
    assert made.id != theirs.id and made.client_id is None
    assert again.id == made.id  # idempotent within its namespace


async def test_a_clients_recovery_key_does_not_block_the_global_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.backup import recovery

    w = await _world(admin_engine)
    raw = os.urandom(32)
    pasted = base64.b64encode(raw).decode()
    await _add(
        admin_engine,
        BackupRecoveryKey(
            org_id=w.org,
            client_id=w.x.client,
            public_key=pasted,
            fingerprint=recovery.fingerprint(raw),
        ),
    )
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        row, _ = await register_recovery_key(
            session, org_id=w.org, actor_id=w.actor, public_key_text=pasted, now=_now()
        )
        assert row.client_id is None
        with pytest.raises(RecoveryKeyError, match="already registered"):
            await register_recovery_key(
                session, org_id=w.org, actor_id=w.actor, public_key_text=pasted, now=_now()
            )

    # The index holds under a race, with the check's own sentence.
    async def never_registered(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(recovery, "_active_key_exists", never_registered)
    with pytest.raises(RecoveryKeyError, match="already registered"):
        async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
            await register_recovery_key(
                session, org_id=w.org, actor_id=w.actor, public_key_text=pasted, now=_now()
            )


async def test_a_clients_approval_is_not_the_global_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    theirs = ROWS["uq_patch_approvals_client_patch_ring"](w, w.x)
    await _add(admin_engine, theirs)
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        made = await approve_patch(
            session, org_id=w.org, actor_id=w.actor, patch_id=w.patch, ring_name="pilot"
        )
        again = await approve_patch(
            session, org_id=w.org, actor_id=w.actor, patch_id=w.patch, ring_name="pilot"
        )
    assert made.id != theirs.id and made.client_id is None
    assert again.id == made.id


async def test_patch_ingest_reads_the_global_approval_not_a_clients(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Task 8b review I2. The scan's auto-approval (PD-02) is global, like an
    operator's: a client's approval of the same (patch, ring) is another row.
    Taken for the global one, it skipped the global approval while the
    device's status was flipped to approved anyway; beside a global one it
    was two rows for `scalar_one_or_none`, a 500."""
    w = await _world(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = await s.get(Device, w.x.device)
        assert device is not None
        device.os_family = "windows"
        device.device_group_id = w.x.group
        patch = Patch(
            org_id=w.org,
            os_family="windows",
            source="windows_update",
            identifier="KB-auto",
            title="a security patch",
            severity="critical",
        )
        s.add_all(
            [
                patch,
                PatchPolicy(
                    org_id=w.org,
                    device_group_id=w.x.group,
                    ring_name=AUTO_APPROVE_RING,
                    auto_approve_security=True,
                ),
            ]
        )
        await s.flush()
        theirs = PatchApproval(
            org_id=w.org, client_id=w.x.client, patch_id=patch.id, ring_name=AUTO_APPROVE_RING
        )
        s.add(theirs)
        await s.commit()
    scan = PatchScanDocument(
        os_family="windows",
        scanned_at=_now().isoformat(),
        installed_identifiers=[],
        patches=[
            {
                "source": "windows_update",
                "identifier": "KB-auto",
                "title": "a security patch",
                "severity": "critical",
            }
        ],
    )
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        scanned = await session.get(Device, w.x.device)
        assert scanned is not None
        stats = await ingest_scan(session, scanned, scan)
    assert stats.auto_approved == 1
    async with AsyncSession(admin_engine) as s:
        approvals = {
            a.client_id: a
            for a in (
                await s.execute(select(PatchApproval).where(PatchApproval.patch_id == patch.id))
            ).scalars()
        }
    assert set(approvals) == {None, w.x.client}
    assert approvals[None].approved_by is None  # the system's
    assert approvals[w.x.client].id == theirs.id


async def test_a_clients_sla_policy_does_not_hold_the_global_level(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    await _add(admin_engine, ROWS["uq_sla_policy_client_wildcard_both"](w, w.x))
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        made = await create_policy(
            session,
            w.org,
            w.actor,
            calendar_id=w.calendar,
            response_target_minutes=30,
            resolution_target_minutes=240,
        )
        assert made.client_id is None
        with pytest.raises(SlaValidationError, match="already exists"):
            await create_policy(
                session,
                w.org,
                w.actor,
                calendar_id=w.calendar,
                response_target_minutes=30,
                resolution_target_minutes=240,
            )


async def test_a_clients_default_health_assignment_is_not_the_orgs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    await _add(admin_engine, ROWS["uq_health_assignment_client_org"](w, w.x))
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        made = await create_assignment(session, w.org, w.actor, profile_id=w.profile)
        assert made.client_id is None
        with pytest.raises(HealthValidationError, match="org-level"):
            await create_assignment(session, w.org, w.actor, profile_id=w.profile)


async def test_a_clients_every_device_gate_is_not_the_orgs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    theirs = ROWS["uq_change_gates_client_group"](w, w.x)
    await _add(admin_engine, theirs)
    async with org_session(w.org, engine=app_engine, clients=ClientScope.all()) as session:
        made = await set_gate(
            session,
            org_id=w.org,
            actor_id=w.actor,
            device_group_id=None,
            applies_to_patches=False,
            applies_to_deployments=True,
        )
        again = await set_gate(
            session,
            org_id=w.org,
            actor_id=w.actor,
            device_group_id=None,
            applies_to_patches=True,
            applies_to_deployments=True,
        )
    assert made.id != theirs.id and again.id == made.id
    async with AsyncSession(admin_engine) as s:
        x = await s.get(ChangeGate, theirs.id)
    assert x is not None and (x.applies_to_patches, x.applies_to_deployments) == (True, False)

"""`RunStartRequest.wake_on_lan` stops being silently dropped (spec §13.5,
§12.3.4).

**THE DEFECT THIS FIXES.** `provisioning/schema.py` accepted
`wake_on_lan: dict[str, Any] | None`; `start_provision_run` had no such
parameter, `api.py` never passed it, and `dispatch.py` hard-coded
`wake_on_lan={}` into the prepare envelope. An operator could request a wake and
NOTHING HAPPENED — no job, no error, no log line. The fix is `site.wake` plus
`wake_via_site_server`, and it is the one piece of the provisioning work spec
§12.3.4 says v1 can deliver.

**TWO DELIBERATE BEHAVIOUR CHANGES, BOTH TESTED IN BOTH DIRECTIONS**, because
each can break a caller that "worked" before:

1. no site server serves the segment => a refusal BEFORE the run is created.
   Starting the run anyway and not waking would reproduce the very defect being
   fixed, one layer down.
2. `wake_on_lan` additionally requires `site.wake`. Letting `provisioning.run`
   confer LAN-wide wake for free would collapse §10.2's separation through a
   side door.

**WHY THE ORIGINAL SIX TESTS IN THIS FILE COULD NOT SEE H3, AND WHAT REPLACED
THEM.** Every one of them seeded AT MOST ONE site server and none passed
`segment`. Under that fixture `list_site_servers(segment=None)` and
`list_site_servers(segment=<anything>)` return the same one-element list, and
`candidates[0]`, `candidates[-1]` and `random.choice(candidates)` are the same
value — so deleting the selection's `segment=` argument was a NO-OP MUTATION and
every assertion still passed. The tests could not distinguish "picked the right
site server" from "picked the only site server", which is fixture blindness in
its purest form: the fixture makes the guarded and unguarded paths observably
identical.

`_seed_ambiguous_fleet` below is built to destroy that. It seeds FOUR site
servers whose correct answer is last by creation order, last by id and last by
name, so that every incidental ordering a careless implementation might rely on
yields a DIFFERENT, NAMEABLE, WRONG value. The mutations and their exact wrong
values are recorded on each test.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.jobs.engine import JobEngine
from app.modules.provisioning.dispatch import start_provision_run
from app.modules.provisioning.repository import (
    ProvisioningPermissionError,
    ProvisioningValidationError,
)
from app.modules.provisioning.schema import ProvisionWakeRequest
from app.modules.site_server.errors import SiteServerValidationError
from app.modules.site_server.handler import register_job_types
from tests.ipam_fixtures import seed_agent, seed_device
from tests.site_server_fixtures import seed_site_server
from tests.test_provisioning_dispatch import _make_profile, _object_store

register_job_types()

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

#: Seed time for the ambiguous fleet. Explicit, and BEFORE `now()`, so creation
#: order is a fact the fixture states rather than a side effect of how fast the
#: inserts happened to run.
SEEDED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


async def _org_user_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """An org, a user holding `provisioning.run` AND `site.wake`, and a device.

    Roles are granted explicitly rather than via a seeded role name, because the
    permission split under test needs principals that no seeded role produces.
    """
    from app.models import Device, Organization, User

    slug = f"pwake-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        device = Device(org_id=org.id, hostname=f"host-{slug}", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, user.id, device.id


async def _grant(admin_engine: AsyncEngine, org_id: UUID, user_id: UUID, *codes: str) -> None:
    async with AsyncSession(admin_engine) as session:
        role_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO roles (id, org_id, name, description)"
                " VALUES (:id, :org, :name, '')"
            ),
            {"id": role_id, "org": org_id, "name": f"r-{uuid4().hex[:8]}"},
        )
        for code in codes:
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id)"
                    " SELECT :role, id FROM permissions WHERE code = :code"
                ),
                {"role": role_id, "code": code},
            )
        await session.execute(
            text(
                "INSERT INTO user_roles (id, org_id, user_id, role_id)"
                " VALUES (gen_random_uuid(), :org, :user, :role)"
            ),
            {"org": org_id, "user": user_id, "role": role_id},
        )
        await session.commit()


async def _counts(engine: AsyncEngine, org_id: UUID) -> tuple[int, int]:
    """`(provision runs, site.wake jobs)` — the pair every test below asserts.

    A pair, because "the run exists" and "the wake exists" are the two facts
    that came apart in the original defect, and asserting either alone cannot
    see it.
    """
    async with AsyncSession(engine) as session:
        runs = (
            await session.execute(
                text("SELECT count(*) FROM provision_run WHERE org_id = :org"),
                {"org": org_id},
            )
        ).scalar_one()
        jobs = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE org_id = :org AND type = 'site.wake'"
                ),
                {"org": org_id},
            )
        ).scalar_one()
    return int(runs), int(jobs)


async def _facts(engine: AsyncEngine, org_id: UUID) -> tuple[int, int, str | None, str | None]:
    """`(runs, wakes, broadcast, woken-through device)` — THE STATE OF THE WORLD.

    Four observable facts rather than "an exception was or was not raised",
    because H3's failure mode raises nothing: the caller gets a 202, the audit
    log records a dispatched wake, and the packet lands on the wrong LAN. The
    only things that differ between a correct and a wrong selection are the
    broadcast address in the signed params and which device the job was aimed
    at, so both are in the tuple.
    """
    runs, wakes = await _counts(engine, org_id)
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT target_id::text AS target, params->>'broadcast' AS broadcast"
                    " FROM jobs WHERE org_id = :org AND type = 'site.wake'"
                    " ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"org": org_id},
            )
        ).one_or_none()
    if row is None:
        return runs, wakes, None, None
    return runs, wakes, row.broadcast, row.target


async def _seed_ambiguous_fleet(
    admin_engine: AsyncEngine, org_id: UUID
) -> dict[str, UUID]:
    """FOUR site servers, arranged so every incidental ordering is WRONG.

    The target is `10.20.7.5`. Rows are seeded with explicit `created_at`, `id`
    and `name`, oldest/lowest/first-alphabetically to newest/highest/last:

    | # | name           | segments        | enabled | serves 10.20.7.5?   |
    |---|----------------|-----------------|---------|---------------------|
    | 1 | `aaa-elsewhere`| `10.30.0.0/16`  | yes     | NO                  |
    | 2 | `bbb-supernet` | `10.20.0.0/16`  | yes     | yes, masklen 16     |
    | 3 | `ccc-off`      | `10.20.7.0/24`  | **no**  | yes, masklen 24     |
    | 4 | `zzz-onwire`   | `10.20.7.0/24`  | yes     | yes, masklen 24     |

    **THE CORRECT ANSWER IS #4, AND IT IS LAST BY ALL THREE INCIDENTAL ORDERS.**
    That is the whole point: each of the three selection terms, deleted, names a
    different one of the other three rows, so each mutation produces a distinct
    and stateable wrong value rather than the same value by a different route.

    * drop `enabled=True`      -> #3, a DISABLED server (`10.20.7.255`, refused)
    * drop `segment=`          -> #1, a server on another wire (refused)
    * drop the specificity sort-> #2, `10.20.255.255` — THE SILENT WRONG LAN

    Each site server is its own device with its own agent, because
    `wake_via_site_server` targets `site_server.device_id`: sharing one device
    would make "which server was chosen" unobservable in the job row, which is
    the same blindness one level down.
    """
    ids: dict[str, UUID] = {}
    # Unique per call (the ids are a PRIMARY KEY and this fixture is used by
    # several tests), but ordered WITHIN the call by the trailing ordinal.
    prefix = uuid4().hex[:8]
    plan = [
        ("aaa-elsewhere", "10.30.0.0/16", True, 1),
        ("bbb-supernet", "10.20.0.0/16", True, 2),
        ("ccc-off", "10.20.7.0/24", False, 3),
        ("zzz-onwire", "10.20.7.0/24", True, 4),
    ]
    for name, segment, enabled, ordinal in plan:
        device_id = await seed_device(admin_engine, org_id, hostname=name)
        await seed_agent(admin_engine, org_id, device_id)
        await seed_site_server(
            admin_engine,
            org_id,
            device_id=device_id,
            segments=[segment],
            enabled=enabled,
            name=name,
            # Ascending id AND ascending created_at, both matching the
            # alphabetical name order, so "first row by any ordering" is #1.
            row_id=UUID(f"{prefix}-0000-4000-8000-{ordinal:012d}"),
            created_at=SEEDED_AT + timedelta(minutes=ordinal),
        )
        ids[name] = device_id
    return ids


async def _start(
    app_engine: AsyncEngine,
    tmp_path: Path,
    *,
    org_id: UUID,
    actor_id: UUID,
    vault: LocalVaultService,
    wake_on_lan: ProvisionWakeRequest | None,
) -> tuple[Any, dict[str, Any] | None]:
    with mock_aws():
        object_store = _object_store(tmp_path)
        async with org_session(org_id, engine=app_engine) as session:
            profile = await _make_profile(session, vault, org_id, actor_id)
            await session.commit()
            profile_id = profile.id

        async with org_session(org_id, engine=app_engine) as session:
            run, envelope = await start_provision_run(
                session,
                object_store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref=None,
                now=NOW,
                actor_id=actor_id,
                actor_type="user",
                wake_on_lan=wake_on_lan,
                job_engine=JobEngine(
                    signer=LocalEd25519Signer(tmp_path / "job-signing.key")
                ),
            )
            await session.commit()
            return run, envelope


# --------------------------------------------------------------------------- #
# The fix: a requested wake reaches a real job
# --------------------------------------------------------------------------- #


async def test_a_requested_wake_now_produces_a_signed_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**THE ASSERTION IS `(runs, wakes) == (1, 1)`.**

    Before this change it was `(1, 0)` — the run started and the wake vanished
    with no error. Counting only runs cannot see that, and counting only jobs
    cannot see the refusal case below.
    """
    org_id, user_id, device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    await seed_agent(admin_engine, org_id, device_id)
    await seed_site_server(
        admin_engine, org_id, device_id=device_id, segments=["10.20.7.0/24"]
    )
    vault = LocalVaultService(tmp_path / "root.key")

    run, envelope = await _start(
        app_engine,
        tmp_path,
        org_id=org_id,
        actor_id=user_id,
        vault=vault,
        wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
    )

    assert await _counts(admin_engine, org_id) == (1, 1)
    assert run.state == "awaiting_enrollment"
    # The envelope is RETURNED, not published: the route publishes it after the
    # `org_session` block exits.
    assert envelope is not None
    assert envelope["job"]["params"]["broadcast"] == "10.20.7.255"
    assert envelope["job"]["device"] == str(device_id)
    assert envelope["sig"]


async def test_no_wake_requested_means_no_job_and_still_a_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The unchanged path. `(1, 0)` here is CORRECT, and it is the same tuple
    the broken behaviour produced when a wake WAS requested — which is exactly
    why the test above asserts the pair rather than either half."""
    org_id, user_id, device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    await seed_agent(admin_engine, org_id, device_id)
    await seed_site_server(
        admin_engine, org_id, device_id=device_id, segments=["10.20.7.0/24"]
    )
    vault = LocalVaultService(tmp_path / "root.key")

    run, envelope = await _start(
        app_engine, tmp_path, org_id=org_id, actor_id=user_id, vault=vault, wake_on_lan=None
    )

    assert (await _counts(admin_engine, org_id), envelope) == ((1, 0), None)
    assert run.state == "awaiting_enrollment"


# --------------------------------------------------------------------------- #
# H3: the SELECTION, with a fixture in which selecting wrongly is observable
# --------------------------------------------------------------------------- #


async def test_the_wake_goes_to_the_site_server_that_serves_the_target(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**FOUR site servers, and only one right answer.**

    `_seed_ambiguous_fleet`'s correct row is last by creation order, last by id
    and last by name, so `candidates[0]` under any incidental ordering is a
    different, wrong server. The asserted tuple is the STATE OF THE WORLD —
    `(runs, wakes, broadcast, device)` — because H3's failure raises nothing:
    the wake succeeds, it just lands on somebody else's LAN.

    MUTATIONS AND THE EXACT WRONG VALUE EACH PRODUCES (dispatch.py
    `_resolve_wake` / registry.py `list_site_servers`):

    * drop the specificity `order_by` -> `(1, 1, '10.20.255.255', bbb-supernet)`.
      A packet on the /16's broadcast, through a server that is not on the /24.
      **Silent** — no exception, a 202, an audit row saying the device was woken.
    * drop `segment=wake.segment`     -> `(0, 0, None, None)` and a
      `SiteServerValidationError`: `aaa-elsewhere` is chosen and refuses, since
      the SQL containment filter and `_resolve_broadcast` use the SAME notion of
      "serves". That agreement is why the loud case is loud, and it is also
      exactly why the `segment is None` case below is the silent one.
    * drop `enabled=True`             -> `(0, 0, None, None)` and a
      `SiteServerValidationError`: `ccc-off`, disabled and equally specific but
      older, wins the tie.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    devices = await _seed_ambiguous_fleet(admin_engine, org_id)
    vault = LocalVaultService(tmp_path / "root.key")

    _run, envelope = await _start(
        app_engine,
        tmp_path,
        org_id=org_id,
        actor_id=user_id,
        vault=vault,
        wake_on_lan=ProvisionWakeRequest(
            target_mac="a4:bb:6d:11:22:33", segment="10.20.7.5"
        ),
    )

    assert await _facts(admin_engine, org_id) == (
        1,
        1,
        "10.20.7.255",
        str(devices["zzz-onwire"]),
    )
    # The signed envelope carries the same two facts: a job row that agreed with
    # the fixture while the SIGNED bytes named another device would be worse
    # than either mistake alone.
    assert envelope is not None
    assert (envelope["job"]["device"], envelope["job"]["params"]["broadcast"]) == (
        str(devices["zzz-onwire"]),
        "10.20.7.255",
    )


async def test_an_unnamed_segment_is_refused_rather_than_guessed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**THIS IS H3's SILENT CASE, AND IT IS THE ONE THAT SHIPPED.**

    `ProvisionWakeRequest.segment` defaults to `None`, `list_site_servers` then
    applies NO containment filter, and every enabled site server in the org
    matches. Picking `candidates[0]` out of that set is picking whichever row was
    promoted first — a fact about the operator's afternoon, not about the
    topology — and `_resolve_broadcast` cannot catch it, because the server it is
    handed declares exactly one segment and resolving it is unambiguous. The
    caller gets a 202 and the packet goes to the wrong LAN.

    MUTATION — delete the `wake.segment is None and len(candidates) > 1` refusal:
    `(1, 1, '10.30.255.255', aaa-elsewhere)`. A wake, successfully dispatched,
    onto a /16 that has nothing to do with the target, with no error anywhere.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    await _seed_ambiguous_fleet(admin_engine, org_id)
    vault = LocalVaultService(tmp_path / "root.key")

    refused = False
    try:
        await _start(
            app_engine,
            tmp_path,
            org_id=org_id,
            actor_id=user_id,
            vault=vault,
            wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
        )
    except ProvisioningValidationError:
        refused = True

    assert (refused, await _facts(admin_engine, org_id)) == (True, (0, 0, None, None))


async def test_a_segment_no_site_server_serves_is_refused_with_others_present(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The refusal must survive a POPULATED registry, not just an empty one.

    `test_no_serving_site_server_refuses_and_creates_no_run` seeds no site server
    at all, so it passes against a selection that ignores `segment` entirely —
    an empty table is empty however you filter it. Here four site servers exist
    and none of them serves `192.168.99.7`, which is the case that separates
    "there is nothing to choose" from "there is plenty to choose and none of it
    is right".

    MUTATION — delete `segment=wake.segment`: `(1, 1, '10.30.255.255',
    aaa-elsewhere)`. The unfiltered list is non-empty, `candidates[0]` is the
    oldest row, its single declared segment resolves without ambiguity, and a
    machine on a completely unrelated network is woken.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    await _seed_ambiguous_fleet(admin_engine, org_id)
    vault = LocalVaultService(tmp_path / "root.key")

    refused = False
    try:
        await _start(
            app_engine,
            tmp_path,
            org_id=org_id,
            actor_id=user_id,
            vault=vault,
            wake_on_lan=ProvisionWakeRequest(
                target_mac="a4:bb:6d:11:22:33", segment="192.168.99.7"
            ),
        )
    except ProvisioningValidationError:
        refused = True

    assert (refused, await _facts(admin_engine, org_id)) == (True, (0, 0, None, None))


async def test_a_disabled_site_server_does_not_win_the_specificity_tie(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`enabled=True` is load-bearing, not decorative.

    `ccc-off` and `zzz-onwire` declare the SAME `10.20.7.0/24`, so specificity
    cannot separate them; `ccc-off` is older and would win the `created_at` tie
    if it were a candidate at all. The only thing keeping it out is
    `enabled=True`.

    MUTATION — delete `enabled=True` from the `list_site_servers` call:
    `SiteServerValidationError('site server ... is disabled')` and
    `(0, 0, None, None)` in place of the correct
    `(1, 1, '10.20.7.255', zzz-onwire)`. Loud, but still the wrong server.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    devices = await _seed_ambiguous_fleet(admin_engine, org_id)
    vault = LocalVaultService(tmp_path / "root.key")

    await _start(
        app_engine,
        tmp_path,
        org_id=org_id,
        actor_id=user_id,
        vault=vault,
        wake_on_lan=ProvisionWakeRequest(
            target_mac="a4:bb:6d:11:22:33", segment="10.20.7.5"
        ),
    )

    assert await _facts(admin_engine, org_id) == (
        1,
        1,
        "10.20.7.255",
        str(devices["zzz-onwire"]),
    )


async def test_the_selection_never_hands_a_server_a_segment_it_cannot_resolve(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The two layers agree, and the agreement is asserted rather than assumed.

    `SiteServerValidationError` escaping `start_provision_run` means
    `_resolve_wake` handed `wake_via_site_server` a server that could not resolve
    the segment — i.e. the selection layer and `_resolve_broadcast` disagree
    about what "serves this segment" means. For every segment the fleet does
    serve, that must never happen, and the run must complete.

    This is the guard that would catch someone "fixing" the selection by relaxing
    the containment filter (`LIKE`, a prefix match, equality on the string form)
    while the tests above still pass on their one happy address.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    devices = await _seed_ambiguous_fleet(admin_engine, org_id)
    vault = LocalVaultService(tmp_path / "root.key")

    # (requested segment, expected broadcast, expected serving device)
    cases = [
        ("10.20.7.5", "10.20.7.255", "zzz-onwire"),
        ("10.20.7.0/24", "10.20.7.255", "zzz-onwire"),
        ("10.20.30.9", "10.20.255.255", "bbb-supernet"),
        ("10.30.1.4", "10.30.255.255", "aaa-elsewhere"),
    ]
    observed: list[tuple[str, str | None, str | None]] = []
    for segment, _broadcast, _server in cases:
        try:
            await _start(
                app_engine,
                tmp_path,
                org_id=org_id,
                actor_id=user_id,
                vault=vault,
                wake_on_lan=ProvisionWakeRequest(
                    target_mac="a4:bb:6d:11:22:33", segment=segment
                ),
            )
        except SiteServerValidationError as exc:  # pragma: no cover - the failure
            observed.append((segment, f"unresolvable: {exc}", None))
            continue
        _runs, _wakes, broadcast, target = await _facts(admin_engine, org_id)
        observed.append((segment, broadcast, target))

    assert observed == [
        (segment, broadcast, str(devices[server])) for segment, broadcast, server in cases
    ]


# --------------------------------------------------------------------------- #
# Behaviour change 1: no serving site server => refusal, and NO run
# --------------------------------------------------------------------------- #


async def test_no_serving_site_server_refuses_and_creates_no_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**BOTH counts are zero**, and that is the deliberate break.

    A caller who passes `wake_on_lan` today gets a run and no wake. After this
    they may get a 422 and no run at all. That is the correct failure: the
    alternative — start the run, skip the wake — is the defect being fixed,
    reproduced one layer down, and the operator would again have no way to know.

    The refusal happens BEFORE `ProvisionRun` is constructed, so there is no
    half-provisioned run to clean up.
    """
    org_id, user_id, _device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    vault = LocalVaultService(tmp_path / "root.key")

    refused = False
    try:
        await _start(
            app_engine,
            tmp_path,
            org_id=org_id,
            actor_id=user_id,
            vault=vault,
            wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
        )
    except ProvisioningValidationError:
        refused = True

    assert (refused, await _counts(admin_engine, org_id)) == (True, (0, 0))


async def test_a_demoted_site_server_stops_serving_the_wake(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The same org, before and after demotion, is the discriminating pair: with
    a site server `(1, 1)`, with it gone the run is refused entirely."""
    org_id, user_id, device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run", "site.wake")
    await seed_agent(admin_engine, org_id, device_id)
    await seed_site_server(
        admin_engine, org_id, device_id=device_id, segments=["10.20.7.0/24"]
    )
    vault = LocalVaultService(tmp_path / "root.key")

    await _start(
        app_engine,
        tmp_path,
        org_id=org_id,
        actor_id=user_id,
        vault=vault,
        wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
    )
    assert await _counts(admin_engine, org_id) == (1, 1)

    async with AsyncSession(admin_engine) as session:
        await session.execute(text("DELETE FROM site_server WHERE org_id = :org"), {"org": org_id})
        await session.commit()

    refused = False
    try:
        await _start(
            app_engine,
            tmp_path,
            org_id=org_id,
            actor_id=user_id,
            vault=vault,
            wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
        )
    except ProvisioningValidationError:
        refused = True

    assert (refused, await _counts(admin_engine, org_id)) == (True, (1, 1))


# --------------------------------------------------------------------------- #
# Behaviour change 2: wake_on_lan additionally requires site.wake
# --------------------------------------------------------------------------- #


async def test_provisioning_run_alone_may_start_a_run_but_not_wake(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**THE SAME PRINCIPAL, THE SAME ROUTE, TWO DIFFERENT OUTCOMES.**

    A holder of `provisioning.run` WITHOUT `site.wake` gets a run when
    `wake_on_lan` is absent and a refusal when it is present. Both halves are
    required: the refusal alone would also be produced by a principal who simply
    cannot provision, and the success alone says nothing about the gate.

    In practice `technician` holds both, so no seeded role loses anything — which
    is precisely why this needs a bespoke grant to test at all, and why it would
    otherwise never have been noticed.
    """
    org_id, user_id, device_id = await _org_user_device(admin_engine)
    await _grant(admin_engine, org_id, user_id, "provisioning.run")
    await seed_agent(admin_engine, org_id, device_id)
    await seed_site_server(
        admin_engine, org_id, device_id=device_id, segments=["10.20.7.0/24"]
    )
    vault = LocalVaultService(tmp_path / "root.key")

    run, envelope = await _start(
        app_engine, tmp_path, org_id=org_id, actor_id=user_id, vault=vault, wake_on_lan=None
    )
    assert (run.state, envelope) == ("awaiting_enrollment", None)
    assert await _counts(admin_engine, org_id) == (1, 0)

    refused = False
    try:
        await _start(
            app_engine,
            tmp_path,
            org_id=org_id,
            actor_id=user_id,
            vault=vault,
            wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:33"),
        )
    except ProvisioningPermissionError:
        refused = True

    # Still exactly one run and no wake job: the refused attempt created
    # nothing, rather than starting a second run and skipping the wake.
    assert (refused, await _counts(admin_engine, org_id)) == (True, (1, 0))


async def test_the_permission_error_is_not_a_validation_error() -> None:
    """`ProvisioningPermissionError` must NOT subclass
    `ProvisioningValidationError`.

    The route maps the latter to 422. "Your request was malformed" is the wrong
    answer to "you may not do this", and if the classes were related the 403
    branch would be unreachable — `except ProvisioningValidationError` appears
    second and would never be reached, or first and would swallow it.
    """
    assert not issubclass(ProvisioningPermissionError, ProvisioningValidationError)
    assert not issubclass(ProvisioningValidationError, ProvisioningPermissionError)


# --------------------------------------------------------------------------- #
# The request model
# --------------------------------------------------------------------------- #


def test_wake_on_lan_is_a_typed_model_that_refuses_unknown_keys() -> None:
    """It was `dict[str, Any] | None`. Untyped, a misspelt `target_mac` is
    accepted and silently ignored — the same class of defect as dropping the
    whole field, one level down."""
    from pydantic import ValidationError

    from app.modules.provisioning.schema import RunStartRequest

    parsed = RunStartRequest(wake_on_lan={"target_mac": "a4:bb:6d:11:22:33"})
    assert parsed.wake_on_lan is not None
    assert parsed.wake_on_lan.target_mac == "a4:bb:6d:11:22:33"

    with pytest.raises(ValidationError):
        RunStartRequest(wake_on_lan={"target_mc": "a4:bb:6d:11:22:33"})


def test_start_provision_run_no_longer_takes_a_signer() -> None:
    """9b, asserted rather than reviewed for.

    The `signer` parameter existed only to feed a call whose result was thrown
    away. An unused parameter is a lie about what a function needs, and this one
    would have been the obvious place for somebody to "restore" the second
    signing path.
    """
    import inspect

    parameters = inspect.signature(start_provision_run).parameters
    assert "signer" not in parameters
    assert "job_engine" in parameters
    assert "wake_on_lan" in parameters

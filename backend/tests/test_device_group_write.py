"""Writing device groups -- cycle 1 of `docs/superpowers/specs/
2026-08-22-device-group-management-design.md`.

WHAT WAS BROKEN, and it is worse than "no create route". `device_groups` is
the platform's targeting and scoping primitive -- a deployment's `target_id`,
a patch/backup/remote policy's `device_group_id`, a health profile's binding,
and the scope on a `user_roles` RBAC grant all take a group id -- and NO code
path in `backend/app/` constructed one. There was no way to put a device INTO
a group either: `AssetCreateRequest` and `AssetUpdateRequest` do not carry
`device_group_id`, and the only writer was `inventory.rules.evaluate_rules`,
fed by a document nobody could author.

WHAT THIS CYCLE DELIBERATELY DOES NOT BUILD. No rules document is accepted:
the closed `GroupRules` model, its three validators, `POST /preview` and the
background re-evaluation sweep are cycle 2, which is where a mistake moves
machines. `POST` and `PATCH` refuse a `rules` field with a message naming it.

THE TENANCY TESTS HERE DRIVE THE REAL ROUTE ON THE BYPASSRLS ADMIN ENGINE
(idiom 3 of `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`).
Everything served through `org_session` runs as `app_user` (NOBYPASSRLS), so a
test that asks for a neighbour's group through the ordinary engine gets its
404 from RLS whether or not the application carries a predicate -- which is
exactly why the audit found that deleting one turns no route test red.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import (
    AuditEvent,
    BackupPolicy,
    BackupRepository,
    Credential,
    Device,
    DeviceGroup,
    HealthProfile,
    HealthProfileAssignment,
    PatchPolicy,
    Policy,
    RemotePolicy,
    Role,
    User,
    UserRole,
)
from app.modules.health.resolution import MAX_GROUP_DEPTH as HEALTH_MAX_GROUP_DEPTH
from app.services.device_groups import MAX_GROUP_DEPTH

GROUPS = "/api/v1/device-groups"


async def _admin(api_env: Any, org_id: UUID) -> dict[str, str]:
    return await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))


async def _create(
    api_env: Any, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    response = await api_env.client.post(GROUPS, headers=headers, json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _device(
    api_env: Any, org_id: UUID, hostname: str, group_id: UUID | None = None
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="windows",
            device_group_id=group_id,
        )
        session.add(device)
        await session.commit()
        return device.id


async def _group_row(api_env: Any, group_id: UUID) -> DeviceGroup:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(
                select(DeviceGroup).where(DeviceGroup.id == group_id)
            )
        ).scalar_one()


async def _audit_actions(api_env: Any, org_id: UUID) -> list[tuple[str, dict[str, Any]]]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.meta)
                .where(AuditEvent.org_id == org_id)
                .order_by(AuditEvent.id)
            )
        ).all()
    return [(action, meta) for action, meta in rows]


# --------------------------------------------------------------------------- #
# RBAC -- `devices.manage`, and no new permission code
# --------------------------------------------------------------------------- #


async def test_every_write_takes_devices_manage_and_technician_is_refused(
    api_env: Any,
) -> None:
    """`devices.manage` gates every write, and it is not a new code.

    Migration 0003 already seeds it to `super-admin` and `org-admin` alone, and
    its catalogue description has always read "Create, edit, GROUP, and
    decommission devices" -- the word was in it before any route used it.
    `e25bbe7` made adding a permission code a deliberate act; this cycle adds
    none.

    THE NEGATIVE CONTROL IS `technician`, NOT "no roles". `0062` gave
    technician `assets.manage` on the reasoning that recording the switch you
    are standing in front of is the ordinary work of the role. Creating or
    renaming a group re-binds patch, backup and remote policy for machines the
    technician is not standing in front of, and a group is an RBAC scope. A
    test that only checked an unprivileged caller would pass with the gate set
    to `assets.manage` by mistake.
    """
    org_id = await api_env.create_org("dgw")
    admin = await _admin(api_env, org_id)
    group = await _create(api_env, admin, name="servers")

    reader = await api_env.headers(
        org_id, await api_env.create_user(org_id, "read-only")
    )
    tech = await api_env.headers(
        org_id, await api_env.create_user(org_id, "technician")
    )
    nobody = await api_env.headers(org_id, await api_env.create_user(org_id))

    for headers in (reader, tech, nobody):
        assert (
            await api_env.client.post(GROUPS, headers=headers, json={"name": "x"})
        ).status_code == 403
        assert (
            await api_env.client.patch(
                f"{GROUPS}/{group['id']}", headers=headers, json={"name": "y"}
            )
        ).status_code == 403
        assert (
            await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
        ).status_code == 403
        assert (
            await api_env.client.post(
                f"{GROUPS}/{group['id']}/unarchive", headers=headers
            )
        ).status_code == 403
        assert (
            await api_env.client.post(
                f"{GROUPS}/{group['id']}/members",
                headers=headers,
                json={"device_ids": [str(uuid4())]},
            )
        ).status_code == 403
        assert (
            await api_env.client.delete(
                f"{GROUPS}/{group['id']}/members/{uuid4()}", headers=headers
            )
        ).status_code == 403

    # Non-vacuous: `read-only` holds `devices.read`, so the DETAIL route -- the
    # one read this cycle adds -- answers it.
    detail = await api_env.client.get(f"{GROUPS}/{group['id']}", headers=reader)
    assert detail.status_code == 200, detail.text


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


async def test_create_returns_the_group_and_a_duplicate_name_is_a_409(
    api_env: Any,
) -> None:
    """A duplicate live name is a 409 with a sentence, never a 500 out of an
    `IntegrityError` (§5.4)."""
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)

    created = await _create(
        api_env, headers, name="servers", description="the loud ones", precedence=3
    )
    assert created["name"] == "servers"
    assert created["precedence"] == 3
    assert created["device_count"] == 0
    assert created["dynamic"] is False
    assert created["archived_at"] is None

    clash = await api_env.client.post(GROUPS, headers=headers, json={"name": "servers"})
    assert clash.status_code == 409, clash.text
    assert "already exists" in clash.json()["detail"]


# `test_a_rules_document_is_refused_with_a_message_naming_cycle_two` LIVED HERE
# AND IS GONE, deliberately rather than by attrition. It pinned cycle 1's
# temporary 422 -- "membership rules are not accepted yet" -- on `POST` and
# `PATCH`, and cycle 2 is the cycle that accepts them. A refusal whose whole
# content was "not yet" cannot be carried forward once the answer is yes; what
# replaces it is `tests/test_device_group_rules.py`, which holds the far
# stronger property that the documents the EVALUATOR cannot run are still
# refused, at authorship, with the fact path named.


# --------------------------------------------------------------------------- #
# The hierarchy the schema does not constrain
# --------------------------------------------------------------------------- #


async def test_the_depth_bound_is_healths_bound(api_env: Any) -> None:
    """Asserted directly rather than imported.

    The kernel must not depend on a module (CLAUDE.md: "modules register into
    the kernel; they do not fork it"), so `services/device_groups.py` defines
    its own constant -- and two copies of a number are two chances to disagree.
    This is what keeps them equal.
    """
    del api_env
    assert MAX_GROUP_DEPTH == HEALTH_MAX_GROUP_DEPTH


async def test_self_parent_and_cycles_of_two_and_three_are_each_422(
    api_env: Any,
) -> None:
    """`parent_group_id` is a bare self-FK with no cycle constraint of any kind.

    `health/resolution.py` SURVIVES a cycle -- bounded walk, `logger.error`,
    fall through to the org default -- but surviving it means every device
    beneath the cycle silently resolves to the wrong profile. Refusing at write
    time is the fix; the health bound stays as the backstop it is.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    a = await _create(api_env, headers, name="a")
    b = await _create(api_env, headers, name="b", parent_group_id=a["id"])
    c = await _create(api_env, headers, name="c", parent_group_id=b["id"])

    itself = await api_env.client.patch(
        f"{GROUPS}/{a['id']}", headers=headers, json={"parent_group_id": a["id"]}
    )
    assert itself.status_code == 422, itself.text
    assert "own parent" in itself.json()["detail"]

    two = await api_env.client.patch(
        f"{GROUPS}/{a['id']}", headers=headers, json={"parent_group_id": b["id"]}
    )
    assert two.status_code == 422, two.text
    assert "cycle" in two.json()["detail"]

    three = await api_env.client.patch(
        f"{GROUPS}/{a['id']}", headers=headers, json={"parent_group_id": c["id"]}
    )
    assert three.status_code == 422, three.text


async def test_a_reparent_that_would_push_a_leaf_past_the_bound_is_refused(
    api_env: Any,
) -> None:
    """THE HALF A NAIVE DEPTH CHECK MISSES. Moving a SHALLOW subtree under a
    deep parent pushes its LEAVES past health's 16-level walk even though the
    group being moved is fine on its own.

    A chain fifteen deep, plus a two-level subtree, is seventeen. Refused. The
    same subtree under the fourteenth link is sixteen, which is allowed -- so
    the refusal is a bound and not a blanket.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)

    chain: list[str] = []
    parent: str | None = None
    for level in range(MAX_GROUP_DEPTH - 1):
        row = await _create(
            api_env, headers, name=f"level-{level:02d}", parent_group_id=parent
        )
        chain.append(row["id"])
        parent = row["id"]

    root = await _create(api_env, headers, name="subtree-root")
    await _create(api_env, headers, name="subtree-leaf", parent_group_id=root["id"])

    too_deep = await api_env.client.patch(
        f"{GROUPS}/{root['id']}", headers=headers, json={"parent_group_id": chain[-1]}
    )
    assert too_deep.status_code == 422, too_deep.text
    assert str(MAX_GROUP_DEPTH) in too_deep.json()["detail"]

    fits = await api_env.client.patch(
        f"{GROUPS}/{root['id']}", headers=headers, json={"parent_group_id": chain[-2]}
    )
    assert fits.status_code == 200, fits.text


async def test_a_parent_in_another_org_is_refused_on_the_admin_engine(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """THE TEST THAT CAN ACTUALLY FAIL (audit idiom 3).

    The self-FK is bare, exactly like `devices.device_group_id`, so the schema
    permits naming a neighbour's group as a parent -- application code is the
    only thing that refuses. Driven through the real route with
    `app.state.app_engine` repointed at the BYPASSRLS admin engine, so RLS is
    not there to mask a missing predicate.

    FALSIFIED: deleting `DeviceGroup.org_id == org_id` from
    `_assert_parent_is_legal`'s parent lookup turns this 422 into a 200 that
    writes a cross-tenant parent edge.
    """
    theirs = await api_env.create_org("dgw-theirs")
    mine = await api_env.create_org("dgw-mine")
    headers = await _admin(api_env, mine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        neighbour = DeviceGroup(org_id=theirs, name="their-parent")
        session.add(neighbour)
        await session.commit()
        neighbour_id = neighbour.id

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(
            GROUPS,
            headers=headers,
            json={"name": "mine", "parent_group_id": str(neighbour_id)},
        )
    finally:
        api_env.app.state.app_engine = original

    assert response.status_code == 422, response.text
    assert "no such parent group" in response.json()["detail"]


async def test_a_neighbours_group_cannot_be_read_or_written_on_the_admin_engine(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """Every route this cycle adds, driven against a neighbour's group id with
    RLS taken out of the picture.

    FALSIFIED: deleting `DeviceGroup.org_id == org_id` from
    `services/device_groups.py::get_device_group` turns the GET into a 200 that
    discloses a neighbour's group and the PATCH into a 200 that renames it.
    """
    theirs = await api_env.create_org("dgw-theirs")
    mine = await api_env.create_org("dgw-mine")
    headers = await _admin(api_env, mine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        victim = DeviceGroup(org_id=theirs, name="their-group")
        session.add(victim)
        await session.commit()
        victim_id = victim.id

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        read = await api_env.client.get(f"{GROUPS}/{victim_id}", headers=headers)
        renamed = await api_env.client.patch(
            f"{GROUPS}/{victim_id}", headers=headers, json={"name": "taken"}
        )
        archived = await api_env.client.delete(f"{GROUPS}/{victim_id}", headers=headers)
        added = await api_env.client.post(
            f"{GROUPS}/{victim_id}/members",
            headers=headers,
            json={"device_ids": [str(uuid4())]},
        )
    finally:
        api_env.app.state.app_engine = original

    assert read.status_code == 404, read.text
    assert renamed.status_code == 404, renamed.text
    assert archived.status_code == 404, archived.text
    assert added.status_code == 404, added.text
    assert (await _group_row(api_env, victim_id)).name == "their-group"


async def test_a_neighbours_device_cannot_be_pulled_into_my_group(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """The membership write's own predicate, on the engine where it is the only
    one there is.

    FALSIFIED: deleting `Device.org_id == org_id` from `add_group_members`
    reports `moved: true` and re-homes a neighbour's machine onto this org's
    patch, backup and remote policy.
    """
    theirs = await api_env.create_org("dgw-theirs")
    mine = await api_env.create_org("dgw-mine")
    headers = await _admin(api_env, mine)
    victim_device = await _device(api_env, theirs, "their-laptop")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=mine, name="mine")
        session.add(group)
        await session.commit()
        group_id = group.id

    original = api_env.app.state.app_engine
    api_env.app.state.app_engine = admin_engine
    try:
        response = await api_env.client.post(
            f"{GROUPS}/{group_id}/members",
            headers=headers,
            json={"device_ids": [str(victim_device)]},
        )
    finally:
        api_env.app.state.app_engine = original

    assert response.status_code == 200, response.text
    assert response.json()["results"] == [
        {"device_id": str(victim_device), "moved": False, "reason": "no such device"}
    ]
    async with AsyncSession(admin_engine) as session:
        still = (
            await session.execute(
                select(Device.device_group_id).where(Device.id == victim_device)
            )
        ).scalar_one()
    assert still is None


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #


async def test_membership_answers_per_id_and_never_discards_the_rest(
    api_env: Any,
) -> None:
    """One stale entry in a selection must not silently discard the rest, and
    "already a member" is not an error -- two operators looking at the same
    screen is the normal case."""
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="servers")
    first = await _device(api_env, org_id, "one")
    second = await _device(api_env, org_id, "two")
    missing = uuid4()

    response = await api_env.client.post(
        f"{GROUPS}/{group['id']}/members",
        headers=headers,
        json={"device_ids": [str(first), str(second), str(missing)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"] == [
        {"device_id": str(first), "moved": True, "reason": "added"},
        {"device_id": str(second), "moved": True, "reason": "added"},
        {"device_id": str(missing), "moved": False, "reason": "no such device"},
    ]

    again = await api_env.client.post(
        f"{GROUPS}/{group['id']}/members",
        headers=headers,
        json={"device_ids": [str(first)]},
    )
    assert again.json()["results"][0]["reason"] == "already a member"

    detail = await api_env.client.get(f"{GROUPS}/{group['id']}", headers=headers)
    assert detail.json()["group"]["device_count"] == 2

    removed = await api_env.client.delete(
        f"{GROUPS}/{group['id']}/members/{first}", headers=headers
    )
    assert removed.status_code == 204, removed.text
    assert (
        await api_env.client.delete(
            f"{GROUPS}/{group['id']}/members/{first}", headers=headers
        )
    ).status_code == 404


async def test_a_rule_bearing_group_refuses_a_hand_placement(api_env: Any) -> None:
    """`evaluate_rules` OVERWRITES a hand placement on the next ingest, so
    offering the edit would be offering an edit the platform reverts with no
    message -- the one thing `787461f` says a console must not get wrong.

    No route in this cycle can create such a group, so the row is written by
    hand here and the test says so. The rule is about the evaluator, not about
    which cycle wrote the row.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(
            org_id=org_id,
            name="dynamic",
            rules={"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]},
        )
        session.add(group)
        await session.commit()
        group_id = group.id

    device_id = await _device(api_env, org_id, "hand-placed")
    refused = await api_env.client.post(
        f"{GROUPS}/{group_id}/members",
        headers=headers,
        json={"device_ids": [str(device_id)]},
    )
    assert refused.status_code == 409, refused.text
    assert "decided by rules" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Archiving -- the refusal, and every reference behind it
# --------------------------------------------------------------------------- #


async def _seed_blocker(
    api_env: Any, org_id: UUID, group_id: UUID, kind: str
) -> None:
    """Exactly one row of `kind` pointing at `group_id`, and nothing else.

    Each branch carries whatever its own table demands (a repository for a
    backup policy, a profile for a health assignment) and nothing that would
    also reference the group -- the test's whole claim is that each reference
    blocks ALONE.
    """
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        if kind == "devices":
            session.add(
                Device(
                    org_id=org_id,
                    hostname="member",
                    os_family="windows",
                    device_group_id=group_id,
                )
            )
        elif kind == "child_groups":
            session.add(
                DeviceGroup(org_id=org_id, name="child", parent_group_id=group_id)
            )
        elif kind == "policies":
            session.add(
                Policy(org_id=org_id, device_group_id=group_id, module="patch", config={})
            )
        elif kind == "patch_policies":
            session.add(PatchPolicy(org_id=org_id, device_group_id=group_id))
        elif kind == "backup_policies":
            repository = BackupRepository(
                org_id=org_id,
                name=f"repo-{uuid4().hex[:6]}",
                storage_ref="file:///dev/null",
                encryption_key_ref="vault://nothing",
            )
            session.add(repository)
            await session.flush()
            session.add(
                BackupPolicy(
                    org_id=org_id,
                    repository_id=repository.id,
                    device_group_id=group_id,
                )
            )
        elif kind == "remote_policies":
            session.add(RemotePolicy(org_id=org_id, device_group_id=group_id))
        elif kind == "health_assignments":
            profile = HealthProfile(org_id=org_id, name=f"p-{uuid4().hex[:6]}")
            session.add(profile)
            await session.flush()
            session.add(
                HealthProfileAssignment(
                    org_id=org_id, profile_id=profile.id, device_group_id=group_id
                )
            )
        elif kind == "credentials":
            session.add(
                Credential(
                    org_id=org_id,
                    name=f"c-{uuid4().hex[:6]}",
                    type="windows_admin",
                    device_group_id=group_id,
                    vault_ref="vault://nothing",
                )
            )
        else:  # pragma: no cover -- a typo in the list below
            raise AssertionError(kind)
        await session.commit()


async def test_each_live_reference_blocks_the_archive_on_its_own(api_env: Any) -> None:
    """EIGHT FOREIGN KEYS POINT AT `device_groups.id` AND EVERY ONE IS `NO
    ACTION` -- there is no `ON DELETE` clause in any migration. So a hard
    `DELETE` raises where one holds and dangles where none does, which is why
    the archive is soft and why it is refused while any of them holds.

    Each kind is proven to block ON ITS OWN: seed exactly that reference,
    watch the 409 name it, remove it, watch the archive succeed. A single
    combined fixture would pass with seven of the eight counts wired to zero.

    A NINTH, THE ROLE GRANT, IS GONE BY DESIGN. Migration 0230 replaced
    `user_roles.device_group_id` with `client_id` (MSP clients spec §2.1), so a
    grant cannot name a group and cannot block one:
    `test_a_role_grant_no_longer_names_a_group` pins that the list does not
    quietly grow it back.

    RE-HOMING THE MEMBERS TO `NULL` WAS THE REJECTED ALTERNATIVE, and the
    reason is that `NULL` is not an absence: `patch/policy.py`,
    `backup/policy.py`, `remote/policy.py` and `health/resolution.py` all read
    `device_group_id IS NULL` as the ORG DEFAULT. A one-click archive of a
    300-device group would be a 300-device patch, backup and remote policy
    change wearing a Delete button.
    """
    kinds = (
        "devices",
        "child_groups",
        "policies",
        "patch_policies",
        "backup_policies",
        "remote_policies",
        "health_assignments",
        "credentials",
    )
    for kind in kinds:
        org_id = await api_env.create_org("dgw")
        headers = await _admin(api_env, org_id)
        group = await _create(api_env, headers, name="target")
        await _seed_blocker(api_env, org_id, UUID(group["id"]), kind)

        refused = await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
        assert refused.status_code == 409, f"{kind}: {refused.text}"
        body = refused.json()
        assert body["blocked_by"][kind] == 1, f"{kind}: {body}"
        assert sum(body["blocked_by"].values()) == 1, f"{kind} did not block alone: {body}"
        assert set(body["blocked_by"]) == set(kinds)

        # Remove only that reference, and the same archive succeeds -- which is
        # what makes the assertion above about THIS reference rather than about
        # archiving being broken.
        async with AsyncSession(api_env.admin_engine) as session:
            await session.execute(_clear(kind, UUID(group["id"])))
            await session.commit()

        allowed = await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
        assert allowed.status_code == 204, f"{kind}: {allowed.text}"


def _clear(kind: str, group_id: UUID) -> Any:
    """Delete only the rows of `kind` that name this group.

    `child_groups` deletes the CHILD row rather than the group under test,
    which is why the column differs from every other branch.
    """
    if kind == "child_groups":
        return DeviceGroup.__table__.delete().where(
            DeviceGroup.parent_group_id == group_id
        )
    table: Any = {
        "devices": Device,
        "policies": Policy,
        "patch_policies": PatchPolicy,
        "backup_policies": BackupPolicy,
        "remote_policies": RemotePolicy,
        "health_assignments": HealthProfileAssignment,
        "credentials": Credential,
    }[kind]
    return table.__table__.delete().where(table.device_group_id == group_id)


async def test_a_role_grant_no_longer_names_a_group(api_env: Any) -> None:
    """Role grants are scoped to a client (migration 0230, spec §2.1), so
    `user_roles` has no `device_group_id` and the archive refusal has no
    `role_grants` count. A client-scoped grant on the org's internal client
    does not block archiving a group, and the refusal body does not name it.
    """
    assert not hasattr(UserRole, "device_group_id")

    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="target")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == "read-only", Role.org_id.is_(None))
            )
        ).scalar_one()
        scoped_user = User(
            org_id=org_id,
            external_subject=f"scoped-{uuid4().hex[:8]}",
            email=f"scoped-{uuid4().hex[:8]}@test.local",
        )
        session.add(scoped_user)
        await session.flush()
        # The internal client has the organization's own id.
        session.add(
            UserRole(org_id=org_id, user_id=scoped_user.id, role_id=role.id, client_id=org_id)
        )
        await session.commit()

    detail = await api_env.client.get(f"{GROUPS}/{group['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "role_grants" not in detail.json()["references"]

    archived = await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    assert archived.status_code == 204, archived.text


async def test_an_archived_group_leaves_the_list_stays_readable_and_frees_its_name(
    api_env: Any,
) -> None:
    """Three properties of the soft delete, in one place because they are one
    decision.

    The name is freed by the PARTIAL unique index (`WHERE archived_at IS
    NULL`), which is the migration detail that is easy to miss: under the total
    constraint an archived group would hold its name against every future group
    in the org, forever. Renaming on archive was the alternative and it
    rewrites a name the audit log already references -- the thing archiving
    exists to avoid.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="retired")

    assert (
        await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    ).status_code == 204

    listed = await api_env.client.get(GROUPS, headers=headers)
    assert [row["id"] for row in listed.json()["items"]] == []

    with_archived = await api_env.client.get(
        GROUPS, params={"include_archived": "true"}, headers=headers
    )
    rows = with_archived.json()["items"]
    assert [row["id"] for row in rows] == [group["id"]]
    assert rows[0]["archived_at"] is not None

    still_readable = await api_env.client.get(f"{GROUPS}/{group['id']}", headers=headers)
    assert still_readable.status_code == 200, still_readable.text

    reused = await _create(api_env, headers, name="retired")
    assert reused["id"] != group["id"]

    collision = await api_env.client.post(
        f"{GROUPS}/{group['id']}/unarchive", headers=headers
    )
    assert collision.status_code == 409, collision.text
    assert "already holds that name" in collision.json()["detail"]

    assert (
        await api_env.client.patch(
            f"{GROUPS}/{reused['id']}", headers=headers, json={"name": "something-else"}
        )
    ).status_code == 200
    back = await api_env.client.post(f"{GROUPS}/{group['id']}/unarchive", headers=headers)
    assert back.status_code == 200, back.text
    assert back.json()["archived_at"] is None


async def test_a_group_targeted_job_aimed_at_an_archived_group_finds_no_devices(
    api_env: Any,
) -> None:
    """WHAT MAKES LEAVING SIX MODULES ALONE SAFE. Deploy, patch, backup,
    remote, health and bcp are deliberately not taught about `archived_at`
    (§7.1), so each of them will still accept an archived group's id typed
    straight in. They do not need to be taught: `deploy/dispatch.py` and
    `deploy/scripts.py` both resolve a `"group"` target as `Device.org_id ==
    org AND Device.device_group_id == target_id`, and the archive refusal
    guarantees no device row satisfies that for an archived group.

    So this asserts the GUARANTEE rather than the resolver: the same predicate
    those two modules issue, before and after, with the archive in between. A
    test that only asserted the resolver would pass even if the refusal were
    removed.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="doomed")
    device_id = await _device(api_env, org_id, "member", UUID(group["id"]))

    async def targets() -> list[UUID]:
        async with AsyncSession(api_env.admin_engine) as session:
            return list(
                (
                    await session.execute(
                        select(Device.id).where(
                            Device.org_id == org_id,
                            Device.device_group_id == UUID(group["id"]),
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert await targets() == [device_id]

    occupied = await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    assert occupied.status_code == 409, occupied.text

    assert (
        await api_env.client.delete(
            f"{GROUPS}/{group['id']}/members/{device_id}", headers=headers
        )
    ).status_code == 204
    assert (
        await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    ).status_code == 204

    assert await targets() == []


async def test_an_archived_group_cannot_become_a_new_rbac_scope(api_env: Any) -> None:
    """A `user_roles` row scoped to an archived group would be a permission
    nobody can audit, and it would newly reference a group the archive refusal
    has just guaranteed is unreferenced. `user_admin::_visible_group` used to
    refuse it.

    NOW NO GROUP CAN BE A SCOPE AT ALL (MSP clients spec §2.1: "los grupos de
    equipos dejan de dar permisos"), archived or not: the grant API takes a
    `client_id`, and a body still naming a `device_group_id` is refused rather
    than quietly read as a grant on every client.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="retired")
    assert (
        await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    ).status_code == 204
    sub = await api_env.create_user(org_id)
    async with AsyncSession(api_env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role_id = (
            await session.execute(
                select(Role.id).where(Role.name == "read-only", Role.org_id.is_(None))
            )
        ).scalar_one()

    refused = await api_env.client.post(
        f"/api/v1/users/{user_id}/roles",
        headers=headers,
        json={"role_id": str(role_id), "device_group_id": group["id"]},
    )

    assert refused.status_code == 422, refused.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(select(UserRole.id).where(UserRole.user_id == user_id))
        ).first() is None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


async def test_every_write_appends_exactly_one_event_and_the_chain_still_verifies(
    api_env: Any,
) -> None:
    """One event per write, with the metadata §8.2 names, and
    `GET /audit/verify` still passing afterwards -- the hash chain is unbroken
    by the new events.

    A RENAME RECORDS A RELABELLING AND NOTHING ELSE, which is exactly what
    migration 0072 bought: before it, `name` decided which membership rule won,
    so this event would have had to carry the devices that moved.
    """
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)

    group = await _create(api_env, headers, name="servers", precedence=2)
    device_id = await _device(api_env, org_id, "one")
    await api_env.client.post(
        f"{GROUPS}/{group['id']}/members",
        headers=headers,
        json={"device_ids": [str(device_id)]},
    )
    await api_env.client.patch(
        f"{GROUPS}/{group['id']}", headers=headers, json={"name": "workstations"}
    )
    await api_env.client.delete(
        f"{GROUPS}/{group['id']}/members/{device_id}", headers=headers
    )
    await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    await api_env.client.post(f"{GROUPS}/{group['id']}/unarchive", headers=headers)

    events = await _audit_actions(api_env, org_id)
    group_events = [
        (action, meta) for action, meta in events if action.startswith("device_group.")
    ]
    assert [action for action, _ in group_events] == [
        "device_group.created",
        "device_group.members_added",
        "device_group.updated",
        "device_group.members_removed",
        "device_group.archived",
        "device_group.unarchived",
    ]
    by_action = dict(group_events)
    assert by_action["device_group.created"] == {
        "name": "servers",
        "parent_group_id": None,
        "precedence": 2,
        "dynamic": False,
        # Cycle 2: the full document when there is one, `null` when there is
        # not. Recorded either way so a log spanning both reads uniformly --
        # and because a rules document decides which machines a signed job
        # reaches, so an entry saying only "was dynamic" answers nothing.
        "rules": None,
    }
    assert by_action["device_group.members_added"] == {"device_ids": [str(device_id)]}
    assert by_action["device_group.updated"] == {
        "changed": {"name": {"from": "servers", "to": "workstations"}}
    }
    assert by_action["device_group.archived"] == {
        "name": "workstations",
        "member_count": 0,
    }

    verified = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["ok"] is True


async def test_a_reparent_records_both_ends_of_the_move(api_env: Any) -> None:
    """`changed` is `{field: {"from": ..., "to": ...}}`, and a uuid travels as
    a string because audit metadata is JSON."""
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    parent = await _create(api_env, headers, name="parent")
    child = await _create(api_env, headers, name="child")

    moved = await api_env.client.patch(
        f"{GROUPS}/{child['id']}", headers=headers, json={"parent_group_id": parent["id"]}
    )
    assert moved.status_code == 200, moved.text

    updates = [
        meta
        for action, meta in await _audit_actions(api_env, org_id)
        if action == "device_group.updated"
    ]
    assert updates == [
        {"changed": {"parent_group_id": {"from": None, "to": parent["id"]}}}
    ]


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


async def test_archived_at_is_a_real_instant_and_unarchive_clears_it(
    api_env: Any,
) -> None:
    org_id = await api_env.create_org("dgw")
    headers = await _admin(api_env, org_id)
    group = await _create(api_env, headers, name="temporary")
    before = datetime.now(UTC)

    await api_env.client.delete(f"{GROUPS}/{group['id']}", headers=headers)
    row = await _group_row(api_env, UUID(group["id"]))
    assert row.archived_at is not None
    assert row.archived_at >= before

    await api_env.client.post(f"{GROUPS}/{group['id']}/unarchive", headers=headers)
    assert (await _group_row(api_env, UUID(group["id"]))).archived_at is None

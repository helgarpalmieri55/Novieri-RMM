"""`GET /api/v1/device-groups` -- the list that lets the console address a
group at all (audit `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`,
recommendation 3).

WHAT WAS BROKEN. `device_groups` is the platform's targeting and scoping
primitive: a deployment or a script run with `target_type="group"` resolves
`devices.device_group_id == target_id`; `patch_policies`, `backup_policies` and
`remote_policies` each key a policy on `device_group_id`, with NULL meaning
"the org default"; `health_profile_assignment` binds a profile to a group and
inherits up `parent_group_id`; `user_roles.device_group_id` narrows a role
grant to one group. Every one of those takes a group id, and NO route has ever
returned one -- so the console could name only the org default, which is the
one row that has no id.

The tests below are grouped by the consumer that wanted each part of the shape.
"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization
from app.services.device_groups import list_device_groups


async def _group(
    api_env,
    org_id: UUID,
    name: str,
    *,
    parent_group_id: UUID | None = None,
    description: str = "",
    rules: dict | None = None,
    precedence: int = 0,
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(
            org_id=org_id,
            name=name,
            parent_group_id=parent_group_id,
            description=description,
            rules=rules,
            precedence=precedence,
        )
        session.add(group)
        await session.commit()
        return group.id


async def _device(
    api_env, org_id: UUID, *, hostname: str, device_group_id: UUID | None = None
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="windows",
            device_group_id=device_group_id,
        )
        session.add(device)
        await session.commit()
        return device.id


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #


async def test_listing_groups_takes_devices_read_and_not_a_new_code(api_env) -> None:
    """`devices.read`, which is the code the reporting registry ALREADY maps
    `device_groups` to (`app/modules/reporting/registry.py`: `"device_groups":
    "devices.read"`). A group is a name for a set of devices, so a caller
    entitled to see the fleet is entitled to see how it is carved up; a new
    code would need a migration and a decision about which seeded roles hold
    it, to gate a strictly smaller fact than `GET /devices` already returns.

    `read-only` holds `devices.read` and nothing that writes (migration 0003),
    so it is the positive control; a user with no roles at all is the
    negative one.
    """
    org_id = await api_env.create_org("dg")
    nobody = await api_env.create_user(org_id)
    reader = await api_env.create_user(org_id, "read-only")

    refused = await api_env.client.get(
        "/api/v1/device-groups", headers=await api_env.headers(org_id, nobody)
    )
    assert refused.status_code == 403

    allowed = await api_env.client.get(
        "/api/v1/device-groups", headers=await api_env.headers(org_id, reader)
    )
    assert allowed.status_code == 200, allowed.text
    # `evaluation: None` IS THE HONEST ANSWER FOR AN ORG THAT HAS NEVER BEEN
    # SWEPT, and it is asserted rather than skipped: a console must be able to
    # tell "no run has happened yet" from "the last run was just now", and a
    # field the response omits reads as the second.
    assert allowed.json() == {
        "items": [],
        "total": 0,
        "ungrouped_device_count": 0,
        "evaluation": None,
    }


# --------------------------------------------------------------------------- #
# Ordering -- the order is not cosmetic here
# --------------------------------------------------------------------------- #


async def test_groups_come_back_in_the_order_that_decides_rule_precedence(
    api_env,
) -> None:
    """Ordered by `(precedence, name)`, and that is a semantic choice rather
    than a tidy one.

    `app/modules/inventory/rules.py::evaluate_rules` walks rule-bearing groups
    in exactly this order and the FIRST match claims the device. So this IS the
    precedence order for dynamic membership, and a console listing groups in
    any other order would show an operator a different answer to "which rule
    wins" from the one the ingest path takes.

    `name` remains the tiebreak, which keeps paging deterministic: the partial
    unique index on `(org_id, name) WHERE archived_at IS NULL` makes `name` a
    total order among an org's live groups -- unlike a timestamp, which can
    tie.
    """
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    for name in ("zulu", "alpha", "mike"):
        await _group(api_env, org_id, name)

    listed = await api_env.client.get("/api/v1/device-groups", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["name"] for row in listed.json()["items"]] == ["alpha", "mike", "zulu"]


async def test_precedence_outranks_the_alphabet_on_the_list_route_too(api_env) -> None:
    """THE HALF THAT MATTERS AFTER MIGRATION 0072. The list route and the
    evaluator have to agree about which rule wins, or the console answers a
    question the ingest path decides differently.

    `zulu` at precedence -1 comes first here for the same reason it would claim
    a device first: lower sorts earlier, and the name is only the tiebreak.
    """
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    await _group(api_env, org_id, "alpha")
    await _group(api_env, org_id, "mike", precedence=9)
    await _group(api_env, org_id, "zulu", precedence=-1)

    listed = await api_env.client.get("/api/v1/device-groups", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()["items"]
    assert [row["name"] for row in body] == ["zulu", "alpha", "mike"]
    assert [row["precedence"] for row in body] == [-1, 0, 9]


# --------------------------------------------------------------------------- #
# The row shape, consumer by consumer
# --------------------------------------------------------------------------- #


async def test_device_count_is_direct_membership_because_that_is_what_deploy_targets(
    api_env,
) -> None:
    """`device_count` counts DIRECT members and does not descend into child
    groups, because that is exactly what a group-targeted job hits:
    `app/modules/deploy/dispatch.py` and `app/modules/deploy/scripts.py` both
    resolve a `"group"` target as `Device.device_group_id == target_id`, with
    no walk over `parent_group_id`.

    A subtree count would therefore overstate the blast radius of the one
    action the number exists to size. (Health profile assignment DOES inherit
    down the tree -- `health/resolution.py` walks `parent_group_id` upward
    from the device -- which is why `parent_group_id` is on the row: the
    console can draw the tree and must not confuse the two semantics.)
    """
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    parent = await _group(api_env, org_id, "a-parent")
    child = await _group(api_env, org_id, "b-child", parent_group_id=parent)
    await _device(api_env, org_id, hostname="p1", device_group_id=parent)
    await _device(api_env, org_id, hostname="c1", device_group_id=child)
    await _device(api_env, org_id, hostname="c2", device_group_id=child)

    body = (await api_env.client.get("/api/v1/device-groups", headers=headers)).json()
    rows = {row["name"]: row for row in body["items"]}
    assert rows["a-parent"]["device_count"] == 1, "the two in the child are not the parent's"
    assert rows["b-child"]["device_count"] == 2
    assert rows["a-parent"]["parent_group_id"] is None
    assert rows["b-child"]["parent_group_id"] == str(parent)


async def test_a_rule_bearing_group_says_so_and_does_not_hand_back_its_rules(
    api_env,
) -> None:
    """`dynamic` is the one fact a console must not get wrong about a group:
    membership of a rule-bearing group is RECOMPUTED on every inventory
    ingest, so a UI offering "drag a device in here" on one of these would be
    offering an edit the next ingest silently reverts.

    The `rules` jsonb itself is deliberately NOT returned. There is no route
    that edits it, so nothing needs it yet -- and a `dict[str, Any]` field
    would make this response an open key space, which `tests/tool_surface.py`
    reads as property 6 failing and which bars the route from ever becoming
    an AI read tool.
    """
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    await _group(api_env, org_id, "manual", description="hand-picked")
    await _group(
        api_env,
        org_id,
        "rule-driven",
        rules={"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]},
    )

    body = (await api_env.client.get("/api/v1/device-groups", headers=headers)).json()
    rows = {row["name"]: row for row in body["items"]}
    assert rows["manual"]["dynamic"] is False
    assert rows["manual"]["description"] == "hand-picked"
    assert rows["rule-driven"]["dynamic"] is True
    assert "rules" not in rows["rule-driven"]


async def test_the_envelope_counts_the_devices_no_group_claims(api_env) -> None:
    """`ungrouped_device_count` -- the population of the ORG DEFAULT, which is
    the row this list can never contain.

    Every policy table in the platform reads `device_group_id IS NULL` as the
    org-wide fallback (`patch/policy.py`, `backup/policy.py`,
    `remote/policy.py`). Its subject is precisely the devices in no group, and
    that number is not derivable from a page of groups. Without it the console
    could finally list groups and still not say what the default it has been
    stuck on actually governs -- which is the audit's complaint, not a
    decoration on it.
    """
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    group = await _group(api_env, org_id, "servers")
    await _device(api_env, org_id, hostname="in-group", device_group_id=group)
    await _device(api_env, org_id, hostname="loose-1")
    await _device(api_env, org_id, hostname="loose-2")

    body = (await api_env.client.get("/api/v1/device-groups", headers=headers)).json()
    assert body["ungrouped_device_count"] == 2
    assert body["items"][0]["device_count"] == 1


async def test_the_page_is_bounded_and_total_counts_past_it(api_env) -> None:
    org_id = await api_env.create_org("dg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    for index in range(5):
        await _group(api_env, org_id, f"g{index}")

    first = await api_env.client.get(
        "/api/v1/device-groups", params={"limit": 2}, headers=headers
    )
    assert first.json()["total"] == 5, "total is the match count BEFORE limit/offset"
    assert [row["name"] for row in first.json()["items"]] == ["g0", "g1"]

    second = await api_env.client.get(
        "/api/v1/device-groups", params={"limit": 2, "offset": 2}, headers=headers
    )
    assert [row["name"] for row in second.json()["items"]] == ["g2", "g3"]
    assert second.json()["total"] == 5


# --------------------------------------------------------------------------- #
# Tenancy (invariant #5)
# --------------------------------------------------------------------------- #


async def test_another_orgs_group_appears_in_neither_items_nor_total(api_env) -> None:
    """A tenant-scoped page carrying an org-wide count is still a leak, so
    `total` and `ungrouped_device_count` are asserted as well as `items`.

    The caller is an `org-admin`, holding every code there is, so nothing but
    the tenant scope is stopping them -- and a group id is not an inert
    label: it is the `target_id` a deployment takes.
    """
    mine = await api_env.create_org("dg-mine")
    theirs = await api_env.create_org("dg-theirs")
    me = await api_env.create_user(mine, "org-admin")
    them = await api_env.create_user(theirs, "org-admin")

    my_group = await _group(api_env, mine, "mine")
    their_group = await _group(api_env, theirs, "theirs")
    await _device(api_env, mine, hostname="m1", device_group_id=my_group)
    await _device(api_env, theirs, hostname="t1", device_group_id=their_group)
    await _device(api_env, theirs, hostname="t2")

    body = (
        await api_env.client.get(
            "/api/v1/device-groups", headers=await api_env.headers(mine, me)
        )
    ).json()
    assert [row["id"] for row in body["items"]] == [str(my_group)]
    assert str(their_group) not in {row["id"] for row in body["items"]}
    assert body["total"] == 1, "the count must be tenant-scoped too, not just the page"
    assert body["ungrouped_device_count"] == 0, "their loose device is not mine"

    # ...and the other org sees its own, so none of the above passes merely
    # because the rows were never written.
    other = (
        await api_env.client.get(
            "/api/v1/device-groups", headers=await api_env.headers(theirs, them)
        )
    ).json()
    assert [row["id"] for row in other["items"]] == [str(their_group)]
    assert other["ungrouped_device_count"] == 1


async def test_a_neighbours_device_cannot_inflate_this_orgs_group_count(api_env) -> None:
    """`devices.device_group_id` is a bare FK with NO org-aware constraint --
    `health/profiles.py::_assert_target_in_org` exists because of exactly that
    -- so nothing in the schema stops org B's device row from naming org A's
    group. The member count has to be tenant-scoped on the DEVICE side too,
    or a neighbour tenant decides how large this org's blast radius looks.
    """
    mine = await api_env.create_org("dg-mine")
    theirs = await api_env.create_org("dg-theirs")
    me = await api_env.create_user(mine, "org-admin")

    my_group = await _group(api_env, mine, "servers")
    await _device(api_env, mine, hostname="m1", device_group_id=my_group)
    # Written on the ADMIN engine: RLS would refuse this from an app session,
    # and the point is that the row can exist at all.
    await _device(api_env, theirs, hostname="t1", device_group_id=my_group)

    body = (
        await api_env.client.get(
            "/api/v1/device-groups", headers=await api_env.headers(mine, me)
        )
    ).json()
    assert body["items"][0]["device_count"] == 1


async def test_rls_hides_another_orgs_group_from_an_unfiltered_select(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The DATABASE half of invariant #5 for `device_groups`.

    The service writes `WHERE org_id = :org` itself and the route test above
    checks what the API answers. Neither says what happens if that predicate
    is ever dropped. This does: the SELECT below carries NO tenant filter at
    all, so a forgotten `WHERE` has to come back empty rather than handing a
    caller another tenant's group id -- which is the value a deployment's
    `target_id` takes.
    """
    ids: dict[str, UUID] = {}
    orgs: dict[str, UUID] = {}
    for key in ("mine", "theirs"):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            org = Organization(name=key, slug=f"dg-{uuid4().hex[:8]}")
            session.add(org)
            await session.commit()
            orgs[key] = org.id
        async with org_session(orgs[key], engine=app_engine, clients=ClientScope.all()) as session:
            group = DeviceGroup(org_id=orgs[key], name=f"group-{key}")
            session.add(group)
            await session.flush()
            ids[key] = group.id

    async with org_session(orgs["mine"], engine=app_engine, clients=ClientScope.all()) as session:
        visible = {row.id for row in (await session.execute(select(DeviceGroup))).scalars()}
    assert visible == {ids["mine"]}, "an unfiltered SELECT must still be tenant-scoped"

    async with org_session(orgs["theirs"], engine=app_engine, clients=ClientScope.all()) as session:
        visible = {row.id for row in (await session.execute(select(DeviceGroup))).scalars()}
    assert visible == {ids["theirs"]}


async def test_the_query_is_tenant_scoped_where_rls_is_not_the_thing_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    """The service's OWN `org_id` predicates, pinned against an engine that
    ignores RLS.

    Every assertion above runs through `org_session` on the app role, where
    Postgres would reject a cross-tenant row whether or not the query asked
    it to -- so deleting `WHERE org_id` from this service turns NONE of them
    red. Verified by doing it: the member-count subquery still answered
    correctly with its tenant predicate removed.

    `its_admin` carries BYPASSRLS (`tests/conftest.py`), so here the SQL is
    the only thing scoping anything, which is the shape
    `test_reporting_patch_compliance.py` uses for the same reason. All four
    numbers are checked, because three of them are counts and a count is
    where an org-wide answer hides behind a correctly-filtered page.
    """
    orgs: dict[str, UUID] = {}
    groups: dict[str, UUID] = {}
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for key in ("mine", "theirs"):
            org = Organization(name=key, slug=f"dg-svc-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs[key] = org.id
            group = DeviceGroup(org_id=org.id, name=f"servers-{key}")
            session.add(group)
            await session.flush()
            groups[key] = group.id
            session.add(
                Device(
                    org_id=org.id,
                    hostname=f"in-{key}",
                    os_family="windows",
                    device_group_id=group.id,
                )
            )
            session.add(Device(org_id=org.id, hostname=f"loose-{key}", os_family="windows"))
        # The row nothing in the schema forbids: THEIR device naming MY group.
        session.add(
            Device(
                org_id=orgs["theirs"],
                hostname="theirs-pointing-at-mine",
                os_family="windows",
                device_group_id=groups["mine"],
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        page = await list_device_groups(session, org_id=orgs["mine"])

    assert [group.id for group, _ in page.rows] == [groups["mine"]]
    assert page.total == 1
    assert page.ungrouped_device_count == 1, "their loose device is not in my default"
    assert page.rows[0][1] == 1, "their device naming my group does not inflate my count"

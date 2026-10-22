"""The operator's own arrangement of the command centre.

The operator, 2026-09-01: "main dashboard should be customizable with tiles
also give me the ability to resize and order in whichever way i want."

WHAT THESE TESTS ARE GUARDING, in order of what would hurt most:

1. **One person's layout is one person's.** The routes take no `user_id` --
   the target is `ctx.user_id` from the validated token -- which is the entire
   reason all three are ungated beyond authentication. Two users in one org
   must not see each other's arrangement, and that is asserted through the
   HTTP surface rather than by reading the service, because the surface is
   what a caller can reach.

2. **The tenant boundary.** `dashboard_layouts` carries the standard forced
   RLS policy. The test that could FAIL if the policy were dropped exercises
   the BYPASSRLS admin engine to plant a neighbour's row, then asks through a
   tenant session -- a route test alone would stay green with the policy gone,
   because `org_session` runs NOBYPASSRLS and the route also filters on
   `org_id` in Python.

3. **Shape, not membership.** The server must bound the document without
   knowing the widget list, since the widget list is a property of the console
   build and a release that adds a tile must not need a server change.
"""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DashboardLayout

LAYOUT = {
    "widgets": [
        {"id": "open-alerts", "size": "small", "hidden": False},
        {"id": "recent-devices", "size": "wide", "hidden": False},
        {"id": "open-tickets", "size": "small", "hidden": True},
    ]
}


async def test_a_dashboard_nobody_has_arranged_reports_absent_not_empty(api_env) -> None:
    """`null`, never `[]`.

    An empty list and "never customized" render the same on a client that
    treats falsy as absent, and they mean opposite things: one is a dashboard
    the operator emptied, the other is the stock arrangement they have not
    touched. An empty list cannot be stored at all, so `null` says exactly one
    thing."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    resp = await api_env.client.get("/api/v1/dashboard/layout", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"widgets": None}


async def test_a_saved_layout_comes_back_in_the_order_it_was_saved(api_env) -> None:
    """Order IS the layout. A route that returned the same widgets in a
    different order would be a feature that silently does nothing."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    put = await api_env.client.put("/api/v1/dashboard/layout", json=LAYOUT, headers=headers)
    assert put.status_code == 200, put.text

    body = (await api_env.client.get("/api/v1/dashboard/layout", headers=headers)).json()
    assert body["widgets"] == LAYOUT["widgets"]


async def test_saving_twice_replaces_rather_than_accumulating(api_env) -> None:
    """The upsert, exercised. A read-then-write would also pass this; what it
    pins is that a second save is not a second row and not an append."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    await api_env.client.put("/api/v1/dashboard/layout", json=LAYOUT, headers=headers)
    second = {"widgets": [{"id": "open-alerts", "size": "wide", "hidden": False}]}
    await api_env.client.put("/api/v1/dashboard/layout", json=second, headers=headers)

    body = (await api_env.client.get("/api/v1/dashboard/layout", headers=headers)).json()
    assert body["widgets"] == second["widgets"]
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(
                select(DashboardLayout).where(DashboardLayout.org_id == org_id)
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_reset_deletes_the_row_and_succeeds_when_there_is_none(api_env) -> None:
    """IDEMPOTENT. "Reset a dashboard I have never arranged" is a request that
    has already been satisfied, and a 404 would make the console's Reset button
    fail for exactly the operators for whom it has the least to do."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    first = await api_env.client.delete("/api/v1/dashboard/layout", headers=headers)
    assert first.status_code == 204, first.text

    await api_env.client.put("/api/v1/dashboard/layout", json=LAYOUT, headers=headers)
    assert (
        await api_env.client.delete("/api/v1/dashboard/layout", headers=headers)
    ).status_code == 204
    body = (await api_env.client.get("/api/v1/dashboard/layout", headers=headers)).json()
    assert body["widgets"] is None


async def test_two_operators_in_one_org_do_not_share_a_layout(api_env) -> None:
    """THE REASON THESE ROUTES MAY BE UNGATED. There is no `user_id` parameter
    to tamper with; the target is the validated token's subject. If that ever
    stopped being true, one operator arranging their dashboard would rearrange
    a colleague's."""
    org_id = await api_env.create_org("dash")
    alice = await api_env.headers(org_id, await api_env.create_user(org_id))
    bob = await api_env.headers(org_id, await api_env.create_user(org_id))

    await api_env.client.put("/api/v1/dashboard/layout", json=LAYOUT, headers=alice)

    assert (await api_env.client.get("/api/v1/dashboard/layout", headers=bob)).json() == {
        "widgets": None
    }
    bobs = {"widgets": [{"id": "jobs-attention", "size": "medium", "hidden": False}]}
    await api_env.client.put("/api/v1/dashboard/layout", json=bobs, headers=bob)

    alices = (await api_env.client.get("/api/v1/dashboard/layout", headers=alice)).json()
    assert alices["widgets"] == LAYOUT["widgets"], "Bob's save reached Alice's row"


async def test_an_empty_layout_is_refused_rather_than_stored(api_env) -> None:
    """So that "no row" keeps its single meaning. Clearing is DELETE."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    resp = await api_env.client.put(
        "/api/v1/dashboard/layout", json={"widgets": []}, headers=headers
    )
    assert resp.status_code == 422, resp.text


async def test_the_document_is_bounded_without_the_server_knowing_the_widgets(
    api_env,
) -> None:
    """SHAPE, NOT MEMBERSHIP, and both halves of that matter.

    Refused: too many entries, an id that is not a slug, a size outside the
    closed set, an unexpected key. Accepted: an id no widget in this build
    has, because the widget list belongs to the console and a release that
    adds a tile must not need a server change."""
    org_id = await api_env.create_org("dash")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id))

    async def put(payload: dict) -> int:
        return (
            await api_env.client.put("/api/v1/dashboard/layout", json=payload, headers=headers)
        ).status_code

    assert await put({"widgets": [{"id": "x", "size": "small"} for _ in range(65)]}) == 422
    assert await put({"widgets": [{"id": "Not A Slug", "size": "small"}]}) == 422
    assert await put({"widgets": [{"id": "ok", "size": "enormous"}]}) == 422
    assert await put({"widgets": [{"id": "ok", "size": "small", "colour": "red"}]}) == 422
    # A widget this build has never heard of stores fine, on purpose.
    assert await put({"widgets": [{"id": "a-widget-from-2027", "size": "wide"}]}) == 200


async def test_the_rls_policy_holds_where_the_route_filter_is_not_the_thing_being_tested(
    api_env,
) -> None:
    """THE ONE THAT COULD ACTUALLY FAIL IF THE POLICY WERE DROPPED.

    A route test cannot see this: `org_session` runs NOBYPASSRLS and the route
    ALSO filters on `org_id` in Python, so deleting the policy leaves every
    route test green. This plants a neighbouring tenant's row through the
    BYPASSRLS admin engine and then reads the table through a tenant session
    with no `org_id` predicate at all -- the only query shape whose result
    depends on the policy and nothing else."""
    mine = await api_env.create_org("dash-mine")
    theirs = await api_env.create_org("dash-theirs")
    my_user = await api_env.create_user(mine)
    their_user = await api_env.create_user(theirs)

    async def user_id_of(sub: str) -> UUID:
        from app.models import User

        async with AsyncSession(api_env.admin_engine) as session:
            return (
                await session.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()

    async with AsyncSession(api_env.admin_engine) as session:
        session.add_all(
            [
                DashboardLayout(
                    org_id=mine, user_id=await user_id_of(my_user), layout=LAYOUT["widgets"]
                ),
                DashboardLayout(
                    org_id=theirs,
                    user_id=await user_id_of(their_user),
                    layout=LAYOUT["widgets"],
                ),
            ]
        )
        await session.commit()

    from app.core.tenancy import org_session

    async with org_session(mine, engine=api_env.app_engine) as session:
        visible = (
            await session.execute(text("SELECT org_id FROM dashboard_layouts"))
        ).scalars().all()
    assert visible == [mine], f"the tenant boundary leaked: {visible}"


async def test_a_row_written_before_a_field_existed_still_reads(api_env) -> None:
    """`hidden` defaults, so a row stored by an older release is not a 500.

    JSONB is not validated by the database on purpose -- which means the shape
    on disk is whatever the release that wrote it believed. The response model
    re-validates on the way OUT for exactly this reason."""
    org_id = await api_env.create_org("dash")
    sub = await api_env.create_user(org_id)
    headers = await api_env.headers(org_id, sub)
    from app.models import User

    async with AsyncSession(api_env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        session.add(
            DashboardLayout(
                org_id=org_id, user_id=user_id, layout=[{"id": "open-alerts", "size": "small"}]
            )
        )
        await session.commit()

    body = (await api_env.client.get("/api/v1/dashboard/layout", headers=headers)).json()
    assert body["widgets"] == [{"id": "open-alerts", "size": "small", "hidden": False}]

"""Permission checks on the record's own client (plan 2026-10-26 Task 14,
ledger T4-C2), and what phase 1 left for this phase: health resolution per
client, the upsert first-writer races, and moving a device between its
client's sites.

T4-C2: a check made INSIDE a route (a second permission, an owner-or-admin
rail, a report's source codes) asked `user_permission_codes` with no client,
which counts only grants on every client -- so a technician holding the code
on one client was refused on that client's own records. Each now asks about
the record's client; a refusal keeps its old shape (403 for a missing
permission, 404 for a record the request cannot see).
"""

import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AiAction,
    AuditEvent,
    ChangeGate,
    Device,
    DeviceGroup,
    HealthAlert,
    Job,
    Permission,
    RemoteSession,
    Role,
    RolePermission,
    Site,
    User,
    UserRole,
)
from app.modules.agent_push.models import PushHostKey
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _holder(
    env: ApiEnv, org: UUID, grants: dict[UUID | None, tuple[str, ...]]
) -> tuple[UUID, dict[str, str]]:
    """A person holding, per client (None: every client), exactly those codes:
    one role per entry, so a code on one client is not a code on another."""
    sub = f"rc-{uuid4().hex[:10]}"
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        user = User(org_id=org, external_subject=sub, email=f"{sub}@test.local")
        s.add(user)
        await s.flush()
        for client, codes in grants.items():
            role = Role(org_id=org, name=f"r-{uuid4().hex[:8]}")
            s.add(role)
            await s.flush()
            for code in codes:
                permission = (
                    await s.execute(select(Permission.id).where(Permission.code == code))
                ).scalar_one()
                s.add(RolePermission(role_id=role.id, permission_id=permission))
            s.add(UserRole(org_id=org, user_id=user.id, role_id=role.id, client_id=client))
        await s.commit()
        return user.id, await env.headers(org, sub)


async def _device(env: ApiEnv, org: UUID, client: UUID, site: UUID, **kw: Any) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(
            org_id=org, client_id=client, site_id=site, hostname=f"pc-{uuid4().hex[:4]}",
            os_family="windows", **kw,
        )
        s.add(d)
        await s.commit()
        return d.id


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


# --------------------------------------------------------------------------- #
# Reports (T4-C2 items 2-3)
# --------------------------------------------------------------------------- #


async def test_the_report_catalogue_counts_codes_held_on_any_client(api_env: ApiEnv) -> None:
    """T4-C2 items 2-3: a technician holding every code on X sees the reports
    they can run (over X, by RLS), not an empty catalogue."""
    org = await api_env.create_org("cfg-reports")
    x, _ = await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get("/api/v1/reports", headers=x_tech)
    assert r.status_code == 200, r.text
    reports = r.json()["reports"]
    assert len(reports) > 0 and all(entry["entitled"] for entry in reports), reports


async def test_the_catalogue_promises_a_report_only_where_one_client_holds_every_code(
    api_env: ApiEnv,
) -> None:
    """A report runs over the clients holding `reporting.read` AND every code
    it needs (`narrowed_to_every`), so codes held on DIFFERENT clients run it
    over no client. The catalogue says so: such a report is not `entitled`,
    and `missing_permissions` names the codes absent from a client where the
    person can read reports -- never an empty list beside `entitled: false`
    (plan 2026-10-26 Task 14 review). Each verdict is checked against the
    run itself."""
    org = await api_env.create_org("cfg-report-cat")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    await _device(api_env, org, x, x_site)
    key = "fleet-posture"  # requires devices.read and health.read

    async def verdict(headers: dict[str, str]) -> tuple[bool, list[str], int]:
        cat = await api_env.client.get("/api/v1/reports", headers=headers)
        assert cat.status_code == 200, cat.text
        [entry] = [e for e in cat.json()["reports"] if e["key"] == key]
        run = await api_env.client.get(f"/api/v1/reports/{key}", headers=headers)
        assert run.status_code == 200, run.text
        return entry["entitled"], entry["missing_permissions"], len(run.json()["rows"])

    _, together = await _holder(
        api_env, org,
        {x: ("reporting.read", "devices.read", "health.read"), y: ("reporting.read",)},
    )
    assert await verdict(together) == (True, [], 1)

    _, apart = await _holder(
        api_env, org, {x: ("reporting.read", "devices.read"), y: ("health.read",)}
    )
    assert await verdict(apart) == (False, ["health.read"], 0)

    _, elsewhere = await _holder(
        api_env, org, {x: ("devices.read", "health.read"), y: ("reporting.read",)}
    )
    assert await verdict(elsewhere) == (False, ["devices.read", "health.read"], 0)


async def test_a_report_runs_over_the_clients_holding_every_code_it_needs(
    api_env: ApiEnv,
) -> None:
    """The report is the request's RLS: the clients where the person holds
    `reporting.read` AND each source code. A technician of X sees X's machines
    only; one who also holds `reporting.read` (and nothing else) on Y still
    sees only X's -- a code held on X is no grant on Y's machines."""
    org = await api_env.create_org("cfg-report-run")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    await _device(api_env, org, y, y_site)
    path = "/api/v1/reports/fleet-posture"

    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.get(path, headers=x_tech)
    assert r.status_code == 200, r.text
    assert [row["device_id"] for row in r.json()["rows"]] == [str(x_device)]

    _, split = await _holder(
        api_env, org,
        {x: ("reporting.read", "devices.read", "health.read"), y: ("reporting.read",)},
    )
    r = await api_env.client.get(path, headers=split)
    assert r.status_code == 200, r.text
    assert [row["device_id"] for row in r.json()["rows"]] == [str(x_device)]

    # The codes held only on DIFFERENT clients run over no client: nothing.
    _, apart = await _holder(
        api_env, org, {x: ("reporting.read", "devices.read"), y: ("health.read",)}
    )
    r = await api_env.client.get(path, headers=apart)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == []

    # A code held nowhere is still the named 403.
    _, lacking = await _holder(api_env, org, {x: ("reporting.read", "devices.read")})
    r = await api_env.client.get(path, headers=lacking)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["missing"] == ["health.read"]


# --------------------------------------------------------------------------- #
# Health remediation (T4-C2 item 10)
# --------------------------------------------------------------------------- #


async def _x_alert(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    """An open alert on a machine of `client`, with an approved
    `inventory.collect` action (`jobs.dispatch` on its own route)."""
    from tests.health_action_fixtures import seed_approved_action
    from tests.health_fixtures import seed_check, seed_profile

    device = await _device(env, org, client, site)
    profile = await seed_profile(env, org)
    check = await seed_check(env, org, profile)
    now = _dt.datetime.now(_dt.UTC)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        alert = HealthAlert(
            org_id=org, device_id=device, check_id=check, profile_id=profile, selector="",
            check_type="cpu.busy_pct", severity="warning", state="active",
            observed_value="97", threshold_display="> 95", opened_at=now,
            opened_policy_version=1, last_seen_at=now,
        )
        s.add(alert)
        await s.commit()
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        author = User(org_id=org, external_subject=f"a-{uuid4().hex[:8]}", email="a@x.y")
        s.add(author)
        await s.commit()
    await seed_approved_action(env, org, author.id, key="collect", autonomy="suggest")
    return alert.id


async def _jobs(env: ApiEnv, org: UUID) -> int:
    async with AsyncSession(env.admin_engine) as s:
        return int(
            (await s.execute(select(func.count()).select_from(Job).where(Job.org_id == org)))
            .scalar_one()
        )


async def test_a_technician_of_x_runs_a_remediation_on_x_s_alert(api_env: ApiEnv) -> None:
    """T4-C2 item 10 (`health/actions.py:265`): the action's second
    permission was looked up on every client only."""
    org = await api_env.create_org("cfg-remed")
    x, x_site = await client_with_site(api_env, org)
    alert = await _x_alert(api_env, org, x, x_site)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"/api/v1/health/alerts/{alert}/remediate", json={"action_key": "collect"},
        headers=x_tech,
    )
    assert r.status_code in (200, 202), r.text
    assert await _jobs(api_env, org) == 1


async def test_the_second_permission_is_the_alert_s_client_s(api_env: ApiEnv) -> None:
    """`jobs.dispatch` held on Y is no grant on X's machine: the same 403,
    naming the code, that a person holding it nowhere gets; nothing sent."""
    org = await api_env.create_org("cfg-remed-y")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    alert = await _x_alert(api_env, org, x, x_site)
    _, elsewhere = await _holder(
        api_env, org, {x: ("health.remediate", "health.read"), y: ("jobs.dispatch",)}
    )
    before = await _count_audits(api_env, org)
    r = await api_env.client.post(
        f"/api/v1/health/alerts/{alert}/remediate", json={"action_key": "collect"},
        headers=elsewhere,
    )
    assert r.status_code == 403, r.text
    assert "jobs.dispatch" in r.json()["detail"]
    assert (await _jobs(api_env, org), await _count_audits(api_env, org)) == (0, before)


async def test_approving_an_awaiting_remediation_asks_the_alert_s_client(
    api_env: ApiEnv,
) -> None:
    """The approve route's second gate is the same per-client question."""
    from app.models import ApprovedAction, HealthAlertAction

    org = await api_env.create_org("cfg-remed-appr")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    alert = await _x_alert(api_env, org, x, x_site)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        action = (
            await s.execute(select(ApprovedAction.id).where(ApprovedAction.org_id == org))
        ).scalar_one()
        row = HealthAlertAction(
            org_id=org, alert_id=alert, kind="remediate", state="awaiting_approval",
            approved_action_id=action,
            expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
        )
        s.add(row)
        await s.commit()
    _, elsewhere = await _holder(
        api_env, org, {x: ("health.remediate", "health.read"), y: ("jobs.dispatch",)}
    )
    before = await _count_audits(api_env, org)
    r = await api_env.client.post(
        f"/api/v1/health/alerts/{alert}/actions/{row.id}/approve", headers=elsewhere
    )
    assert r.status_code == 403, r.text
    assert (await _jobs(api_env, org), await _count_audits(api_env, org)) == (0, before)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        f"/api/v1/health/alerts/{alert}/actions/{row.id}/approve", headers=x_tech
    )
    assert r.status_code == 200, r.text
    assert await _jobs(api_env, org) == 1


# --------------------------------------------------------------------------- #
# Health resolution (Step 4b)
# --------------------------------------------------------------------------- #


async def _profile(env: ApiEnv, org: UUID, *, active: bool = True) -> UUID:
    from tests.health_fixtures import seed_profile

    return await seed_profile(env, org, active=active)


async def _default_assignment(
    env: ApiEnv, org: UUID, profile: UUID, client: UUID | None
) -> None:
    from app.models import HealthProfileAssignment

    async with AsyncSession(env.admin_engine) as s:
        s.add(HealthProfileAssignment(org_id=org, profile_id=profile, client_id=client))
        await s.commit()


async def _resolve(env: ApiEnv, org: UUID, device_id: UUID) -> tuple[UUID | None, int]:
    """The profile the device resolves to under scope all (the health
    sweep's), and how many statements read `health_profile_assignment`."""
    from app.modules.health.resolution import resolve_profile_for_device

    seen: list[str] = []

    def count(conn: Connection, cursor: object, statement: str, *rest: object) -> None:
        if "health_profile_assignment" in statement:
            seen.append(statement)

    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        device = (await s.execute(select(Device).where(Device.id == device_id))).scalar_one()
        connection = (await s.connection()).sync_connection
        event.listen(connection, "before_cursor_execute", count)
        try:
            resolved = await resolve_profile_for_device(s, org, device)
        finally:
            event.remove(connection, "before_cursor_execute", count)
    return (None if resolved is None else resolved.profile.id), len(seen)


async def test_a_device_takes_its_client_s_default_then_the_global_one(api_env: ApiEnv) -> None:
    """Device -> group -> the default OF THE DEVICE'S CLIENT -> the global
    default (0231 lets each client have one). Two clients, a default on X:
    X's machine gets X's, Y's gets the global one -- under scope all, where
    only the explicit client predicate tells them apart. In ONE statement
    for the two defaults (no query per level)."""
    org = await api_env.create_org("cfg-resolve")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    y_device = await _device(api_env, org, y, y_site)
    shared = await _profile(api_env, org)
    mine = await _profile(api_env, org)
    await _default_assignment(api_env, org, shared, None)
    await _default_assignment(api_env, org, mine, x)

    # Device-level lookup + the defaults: two statements, whichever wins.
    assert await _resolve(api_env, org, x_device) == (mine, 2)
    assert await _resolve(api_env, org, y_device) == (shared, 2)


async def test_an_inactive_client_default_falls_through_to_the_global_one(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("cfg-resolve-off")
    x, x_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    shared = await _profile(api_env, org)
    retired = await _profile(api_env, org, active=False)
    await _default_assignment(api_env, org, shared, None)
    await _default_assignment(api_env, org, retired, x)
    assert (await _resolve(api_env, org, x_device))[0] == shared

    # And a client default alone serves only its client.
    org2 = await api_env.create_org("cfg-resolve-only")
    a, a_site = await client_with_site(api_env, org2)
    b, b_site = await client_with_site(api_env, org2)
    a_device = await _device(api_env, org2, a, a_site)
    b_device = await _device(api_env, org2, b, b_site)
    only = await _profile(api_env, org2)
    await _default_assignment(api_env, org2, only, a)
    assert (await _resolve(api_env, org2, a_device))[0] == only
    assert (await _resolve(api_env, org2, b_device))[0] is None


# --------------------------------------------------------------------------- #
# AI (T4-C2 items 11-12)
# --------------------------------------------------------------------------- #

AI = "/api/v1/ai"


async def _ai_world(api_env: ApiEnv, tag: str) -> Any:
    """`build_world` (an approved `inventory.collect`, the admin persona,
    every code on every client for its user), plus clients X and Y with one
    machine each."""
    from tests.ai_act_fixtures import build_world

    world = await build_world(
        api_env, tag=tag,
        codes=("ai.use", "ai.act.propose", "ai.act.confirm", "devices.read", "jobs.dispatch"),
    )
    world.x, world.x_site = await client_with_site(api_env, world.org_id)
    world.y, world.y_site = await client_with_site(api_env, world.org_id)
    world.x_device = await _device(api_env, world.org_id, world.x, world.x_site)
    world.y_device = await _device(api_env, world.org_id, world.y, world.y_site)
    return world


async def _propose(api_env: ApiEnv, world: Any, device: UUID, *, binding: Any = None,
                   ctx: Any = None) -> Any:
    from app.core.ai.act import propose
    from app.core.ai.executor_staff import invoke_tool

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as s:
        return await propose(
            s, org_id=world.org_id, binding=binding or world.binding, ctx=ctx or world.ctx,
            executor=invoke_tool, action_key=world.action_key, device_id=device,
        )


async def test_ai_proposals_are_read_on_the_proposal_s_client(api_env: ApiEnv) -> None:
    """`devices.read` is asked about the proposal's machine's client: a reader
    holding it on X sees X's proposal and not Y's (404, like a missing one),
    though `ai.use` reaches both; held nowhere it is the old 403."""
    world = await _ai_world(api_env, "cfg-ai-read")
    on_x = await _propose(api_env, world, world.x_device)
    on_y = await _propose(api_env, world, world.y_device)

    _, reader = await _holder(
        api_env, world.org_id, {world.x: ("ai.use", "devices.read"), world.y: ("ai.use",)}
    )
    listed = await api_env.client.get(f"{AI}/actions", headers=reader)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [str(on_x.ai_action_id)]
    assert (await api_env.client.get(f"{AI}/actions/{on_x.ai_action_id}",
                                     headers=reader)).status_code == 200
    # The very answer a missing id gets: its 404 names the id asked for.
    absent = uuid4()
    hidden = await api_env.client.get(f"{AI}/actions/{on_y.ai_action_id}", headers=reader)
    missing = await api_env.client.get(f"{AI}/actions/{absent}", headers=reader)
    assert (hidden.status_code, hidden.json()) == (
        404, {"detail": f"no ai action {on_y.ai_action_id}"}
    )
    assert (missing.status_code, missing.json()) == (404, {"detail": f"no ai action {absent}"})

    _, blind = await _holder(api_env, world.org_id, {world.x: ("ai.use",)})
    assert (await api_env.client.get(f"{AI}/actions", headers=blind)).status_code == 403


async def test_ai_confirm_and_reject_ask_the_proposal_s_client(api_env: ApiEnv) -> None:
    """Confirm needs `ai.act.confirm` and the job type's own permission ON
    THE MACHINE'S CLIENT; reject needs `ai.act.propose` there. Held on X,
    X's proposal is confirmed (one job) and rejected; `jobs.dispatch` held
    on Y only is the named 403 and sends nothing."""
    world = await _ai_world(api_env, "cfg-ai-confirm")
    first = await _propose(api_env, world, world.x_device)
    second = await _propose(api_env, world, world.x_device)

    _, elsewhere = await _holder(
        api_env, world.org_id,
        {world.x: ("ai.use", "ai.act.confirm", "devices.read"), world.y: ("jobs.dispatch",)},
    )
    before = await _count_audits(api_env, world.org_id)
    r = await api_env.client.post(
        f"{AI}/actions/{first.ai_action_id}/confirm", json={"digest": first.preview_digest},
        headers=elsewhere,
    )
    assert r.status_code == 403, r.text
    assert "jobs.dispatch" in r.json()["detail"]
    assert (await _jobs(api_env, world.org_id), await _count_audits(api_env, world.org_id)) == (
        0, before
    )

    x_tech = await everything_on(api_env, world.org_id, world.x)
    r = await api_env.client.post(
        f"{AI}/actions/{first.ai_action_id}/confirm", json={"digest": first.preview_digest},
        headers=x_tech,
    )
    assert r.status_code == 200, r.text
    assert await _jobs(api_env, world.org_id) == 1
    r = await api_env.client.post(
        f"{AI}/actions/{second.ai_action_id}/reject", json={"reason": "not_needed"},
        headers=x_tech,
    )
    assert r.status_code == 200, r.text


async def test_a_proposal_needs_ai_act_propose_on_the_machine_s_client(
    api_env: ApiEnv,
) -> None:
    """The model names the machine; the person proposing must hold
    `ai.act.propose` on ITS client. Held on Y only, the proposal against X's
    machine is refused and nothing is written; held on X, it is previewed."""
    from app.core.ai.act import ActRefused, ProposeBinding
    from app.core.ai.types import TurnContext

    world = await _ai_world(api_env, "cfg-ai-propose")

    async def as_person(grants: dict[UUID | None, tuple[str, ...]]) -> tuple[Any, Any]:
        user, headers = await _holder(api_env, world.org_id, grants)
        ctx = TurnContext(app=api_env.app, bearer=headers["Authorization"],
                          deadline=world.ctx.deadline, org_id=world.org_id, user_id=user)
        binding = ProposeBinding(persona="admin", ai_session_id=world.ai_session_id,
                                 message_seq=1, actor_id=user)
        return binding, ctx

    binding, ctx = await as_person(
        {world.x: ("ai.use", "devices.read"), world.y: ("ai.act.propose",)}
    )
    before = await _count_audits(api_env, world.org_id)
    with pytest.raises(ActRefused):
        await _propose(api_env, world, world.x_device, binding=binding, ctx=ctx)
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(
                select(func.count()).select_from(AiAction).where(AiAction.org_id == world.org_id)
            )
        ).scalar_one() == 0
    assert await _count_audits(api_env, world.org_id) == before

    binding, ctx = await as_person({world.x: ("ai.use", "devices.read", "ai.act.propose")})
    preview = await _propose(api_env, world, world.x_device, binding=binding, ctx=ctx)
    assert preview.ai_action_id is not None


async def test_an_act_session_and_its_tools_count_codes_held_on_any_client(
    api_env: ApiEnv, monkeypatch: Any
) -> None:
    """Opening an act conversation and the tool prefilter are ergonomics --
    each proposal and read re-checks on its record's client -- so they count
    a code held on ANY client: a technician of X opens an act session and is
    offered the act tool and the read tools."""
    from app.core.ai.providers import TextBlock
    from app.modules.ai_admin import api as api_module
    from tests.ai_admin_fakes import ScriptedProvider

    world = await _ai_world(api_env, "cfg-ai-session")
    x_tech = await everything_on(api_env, world.org_id, world.x)
    created = await api_env.client.post(f"{AI}/sessions", json={"mode": "act"}, headers=x_tech)
    assert created.status_code == 201, created.text
    provider = ScriptedProvider([[TextBlock(text="nothing to report")]])
    monkeypatch.setattr(api_module, "_provider_for", lambda: provider)
    turn = await api_env.client.post(
        f"{AI}/sessions/{created.json()['id']}/turns", json={"question": "anything?"},
        headers=x_tech,
    )
    assert turn.status_code == 200, turn.text
    tools = " ".join(provider.seen[0]["tools"])
    assert "actions.propose" in tools and "devices.health" in tools, tools


async def test_the_copilot_asks_ai_use_on_the_ticket_s_client(
    api_env: ApiEnv, monkeypatch: Any
) -> None:
    """`ai.use` and the copilot's tool list are the ticket's client's: held
    on X, a copilot session about X's ticket opens; held on Y only (with
    `ticket.read` on X), it is the old 403."""
    from app.models import Ticket, TicketCategory, TicketQueue

    org = await api_env.create_org("cfg-copilot")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin_user = await api_env.create_user(org, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == admin_user))
        ).scalar_one()
        queue = TicketQueue(org_id=org, key=f"q-{uuid4().hex[:6]}", name="Q", created_by=user_id)
        s.add(queue)
        await s.flush()
        category = TicketCategory(org_id=org, key=f"c-{uuid4().hex[:6]}", name="C",
                                  type="incident", default_queue_id=queue.id,
                                  created_by=user_id)
        s.add(category)
        await s.flush()
        ticket = Ticket(org_id=org, client_id=x, number=f"T-{uuid4().hex[:6]}",
                        type="incident", category_id=category.id, queue_id=queue.id,
                        status="new", subject="lento", requester_id=user_id,
                        origin="technician", created_by=user_id)
        s.add(ticket)
        await s.commit()
    path = f"/api/v1/service-desk/tickets/{ticket.id}/ai/sessions"

    _, elsewhere = await _holder(api_env, org, {x: ("ticket.read",), y: ("ai.use",)})
    r = await api_env.client.post(path, json={"mode": "read"}, headers=elsewhere)
    assert r.status_code == 403, r.text
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(path, json={"mode": "act"}, headers=x_tech)
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Remote (T4-C2 item 13)
# --------------------------------------------------------------------------- #

REMOTE = "/api/v1/remote/sessions"


async def _remote_session(env: ApiEnv, org: UUID, device: UUID, technician: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        row = RemoteSession(org_id=org, device_id=device, technician_id=technician,
                            protocol="screen", consent_mode="prompt", state="awaiting_consent")
        s.add(row)
        await s.commit()
        return row.id


async def _user(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        u = User(org_id=org, external_subject=f"u-{uuid4().hex[:8]}", email="u@x.y")
        s.add(u)
        await s.commit()
        return u.id


async def test_remote_sessions_are_read_on_their_machine_s_client(api_env: ApiEnv) -> None:
    """`remote.control` or `remote.recordings.read` is asked about the
    session's machine's client: held on X, X's sessions are listed and read,
    Y's are not (404 like a missing one) though the person holds a role on Y;
    held nowhere, the old 403."""
    org = await api_env.create_org("cfg-remote")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    tech = await _user(api_env, org)
    x_session = await _remote_session(api_env, org, await _device(api_env, org, x, x_site), tech)
    y_session = await _remote_session(api_env, org, await _device(api_env, org, y, y_site), tech)

    _, reader = await _holder(
        api_env, org, {x: ("remote.recordings.read",), y: ("devices.read",)}
    )
    listed = await api_env.client.get(REMOTE, headers=reader)
    assert listed.status_code == 200, listed.text
    assert ([i["id"] for i in listed.json()["items"]], listed.json()["total"]) == (
        [str(x_session)], 1
    )
    assert (await api_env.client.get(f"{REMOTE}/{x_session}", headers=reader)).status_code == 200
    hidden = await api_env.client.get(f"{REMOTE}/{y_session}", headers=reader)
    missing = await api_env.client.get(f"{REMOTE}/{uuid4()}", headers=reader)
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()) == (
        404, missing.json()
    )

    _, nobody = await _holder(api_env, org, {x: ("devices.read",)})
    assert (await api_env.client.get(REMOTE, headers=nobody)).status_code == 403


async def test_stopping_someone_else_s_session_needs_orgs_manage_on_its_client(
    api_env: ApiEnv,
) -> None:
    """The owner-or-admin rail: `orgs.manage` held on X stops another
    technician's session on X's machine; held on Y only, the old 403, and
    the session is unchanged."""
    org = await api_env.create_org("cfg-remote-stop")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    first = await _remote_session(api_env, org, device, owner)
    second = await _remote_session(api_env, org, device, owner)

    _, elsewhere = await _holder(api_env, org, {x: ("remote.control",), y: ("orgs.manage",)})
    r = await api_env.client.post(f"{REMOTE}/{first}/stop", headers=elsewhere)
    assert r.status_code == 403, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.get(RemoteSession, first)).state == "awaiting_consent"  # type: ignore[union-attr]

    _, x_admin = await _holder(api_env, org, {x: ("remote.control", "orgs.manage")})
    r = await api_env.client.post(f"{REMOTE}/{second}/stop", headers=x_admin)
    assert r.status_code == 200, r.text


async def test_the_screen_tunnel_s_rail_asks_the_session_s_client(api_env: ApiEnv) -> None:
    """`owner_or_admin`, shared by the stop route and the tunnel door: the
    person's `orgs.manage` on the session's client."""
    from app.modules.remote.tunnel import owner_or_admin

    org = await api_env.create_org("cfg-remote-rail")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    owner = await _user(api_env, org)
    device = await _device(api_env, org, x, x_site)
    on_x, _ = await _holder(api_env, org, {x: ("orgs.manage",)})
    on_y, _ = await _holder(api_env, org, {y: ("orgs.manage",)})
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await owner_or_admin(
            s, user_id=on_x, session_technician_id=owner, client_id=x
        )
        assert not await owner_or_admin(
            s, user_id=on_y, session_technician_id=owner, client_id=x
        )
        assert await owner_or_admin(s, user_id=owner, session_technician_id=owner, client_id=x)
    del device


# --------------------------------------------------------------------------- #
# Upsert races (Step 4b; ledger "upsert first-writer races")
# --------------------------------------------------------------------------- #


def _race_hooks(table: str, competitor: Any) -> tuple[list[str], Any, Any]:
    """The Task 8 interleave (`tests/test_budget_fx.py`): the competitor
    commits INSIDE the window a check-then-act lives in -- right after the
    first SELECT that touches `table`, or right before the first INSERT,
    whichever comes first, once. It always commits before the first writer
    has written, so it never waits on the first writer and cannot hang."""
    raced: list[str] = []

    def first_touch(statement: str, kind: str) -> bool:
        return not raced and table in statement and statement.lstrip().upper().startswith(kind)

    def before(conn: Connection, cursor: object, statement: str, *rest: object) -> None:
        if first_touch(statement, "INSERT"):
            raced.append("before INSERT")
            await_only(competitor())

    def after(conn: Connection, cursor: object, statement: str, *rest: object) -> None:
        if first_touch(statement, "SELECT"):
            raced.append("after SELECT")
            await_only(competitor())

    return raced, before, after


@pytest.mark.parametrize("scope", ["group", "every-device"])
async def test_two_first_writers_of_one_gate_do_not_race(api_env: ApiEnv, scope: str) -> None:
    """`set_gate` is one `INSERT ... ON CONFLICT` on the per-client key: a
    colleague's gate for the same group (or the global every-device gate)
    committed inside the window is updated in place, not a 500."""
    from app.modules.change_mgmt.gates import set_gate

    org = await api_env.create_org(f"cfg-gate-race-{scope}")
    x, _ = await client_with_site(api_env, org)
    group: UUID | None = None
    if scope == "group":
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
            g = DeviceGroup(org_id=org, client_id=x, name="g")
            s.add(g)
            await s.commit()
            group = g.id
        admin = await _admin(api_env, org)
        r = await api_env.client.patch(
            f"/api/v1/clients/{x}", json={"enabled_modules": ["change"]}, headers=admin
        )
        assert r.status_code == 200, r.text

    async def colleague() -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            s.add(ChangeGate(org_id=org, device_group_id=group, applies_to_patches=True,
                             applies_to_deployments=True))
            await s.commit()

    raced, before, after = _race_hooks("change_gates", colleague)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        connection = (await s.connection()).sync_connection
        event.listen(connection, "before_cursor_execute", before)
        event.listen(connection, "after_cursor_execute", after)
        gate = await set_gate(s, org_id=org, actor_id=None, device_group_id=group,  # type: ignore[arg-type]
                              applies_to_patches=True, applies_to_deployments=False)
        gate_id = gate.id
    assert raced
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(ChangeGate.id, ChangeGate.applies_to_deployments).where(
                    ChangeGate.org_id == org
                )
            )
        ).all()
    assert [(row.id, row.applies_to_deployments) for row in rows] == [(gate_id, False)]


async def test_gate_audits_carry_the_gate_s_client(api_env: ApiEnv) -> None:
    """`change.gate_set` / `change.gate_removed` are filed under the gate's
    client: its group's, or none for the global every-device gate."""
    org = await api_env.create_org("cfg-gate-audit")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    r = await api_env.client.patch(
        f"/api/v1/clients/{x}", json={"enabled_modules": ["change"]}, headers=admin
    )
    assert r.status_code == 200, r.text
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        g = DeviceGroup(org_id=org, client_id=x, name="g")
        s.add(g)
        await s.commit()
    for group in (g.id, None):
        r = await api_env.client.put(
            "/api/v1/changes/gates",
            json={"device_group_id": None if group is None else str(group),
                  "applies_to_patches": True, "applies_to_deployments": True},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        r = await api_env.client.delete(f"/api/v1/changes/gates/{r.json()['id']}",
                                        headers=admin)
        assert r.status_code in (200, 204), r.text
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id).where(
                    AuditEvent.org_id == org, AuditEvent.action.like("change.gate_%")
                )
            )
        ).all()
    assert sorted(rows, key=lambda r: (r[0], str(r[1]))) == sorted(
        [("change.gate_removed", x), ("change.gate_removed", None),
         ("change.gate_set", x), ("change.gate_set", None)],
        key=lambda r: (r[0], str(r[1])),
    )


@pytest.mark.parametrize("transport", ["ssh", "winrm"])
async def test_two_first_observations_of_one_host_do_not_race(
    api_env: ApiEnv, transport: str
) -> None:
    """`record_observation` / `record_readiness` are one `INSERT ... ON
    CONFLICT` on (org, client, address): a probe of the same host committed
    inside the window is updated, not a 500 -- and its confirmation, the
    trust anchor, survives the update."""
    from app.modules.agent_push import host_key as host_key_service

    org = await api_env.create_org(f"cfg-hk-race-{transport}")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    address = "10.77.0.9"

    async def colleague() -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            s.add(PushHostKey(org_id=org, device_id=device, address=address, transport="ssh",
                              observed_fingerprint="SHA256:b", observed_key_type="ssh-ed25519",
                              confirmed_fingerprint="SHA256:b"))
            await s.commit()

    raced, before, after = _race_hooks("push_host_key", colleague)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        connection = (await s.connection()).sync_connection
        event.listen(connection, "before_cursor_execute", before)
        event.listen(connection, "after_cursor_execute", after)
        if transport == "ssh":
            await host_key_service.record_observation(
                s, org, address=address, transport="ssh", fingerprint="SHA256:a",
                key_type="ssh-ed25519", device_id=device, job_id=None,
            )
        else:
            await host_key_service.record_readiness(
                s, org, address=address, device_id=device, job_id=None,
                readiness={"winrm_https": True, "winrm_http": False, "kdc_reachable": True},
            )
    assert raced
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(select(PushHostKey).where(PushHostKey.org_id == org))
        ).scalars().all()
    observed = "SHA256:a" if transport == "ssh" else host_key_service.WINRM_OBSERVATION
    assert [(r.client_id, r.observed_fingerprint, r.confirmed_fingerprint, r.transport)
            for r in rows] == [(x, observed, "SHA256:b", transport)]


# --------------------------------------------------------------------------- #
# Moving a device between its client's sites (Step 4b; spec §1.5)
# --------------------------------------------------------------------------- #


async def _site(env: ApiEnv, org: UUID, client: UUID, *, archived: bool = False) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        site = Site(org_id=org, client_id=client, name=f"Sede {uuid4().hex[:4]}",
                    archived_at=_dt.datetime.now(_dt.UTC) if archived else None)
        s.add(site)
        await s.commit()
        return site.id


async def _site_of(env: ApiEnv, device: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        found: UUID = (
            await s.execute(select(Device.site_id).where(Device.id == device))
        ).scalar_one()
    return found


async def test_a_device_moves_between_its_client_s_sites(api_env: ApiEnv) -> None:
    """Within its client: 200, the new site answered and stored, audited
    `device.site_changed` with both sites under the device's client. To a
    site of another client: 422, nothing written; to one the technician
    cannot see: 404; to an archived one: 409."""
    org = await api_env.create_org("cfg-site")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    second = await _site(api_env, org, x)
    closed = await _site(api_env, org, x, archived=True)
    device = await _device(api_env, org, x, x_site)
    x_tech = await everything_on(api_env, org, x)
    admin = await _admin(api_env, org)
    path = f"/api/v1/devices/{device}"

    r = await api_env.client.patch(path, json={"site_id": str(second)}, headers=x_tech)
    assert r.status_code == 200, r.text
    assert (r.json()["site_id"], r.json()["client_id"]) == (str(second), str(x))
    assert await _site_of(api_env, device) == second
    async with AsyncSession(api_env.admin_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id, AuditEvent.meta).where(
                    AuditEvent.org_id == org, AuditEvent.target_id == str(device)
                )
            )
        ).all()
    assert [(e.action, e.client_id, e.meta) for e in events] == [
        ("device.site_changed", x, {"from_site_id": str(x_site), "to_site_id": str(second)})
    ]

    def written() -> Any:
        return _count_audits(api_env, org)

    before = await written()
    cases = [
        (admin, y_site, 422, "site {} belongs to another client"),
        (x_tech, y_site, 404, "no such site"),
        (admin, uuid4(), 404, "no such site"),
        (admin, closed, 409, "that site is archived"),
    ]
    for headers, site, code, detail in cases:
        r = await api_env.client.patch(path, json={"site_id": str(site)}, headers=headers)
        assert (r.status_code, r.json()["detail"]) == (code, detail.format(site)), r.text
    assert await _site_of(api_env, device) == second
    assert await written() == before


async def test_moving_to_the_same_site_or_editing_other_fields_is_no_site_change(
    api_env: ApiEnv,
) -> None:
    """A PATCH naming the site the device is already at moves nothing and
    writes no `device.site_changed`; other fields still audit
    `asset.updated` under the device's client, without the site in them."""
    org = await api_env.create_org("cfg-site-same")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.patch(
        f"/api/v1/devices/{device}", json={"site_id": str(x_site), "owner": "Ana"},
        headers=x_tech,
    )
    assert r.status_code == 200, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id, AuditEvent.meta).where(
                    AuditEvent.org_id == org, AuditEvent.target_id == str(device)
                )
            )
        ).all()
    assert [(e.action, e.client_id, e.meta) for e in events] == [
        ("asset.updated", x, {"fields": ["owner"]})
    ]
    r = await api_env.client.patch(
        f"/api/v1/devices/{device}", json={"site_id": None}, headers=x_tech
    )
    assert r.status_code == 422, r.text

    # A move AND another field in one request: both events, each under X.
    second = await _site(api_env, org, x)
    r = await api_env.client.patch(
        f"/api/v1/devices/{device}", json={"site_id": str(second), "owner": "Bo"},
        headers=x_tech,
    )
    assert r.status_code == 200, r.text
    assert r.json()["site_id"] == str(second)
    async with AsyncSession(api_env.admin_engine) as s:
        later = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id, AuditEvent.meta)
                .where(AuditEvent.org_id == org, AuditEvent.target_id == str(device))
                .order_by(AuditEvent.id)
            )
        ).all()
    assert [(e.action, e.client_id, e.meta) for e in later[1:]] == [
        ("device.site_changed", x, {"from_site_id": str(x_site), "to_site_id": str(second)}),
        ("asset.updated", x, {"fields": ["owner"]}),
    ]


async def _count_audits(env: ApiEnv, org: UUID) -> int:
    async with AsyncSession(env.admin_engine) as s:
        return int(
            (
                await s.execute(
                    select(func.count()).select_from(AuditEvent).where(AuditEvent.org_id == org)
                )
            ).scalar_one()
        )


async def test_the_device_client_trigger_is_the_backstop(api_env: ApiEnv) -> None:
    """Were the route's check gone, `its_device_client_guard` (0231) would
    still refuse a site of another client."""
    from sqlalchemy.exc import DBAPIError

    org = await api_env.create_org("cfg-site-trigger")
    x, x_site = await client_with_site(api_env, org)
    _, y_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    async with AsyncSession(api_env.admin_engine) as s:
        with pytest.raises(DBAPIError, match="device site belongs to another client"):
            await s.execute(
                text("UPDATE devices SET site_id = :s WHERE id = :d"), {"s": y_site, "d": device}
            )
        await s.rollback()

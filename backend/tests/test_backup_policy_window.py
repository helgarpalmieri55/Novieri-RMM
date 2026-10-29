"""A backup policy's window is checked before it is stored (re-review of
b805432, Important).

`PUT /backup/policies` took any string as `window_cron`. The window sweep
reads it with `app.core.schedule.last_fire`, which raises on anything that is
not five valid cron fields, and the sweep runs ONE transaction per
organisation: one bad expression rolled back every device's window in that
organisation, every 15 minutes. So the write now reads the expression with
that same parser (`app.core.schedule.checked_window`, which deployments
share since the final review) and refuses what it
refuses: a 422 with a sentence in the person's language, and nothing written
anywhere, the audit log included. `null` is no window (backups on demand
only); an empty or blank string is the same and is stored as NULL. The check
is in the service, so every caller meets it: the console's route, and the
MCP tool `backup_policies_set`, which runs that route.

Re-review 2 (421c806): the check reads only the grammar's own ASCII
characters before the parser runs (`int()` takes `9_9_9` and every script's
digits, which slipped past a digit cap), refuses a field that matches nothing,
and the sweep itself now contains a window it cannot read -- one stored
before any of this -- to its own policy (`dispatch_due_backup`), recorded
once per policy version.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.schedule import InvalidWindow, checked_window
from app.core.tenancy import org_session
from app.models import AuditEvent, BackupPolicy, BackupRun, Device, DeviceGroup
from app.modules.backup.dispatch import dispatch_due_backup
from app.modules.backup.policy import upsert_policy
from app.modules.backup.repository import create_repository
from tests.conftest import ApiEnv
from tests.mcp_support import (
    MCP_BASE,
    mcp_org,
    mcp_session,
    mcp_token,
    running_mcp,
    tool_error_text,
)
from tests.test_backup_dispatch import _FakeJobEngine

BK = "/api/v1/backup"
REFUSED = (
    "window_cron must be a cron expression of five fields (minute hour day-of-month month "
    "day-of-week), or null for no window"
)
REFUSED_ES = (
    "window_cron debe ser una expresión cron de cinco campos (minuto hora día-del-mes mes "
    "día-de-la-semana), o null para no tener ventana"
)

#: What the sweep's parser refuses: the string "null" (not JSON null), four
#: fields, six (a seconds field), garbage, a value out of range, a zero step.
BAD = ["null", "0 2 * *", "0 0 2 * * *", "every night", "61 2 * * *", "*/0 2 * * *"]
#: Refused before the parser ever runs on them: a number no cron field
#: reaches (the parser used to expand a billion-value range first), the same
#: number spelled with PEP 515 underscores, Arabic-Indic and full-width digits
#: (`int()` reads all three), a sign (`int("+5")` is 5, the grammar has none),
#: and an expression longer than the column.
UNREAD = [
    "0-999999999 2 * * *",
    "0-9_9_9_9_9_9_9_9_9 2 * * *",
    "\u0660-\u0669\u0669 2 * * *",
    "\uff10-\uff19\uff19 2 * * *",
    "+5 2 * * *",
    "0 2 * * " + ",".join(["1"] * 60),
]
#: Read by the sweep, and never open: a field that matches nothing.
NEVER = ["30-10 2 * * *", "*/-1 2 * * *"]
#: What is stored, and as what.
GOOD = [("30 2 * * 1-5", "30 2 * * 1-5"), (" 0 3 * * * ", "0 3 * * *"), ("", None),
        ("   ", None), (None, None)]


async def _world(env: ApiEnv, org: UUID) -> tuple[dict[str, str], UUID, UUID]:
    """(admin headers, the actor, a repository of the internal client)."""
    sub = await env.create_user(org, "org-admin")
    headers = await env.headers(org, sub)
    me = await env.client.get("/api/v1/me", headers=headers)
    actor = UUID(me.json()["user_id"])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        repo = await create_repository(s, env.app.state.vault, org_id=org, actor_id=actor,
                                       client_id=org, name="r")
    return headers, actor, repo.id


async def _written(env: ApiEnv, org: UUID) -> tuple[int, int]:
    async with AsyncSession(env.admin_engine) as s:
        policies = (await s.execute(select(func.count()).select_from(BackupPolicy).where(
            BackupPolicy.org_id == org))).scalar_one()
        events = (await s.execute(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.org_id == org))).scalar_one()
    return policies, events


async def _stored(env: ApiEnv, org: UUID) -> str | None:
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(BackupPolicy.window_cron).where(
            BackupPolicy.org_id == org))).scalar_one()


def test_the_check_is_the_sweep_s_own_parser() -> None:
    """Every refused expression is one `last_fire` raises on, every stored one
    an expression it reads (or no window)."""
    from datetime import UTC, datetime

    from app.core.schedule import last_fire

    for cron in BAD:
        with pytest.raises(ValueError):
            last_fire(cron, datetime.now(UTC))
        with pytest.raises(InvalidWindow):
            checked_window(cron)
    for given, stored in GOOD:
        assert checked_window(given) == stored
        if stored is not None:
            last_fire(stored, datetime.now(UTC))


def test_what_the_parser_would_choke_on_is_refused_before_it() -> None:
    from unittest import mock

    with (
        mock.patch("app.core.schedule.parse_cron") as parser,
        mock.patch("app.core.schedule.last_fire") as fire,
    ):
        for cron in UNREAD:
            with pytest.raises(InvalidWindow):
                checked_window(cron)
    parser.assert_not_called()
    fire.assert_not_called()


def test_a_window_that_never_opens_is_refused() -> None:
    """The sweep reads these and finds no fire ever: refused, not stored."""
    from datetime import UTC, datetime

    from app.core.schedule import last_fire

    for cron in NEVER:
        assert last_fire(cron, datetime.now(UTC)) is None
        with pytest.raises(InvalidWindow):
            checked_window(cron)


@pytest.mark.parametrize("cron", [*BAD, *UNREAD, *NEVER])
async def test_the_api_refuses_a_window_the_sweep_cannot_read(
    api_env: ApiEnv, cron: str
) -> None:
    org = await api_env.create_org("bk-window-bad")
    headers, _actor, repo = await _world(api_env, org)
    before = await _written(api_env, org)
    body = {"client_id": str(org), "repository_id": str(repo), "window_cron": cron}
    r = await api_env.client.put(f"{BK}/policies", json=body, headers=headers)
    assert (r.status_code, r.json()) == (422, {"detail": REFUSED}), r.text
    r = await api_env.client.put(f"{BK}/policies", json=body,
                                 headers={**headers, "Accept-Language": "es"})
    assert (r.status_code, r.json()) == (422, {"detail": REFUSED_ES}), r.text
    assert await _written(api_env, org) == before


@pytest.mark.parametrize(("cron", "stored"), GOOD)
async def test_the_api_stores_a_window_the_sweep_can_read(
    api_env: ApiEnv, cron: str | None, stored: str | None
) -> None:
    org = await api_env.create_org("bk-window-good")
    headers, _actor, repo = await _world(api_env, org)
    body = {"client_id": str(org), "repository_id": str(repo), "window_cron": cron}
    r = await api_env.client.put(f"{BK}/policies", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["window_cron"] == stored
    assert await _stored(api_env, org) == stored


async def test_the_service_refuses_before_it_takes_a_lock_or_writes(api_env: ApiEnv) -> None:
    """The service path, for any caller other than the route: every refused
    expression raises before the client is even read, so nothing is held
    and nothing written."""
    org = await api_env.create_org("bk-window-svc")
    _headers, actor, repo = await _world(api_env, org)
    before = await _written(api_env, org)
    for cron in BAD:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            with pytest.raises(InvalidWindow):
                await upsert_policy(
                    s, org_id=org, actor_id=actor, device_group_id=None, client_id=org,
                    repository_id=repo, sources={}, window_cron=cron, retention_gfs={},
                    enabled=True,
                )
    assert await _written(api_env, org) == before


@pytest.fixture()
async def mcp_env(api_env: ApiEnv) -> ApiEnv:
    """`api_env` with `ITS_PUBLIC_CONSOLE_URL` set before the MCP server
    starts: the gate answers only there."""
    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def test_the_mcp_tool_meets_the_same_check(mcp_env: Any) -> None:
    """`backup_policies_set` runs the route through the full ASGI stack, so it
    meets the same check: each refused expression is the tool's "invalid
    arguments" with the API's sentence, and the route writes nothing (the
    MCP layer's own `mcp.tool_called` record of each call is its business);
    the empty string is stored as no window."""
    org, slug = await mcp_org(mcp_env)
    sub = await mcp_env.create_user(org, "org-admin")
    me = await mcp_env.client.get("/api/v1/me", headers=await mcp_env.headers(org, sub))
    actor = UUID(me.json()["user_id"])
    async with org_session(org, clients=ClientScope.all(), engine=mcp_env.app_engine) as s:
        repo = await create_repository(s, mcp_env.app.state.vault, org_id=org, actor_id=actor,
                                       client_id=org, name="r")
    token = mcp_token(mcp_env, slug, sub)
    before = await _written(mcp_env, org)
    async with running_mcp(mcp_env), mcp_session(mcp_env, token) as session:
        for cron in [*BAD, *UNREAD, *NEVER]:
            result = await session.call_tool("backup_policies_set", {"body": {
                "client_id": str(org), "repository_id": str(repo.id), "window_cron": cron,
            }})
            assert tool_error_text(result) == f"invalid arguments: {REFUSED}", cron
        policies, _events = await _written(mcp_env, org)
        assert policies == before[0]
        async with AsyncSession(mcp_env.admin_engine) as s:
            changed = (await s.execute(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.org_id == org, AuditEvent.action == "backup.policy_changed"))
            ).scalar_one()
        assert changed == 0
        result = await session.call_tool("backup_policies_set", {"body": {
            "client_id": str(org), "repository_id": str(repo.id), "window_cron": "",
        }})
    assert result.is_error is False, result
    assert result.structured_content is not None
    assert result.structured_content["window_cron"] is None
    assert await _stored(mcp_env, org) is None


# --------------------------------------------------------------------------- #
# The sweep contains a stored window it cannot read to its own policy
# --------------------------------------------------------------------------- #


async def _grouped_device(env: ApiEnv, org: UUID, repo: UUID, window: str) -> tuple[UUID, UUID]:
    """A device under a group policy of the internal client whose window is
    `window`, written straight to the table -- the way a value stored before
    the API checked it is held. Both protected since long ago."""
    from datetime import UTC, datetime

    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        group = DeviceGroup(org_id=org, client_id=org, name=f"g-{window}")
        s.add(group)
        await s.flush()
        policy = BackupPolicy(org_id=org, device_group_id=group.id, repository_id=repo,
                              sources={}, window_cron="0 2 * * *", retention_gfs={},
                              enabled=True)
        device = Device(org_id=org, client_id=org, site_id=org, hostname=f"h-{window}",
                        os_family="linux", device_group_id=group.id)
        s.add_all([policy, device])
        await s.flush()
        policy.created_at = long_ago
        device.created_at = long_ago
        await s.commit()
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(text("UPDATE backup_policies SET window_cron = :w WHERE id = :p"),
                        {"w": window, "p": policy.id})
        await s.commit()
    return device.id, policy.id


async def test_one_unreadable_stored_window_stops_only_its_own_policy(api_env: ApiEnv) -> None:
    """Re-review 2: a policy whose stored window the parser cannot read --
    four fields, or the billion-wide range that used to be expanded first --
    used to raise inside the organisation's one sweep transaction and roll
    back every device's window. Now, on the same sweep, the device under the
    valid policy is dispatched; the others are skipped; and each unreadable
    policy is recorded ONCE (`backup.dispatch_refused`, reason
    `unreadable_window`, target the policy), not again on the next sweep."""
    from datetime import UTC, datetime, timedelta

    org = await api_env.create_org("bk-window-contain")
    _headers, _actor, repo = await _world(api_env, org)
    valid, _ = await _grouped_device(api_env, org, repo, "0 2 * * *")
    unreadable = {
        window: await _grouped_device(api_env, org, repo, window)
        for window in ("0 2 * *", "0-999999999 2 * * *")
    }
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    engine = _FakeJobEngine()
    for minute in (5, 20):
        now = datetime(day.year, day.month, day.day, 2, minute, tzinfo=UTC)
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            for device in [*(d for d, _ in unreadable.values()), valid]:
                await dispatch_due_backup(s, engine, api_env.app.state.vault, org_id=org,
                                          device_id=device, now=now)
    assert [c["target_device_id"] for c in engine.created] == [valid]
    async with AsyncSession(api_env.admin_engine) as s:
        runs = (await s.execute(select(BackupRun.device_id).where(BackupRun.org_id == org))
                ).scalars().all()
        refusals = (await s.execute(select(AuditEvent).where(
            AuditEvent.org_id == org, AuditEvent.action == "backup.dispatch_refused"
        ).order_by(AuditEvent.id))).scalars().all()
    assert runs == [valid]
    assert sorted((e.target_type, e.target_id, e.meta["reason"], e.meta["window_cron"])
                  for e in refusals) == sorted(
        ("backup_policy", str(policy), "unreadable_window", window)
        for window, (_, policy) in unreadable.items()
    )
    assert {e.actor_type for e in refusals} == {"system"}
    assert {e.client_id for e in refusals} == {org}


async def test_an_unreadable_window_of_a_policy_for_every_client_is_recorded_once(
    api_env: ApiEnv,
) -> None:
    """A client-less policy has no client, so its record has none: the
    "already recorded?" question compares clients with IS NOT DISTINCT FROM,
    or a NULL would never match and every sweep would record it again. There
    is no policy for every client (`backup/policy.py`), so the row is forced
    in with the repository guard off, as only a hand could plant one."""
    from datetime import UTC, datetime, timedelta

    from tests.client_scope_support import client_with_site

    org = await api_env.create_org("bk-window-global")
    _headers, _actor, repo = await _world(api_env, org)
    customer, site = await client_with_site(api_env, org)
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await s.execute(text(
            "ALTER TABLE backup_policies DISABLE TRIGGER its_zz_backup_policy_repository_guard"
        ))
        policy = BackupPolicy(org_id=org, client_id=None, device_group_id=None,
                              repository_id=repo, sources={}, window_cron="0 2 * *",
                              retention_gfs={}, enabled=True)
        device = Device(org_id=org, client_id=customer, site_id=site, hostname="h",
                        os_family="linux")
        s.add_all([policy, device])
        await s.flush()
        policy.created_at = long_ago
        device.created_at = long_ago
        await s.execute(text(
            "ALTER TABLE backup_policies ENABLE TRIGGER its_zz_backup_policy_repository_guard"
        ))
        await s.commit()
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    for minute in (5, 20, 35):
        now = datetime(day.year, day.month, day.day, 2, minute, tzinfo=UTC)
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            assert await dispatch_due_backup(s, _FakeJobEngine(), api_env.app.state.vault,
                                             org_id=org, device_id=device.id, now=now) == []
    async with AsyncSession(api_env.admin_engine) as s:
        refusals = (await s.execute(select(AuditEvent).where(
            AuditEvent.org_id == org, AuditEvent.action == "backup.dispatch_refused"
        ))).scalars().all()
    assert [(e.target_id, e.client_id, e.meta["reason"]) for e in refusals] == [
        (str(policy.id), None, "unreadable_window")
    ]


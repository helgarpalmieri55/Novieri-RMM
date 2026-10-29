"""A backup policy's window is checked before it is stored (re-review of
b805432, Important).

`PUT /backup/policies` took any string as `window_cron`. The window sweep
reads it with `app.core.schedule.last_fire`, which raises on anything that is
not five valid cron fields, and the sweep runs ONE transaction per
organisation: one bad expression rolled back every device's window in that
organisation, every 15 minutes. So the write now reads the expression with
that same parser (`backup.schedule.checked_window`) and refuses what it
refuses: a 422 with a sentence in the person's language, and nothing written
anywhere, the audit log included. `null` is no window (backups on demand
only); an empty or blank string is the same and is stored as NULL. The check
is in the service, so every caller meets it: the console's route, and the
MCP tool `backup_policies_set`, which runs that route.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, BackupPolicy
from app.modules.backup.policy import upsert_policy
from app.modules.backup.repository import create_repository
from app.modules.backup.schedule import InvalidWindow, checked_window
from tests.conftest import ApiEnv
from tests.mcp_support import mcp_token, running_mcp
from tests.test_mcp_tools import _error_text, _org, _session, mcp_env  # noqa: F401

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
#: reaches (the parser would first expand a billion-value range), and an
#: expression longer than the column.
UNREAD = ["0-999999999 2 * * *", "0 2 * * " + ",".join(["1"] * 60)]
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

    with mock.patch("app.modules.backup.schedule.last_fire") as parser:
        for cron in UNREAD:
            with pytest.raises(InvalidWindow):
                checked_window(cron)
    parser.assert_not_called()


@pytest.mark.parametrize("cron", [*BAD, *UNREAD])
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


async def test_the_mcp_tool_meets_the_same_check(mcp_env: Any) -> None:  # noqa: F811
    """`backup_policies_set` runs the route through the full ASGI stack, so it
    meets the same check: each refused expression is the tool's "invalid
    arguments" with the API's sentence, and the route writes nothing (the
    MCP layer's own `mcp.tool_called` record of each call is its business);
    the empty string is stored as no window."""
    org, slug = await _org(mcp_env)
    sub = await mcp_env.create_user(org, "org-admin")
    me = await mcp_env.client.get("/api/v1/me", headers=await mcp_env.headers(org, sub))
    actor = UUID(me.json()["user_id"])
    async with org_session(org, clients=ClientScope.all(), engine=mcp_env.app_engine) as s:
        repo = await create_repository(s, mcp_env.app.state.vault, org_id=org, actor_id=actor,
                                       client_id=org, name="r")
    token = mcp_token(mcp_env, slug, sub)
    before = await _written(mcp_env, org)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        for cron in BAD:
            result = await session.call_tool("backup_policies_set", {"body": {
                "client_id": str(org), "repository_id": str(repo.id), "window_cron": cron,
            }})
            assert _error_text(result) == f"invalid arguments: {REFUSED}", cron
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

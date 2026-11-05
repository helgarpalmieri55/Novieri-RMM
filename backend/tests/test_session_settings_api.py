"""Settings → Security → Sessions: `GET/PUT /api/v1/settings/sessions` (P2 design §2.4).

The every-client gate of the PUT is held by `tests/test_global_writes.py`
(listed in `GLOBAL_WRITES`); here: the defaults, the bounds, the audit event,
and that a saved value governs the very next request of an open session.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent
from app.models.auth import AuthSession
from tests.session_support import sign_in, use_console

URL = "/api/v1/settings/sessions"
DEFAULTS = {
    "staff_idle_minutes": 30,
    "staff_max_hours": 12,
    "remember_device_enabled": True,
    "remember_device_days": 15,
    "remembered_max_hours": 168,
    "contact_idle_minutes": 60,
    "contact_max_days": 7,
}


async def _admin(env: Any) -> tuple[Any, dict[str, str]]:
    org_id = await env.create_org("sessions-settings")
    sub = await env.create_user(org_id, "org-admin")
    return org_id, await env.headers(org_id, sub)


async def test_an_organisation_that_never_saved_reads_the_defaults(api_env: Any) -> None:
    _, headers = await _admin(api_env)
    response = await api_env.client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {**DEFAULTS, "updated_at": None, "updated_by": None}


async def test_reading_needs_orgs_manage(api_env: Any) -> None:
    org_id = await api_env.create_org("sessions-settings")
    sub = await api_env.create_user(org_id, "read-only")
    response = await api_env.client.get(URL, headers=await api_env.headers(org_id, sub))
    assert response.status_code == 403


async def test_saving_records_each_change_before_and_after(api_env: Any) -> None:
    org_id, headers = await _admin(api_env)
    body = {**DEFAULTS, "staff_idle_minutes": 15, "remember_device_enabled": False}
    response = await api_env.client.put(URL, json=body, headers=headers)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert {k: saved[k] for k in DEFAULTS} == body
    assert saved["updated_at"] is not None and saved["updated_by"] is not None
    assert (await api_env.client.get(URL, headers=headers)).json() == saved

    async with AsyncSession(api_env.admin_engine) as s:
        events = (
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "auth.session_settings_updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_type == "organization"
    assert events[0].meta == {
        "changed": {"staff_idle_minutes": [30, 15], "remember_device_enabled": [True, False]}
    }

    again = await api_env.client.put(URL, json=body, headers=headers)
    assert again.status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        count = len(
            (
                await s.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "auth.session_settings_updated",
                    )
                )
            ).all()
        )
    assert count == 1, "an unchanged save records nothing"


@pytest.mark.parametrize(
    "change",
    [
        {"staff_idle_minutes": 4},
        {"staff_idle_minutes": 481},
        {"staff_max_hours": 25},
        {"remember_device_days": 31},
        {"remembered_max_hours": 721},
        {"contact_max_days": 31},
        {"staff_idle_minutes": 120, "staff_max_hours": 1},
        {"unknown": 1},
    ],
)
async def test_a_value_out_of_bounds_is_a_422(api_env: Any, change: dict[str, Any]) -> None:
    """The bounds are the table's CHECKs (Task 1) plus one the table cannot
    say: inactivity longer than the longest sign-in would never apply."""
    _, headers = await _admin(api_env)
    response = await api_env.client.put(URL, json={**DEFAULTS, **change}, headers=headers)
    assert response.status_code == 422, response.text


async def test_a_shorter_inactivity_limit_signs_out_the_idle_at_once(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read per request, no cache (`session_policy.py`): a session idle for 20
    minutes is alive under the default 30 and gone the moment 15 is saved."""
    use_console(monkeypatch, api_env)
    org_id = await api_env.create_org("sessions-settings")
    sub = await api_env.create_user(org_id, "org-admin")
    signed = await sign_in(api_env, org_id, sub)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await s.commit()
    assert (await api_env.client.get("/api/v1/me", headers=signed.headers)).status_code == 200

    headers = await api_env.headers(org_id, sub)
    await api_env.client.put(URL, json={**DEFAULTS, "staff_idle_minutes": 15}, headers=headers)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await s.commit()
    assert (await api_env.client.get("/api/v1/me", headers=signed.headers)).status_code == 401

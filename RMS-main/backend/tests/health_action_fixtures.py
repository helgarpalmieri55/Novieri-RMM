"""Seed helpers for the action-worker tests (Task 11).

SEPARATE FROM `health_fixtures.py` because these need the SERVICE-DESK and
DEPLOY rows an action touches -- a queue, a category, a script -- and most
health tests need none of them. A single fixture module that always seeded a
ticket queue would make "the org has a queue" a constant nobody notices, and
`open_ticket`'s skip-when-unconfigured branch would then be unreachable.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tests.health_fixtures import admin_engine_of


@dataclass(frozen=True)
class TicketingRefs:
    queue_id: UUID
    category_id: UUID


async def seed_ticketing(env_or_engine: Any, org_id: UUID, user_id: UUID) -> TicketingRefs:
    """A queue and an incident category, so `open_ticket` can actually run."""
    from app.models import TicketCategory, TicketQueue

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        queue = TicketQueue(
            org_id=org_id, key=f"q-{uuid4().hex[:8]}", name="Alerts", created_by=user_id
        )
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"c-{uuid4().hex[:8]}",
            name="Endpoint health",
            type="incident",
            default_queue_id=queue.id,
            created_by=user_id,
        )
        session.add(category)
        await session.commit()
        return TicketingRefs(queue_id=queue.id, category_id=category.id)


async def seed_script(env_or_engine: Any, org_id: UUID, user_id: UUID, **kw: Any) -> UUID:
    from app.models import Script

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {
        "name": f"script-{uuid4().hex[:8]}",
        "os_family": "windows",
        "interpreter": "powershell",
        "body": "Restart-Service -Name Spooler",
        "version": 1,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        script = Script(org_id=org_id, created_by=user_id, **values)
        session.add(script)
        await session.commit()
        return script.id


async def seed_approved_action(
    env_or_engine: Any, org_id: UUID, user_id: UUID, **kw: Any
) -> UUID:
    """An `approved_action` inserted DIRECTLY, bypassing the service layer.

    Deliberate: a test that exercises the dispatcher must not be seeded by the
    validation it is testing, or a validation bug seeds itself green.
    """
    from app.models import ApprovedAction

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {
        "key": f"act-{uuid4().hex[:8]}",
        "name": "Collect inventory",
        "description": "",
        "job_type": "inventory.collect",
        "params_template": {},
        "script_id": None,
        "script_version": None,
        "risk_class": "low",
        "destructive": False,
        "max_target_scope": "device",
        "autonomy": "autonomous",
        "personas": ["health"],
        "cooldown_s": 0,
        "max_per_device_per_day": 3,
        "max_per_org_per_hour": 20,
        "active": True,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        action = ApprovedAction(org_id=org_id, created_by=user_id, **values)
        session.add(action)
        await session.commit()
        return action.id


async def seed_action_row(
    env_or_engine: Any, org_id: UUID, alert_id: UUID, kind: str, **kw: Any
) -> UUID:
    from app.models import HealthAlertAction

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {"state": "pending"}
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = HealthAlertAction(org_id=org_id, alert_id=alert_id, kind=kind, **values)
        session.add(row)
        await session.commit()
        return row.id

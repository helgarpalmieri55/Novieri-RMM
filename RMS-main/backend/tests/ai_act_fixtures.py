"""Seeding for the act path's tests (docs/modules/ai-automation.md §6).

ONE HELPER BUILDS THE WHOLE WORLD a proposal needs -- an org, a device, an
`ai_session`, an `approved_action` carrying the persona, a ceiling row, and a
principal holding exactly the permission codes the test names. `ai_user` is used
rather than a seeded role because every seeded role is a bundle, and the
distinctions these tests are about ("holds `ai.act.confirm` and NOT
`patch.apply`") cannot be expressed with one.

THE APPROVED ACTION IS INSERTED DIRECTLY, bypassing health's service layer, for
the reason `health_action_fixtures.seed_approved_action` states: a test that
exercises a gate must not be seeded by the validation it is testing, or a
validation bug seeds itself green. It is also the only way to seed §13 test 4's
case -- a legacy row naming a job type the allowlist now refuses, which the write
path would reject.
"""

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.act import ProposeBinding
from app.core.ai.executor_staff import invoke_tool
from app.core.ai.types import TurnContext
from tests.ai_admin_fakes import ai_user
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_device, seed_org
from tests.test_ai_admin_migration import seed_session


@dataclass
class ActWorld:
    org_id: UUID
    user_id: UUID
    device_id: UUID
    ai_session_id: UUID
    action_id: UUID
    action_key: str
    headers: dict[str, str]
    ctx: TurnContext
    binding: ProposeBinding

    executor: Any = invoke_tool


async def build_world(
    api_env: Any,
    *,
    tag: str = "act",
    codes: tuple[str, ...] = ("ai.use", "ai.act.propose", "devices.read"),
    persona: str = "admin",
    ceiling: str | None = "require_approval",
    risk_class: str = "low",
    action_kw: dict[str, Any] | None = None,
    binding_kw: dict[str, Any] | None = None,
) -> ActWorld:
    """An org where `persona` may propose `action_key` against `device_id`.

    `ceiling=None` seeds NO ceiling row, which is how the deny-by-default case is
    exercised without a special path.
    """
    from app.models import AiAutonomyCeiling

    engine = api_env.admin_engine
    org = await seed_org(engine, tag)
    device_id = await seed_device(engine, org.org_id, hostname=f"{tag}-target")
    session_id = await seed_session(engine, org.org_id, org.user_id, persona=persona, mode="act")

    values: dict[str, Any] = {
        "job_type": "inventory.collect",
        "risk_class": risk_class,
        "destructive": False,
        "autonomy": "require_approval",
        "personas": ["health", persona],
        "cooldown_s": 0,
        "max_per_device_per_day": 50,
        "max_per_org_per_hour": 200,
    }
    values.update(action_kw or {})
    action_id = await seed_approved_action(engine, org.org_id, org.user_id, **values)

    if ceiling is not None:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                AiAutonomyCeiling(
                    org_id=org.org_id,
                    persona=persona,
                    risk_class=risk_class,
                    level=ceiling,
                )
            )
            await session.commit()

    user_id, sub = await ai_user(api_env, org.org_id, *codes)
    headers = await api_env.headers(org.org_id, sub)

    async with AsyncSession(engine) as session:
        from app.models import ApprovedAction

        action_key = (
            await session.get(ApprovedAction, action_id)
        ).key  # type: ignore[union-attr]

    ctx = TurnContext(
        app=api_env.app,
        bearer=headers["Authorization"],
        deadline=time.monotonic() + 30.0,
        org_id=org.org_id,
        user_id=user_id,
    )
    binding = ProposeBinding(
        persona=persona,
        ai_session_id=session_id,
        message_seq=1,
        actor_id=user_id,
        **(binding_kw or {}),
    )
    return ActWorld(
        org_id=org.org_id,
        user_id=user_id,
        device_id=device_id,
        ai_session_id=session_id,
        action_id=action_id,
        action_key=action_key,
        headers=headers,
        ctx=ctx,
        binding=binding,
    )

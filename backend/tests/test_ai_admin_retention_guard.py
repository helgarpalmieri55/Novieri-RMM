"""`ai_admin/retention.py::sweep_all_orgs`: the per-org guard named
`SQLAlchemyError`, so a failure that was not a database error escaped it.

Same family as `ipam_sweep_dispatch_all_orgs`' missing guard
(tests/test_worker_ipam_sweep_containment.py), and the opposite of the shape
`health/sweep.py::_for_each_org` and `ipam/reconcile.py::
reconcile_sweep_all_orgs` both use, which is `except Exception`.

`sweep_org`'s FIRST statement is `get_settings`, resolving that org's
`transcript_retention_days`. What it can raise is not bounded by SQLAlchemy's
exception hierarchy, and a guard naming one library's base class contains the
failures somebody already enumerated -- which is exactly the set that was never
going to take the fleet down.

Kept out of `test_ai_admin_retention.py` on purpose: this is a containment
property of the fan-out, not a retention-window property, and that file is
already the place where the `<` / `<=` boundary cases live.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AiMessage, AiSession
from tests.test_ai_admin_migration import seed_org_and_user

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _seed_expired_message(
    admin_engine: AsyncEngine, org_id: UUID, user_id: UUID
) -> UUID:
    """One conversation and one message, both far outside any retention window
    (the setting is bounded 1..365 days by a CHECK, so 400 days is expired for
    every possible org configuration)."""
    created = NOW - timedelta(days=400)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ai_session = AiSession(
            org_id=org_id,
            user_id=user_id,
            title="t",
            model_id="m",
            created_at=created,
            last_turn_at=created,
        )
        session.add(ai_session)
        await session.flush()
        message = AiMessage(
            org_id=org_id,
            session_id=ai_session.id,
            seq=0,
            role="user",
            content="q",
            created_at=created,
        )
        session.add(message)
        await session.commit()
        return message.id


async def test_ai_retention_survives_a_non_sqlalchemy_error_from_get_settings(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`sweep_org`'s FIRST statement is `get_settings`, and what it raises need
    not be a database error at all.

    TWO OBSERVABLE VALUES, both of which flip when the guard is narrowed back to
    `SQLAlchemyError`: the call returns instead of raising, and the healthy org's
    expired message is actually gone. The second matters because "it returned"
    alone would also be true of a sweep that gave up quietly."""
    from app.core.ai import retention

    doomed_org, _doomed_user = await seed_org_and_user(admin_engine, "retguard-doomed")
    healthy_org, healthy_user = await seed_org_and_user(admin_engine, "retguard-healthy")
    expired = await _seed_expired_message(admin_engine, healthy_org, healthy_user)

    real_get_settings = retention.get_settings

    async def flaky_get_settings(session: Any, org_id: UUID) -> Any:
        if org_id == doomed_org:
            # Deliberately NOT a SQLAlchemyError: a settings resolver can fail
            # on a value, a cache, or a network the database knows nothing about.
            raise RuntimeError("ai settings resolution failed")
        return await real_get_settings(session, org_id)

    total = 0
    escaped: BaseException | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(retention, "get_settings", flaky_get_settings)
        try:
            total = await retention.sweep_all_orgs(
                engine=app_engine, admin_engine=admin_engine, now=NOW
            )
        except Exception as exc:  # noqa: BLE001 - this IS the defect under test
            escaped = exc

    async with AsyncSession(admin_engine) as session:
        survivors = list(
            (
                await session.execute(select(AiMessage.id).where(AiMessage.id == expired))
            ).scalars()
        )
    assert survivors == [], (
        f"a healthy org's 400-day-old transcript survived ({escaped!r} escaped the "
        f"org loop) because another org's settings lookup raised something the "
        f"guard did not name"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"
    assert total >= 1

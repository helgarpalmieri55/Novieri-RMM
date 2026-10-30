"""Seed helpers shared by every `test_site_server_*.py` module.

Deliberately THIN, and on the ADMIN (RLS-bypassing) engine: a test that
exercises `registry.promote` must never be set up by `registry.promote`, or the
fixture and the code under test agree with each other and with nothing else.
Same shape and same reasoning as `tests/ipam_fixtures.py`, whose `seed_org`,
`seed_device` and `seed_agent` this module reuses rather than duplicating.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.ipam_fixtures import admin_engine_of

__all__ = ["seed_site_server", "site_server_rows"]


async def seed_site_server(
    env_or_engine: Any,
    org_id: UUID,
    *,
    device_id: UUID,
    segments: Sequence[str],
    roles: Sequence[str] = ("wake",),
    enabled: bool = True,
    name: str | None = None,
    site_id: UUID | None = None,
    row_id: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    """One `site_server` row, written straight through the admin engine.

    Raw SQL rather than the ORM so `segments` reaches Postgres as a real
    `cidr[]` literal: the point of the native type (§2.1) is containment, and a
    fixture that quietly stored text would make Task 8's `<<=` term untestable
    while every assertion here still passed.

    **`row_id` AND `created_at` ARE EXPLICIT BECAUSE "WHICH SITE SERVER GETS
    PICKED" IS A REAL QUESTION AND `uuid4()` + `now()` CANNOT ASK IT.** A
    selection test whose rows are seeded with random ids and a monotonic clock
    can only ever assert that SOME row came back; it cannot distinguish "the code
    chose the server that serves this segment" from "the code returned whichever
    row sorted first". Pass both when the test needs the CORRECT answer to be
    last by creation order, last by id and last by name — which is the only
    arrangement under which the selection terms are load-bearing.
    """
    engine: AsyncEngine = admin_engine_of(env_or_engine)
    row_id = row_id or uuid4()
    async with AsyncSession(engine) as session:
        await session.execute(
            text(
                "INSERT INTO site_server"
                " (id, org_id, device_id, name, segments, roles, site_id,"
                "  enabled, notes, created_at, updated_at)"
                " VALUES (:id, :org, :device, :name, CAST(:segments AS cidr[]),"
                "         CAST(:roles AS text[]), :site, :enabled, '',"
                "         COALESCE(:created_at, now()), COALESCE(:created_at, now()))"
            ),
            {
                "id": row_id,
                "org": org_id,
                "device": device_id,
                "name": name or f"site-{uuid4().hex[:8]}",
                "segments": list(segments),
                "roles": list(roles),
                "site": site_id,
                "enabled": enabled,
                "created_at": created_at,
            },
        )
        await session.commit()
    return row_id


async def site_server_rows(env_or_engine: Any, org_id: UUID) -> list[tuple[UUID, list[str], bool]]:
    """`(device_id, segments, enabled)` per row, ordered — VALUES, not a count.

    A guard test that asserts "an exception was raised" proves nothing about
    what is stored; this is what the tests assert against instead.
    """
    engine: AsyncEngine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT device_id, segments::text[] AS segments, enabled"
                    " FROM site_server WHERE org_id = :org ORDER BY created_at, id"
                ),
                {"org": org_id},
            )
        ).all()
    return [(r.device_id, list(r.segments), r.enabled) for r in rows]

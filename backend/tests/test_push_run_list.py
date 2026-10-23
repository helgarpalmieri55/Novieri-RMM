"""Expansion: a subnet becomes the named list a person confirms
(design §1.2, §1.3; CLAUDE.md 2a(d), 2a(f) as amended 2026-09-30).

Every test seeds a row that a WRONG expansion would list, and asserts it is
absent, or present with its named reason.
"""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job
from app.modules.agent_push import run_list
from app.modules.agent_push.errors import PushPermissionError, PushValidationError
from tests.agent_push_fixtures import (
    seed_asset,
    seed_push_authorization,
    seed_push_credential_scope,
)
from tests.ipam_fixtures import seed_agent
from tests.push_run_fixtures import CONSOLE, RunWorld, run_world


async def _expand(
    app_engine: AsyncEngine, world: RunWorld, *, prefix: str | None = None, actor=None
):
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=actor or world.org.user_id,
            prefix=prefix or world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )


async def test_the_list_is_exactly_the_vouched_unenrolled_machines_of_the_prefix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-set", octet=81, count=2)
    org = world.org.org_id
    # Found by a sweep, never claimed: not listed, but counted.
    await seed_asset(
        admin_engine, org, primary_ip="10.81.0.20", source="discovery", provenance="sweep"
    )
    # Already running the agent: not listed.
    enrolled = await seed_asset(admin_engine, org, primary_ip="10.81.0.21")
    await seed_agent(admin_engine, org, enrolled)
    # Waiting for review: not vouched for, not listed (R5).
    await seed_asset(admin_engine, org, primary_ip="10.81.0.22", review_state="pending")
    # Outside the prefix: not listed, and counted nowhere.
    await seed_asset(admin_engine, org, primary_ip="10.99.0.5")
    await seed_asset(
        admin_engine, org, primary_ip="10.99.0.6", source="discovery", provenance="sweep"
    )
    await seed_asset(admin_engine, org, primary_ip="10.99.0.7", review_state="pending")
    # A rejected tombstone is hidden from every list, this one included.
    await seed_asset(admin_engine, org, primary_ip="10.81.0.24", review_state="rejected")
    # Found by a sweep AND already running the agent: nothing to claim for.
    swept_enrolled = await seed_asset(
        admin_engine, org, primary_ip="10.81.0.25", source="discovery", provenance="sweep"
    )
    await seed_agent(admin_engine, org, swept_enrolled)
    # Vouched for, never probed: LISTED, blocked, with its reason.
    unprobed = await seed_asset(admin_engine, org, primary_ip="10.81.0.23")

    listed = await _expand(app_engine, world)

    by_address = {row.address: row for row in listed.rows}
    assert set(by_address) == {"10.81.0.10", "10.81.0.11", "10.81.0.23"}
    assert [r.address for r in listed.ready_rows] == ["10.81.0.10", "10.81.0.11"]
    assert by_address["10.81.0.23"].device_id == unprobed
    assert by_address["10.81.0.23"].reasons == ("host_key_unconfirmed",)
    # Review item 6: two counts, for two different remedies. The sweep row
    # nobody vouched for (claim it), and the vouched row waiting for review
    # (approve it). The enrolled row needs no push, so it is in neither.
    assert listed.unvouched_count == 1
    assert listed.awaiting_review_count == 1


async def test_each_blocked_reason_is_named(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-why", octet=82, count=1)
    org = world.org.org_id
    # A /23 authorization reaches 10.82.1.0/24; the credential scope and the
    # site server segment cover only 10.82.0.0/24.
    await seed_push_authorization(admin_engine, world.org, "10.82.0.0/23")
    await seed_asset(admin_engine, org, primary_ip="10.82.1.5")
    # A Windows machine with no DNS name, offered an SSH credential.
    await seed_asset(admin_engine, org, primary_ip="10.82.0.30", os_family="windows")

    listed = await _expand(app_engine, world, prefix="10.82.0.0/23")
    by_address = {row.address: row for row in listed.rows}
    assert "credential_not_scoped" in by_address["10.82.1.5"].reasons
    assert "no_site_server" in by_address["10.82.1.5"].reasons
    assert "no_dns_name" in by_address["10.82.0.30"].reasons
    assert "wrong_credential_kind" in by_address["10.82.0.30"].reasons
    assert by_address["10.82.0.10"].ready


async def test_the_digest_is_over_the_sorted_ready_pairs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-dig", octet=83, count=2)
    listed = await _expand(app_engine, world)
    lines = sorted(f"{r.device_id}|{r.address}" for r in listed.ready_rows)
    assert listed.list_digest == hashlib.sha256("\n".join(lines).encode()).hexdigest()
    pairs = [(r.device_id, r.address) for r in listed.ready_rows]
    assert run_list.compute_list_digest(pairs) == run_list.compute_list_digest(reversed(pairs))

    # A ready machine turning blocked changes the digest (R7).
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_host_key WHERE org_id = :o AND address = CAST(:a AS inet)"),
            {"o": world.org.org_id, "a": world.addresses[0]},
        )
        await session.commit()
    assert (await _expand(app_engine, world)).list_digest != listed.list_digest


async def test_only_an_active_authorization_exactly_as_authorized_may_be_expanded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-auth", octet=84, count=1)
    with pytest.raises(PushValidationError, match="not one of this organization"):
        await _expand(app_engine, world, prefix="10.84.0.0/25")
    await seed_push_authorization(admin_engine, world.org, "10.84.4.0/24", revoked=True)
    with pytest.raises(PushValidationError, match="not one of this organization"):
        await _expand(app_engine, world, prefix="10.84.4.0/24")


async def test_more_ready_machines_than_a_run_holds_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch
) -> None:
    world = await run_world(admin_engine, "rl-max", octet=85, count=2)
    monkeypatch.setattr(run_list, "PUSH_RUN_MAX", 1)
    with pytest.raises(PushValidationError, match="narrower"):
        await _expand(app_engine, world)


async def test_a_list_too_long_to_read_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch
) -> None:
    world = await run_world(admin_engine, "rl-long", octet=86, count=2)
    monkeypatch.setattr(run_list, "PUSH_RUN_LIST_MAX", 1)
    with pytest.raises(PushValidationError, match="narrower"):
        await _expand(app_engine, world)


async def test_expansion_requires_credentials_checkout(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.models import User

    world = await run_world(admin_engine, "rl-perm", octet=87, count=1)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stranger = User(
            org_id=world.org.org_id,
            external_subject=f"sub-{uuid4().hex[:8]}",
            email=f"{uuid4().hex[:8]}@test.local",
        )
        session.add(stranger)
        await session.commit()
    with pytest.raises(PushPermissionError, match="credentials.checkout"):
        await _expand(app_engine, world, actor=stranger.id)


async def test_expansion_writes_nothing_and_decrypts_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-ro", octet=88, count=2)
    await _expand(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        for model in (Job, EnrollmentToken, AuditEvent):
            count = (
                await session.execute(
                    select(func.count()).select_from(model).where(model.org_id == world.org.org_id)
                )
            ).scalar_one()
            assert count == 0, model.__name__
    # `expand_prefix` takes no vault: decrypting is not expressible here.
    import inspect

    assert "vault" not in inspect.signature(run_list.expand_prefix).parameters


def test_the_bounds_are_the_approved_numbers() -> None:
    from app.modules.agent_push.preflight import PUSH_BATCH_MAX

    assert run_list.PUSH_RUN_MAX == 1024
    assert PUSH_BATCH_MAX == 25


async def test_a_second_scope_on_the_credential_does_not_widen_the_prefix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A credential scoped wider than the authorization lists nothing outside
    the chosen prefix: the prefix bounds the list, not the scope."""
    world = await run_world(admin_engine, "rl-wide", octet=89, count=1)
    await seed_push_credential_scope(admin_engine, world.org, world.credential_id, "10.89.0.0/16")
    await seed_asset(admin_engine, world.org.org_id, primary_ip="10.89.7.7")
    listed = await _expand(app_engine, world)
    assert [r.address for r in listed.rows] == ["10.89.0.10"]

"""People service: account mirroring, the sign-in resolver, the inventory
hook and the checklists (docs/modules/people/spec.md §2, §3.1, §5)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    Device,
    DevicePerson,
    Organization,
    Person,
    PersonLogin,
    User,
)
from app.modules.inventory.handler import register_job_type
from app.modules.inventory.hooks import (
    clear_post_ingest_hooks,
    post_ingest_hooks,
    register_post_ingest_hook,
)
from app.modules.people import checklists, service
from app.modules.people.errors import PeopleConflictError, PeopleError
from app.modules.people.hooks import inventory_post_ingest


async def _org(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"people-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        actor = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"it@{slug}.example")
        s.add(actor)
        await s.commit()
        return org.id, actor.id


async def _person(app_engine: AsyncEngine, org_id: UUID, name: str, email: str | None) -> UUID:
    async with org_session(org_id, engine=app_engine) as s:
        p = Person(org_id=org_id, display_name=name, email=email, source="operator")
        s.add(p)
        await s.flush()
        return p.id


async def _device(app_engine: AsyncEngine, org_id: UUID, **kw: Any) -> UUID:
    async with org_session(org_id, engine=app_engine) as s:
        d = Device(org_id=org_id, hostname=f"pc-{uuid4().hex[:6]}", os_family="windows", **kw)
        s.add(d)
        await s.flush()
        return d.id


async def _actions(app_engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with org_session(org_id, engine=app_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# Mirroring accounts
# --------------------------------------------------------------------------- #


async def test_sync_gives_every_account_one_person_and_is_idempotent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor = await _org(admin_engine)
    existing = await _person(app_engine, org_id, "Maria", "MARIA@corp.example")
    async with AsyncSession(admin_engine) as s:
        s.add(User(org_id=org_id, external_subject="m", email="maria@corp.example"))
        s.add(User(org_id=org_id, external_subject="b", email="bob@corp.example",
                   display_name="Bob Builder"))
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        linked, created = await service.sync_accounts(s, org_id)
    # Maria's account links to her existing person; Bob and the actor get one.
    assert (linked, created) == (1, 2)
    async with org_session(org_id, engine=app_engine) as s:
        assert await service.sync_accounts(s, org_id) == (0, 0)
        people = (await s.execute(select(Person).where(Person.org_id == org_id))).scalars().all()
    by_email = {(p.email or "").lower(): p for p in people}
    assert by_email["maria@corp.example"].id == existing
    assert by_email["maria@corp.example"].user_id is not None
    assert by_email["bob@corp.example"].display_name == "Bob Builder"
    assert by_email["bob@corp.example"].source == "account"
    assert (await _actions(app_engine, org_id)).count("people.accounts_mirrored") == 1


async def test_sync_is_scoped_to_one_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    org_a, _ = await _org(admin_engine)
    org_b, _ = await _org(admin_engine)
    await service.sync_accounts(bypassrls_session, org_a)
    await bypassrls_session.commit()
    count_b = (
        await bypassrls_session.execute(
            select(func.count()).select_from(Person).where(Person.org_id == org_b)
        )
    ).scalar_one()
    assert count_b == 0


async def test_create_person_refuses_a_second_person_for_one_address(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        await service.create_person(
            s, org_id=org_id, actor_id=actor, display_name="Ann", email="ann@corp.example"
        )
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(PeopleConflictError):
            await service.create_person(
                s, org_id=org_id, actor_id=actor, display_name="Ann 2", email=" ANN@corp.example"
            )


async def test_create_person_links_an_existing_account(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor = await _org(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        email = (await s.execute(select(User.email).where(User.id == actor))).scalar_one()
        person = await service.create_person(
            s, org_id=org_id, actor_id=actor, display_name="IT", email=email.upper()
        )
        assert person.user_id == actor


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #


async def test_resolver_rules_in_order(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, actor = await _org(admin_engine)
    maria = await _person(app_engine, org_id, "Maria Garcia", "maria.garcia@corp.example")
    alice = await _person(app_engine, org_id, "Alice", "alice@corp.example")
    await _person(app_engine, org_id, "Sam One", "sam@corp.example")
    await _person(app_engine, org_id, "Sam Two", "sam@other.example")
    kiosk = await _person(app_engine, org_id, "Kiosk", "kiosk@corp.example")
    async with org_session(org_id, engine=app_engine) as s:
        s.add(PersonLogin(org_id=org_id, person_id=kiosk, login="corp\\frontdesk"))
        await s.flush()
        assert await service.resolve_login(s, org_id, "CORP\\FrontDesk") == kiosk
        assert await service.resolve_login(s, org_id, "Maria.Garcia@corp.example") == maria
        assert await service.resolve_login(s, org_id, "AzureAD\\MariaGarcia") == maria
        assert await service.resolve_login(s, org_id, "CORP\\alice") == alice
        # Two people have the local part "sam": never guessed.
        assert await service.resolve_login(s, org_id, "CORP\\sam") is None
        assert await service.resolve_login(s, org_id, "CORP\\nobody") is None
        assert await service.resolve_login(s, org_id, "  ") is None


# --------------------------------------------------------------------------- #
# The inventory hook
# --------------------------------------------------------------------------- #


@pytest.fixture()
def people_hook_only() -> Iterator[None]:
    original = post_ingest_hooks()
    clear_post_ingest_hooks()
    register_post_ingest_hook(inventory_post_ingest)
    try:
        yield
    finally:
        clear_post_ingest_hooks()
        for hook in original:
            register_post_ingest_hook(hook)


async def _collect(
    app_engine: AsyncEngine, org_id: UUID, device_id: UUID, tmp_path: Path, facts: dict[str, Any]
) -> None:
    register_job_type()
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    doc = {"hostname": "pc-maria", "os_family": "windows", "facts": facts}
    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.complete(session, job, state="succeeded", result=doc)


async def _link(app_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> DevicePerson | None:
    async with org_session(org_id, engine=app_engine) as s:
        return (
            await s.execute(select(DevicePerson).where(DevicePerson.device_id == device_id))
        ).scalar_one_or_none()


async def test_inventory_sets_the_primary_user_and_an_override_survives(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, people_hook_only: None
) -> None:
    org_id, actor = await _org(admin_engine)
    maria = await _person(app_engine, org_id, "Maria Garcia", "maria.garcia@corp.example")
    bob = await _person(app_engine, org_id, "Bob", "bob@corp.example")
    device = await _device(app_engine, org_id)

    await _collect(app_engine, org_id, device, tmp_path, {"console_user": "AzureAD\\MariaGarcia"})
    link = await _link(app_engine, org_id, device)
    assert link is not None and link.person_id == maria and link.source == "inventory"
    assert link.observed_login == "AzureAD\\MariaGarcia"

    # Nobody signed in: nothing changes.
    await _collect(app_engine, org_id, device, tmp_path, {})
    link = await _link(app_engine, org_id, device)
    assert link is not None and link.person_id == maria

    # A stranger signs in: unknown, so unset, and it goes on the review list.
    await _collect(app_engine, org_id, device, tmp_path, {"console_user": "CORP\\temp"})
    link = await _link(app_engine, org_id, device)
    assert link is not None and link.person_id is None

    async with org_session(org_id, engine=app_engine) as s:
        await service.set_device_person(
            s, org_id=org_id, actor_id=actor, device_id=device, person_id=bob
        )
    # The override taught the resolver the sign-in name.
    async with org_session(org_id, engine=app_engine) as s:
        assert await service.resolve_login(s, org_id, "corp\\TEMP") == bob

    await _collect(app_engine, org_id, device, tmp_path, {"console_user": "AzureAD\\MariaGarcia"})
    link = await _link(app_engine, org_id, device)
    assert link is not None and link.person_id == bob and link.source == "operator"

    async with org_session(org_id, engine=app_engine) as s:
        back = await service.clear_device_override(
            s, org_id=org_id, actor_id=actor, device_id=device
        )
        assert back is not None and back.person_id == maria and back.source == "inventory"

    actions = await _actions(app_engine, org_id)
    assert actions.count("people.device_person_detected") == 2
    assert "people.device_person_set" in actions
    assert "people.device_person_automatic" in actions


# --------------------------------------------------------------------------- #
# Checklists
# --------------------------------------------------------------------------- #


async def test_leaver_steps_tick_from_live_data_and_completion_waits_for_them(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        leaver_user = User(org_id=org_id, external_subject="lv", email="lee@corp.example")
        s.add(leaver_user)
        await s.commit()
    async with org_session(org_id, engine=app_engine) as s:
        await service.sync_accounts(s, org_id)
        person = await service.person_for_user(s, org_id, leaver_user.id)
        assert person is not None
        person_id = person.id
    device = await _device(app_engine, org_id)
    async with org_session(org_id, engine=app_engine) as s:
        await service.set_device_person(
            s, org_id=org_id, actor_id=actor, device_id=device, person_id=person_id
        )
        # They revealed a stored password.
        await audit_service.record(
            s, org_id=org_id, actor_type="user", actor_id=leaver_user.id,
            action="credential.checked_out", target_type="credential", target_id="cred-1",
        )

    async with org_session(org_id, engine=app_engine) as s:
        checklist = await checklists.start_checklist(
            s, org_id=org_id, actor_id=actor, person_id=person_id, kind="leaver"
        )
        checklist_id = checklist.id
        assert (await service.load_person(s, org_id, person_id)).status == "leaving"

    async def view() -> dict[str, Any]:
        async with org_session(org_id, engine=app_engine) as s:
            person = await service.load_person(s, org_id, person_id)
            row = await checklists.load_checklist(s, org_id, person_id, checklist_id)
            return await checklists.checklist_view(
                s, org_id, row, await checklists.gather_facts(s, org_id, person)
            )

    steps = {s["key"]: s for s in (await view())["steps"]}
    assert steps["access"]["state"] == "pending"
    assert steps["seats"]["state"] == "done"  # holds none
    assert steps["devices"]["state"] == "pending"
    assert steps["credentials"]["state"] == "pending"
    assert steps["tickets"]["state"] == "done"

    # Marking a step done while the data disagrees needs a reason.
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(PeopleError, match="note"):
            await checklists.update_step(
                s, org_id=org_id, actor_id=actor, person_id=person_id,
                checklist_id=checklist_id, key="access", state="done", note="",
            )
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(PeopleError, match="Not finished"):
            await checklists.close_checklist(
                s, org_id=org_id, actor_id=actor, person_id=person_id,
                checklist_id=checklist_id, outcome="completed",
            )

    # The real work happens in the owning modules; the steps follow.
    async with org_session(org_id, engine=app_engine) as s:
        u = (await s.execute(select(User).where(User.id == leaver_user.id))).scalar_one()
        u.status = "disabled"
        d = (await s.execute(select(Device).where(Device.id == device))).scalar_one()
        d.lifecycle = "stock"
        await audit_service.record(
            s, org_id=org_id, actor_type="user", actor_id=actor,
            action="credential.rotated", target_type="credential", target_id="cred-1",
        )
    steps = {s["key"]: s for s in (await view())["steps"]}
    assert all(step["state"] == "done" for step in steps.values()), steps

    async with org_session(org_id, engine=app_engine) as s:
        await checklists.close_checklist(
            s, org_id=org_id, actor_id=actor, person_id=person_id,
            checklist_id=checklist_id, outcome="completed",
        )
        person = await service.load_person(s, org_id, person_id)
        assert person.status == "left"
        assert person.end_date == datetime.now(UTC).date() or person.end_date is not None

    actions = await _actions(app_engine, org_id)
    assert "people.checklist_started" in actions
    assert "people.checklist_completed" in actions


async def test_one_open_checklist_at_a_time_and_a_skip_needs_a_note(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor = await _org(admin_engine)
    person_id = await _person(app_engine, org_id, "New Starter", "new@corp.example")
    async with org_session(org_id, engine=app_engine) as s:
        checklist = await checklists.start_checklist(
            s, org_id=org_id, actor_id=actor, person_id=person_id, kind="starter"
        )
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(PeopleConflictError):
            await checklists.start_checklist(
                s, org_id=org_id, actor_id=actor, person_id=person_id, kind="leaver"
            )
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(PeopleError):
            await checklists.update_step(
                s, org_id=org_id, actor_id=actor, person_id=person_id,
                checklist_id=checklist.id, key="baseline", state="skipped", note=" ",
            )
        # `baseline` has no automatic check, so done needs no note.
        await checklists.update_step(
            s, org_id=org_id, actor_id=actor, person_id=person_id,
            checklist_id=checklist.id, key="baseline", state="done", note="",
        )
    async with org_session(org_id, engine=app_engine) as s:
        await checklists.close_checklist(
            s, org_id=org_id, actor_id=actor, person_id=person_id,
            checklist_id=checklist.id, outcome="cancelled",
        )
        assert (await service.load_person(s, org_id, person_id)).status == "active"


def test_the_gateway_registers_the_people_hook() -> None:
    """Inventory results complete in the gateway, not the API process."""
    from app.gateway.main import register_module_job_types

    original = post_ingest_hooks()
    try:
        clear_post_ingest_hooks()
        register_module_job_types()
        assert inventory_post_ingest in post_ingest_hooks()
    finally:
        clear_post_ingest_hooks()
        for hook in original:
            register_post_ingest_hook(hook)

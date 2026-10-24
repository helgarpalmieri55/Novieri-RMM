from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (  # noqa: F401 -- PatchApproval/PatchRun verify importability
    Device,
    DevicePatchStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchRun,
    User,
)


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pt", slug=f"pt-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(
            org_id=org.id, hostname="h", os_family="windows", client_id=org.id, site_id=org.id
        )
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_patch_and_status_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        p = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB5034123",
            title="Cumulative Update",
            severity="critical",
        )
        s.add(p)
        await s.flush()
        st = DevicePatchStatus(org_id=org_id, device_id=device_id, patch_id=p.id, state="missing")
        s.add(st)
        await s.flush()
        assert p.priority is None and p.cve is None
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        got = (
            await s.execute(select(DevicePatchStatus).where(DevicePatchStatus.patch_id == p.id))
        ).scalar_one()
        assert got.state == "missing" and got.applied_at is None


async def test_policy_defaults(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        pol = PatchPolicy(org_id=org_id, device_group_id=None)
        s.add(pol)
        await s.flush()
        assert pol.ring_name == "broad" and pol.ring_order == 100
        assert pol.auto_approve_security is False and pol.reboot == "if_required"
        assert pol.reboot_deferral_hours == 24 and pol.scan_interval_hours == 24


async def test_rls_hides_another_orgs_approval_from_an_unfiltered_select(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The database half of invariant #5 for `patch_approvals`, which is the
    table `GET /api/v1/patch/approvals` reads.

    `list_approvals` writes `WHERE org_id = :org` itself, and
    `tests/test_patch_api.py::test_list_approvals_never_shows_another_orgs_approval`
    checks the route's behaviour. Neither of those says what happens if that
    WHERE is ever dropped. This does: the SELECT below carries NO tenant
    filter at all, so a forgotten predicate has to come back empty rather
    than handing a caller another tenant's approval id -- which is exactly
    the value `DELETE /approvals/{id}` takes.
    """
    mine, _, _ = await _org_device_user(admin_engine)
    theirs, _, _ = await _org_device_user(admin_engine)

    ids: dict[str, object] = {}
    for org_id, key in ((mine, "mine"), (theirs, "theirs")):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
            patch = Patch(
                org_id=org_id,
                os_family="windows",
                source="windows_update",
                identifier=f"KB-{key}",
                title="Cumulative Update",
                severity="critical",
            )
            s.add(patch)
            await s.flush()
            approval = PatchApproval(org_id=org_id, patch_id=patch.id, ring_name="broad")
            s.add(approval)
            await s.flush()
            ids[key] = approval.id

    async with org_session(mine, engine=app_engine, clients=ClientScope.all()) as s:
        visible = {row.id for row in (await s.execute(select(PatchApproval))).scalars()}
    assert visible == {ids["mine"]}, "an unfiltered SELECT must still be tenant-scoped"

    # ...and the other org can see its own, so the assertion above is not
    # passing merely because the row was never written.
    async with org_session(theirs, engine=app_engine, clients=ClientScope.all()) as s:
        visible = {row.id for row in (await s.execute(select(PatchApproval))).scalars()}
    assert visible == {ids["theirs"]}

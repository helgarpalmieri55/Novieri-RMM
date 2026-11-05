"""Settings → Security → Sessions (P2 design §2.4, §2.6, §3).

How long a sign-in lasts, for the whole organisation: `auth_org_settings`
(Task 1), read by `app/core/auth/session_policy.py` on every request.
Configuration that serves every client, so reading it needs `orgs.manage` and
changing it needs `orgs.manage` on EVERY client
(`require_permission_on_every_client`, `tests/test_global_writes.py`), as for
Settings → Email. The bounds are the table's CHECKs, repeated here so a value
out of range is a 422 that names the field rather than a database error.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.audit import audit_service
from app.core.auth.session_policy import POLICY_FIELDS, SessionPolicy, policy_of
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models.auth import AuthOrgSettings

router = APIRouter(prefix="/settings", tags=["settings"])


class SessionSettingsOut(BaseModel):
    """The organisation's sign-in lifetimes; the defaults until somebody saves."""

    staff_idle_minutes: int = Field(description="Technicians: minutes without activity.")
    staff_max_hours: int = Field(description="Technicians: longest sign-in, in hours.")
    remember_device_enabled: bool = Field(description="Whether a device may be remembered.")
    remember_device_days: int = Field(description="Days a remembered device skips the factor.")
    remembered_max_hours: int = Field(description="Longest sign-in on a remembered device.")
    contact_idle_minutes: int = Field(description="Portal contacts: minutes without activity.")
    contact_max_days: int = Field(description="Portal contacts: longest sign-in, in days.")
    updated_at: datetime | None = Field(description="When it was last saved.")
    updated_by: UUID | None = Field(description="Who last saved it.")


class SessionSettingsIn(BaseModel):
    """Every value, every time (a PUT): the bounds of `auth_org_settings`."""

    model_config = ConfigDict(extra="forbid")

    staff_idle_minutes: int = Field(ge=5, le=480)
    staff_max_hours: int = Field(ge=1, le=24)
    remember_device_enabled: bool
    remember_device_days: int = Field(ge=1, le=30)
    remembered_max_hours: int = Field(ge=1, le=720)
    contact_idle_minutes: int = Field(ge=5, le=480)
    contact_max_days: int = Field(ge=1, le=30)

    @model_validator(mode="after")
    def _idle_within_the_maximum(self) -> "SessionSettingsIn":
        if (
            self.staff_idle_minutes > self.staff_max_hours * 60
            or self.contact_idle_minutes > self.contact_max_days * 24 * 60
        ):
            raise ValueError(
                tr("the inactivity limit cannot be longer than the longest sign-in")
            )
        return self


def _out(policy: SessionPolicy, row: AuthOrgSettings | None) -> SessionSettingsOut:
    return SessionSettingsOut(
        **{name: getattr(policy, name) for name in POLICY_FIELDS},
        updated_at=None if row is None else row.updated_at,
        updated_by=None if row is None else row.updated_by,
    )


@router.get("/sessions", response_model=SessionSettingsOut)
async def read_session_settings(
    request: Request,
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
) -> SessionSettingsOut:
    """How long a sign-in lasts for this organisation: inactivity and maximum
    for technicians and contacts, and "remember this device"."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(AuthOrgSettings).where(AuthOrgSettings.org_id == ctx.org_id)
            )
        ).scalar_one_or_none()
        return _out(policy_of(row), row)


@router.put("/sessions", response_model=SessionSettingsOut)
async def write_session_settings(
    body: SessionSettingsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("orgs.manage")),
) -> SessionSettingsOut:
    """Save every value. Applies to the next request of every session; the
    audit event records each value that changed, before and after."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(AuthOrgSettings).where(AuthOrgSettings.org_id == ctx.org_id)
            )
        ).scalar_one_or_none()
        before = policy_of(row)
        if row is None:
            row = AuthOrgSettings(org_id=ctx.org_id)
            session.add(row)
        for name in POLICY_FIELDS:
            setattr(row, name, getattr(body, name))
        row.updated_at = datetime.now(UTC)
        row.updated_by = ctx.user_id
        await session.flush()
        changed = {
            name: [getattr(before, name), getattr(body, name)]
            for name in POLICY_FIELDS
            if getattr(before, name) != getattr(body, name)
        }
        if changed:
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="auth.session_settings_updated",
                target_type="organization",
                target_id=str(ctx.org_id),
                metadata={"changed": changed},
            )
        return _out(policy_of(row), row)

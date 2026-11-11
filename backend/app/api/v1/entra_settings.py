"""Settings → Microsoft sign-in (design §4; plan 2026-11-06, Task 5, D34, C4).

`GET` needs `orgs.manage`; `PUT` needs it on EVERY client
(`require_permission_on_every_client`, `tests/test_global_writes.py`), as
Settings → Email and → Sessions. The platform makes the certificate the
application proves itself with; its private key goes to the vault and never
out (`app/core/auth/entra_settings.py`), and the certificate -- public -- is
returned to be uploaded to Entra. A renewal waits beside the certificate in
use until a person switches to it (`activate_certificate`, decision 5), in
the same `PUT`, under the same permission, audit and MCP rules.
`redirect_uri` is what to register in the Microsoft application, from
`ITS_PUBLIC_CONSOLE_URL`.

Two FIRST saves at the same moment both find no row to lock, and the second
INSERT meets the first's primary key: that one conflict is a 409 asking for a
reload, not a 500. Any other integrity error is still an error.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.auth import entra, entra_settings
from app.core.i18n import tr
from app.core.tenancy import org_session

router = APIRouter(prefix="/settings", tags=["settings"])

#: The settings row's primary key (`org_id`): what the second of two first
#: saves meets (`entra_settings.save`).
_ROW_KEY = "auth_entra_settings_pkey"


def _constraint(exc: IntegrityError) -> str | None:
    """The constraint asyncpg names (`app/services/clients.py::_constraint`)."""
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


class EntraSettingsOut(BaseModel):
    """Sign-in with Microsoft for this organization. The private keys are
    never returned; `configured` says whether an application, its key and its
    certificate are stored."""

    enabled: bool = Field(description="Whether people may sign in with Microsoft.")
    app_client_id: UUID | None = Field(description="The Microsoft application (client) ID.")
    staff_tenant_id: UUID | None = Field(
        description="Novieri's tenant: the only one technicians sign in from. Null: none."
    )
    configured: bool = Field(
        description="An application ID and the platform's certificate for it are stored."
    )
    certificate: str | None = Field(
        description="The certificate in use (PEM, public), uploaded to the application."
    )
    certificate_thumbprint: str | None = Field(
        description="Its SHA-256 thumbprint, base64url, as the sign-in sends it."
    )
    certificate_expires_at: datetime | None = Field(
        description="When the certificate in use expires; sign-in with Microsoft stops then."
    )
    next_certificate: str | None = Field(
        description="A new certificate (PEM, public) to upload beside the one in use; sign-in "
        "keeps using the one in use until it is switched to. Null: none waiting."
    )
    next_certificate_thumbprint: str | None = Field(
        description="The waiting certificate's SHA-256 thumbprint, base64url."
    )
    next_certificate_expires_at: datetime | None = Field(
        description="When the waiting certificate expires."
    )
    redirect_uri: str | None = Field(
        description="The redirect URI to register in the Microsoft application; null while "
        "the console's address is not configured."
    )
    updated_at: datetime | None = Field(description="When it was last saved.")
    updated_by: UUID | None = Field(description="Who last saved it.")


class EntraSettingsIn(BaseModel):
    """Every value, every time (a PUT), and at most one of the two acts."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    app_client_id: UUID | None
    staff_tenant_id: UUID | None
    #: Make a new key and certificate: the application's first goes into
    #: use; a later one waits beside the one in use (decision 5).
    new_certificate: bool = False
    #: "I uploaded the waiting certificate to Entra": switch to it, and
    #: destroy the old key.
    activate_certificate: bool = False


def _out(request: Request, config: entra_settings.EntraConfig) -> EntraSettingsOut:
    try:
        redirect = entra.redirect_uri(request.app.state.settings)
    except entra.EntraNotConfigured:
        redirect = None
    return EntraSettingsOut(
        enabled=config.enabled,
        app_client_id=config.app_client_id,
        staff_tenant_id=config.staff_tenant_id,
        configured=config.configured,
        certificate=config.certificate_pem,
        certificate_thumbprint=config.certificate_thumbprint,
        certificate_expires_at=config.certificate_expires_at,
        next_certificate=config.next_certificate_pem,
        next_certificate_thumbprint=config.next_certificate_thumbprint,
        next_certificate_expires_at=config.next_certificate_expires_at,
        redirect_uri=redirect,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


@router.get("/microsoft-sign-in", response_model=EntraSettingsOut)
async def read_entra_settings(
    request: Request, ctx: AuthContext = Depends(require_permission("orgs.manage"))
) -> EntraSettingsOut:
    """How people sign in with Microsoft here: the application, Novieri's
    tenant, the platform's certificate in use and any waiting one, their
    expiry, and whether it is switched on. Never a private key."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return _out(request, await entra_settings.read(session, ctx.org_id))


@router.put("/microsoft-sign-in", response_model=EntraSettingsOut)
async def write_entra_settings(
    body: EntraSettingsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("orgs.manage")),
) -> EntraSettingsOut:
    """Save the application, Novieri's tenant and the switch; with
    `new_certificate`, make a new key and certificate (the key into the
    vault); with `activate_certificate`, switch to the waiting one. Audited,
    the keys never."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            config = await entra_settings.save(
                session,
                request.app.state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                enabled=body.enabled,
                app_client_id=body.app_client_id,
                staff_tenant_id=body.staff_tenant_id,
                new_certificate=body.new_certificate,
                activate_certificate=body.activate_certificate,
            )
    except entra_settings.EntraSettingsInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except IntegrityError as exc:
        if _constraint(exc) != _ROW_KEY:
            raise
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            tr("another save happened at the same time; reload and try again"),
        ) from None
    return _out(request, config)

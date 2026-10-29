"""Credential vault API (docs/kernel/vault.md §6).

The secret is written once, encrypted, and never returned again except through
the audited checkout endpoint. No response model carries the secret,
ciphertext, or key material.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.tenancy import org_session
from app.models import Credential, DeviceGroup
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.agent_push import credential_realm
from app.modules.agent_push.credential_scope import ONE_CLIENT, PUSHABLE_CREDENTIAL_TYPES
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

# A Windows domain push account's realm is read off its user principal name
# when it is saved and recorded unencrypted (review I1): dispatch refuses a
# target outside it before the credential is ever decrypted.
_WINDOWS_DOMAIN = "windows_domain"


def _windows_domain_realm(secret: str) -> str:
    try:
        return credential_realm.realm_of_secret(secret)
    except credential_realm.InvalidWindowsDomainAccount as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

router = APIRouter(prefix="/credentials", tags=["credentials"])

# Types whose secret is decrypted by one service at its point of use and by no
# person: the off-site backup key opens the ransomware-recovery copy, and
# spec-server-backup.md §9 confines it to the replication worker.
_NEVER_CHECKED_OUT = frozenset({"s3_offsite"})


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=50)
    device_group_id: UUID | None = None
    # MSP clients (plan 2026-10-26 Task 12). With a device group the client is
    # the group's, and a different one here is a 422; without one, this. A
    # push credential (`push_install`, `windows_domain`) has exactly one client;
    # any other type with none is every client's, as before.
    client_id: UUID | None = None
    secret: str = Field(min_length=1)


class CredentialUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    secret: str | None = None  # rotate the secret when present


class CheckoutRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class CredentialOut(BaseModel):
    id: UUID
    name: str
    type: str
    device_group_id: UUID | None
    #: The client this credential serves; null is every client's.
    client_id: UUID | None
    created_at: datetime | None
    last_used_at: datetime | None
    has_secret: bool


class CheckoutResponse(BaseModel):
    secret: str


def _out(credential: Credential) -> CredentialOut:
    return CredentialOut(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        device_group_id=credential.device_group_id,
        client_id=credential.client_id,
        created_at=credential.created_at,
        last_used_at=credential.last_used_at,
        has_secret=credential.ciphertext is not None,
    )


async def _get(session: AsyncSession, org_id: UUID, credential_id: UUID) -> Credential:
    # RLS already scopes this to the org; the explicit org_id predicate is
    # belt-and-suspenders on a tenant-critical query (defense in depth).
    credential: Credential | None = (
        await session.execute(
            select(Credential).where(
                Credential.id == credential_id, Credential.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such credential"))
    return credential


async def _client_of_new_credential(
    session: AsyncSession, org_id: UUID, body: CredentialCreate
) -> UUID | None:
    """The client a new credential is filed under (MSP clients spec §1.5):
    its group's when it names one -- a body client that differs is a 422 --
    else the body's, loaded as every new row's client is
    (`client_for_new_rows`, held FOR SHARE until commit: 404 invisible, 409
    archived), and never Unassigned (C10). `None` is every client's."""
    client_id = body.client_id
    if body.device_group_id is not None:
        group_client = (
            await session.execute(
                select(DeviceGroup.client_id).where(
                    DeviceGroup.id == body.device_group_id, DeviceGroup.org_id == org_id
                )
            )
        ).scalar_one_or_none()
        if group_client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device group"))
        if client_id is not None and client_id != group_client:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr("that device group belongs to another client"),
            )
        client_id = group_client
    if client_id is None:
        return None
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr("a credential cannot be filed under Unassigned"),
        )
    return client_id


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CredentialOut)
async def create_credential(
    body: CredentialCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.manage")),
) -> CredentialOut:
    state = request.app.state
    if (
        body.type in PUSHABLE_CREDENTIAL_TYPES
        and body.client_id is None
        and body.device_group_id is None
    ):
        # A push credential has exactly one client (Task 12): every push made
        # with it is made in that client, and its scopes and realm are H.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, tr_marked(ONE_CLIENT))
    realm = _windows_domain_realm(body.secret) if body.type == _WINDOWS_DOMAIN else None
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        client_id = await _client_of_new_credential(session, ctx.org_id, body)
        credential = Credential(
            org_id=ctx.org_id,
            client_id=client_id,
            name=body.name,
            type=body.type,
            device_group_id=body.device_group_id,
            vault_ref="",
            created_by=ctx.user_id,
        )
        session.add(credential)
        await session.flush()
        stored = await state.vault.store(
            session, org_id=ctx.org_id, credential_id=credential.id, plaintext=body.secret
        )
        credential.ciphertext = stored.ciphertext_b64
        credential.wrapped_dek = stored.wrapped_dek
        credential.key_version = stored.key_version
        credential.vault_ref = f"envelope:{credential.id}"
        session.add(credential)
        if realm is not None:
            await credential_realm.record_realm(
                session, org_id=ctx.org_id, credential_id=credential.id, realm=realm
            )
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="credential.created",
            target_type="credential",
            target_id=str(credential.id),
            client_id=client_id,
            metadata={"name": body.name, "type": body.type},
        )
        await session.flush()
        await session.refresh(credential)
        return _out(credential)


@router.get("", response_model=list[CredentialOut])
async def list_credentials(
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.read")),
) -> list[CredentialOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (await session.execute(select(Credential).order_by(Credential.name)))
            .scalars()
            .all()
        )
        return [_out(c) for c in rows]


@router.get("/{credential_id}", response_model=CredentialOut)
async def get_credential(
    credential_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.read")),
) -> CredentialOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return _out(await _get(session, ctx.org_id, credential_id))


@router.post("/{credential_id}/checkout", response_model=CheckoutResponse)
async def checkout_credential(
    credential_id: UUID,
    body: CheckoutRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.checkout")),
) -> CheckoutResponse:
    """Audited decrypt-at-use: returns the plaintext once and records the access.

    Requires credentials.checkout — a distinct privilege from credentials.read
    (metadata), so a read-only auditor cannot exfiltrate plaintext secrets."""
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        credential = await _get(session, ctx.org_id, credential_id)
        client_id = credential.client_id  # read before the update below expires it
        if credential.type in _NEVER_CHECKED_OUT:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr("This credential is used only by the backup service and cannot be revealed."),
            )
        secret = await state.vault.checkout(
            session, org_id=ctx.org_id, credential_id=credential_id
        )
        credential.last_used_at = datetime.now(UTC)
        session.add(credential)
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="credential.checked_out",
            target_type="credential",
            target_id=str(credential_id),
            client_id=client_id,
            metadata={"reason": body.reason, "name": credential.name},
        )
        await session.flush()
    return CheckoutResponse(secret=secret)


@router.put("/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: UUID,
    body: CredentialUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.manage")),
) -> CredentialOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        credential = await _get(session, ctx.org_id, credential_id)
        client_id = credential.client_id  # read before the update below expires it
        credential.name = body.name
        if body.secret is not None:
            if credential.type == _WINDOWS_DOMAIN:
                await credential_realm.record_realm(
                    session,
                    org_id=ctx.org_id,
                    credential_id=credential.id,
                    realm=_windows_domain_realm(body.secret),
                )
            stored = await state.vault.store(
                session, org_id=ctx.org_id, credential_id=credential.id, plaintext=body.secret
            )
            credential.ciphertext = stored.ciphertext_b64
            credential.wrapped_dek = stored.wrapped_dek
            credential.key_version = stored.key_version
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="credential.rotated",
                target_type="credential",
                target_id=str(credential.id),
                client_id=client_id,
                metadata={"name": body.name},
            )
        session.add(credential)
        await session.flush()
        await session.refresh(credential)
        return _out(credential)


@router.delete("/{credential_id}")
async def delete_credential(
    credential_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("credentials.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        credential = await _get(session, ctx.org_id, credential_id)
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="credential.deleted",
            target_type="credential",
            target_id=str(credential_id),
            client_id=credential.client_id,
            metadata={"name": credential.name},
        )
        await session.delete(credential)
        await session.flush()
    return {"deleted": True}

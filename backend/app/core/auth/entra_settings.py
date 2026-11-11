"""Settings → Microsoft sign-in (design §4; plan decisions D33, D34; Task 5,
decision 5; operator decision C4): the organization's one Entra application,
the platform's certificate for it, and Novieri's tenant.

THE PRIVATE KEY IS A VAULT SECRET (invariant 3; B2-35): a `credentials` row of
type `entra_sign_in`, envelope-encrypted under the organization's key, which
OpenBao wraps -- D9's pattern for the account-mail relay. It is written by
`save` when a new certificate is made, read only by `signing_key` at the
moment of a code exchange (audited `auth.entra_credential_used`), and refused
by every route of `app/api/v1/credentials.py`. Nothing returns it. The
CERTIFICATE is public, and kept in clear on the settings row to be shown and
uploaded to Entra.

A NEW CERTIFICATE WAITS BESIDE THE ONE IN USE (Task 1 decision 5; f2-2 I-1).
Entra keeps several certificates for one application ("The keyCredentials
property is multi-valued") and an assertion names its own by `x5t#S256`. So
`new_certificate` fills the next slot while the current key keeps signing,
and a person uploads the new certificate to Entra; `activate_certificate` --
"I uploaded it" -- promotes the next pair and destroys the old key. Only an
application's first certificate goes straight into use: nothing signs yet.

THE SESSION IS THE CALLER'S, and must be able to write a global row: the
settings route holds `orgs.manage` on every client, and the sign-in flows
(Task 9) an every-client identity session. It must be the organization's own
`org_session`: `credential_id` references `credentials.id` with no
organization check (a foreign key ignores RLS), so `signing_key` refuses any
other session and opens a key only by this organization and this type -- a
row naming another organization's credential opens nothing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth import entra
from app.core.auth.entra import CONSUMER_TENANT
from app.core.global_rows import session_scope
from app.core.i18n import tr
from app.core.vault import VaultService
from app.models import Credential
from app.models.auth import AuthEntraSettings

ENTRA_CREDENTIAL_TYPE = "entra_sign_in"
#: The audit action of every use of the private key (invariant 4).
USED_ACTION = "auth.entra_credential_used"


class EntraSettingsInvalid(Exception):
    """A save refused, with a sentence already translated (`tr`)."""


@dataclass(frozen=True)
class EntraConfig:
    enabled: bool = False
    app_client_id: UUID | None = None
    #: Novieri's tenant, the only one a technician signs in from. None means
    #: NO technician signs in with Microsoft (contacts may): never a match,
    #: never "any tenant".
    staff_tenant_id: UUID | None = None
    #: The pair in use: every client assertion is signed with this key.
    credential_id: UUID | None = None
    certificate_pem: str | None = None
    certificate_thumbprint: str | None = None
    certificate_expires_at: datetime | None = None
    #: The pair made and not yet switched to (decision 5).
    next_credential_id: UUID | None = None
    next_certificate_pem: str | None = None
    next_certificate_thumbprint: str | None = None
    next_certificate_expires_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: UUID | None = None

    @property
    def configured(self) -> bool:
        """An application id, a private key and its certificate are stored."""
        return (
            self.app_client_id is not None
            and self.credential_id is not None
            and self.certificate_pem is not None
        )

    @property
    def pending(self) -> bool:
        """A new certificate waits to be uploaded to Entra and switched to."""
        return self.next_certificate_pem is not None

    @property
    def expired(self) -> bool:
        return self.certificate_expires_at is not None and (
            self.certificate_expires_at <= datetime.now(UTC)
        )

    @property
    def usable(self) -> bool:
        """Switched on, complete, and the certificate in use still valid:
        sign-in with Microsoft may start (decision 5)."""
        return self.enabled and self.configured and not self.expired


def _config(row: AuthEntraSettings | None) -> EntraConfig:
    if row is None:
        return EntraConfig()
    return EntraConfig(
        enabled=row.enabled,
        app_client_id=row.app_client_id,
        staff_tenant_id=row.staff_tenant_id,
        credential_id=row.credential_id,
        certificate_pem=row.certificate_pem,
        certificate_thumbprint=row.certificate_thumbprint,
        certificate_expires_at=row.certificate_expires_at,
        next_credential_id=row.next_credential_id,
        next_certificate_pem=row.next_certificate_pem,
        next_certificate_thumbprint=row.next_certificate_thumbprint,
        next_certificate_expires_at=row.next_certificate_expires_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def _row(
    session: AsyncSession, org_id: UUID, *, lock: bool = False
) -> AuthEntraSettings | None:
    query = select(AuthEntraSettings).where(AuthEntraSettings.org_id == org_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def read(session: AsyncSession, org_id: UUID) -> EntraConfig:
    return _config(await _row(session, org_id))


async def _store_key(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    private_pem: str,
    existing: UUID | None,
) -> UUID:
    """Into the vault, replacing `existing` in place (`app/core/mail.py::_store_secret`)."""
    credential: Credential | None = None
    if existing is not None:
        credential = (
            await session.execute(
                select(Credential).where(Credential.org_id == org_id, Credential.id == existing)
            )
        ).scalar_one_or_none()
    if credential is None:
        credential = Credential(
            org_id=org_id,
            name="entra-sign-in",
            type=ENTRA_CREDENTIAL_TYPE,
            vault_ref="",
            created_by=actor_id,
        )
        session.add(credential)
        await session.flush()
    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=private_pem
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


async def _drop_key(
    session: AsyncSession, vault: VaultService, *, org_id: UUID, credential_id: UUID
) -> None:
    """Destroy a key nothing points at any more: the caller flushed that, so
    `ON DELETE SET NULL` finds nothing to clear (`app/core/mail.py::_drop_secret`)."""
    await vault.delete(session, org_id=org_id, credential_id=credential_id)
    await session.execute(
        delete(Credential).where(Credential.org_id == org_id, Credential.id == credential_id)
    )


def _in_use(
    row: AuthEntraSettings,
    credential_id: UUID | None,
    pem: str | None,
    thumbprint: str | None,
    expires_at: datetime | None,
) -> None:
    row.credential_id, row.certificate_pem = credential_id, pem
    row.certificate_thumbprint, row.certificate_expires_at = thumbprint, expires_at


def _waiting(
    row: AuthEntraSettings,
    credential_id: UUID | None,
    pem: str | None,
    thumbprint: str | None,
    expires_at: datetime | None,
) -> None:
    row.next_credential_id, row.next_certificate_pem = credential_id, pem
    row.next_certificate_thumbprint, row.next_certificate_expires_at = thumbprint, expires_at


def _plain(value: object) -> object:
    return str(value) if isinstance(value, UUID) else value


async def save(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    enabled: bool,
    app_client_id: UUID | None,
    staff_tenant_id: UUID | None,
    new_certificate: bool = False,
    activate_certificate: bool = False,
) -> EntraConfig:
    """Every value, every time, and at most one act on the certificates
    (decision 5). `new_certificate` makes a key and a certificate: into use
    if the application has none yet, else into the next slot, the current
    key still signing. `activate_certificate` ("I uploaded it to Entra")
    promotes the next pair and destroys the old key. Clearing the application
    id, switched off, drops both pairs and their keys: they were made for it
    (f2-2 M7).

    Refused (`EntraSettingsInvalid`) before anything is written: a personal-
    account tenant as Novieri's; both acts in one save; on without an
    application id; a certificate asked for without one; a switch with
    nothing waiting, or along with another application id; on with no
    certificate and none asked for; another application id without a new
    certificate (A2-29). A save that changes nothing writes nothing -- no
    row, no audit -- and returns what was there.

    ONE SAVE AT A TIME (N-M2 of the second re-review): the row is read FOR
    UPDATE, so a second save -- another person, or an AI app (B2-20) --
    waits and then reads what the first wrote. Unlocked, an activation and
    a renewal that read the same waiting pair could leave the renewal's key
    under the activated certificate, or fail on the next-slot CHECK. Two
    FIRST saves have no row to lock:
    the second fails on the primary key and writes nothing."""
    if staff_tenant_id == CONSUMER_TENANT:
        raise EntraSettingsInvalid(
            tr("a personal Microsoft account tenant cannot sign anyone in here")
        )
    if new_certificate and activate_certificate:
        raise EntraSettingsInvalid(
            tr(
                "make the new certificate and switch to it in two saves: upload it to Entra "
                "in between"
            )
        )
    row = await _row(session, org_id, lock=True)
    before = _config(row)
    if enabled and app_client_id is None:
        raise EntraSettingsInvalid(
            tr(
                "the Microsoft application (client) ID is needed to switch sign-in with "
                "Microsoft on"
            )
        )
    if new_certificate and app_client_id is None:
        raise EntraSettingsInvalid(
            tr("enter the Microsoft application (client) ID before asking for a certificate")
        )
    if activate_certificate and not before.pending:
        raise EntraSettingsInvalid(tr("there is no new certificate to switch to"))
    if activate_certificate and app_client_id != before.app_client_id:
        raise EntraSettingsInvalid(
            tr("switch to the new certificate without changing the Microsoft application")
        )
    if enabled and before.certificate_pem is None and not new_certificate:
        raise EntraSettingsInvalid(
            tr("ask for a certificate before switching sign-in with Microsoft on")
        )
    if (
        before.certificate_pem is not None
        and app_client_id is not None
        and app_client_id != before.app_client_id
        and not new_certificate
    ):
        raise EntraSettingsInvalid(
            tr("another Microsoft application needs a certificate of its own: ask for a new one")
        )
    changed = {
        name: [_plain(getattr(before, name)), _plain(value)]
        for name, value in (
            ("enabled", enabled),
            ("app_client_id", app_client_id),
            ("staff_tenant_id", staff_tenant_id),
        )
        if getattr(before, name) != value
    }
    if not changed and not new_certificate and not activate_certificate:
        return before
    if row is None:
        row = AuthEntraSettings(org_id=org_id)
        session.add(row)
    retired: list[UUID] = []
    certificate: dict[str, object] | None = None
    if new_certificate:
        assert app_client_id is not None  # refused above without one
        made = entra.new_certificate(app_client_id=app_client_id, now=datetime.now(UTC))
        first = before.certificate_pem is None or app_client_id != before.app_client_id
        if first:
            # This application's first certificate: nothing signs for it
            # yet, so nothing has to keep working. A waiting pair was made
            # for the application it replaces, and goes.
            key = await _store_key(
                session,
                vault,
                org_id=org_id,
                actor_id=actor_id,
                private_pem=made.private_pem,
                existing=row.credential_id,
            )
            _in_use(row, key, made.certificate_pem, made.thumbprint, made.expires_at)
            if row.next_credential_id is not None:
                retired.append(row.next_credential_id)
            _waiting(row, None, None, None, None)
        else:
            key = await _store_key(
                session,
                vault,
                org_id=org_id,
                actor_id=actor_id,
                private_pem=made.private_pem,
                existing=row.next_credential_id,
            )
            _waiting(row, key, made.certificate_pem, made.thumbprint, made.expires_at)
        certificate = {
            "made": "in_use" if first else "waiting",
            "thumbprint": made.thumbprint,
            "expires_at": made.expires_at.isoformat(),
        }
    elif activate_certificate:
        certificate = {
            "activated": row.next_certificate_thumbprint,
            "retired": row.certificate_thumbprint,
        }
        if row.credential_id is not None:
            retired.append(row.credential_id)
        _in_use(
            row,
            row.next_credential_id,
            row.next_certificate_pem,
            row.next_certificate_thumbprint,
            row.next_certificate_expires_at,
        )
        _waiting(row, None, None, None, None)
    elif app_client_id is None and before.certificate_pem is not None:
        certificate = {
            "dropped": [
                t for t in (row.certificate_thumbprint, row.next_certificate_thumbprint) if t
            ]
        }
        retired.extend(c for c in (row.credential_id, row.next_credential_id) if c is not None)
        _in_use(row, None, None, None, None)
        _waiting(row, None, None, None, None)
    row.enabled = enabled
    row.app_client_id = app_client_id
    row.staff_tenant_id = staff_tenant_id
    row.updated_at, row.updated_by = datetime.now(UTC), actor_id
    # One UPDATE: the CHECKs see both slots already moved (they are checked
    # per statement), and no column points at a retired key when it goes.
    await session.flush()
    for credential_id in retired:
        await _drop_key(session, vault, org_id=org_id, credential_id=credential_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="auth.entra_settings_updated",
        target_type="organization",
        target_id=str(org_id),
        metadata={"changed": changed, "certificate": certificate},
    )
    return _config(row)


async def signing_key(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    config: EntraConfig,
    actor_id: UUID | None,
) -> str:
    """The private key in use, for one client assertion, audited in the same
    breath (invariant 4). Never the waiting one: Entra may not have its
    certificate yet (decision 5). `actor_id` is the person when the flow
    already knows them -- the session's at a link, the invitee's at an
    invitation -- and None at a sign-in, where nobody is known yet
    (`system`).

    ONLY UNDER THE ORGANIZATION'S OWN `org_session` (module docstring): any
    other session -- the admin engine's bypasses RLS -- is a RuntimeError
    before anything is read. The row is then found by id, organization AND
    type, so a `credential_id` naming another organization's credential, or
    one of this organization's other secrets, is a LookupError: nothing is
    decrypted and nothing is audited. LookupError is "no readable key"."""
    if session_scope(session) is None:
        raise RuntimeError(  # i18n-exempt: a caller's contract, never a person's answer
            "the sign-in key is read only under the organization's own org_session"
        )
    if config.credential_id is None:
        raise LookupError("no Entra private key is stored")  # i18n-exempt: the caller answers 503
    stored = (
        await session.execute(
            select(Credential.id).where(
                Credential.id == config.credential_id,
                Credential.org_id == org_id,
                Credential.type == ENTRA_CREDENTIAL_TYPE,
            )
        )
    ).scalar_one_or_none()
    if stored is None:
        raise LookupError("no Entra private key is stored")  # i18n-exempt: the caller answers 503
    pem = await vault.checkout(session, org_id=org_id, credential_id=stored)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=USED_ACTION,
        target_type="credential",
        target_id=str(config.credential_id),
        metadata={"purpose": "sign_in", "thumbprint": config.certificate_thumbprint},
    )
    return pem

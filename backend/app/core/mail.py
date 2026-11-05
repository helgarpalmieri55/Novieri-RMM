"""Account email: the platform's own outgoing mail, and the one SMTP client.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §7
("Correo de cuentas"): invitations, password resets and security notices go
out through the platform's own sender, configured in Settings -> Email. Until
the platform had its own sign-in they went out through the identity
provider's realm, which held the relay password itself. Now this platform
sends them, so it holds the password -- in the vault (invariant 3), never in a
column, a log or a response.

THREE THINGS LIVE HERE.

1. `send_smtp_sync` / `authenticate_xoauth2`: the blocking SMTP conversation,
   moved from `app/modules/notification/delivery.py` so notification email and
   account email speak one implementation. Always run through
   `asyncio.to_thread`: this process also serves the agent gateway's
   WebSocket connections on the same event loop. SMTP is deliberately NOT
   egress-guarded (`app/core/egress.py`'s docstring: an on-prem relay on the
   customer's LAN is the normal deployment).
2. The organization's account-mail settings: one `account_mail_settings` row
   (non-secret: relay, sender, sign-in mode) and, when the relay needs a
   sign-in, one `credentials` row of type `account_mail` whose vault secret is
   a small JSON object -- `{"password": ...}` or `{"client_secret": ...}`. The
   credential is every client's (`client_id` NULL): one relay serves the
   whole organization.
3. `send_account_mail`: the one function every account notice goes through.

THE CREDENTIAL (invariants 3 and 4). Checked out at the moment of sending,
held for one SMTP conversation, never assigned to anything that outlives it,
and every checkout is followed at once by an `email.credential_used` audit row
-- `VaultService.checkout` does not audit; its caller must (the rule
`notification/delivery.py` states). Because that row is written in the
CALLER'S session, A CALLER MUST NOT LET `MailSendFailed` ROLL ITS SESSION
BACK: catch it inside the `org_session` block and refuse afterwards, or the
record of the checkout goes with the rollback.
`app/api/v1/realm_settings.py::send_test_message` is the pattern.

MICROSOFT 365 (`auth_type="token"`). The relay signs in with a Microsoft
application's client id and secret, exchanged per message for an app-only
token (`email_intake.tokens.app_access_token`, the client-credentials grant
notification email already uses) and presented with XOAUTH2. Two things are
PINNED whatever the settings say, because the token can send as the mailbox:
the token endpoint must be Microsoft's own (`microsoft_tenant`), and the SMTP
conversation goes to `smtp.office365.com:587` with STARTTLS. The token is held
for one conversation and never stored. The tenant is checked BEFORE the vault
is opened, so a row holding another token URL never moves the secret.

A DEPENDENCY ON A MODULE, NAMED. This kernel module imports
`app.modules.email_intake.microsoft` and `.tokens`: they are the only code
that pins the Microsoft host, and a second copy here would be a second place
to get that wrong. Neither imports this module.
"""

import asyncio
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from enum import StrEnum
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity.email_domain import is_allowed_domain
from app.core.vault import VaultService
from app.models import Credential
from app.models.auth import AccountMailSettings
from app.modules.email_intake import microsoft
from app.modules.email_intake.tokens import TokenRevoked, TokenTransportError, app_access_token

__all__ = [
    "ACCOUNT_MAIL_CREDENTIAL_TYPE",
    "SMTP_TIMEOUT_S",
    "MailFailure",
    "MailNotConfigured",
    "MailRecipientRefused",
    "MailSendFailed",
    "MailSettingsInput",
    "authenticate_xoauth2",
    "failure_message",
    "mail_configured",
    "microsoft_tenant",
    "pick_test_recipient",
    "read_settings",
    "recipient_permitted",
    "save_settings",
    "send_account_mail",
    "send_smtp_sync",
    "split_address",
]

#: Seconds. Bounds one SMTP conversation (it was `delivery.py`'s
#: `_SMTP_TIMEOUT_S`): an unbounded one holds a worker slot forever.
SMTP_TIMEOUT_S = 30.0

#: `credentials.type` of the relay sign-in. `app/api/v1/credentials.py` refuses
#: to check it out: only the sender opens it.
ACCOUNT_MAIL_CREDENTIAL_TYPE = "account_mail"

Encryption = Literal["none", "starttls", "ssl"]
AuthType = Literal["password", "token"]

#: Microsoft's token endpoint, the directory its one variable segment.
_MICROSOFT_TOKEN = re.compile(
    r"https://login\.microsoftonline\.com/([^/?#]+)/oauth2/v2\.0/token"
)


class MailNotConfigured(Exception):
    """No relay is saved for this organization. A supported state, not a fault:
    the invitation flow shows its link once instead of mailing it (design §7)."""


class MailFailure(StrEnum):
    """Why a send failed, from a fixed vocabulary: never the relay's or
    Microsoft's own text, which can echo what it was sent."""

    RELAY_REFUSED = "relay_refused"
    RECIPIENT_REFUSED = "recipient_refused"
    UNREACHABLE = "unreachable"
    CERTIFICATE_UNVERIFIED = "certificate_unverified"
    SIGN_IN_REFUSED = "sign_in_refused"
    SIGN_IN_UNAVAILABLE = "sign_in_unavailable"
    SIGN_IN_MISSING = "sign_in_missing"
    UNSENDABLE = "unsendable"


class MailSendFailed(Exception):
    """The relay, or Microsoft, did not take the message. `reason` only."""

    def __init__(self, reason: MailFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


class MailRecipientRefused(Exception):
    """A test address outside the short list. The message is already translated."""


def failure_message(reason: MailFailure) -> str:
    """What a person reads about a failed send."""
    if reason is MailFailure.RELAY_REFUSED:
        return tr("The mail relay refused the message.")
    if reason is MailFailure.RECIPIENT_REFUSED:
        return tr("The mail relay refused the recipient address.")
    if reason is MailFailure.UNREACHABLE:
        return tr("The mail relay could not be reached.")
    if reason is MailFailure.CERTIFICATE_UNVERIFIED:
        return tr("The mail relay's certificate could not be verified.")
    if reason is MailFailure.SIGN_IN_REFUSED:
        return tr("The mail relay or Microsoft 365 refused the sign-in.")
    if reason is MailFailure.SIGN_IN_UNAVAILABLE:
        return tr("Microsoft 365 sign-in could not be reached.")
    if reason is MailFailure.SIGN_IN_MISSING:
        return tr("The saved sign-in is missing or unreadable; enter it again.")
    return tr(
        "The message could not be built from these settings; check the sender, the display "
        "name and the reply-to address."
    )


# --------------------------------------------------------------------------- #
# the SMTP conversation (moved from notification/delivery.py)
# --------------------------------------------------------------------------- #


def send_smtp_sync(
    *,
    host: str,
    port: int,
    use_tls: bool,
    username: str | None,
    password: str | None,
    from_address: str,
    to_address: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    access_token: str | None = None,
    implicit_tls: bool = False,
    html: str | None = None,
) -> None:
    """Blocking SMTP conversation -- always called through `asyncio.to_thread`
    (see the module docstring: a blocking `smtplib` call inline would stall
    every other connection on the event loop for as long as the round-trip
    takes).

    `use_tls` means STARTTLS on the given port (the common on-prem relay shape,
    e.g. 587). `implicit_tls` means TLS from the first byte (SMTPS, e.g. 465):
    account email offers it as `encryption="ssl"`; notification channels never
    set it, so their messages are what they always were.

    `reply_to` and `html` are OPTIONAL and default to None. Absent, the message
    is plain text with no `Reply-To` -- byte-for-byte what notification email
    sent before account email existed. `reply_to` is email intake's per-ticket
    reply address for a ticket notification, or account email's configured
    reply-to; `html` adds a `text/html` alternative after the plain part.

    `email.policy.default` refuses a CR or LF in a header by raising
    `ValueError` here; callers classify it.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    connection = (
        smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context)
        if implicit_tls
        else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
    )
    with connection as client:
        if use_tls and not implicit_tls:
            client.starttls(context=context)
        if access_token is not None:
            authenticate_xoauth2(client, username or from_address, access_token)
        elif username:
            client.login(username, password or "")
        client.send_message(message)


def authenticate_xoauth2(client: smtplib.SMTP, user: str, access_token: str) -> None:
    """`AUTH XOAUTH2` with the SASL string as the initial response. On failure
    Microsoft sends a 334 JSON challenge and waits for an empty line; answering
    it lets the server finish with 535 instead of holding the connection."""
    client.ehlo_or_helo_if_needed()

    def respond(challenge: bytes | None = None) -> str:
        if challenge is not None:
            return ""
        return f"user={user}\x01auth=Bearer {access_token}\x01\x01"

    client.auth("XOAUTH2", respond, initial_response_ok=True)


# --------------------------------------------------------------------------- #
# where a test message may go (moved from user_admin/realm_config.py)
# --------------------------------------------------------------------------- #


def split_address(address: str) -> tuple[str, str] | None:
    """(local part without any `+tag`, lower-cased domain) of a plausible single
    address; None otherwise.

    Deliberately small: the relay is the authority on whether an address
    exists. This only has to say which mailbox an address names, and refuse
    anything that is not one address.
    """
    address = address.strip()
    if (
        address.count("@") != 1
        or any(ch.isspace() or ch in ',;<>"' for ch in address)
        or len(address) > 254
    ):
        return None
    local, domain = address.split("@")
    domain = domain.lower().rstrip(".")
    base = local.split("+", 1)[0].lower()
    if not base or "." not in domain or domain.startswith("."):
        return None
    return base, domain


def recipient_permitted(
    address: str, sender: str, caller_email: str, org_domain: str | None
) -> bool:
    """The sending address, the caller's own mailbox (a `+tag` is fine), or an
    address in the organization's console sign-in domain. Why the list is this
    short: `realm_config.py`'s module docstring, "THE TEST SEND"."""
    wanted = split_address(address)
    if wanted is None:
        return False
    if address.strip().lower() == sender.strip().lower():
        return True
    if wanted == split_address(caller_email):
        return True
    root = (org_domain or "").strip()
    return bool(root) and is_allowed_domain(address.strip(), root)


def pick_test_recipient(
    requested: str | None, *, sender: str, caller_email: str, org_domain: str | None
) -> str:
    """Where a test message goes: `requested` when `recipient_permitted` allows
    it, the saved sending address when nothing was asked for. Raises
    `MailRecipientRefused` with the reason, already translated."""
    if requested is None or not requested.strip():
        return sender
    address = requested.strip()
    if split_address(address) is None:
        raise MailRecipientRefused(tr("{address} is not an email address", address=address))
    if not recipient_permitted(address, sender, caller_email, org_domain):
        root = (org_domain or "").strip()
        if root:
            raise MailRecipientRefused(
                tr(
                    "a test can only go to the sending address, to your own address "
                    "(a +tag is fine), or any address at {domain}",
                    domain=root,
                )
            )
        raise MailRecipientRefused(
            tr(
                "a test can only go to the sending address, to your own address "
                "(a +tag is fine)"
            )
        )
    return address


def microsoft_tenant(token_url: str) -> str | None:
    """The directory in Microsoft's own token endpoint, or None for any other URL.

    Two checks, both needed: the shape (`_MICROSOFT_TOKEN`, a full match, so no
    other host, scheme, path or query), and the directory itself
    (`microsoft.token_url`, which refuses anything that could add a segment).
    """
    match = _MICROSOFT_TOKEN.fullmatch(token_url)
    if match is None:
        return None
    tenant = match.group(1)
    try:
        pinned = microsoft.token_url(tenant)
    except ValueError:
        return None
    return tenant if pinned == token_url else None


# --------------------------------------------------------------------------- #
# the settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MailSettingsInput:
    """The non-secret half of Settings -> Email, as the route validated it."""

    host: str
    port: int
    from_address: str
    from_display_name: str
    reply_to: str
    encryption: Encryption
    username: str
    auth_type: AuthType
    token_url: str
    token_scope: str
    token_client_id: str


async def read_settings(session: AsyncSession, org_id: UUID) -> AccountMailSettings | None:
    row: AccountMailSettings | None = (
        await session.execute(
            select(AccountMailSettings).where(AccountMailSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    return row


async def mail_configured(session: AsyncSession, org_id: UUID) -> bool:
    row = await read_settings(session, org_id)
    return row is not None and bool(row.host)


async def save_settings(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    values: MailSettingsInput,
    password: str | None,
    client_secret: str | None,
) -> AccountMailSettings:
    """Write the organization's settings, and its relay secret to the vault.

    THE SECRET FOLLOWS THE SIGN-IN, the rules the realm form always had
    (`docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §3):

    * no username: no sign-in, and any stored secret is DROPPED;
    * password mode: `password` replaces the secret; omitted, the stored one
      is kept only if it is a password -- switching from Microsoft drops it;
    * Microsoft mode: `client_secret` replaces it; omitted, kept only if it is
      a client secret -- switching from a password drops it.

    A secret nothing uses is a secret nobody rotates, so "drop" deletes the
    `credentials` row rather than leaving it behind. The session must be scope
    `all`: the credential is a global row (`app.core.global_rows`).
    """
    row = await read_settings(session, org_id)
    if row is None:
        row = AccountMailSettings(org_id=org_id)
        session.add(row)
    saved_mode = row.auth_type if row.credential_id is not None else None

    row.host = values.host.strip().lower()
    row.port = values.port
    row.from_address = values.from_address.strip()
    row.from_display_name = values.from_display_name.strip()
    row.reply_to = values.reply_to.strip()
    row.encryption = values.encryption
    row.username = values.username.strip()
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor_id

    secret: dict[str, str] | None = None
    if row.username and values.auth_type == "token":
        row.auth_type = "token"
        # What sending really uses (`send_account_mail` pins them): store and
        # report the truth, not what was typed.
        row.host = microsoft.SMTP_HOST
        row.port = microsoft.SMTP_PORT
        row.encryption = "starttls"
        row.token_url = values.token_url.strip()
        row.token_scope = values.token_scope.strip()
        row.token_client_id = values.token_client_id.strip()
        if client_secret is not None:
            secret = {"client_secret": client_secret}
        keep = client_secret is None and saved_mode == "token"
    else:
        # A password relay, or no sign-in at all: the Microsoft application is
        # cleared either way.
        row.auth_type = "password"
        row.token_url = ""
        row.token_scope = ""
        row.token_client_id = ""
        if row.username and password is not None:
            secret = {"password": password}
        keep = bool(row.username) and password is None and saved_mode == "password"

    if secret is not None:
        row.credential_id = await _store_secret(
            session,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            secret=secret,
            existing=row.credential_id,
        )
    elif not keep and row.credential_id is not None:
        dropped = row.credential_id
        row.credential_id = None
        await session.flush()
        await _drop_secret(session, vault, org_id=org_id, credential_id=dropped)
    await session.flush()
    return row


async def _store_secret(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: dict[str, str],
    existing: UUID | None,
) -> UUID:
    """The relay secret into the vault, rotating `existing` in place when there
    is one -- the shape of `notification/channels.py::_store_secret`."""
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
            name="account-mail-sign-in",
            type=ACCOUNT_MAIL_CREDENTIAL_TYPE,
            vault_ref="",
            created_by=actor_id,
        )
        session.add(credential)
        await session.flush()
    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=json.dumps(secret)
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


async def _drop_secret(
    session: AsyncSession, vault: VaultService, *, org_id: UUID, credential_id: UUID
) -> None:
    """Destroy a secret nothing points at any more (the caller flushed that)."""
    await vault.delete(session, org_id=org_id, credential_id=credential_id)
    credential = (
        await session.execute(
            select(Credential).where(Credential.org_id == org_id, Credential.id == credential_id)
        )
    ).scalar_one_or_none()
    if credential is not None:
        await session.delete(credential)
        await session.flush()


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #


async def send_account_mail(
    session: AsyncSession,
    *,
    org_id: UUID,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    vault: VaultService,
    actor_id: UUID | None = None,
) -> None:
    """Send one account notice through the organization's saved relay.

    `actor_id` is the person whose act sends it (the audit row's actor), or
    None for the platform itself. Raises `MailNotConfigured` when nothing is
    saved and `MailSendFailed(reason)` when the relay or Microsoft did not take
    it -- AFTER the vault checkout has been audited in `session`; see the
    module docstring for why the caller must catch it inside the session.
    `subject`, `text` and `html` are already in the recipient's language.
    """
    settings = await read_settings(session, org_id)
    if settings is None or not settings.host:
        raise MailNotConfigured()
    signs_in = bool(settings.username)
    tenant: str | None = None
    if signs_in and settings.auth_type == "token":
        tenant = microsoft_tenant(settings.token_url)
        if tenant is None or not settings.token_client_id:
            raise MailSendFailed(MailFailure.SIGN_IN_MISSING)

    creds = await _sign_in(session, vault, org_id=org_id, settings=settings, actor_id=actor_id)
    host, port = settings.host, settings.port
    use_tls = settings.encryption == "starttls"
    implicit_tls = settings.encryption == "ssl"
    password: str | None = None
    access_token: str | None = None
    if tenant is not None:
        access_token = await _microsoft_token(tenant, settings, creds)
        # Pinned, whatever the settings say: the token can send as the mailbox.
        host, port, use_tls, implicit_tls = microsoft.SMTP_HOST, microsoft.SMTP_PORT, True, False
    elif signs_in:
        password = creds.get("password")
        if password is None:
            # A username with no saved password must not sign in with an empty
            # one: refuse before connecting, as the Microsoft branch does.
            raise MailSendFailed(MailFailure.SIGN_IN_MISSING)
    del creds

    sender = (
        formataddr((settings.from_display_name, settings.from_address))
        if settings.from_display_name
        else settings.from_address
    )
    try:
        await asyncio.to_thread(
            send_smtp_sync,
            host=host,
            port=port,
            use_tls=use_tls,
            implicit_tls=implicit_tls,
            username=settings.username or None,
            password=password,
            from_address=sender,
            to_address=to,
            subject=subject,
            body=text,
            html=html,
            reply_to=settings.reply_to or None,
            access_token=access_token,
        )
    except ssl.SSLCertVerificationError:
        # First: it is an OSError AND a ValueError, and would otherwise be
        # reported as "unreachable" or "unsendable" -- a wrong diagnosis for
        # a relay that answered with a certificate this platform will not trust.
        raise MailSendFailed(MailFailure.CERTIFICATE_UNVERIFIED) from None
    except smtplib.SMTPAuthenticationError:
        raise MailSendFailed(MailFailure.SIGN_IN_REFUSED) from None
    except smtplib.SMTPRecipientsRefused:
        raise MailSendFailed(MailFailure.RECIPIENT_REFUSED) from None
    except smtplib.SMTPResponseException:
        raise MailSendFailed(MailFailure.RELAY_REFUSED) from None
    except smtplib.SMTPServerDisconnected:
        raise MailSendFailed(MailFailure.UNREACHABLE) from None
    except smtplib.SMTPException:
        raise MailSendFailed(MailFailure.RELAY_REFUSED) from None
    except OSError:
        # Below smtplib's own exceptions (refused, reset, DNS, TLS, timeout).
        # `SMTPException` derives from OSError, hence the order.
        raise MailSendFailed(MailFailure.UNREACHABLE) from None
    except ValueError:
        raise MailSendFailed(MailFailure.UNSENDABLE) from None


async def _sign_in(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    settings: AccountMailSettings,
    actor_id: UUID | None,
) -> dict[str, str]:
    """The relay secret, checked out and AUDITED in the same breath."""
    if not settings.username or settings.credential_id is None:
        return {}
    credential_id = settings.credential_id
    try:
        secret = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
    except (ValueError, InvalidTag):
        raise MailSendFailed(MailFailure.SIGN_IN_MISSING) from None
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="email.credential_used",
        target_type="credential",
        target_id=str(credential_id),
        metadata={"sign_in": settings.auth_type},
    )
    try:
        decoded = json.loads(secret)
    except ValueError:
        decoded = None
    del secret
    if not isinstance(decoded, dict):
        raise MailSendFailed(MailFailure.SIGN_IN_MISSING)
    return {
        key: value
        for key, value in decoded.items()
        if isinstance(key, str) and isinstance(value, str)
    }


async def _microsoft_token(
    tenant: str, settings: AccountMailSettings, creds: dict[str, str]
) -> str:
    """An app-only token for SMTP.SendAsApp. Never carries provider text:
    `TokenRevoked` and `TokenTransportError` do not."""
    client_secret = creds.get("client_secret")
    if client_secret is None:
        raise MailSendFailed(MailFailure.SIGN_IN_MISSING)
    try:
        return await app_access_token(
            tenant_id=tenant,
            client_id=settings.token_client_id,
            client_secret=client_secret,
            scope=settings.token_scope or microsoft.SMTP_APP_SCOPE,
        )
    except (TokenRevoked, ValueError):
        raise MailSendFailed(MailFailure.SIGN_IN_REFUSED) from None
    except TokenTransportError:
        raise MailSendFailed(MailFailure.SIGN_IN_UNAVAILABLE) from None

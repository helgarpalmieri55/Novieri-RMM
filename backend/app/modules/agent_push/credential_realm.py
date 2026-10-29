"""The realm a Windows domain push credential names (review I1, 2026-09-29).

**WHY THE REALM IS THE CREDENTIAL'S.** The site server proves the account's
password to a KDC (the AS exchange) before it can ask for a ticket for the
target. If the realm -- and so the KDC, found by DNS SRV for the realm -- came
from the TARGET's DNS name, then whoever could write that name (an operator
typing it, or inventory on a managed device) chose which "domain controller"
received the pre-authentication of a Domain-Admin-capable account. So:

* the account must be a **user principal name**, `user@corp.example.com`; its
  realm is the domain, upper-cased. A down-level `CORP\\user` names a NetBIOS
  domain, which is not a Kerberos realm, and is refused at save time. (Chosen
  over an extra "realm" field in the secret: nothing new travels on the wire,
  the agent already reads a UPN's realm from the account, and the console
  already shows the realm a UPN names for the operator to confirm.)
* the realm is recorded, not secret, in `push_credential_realm` when the
  credential is saved -- the one moment its plaintext is in hand -- so dispatch
  can refuse a target outside it BEFORE the credential is decrypted;
* a target is inside the realm when its DNS name ends with `.<realm>`
  (case-insensitive, on a label boundary): `pc1.corp.example.com` is inside
  `CORP.EXAMPLE.COM`; `pc1.notcorp.example.com` and `corp.example.com` itself
  are not.

**THE ROW IS ITS CREDENTIAL'S CLIENT'S** (H, migration 0241; plan 2026-10-26
Task 12): the trigger copies the client, so `record_realm` sends none, and a
`windows_domain` credential with no client is refused at `POST /credentials`
before a realm is ever recorded for it.
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import N_, tr, tr_marked
from app.modules.agent_push.models import PushCredentialRealm
from app.services.dns_name import InvalidDnsName, normalise_dns_name

__all__ = [
    "InvalidWindowsDomainAccount",
    "UPN_REQUIRED",
    "fqdn_in_realm",
    "realm_for",
    "realm_of_account",
    "realm_of_secret",
    "record_realm",
]

#: The sentence an operator sees for a `DOMAIN\\user` (or any account that
#: names no realm). Plain language: no type code, no field name.
UPN_REQUIRED = N_(
    "Enter the account as user@your.domain (for example svc-push@corp.example.com). "
    "The domain after the @ is the Active Directory domain a push signs in to; "
    "DOMAIN\\user does not name one."
)


class InvalidWindowsDomainAccount(ValueError):
    """The saved account cannot be used for a Windows push. Operator-facing."""


def realm_of_account(username: str) -> str:
    """The realm a UPN names, upper-cased, or `InvalidWindowsDomainAccount`."""
    user, sep, domain = username.strip().partition("@")
    if not sep or not user or "\\" in user or "@" in domain:
        raise InvalidWindowsDomainAccount(tr_marked(UPN_REQUIRED))
    try:
        return normalise_dns_name(domain).upper()
    except InvalidDnsName as exc:
        raise InvalidWindowsDomainAccount(
            tr("{upn_required} ({exc})", upn_required=tr_marked(UPN_REQUIRED), exc=exc)
        ) from None


def realm_of_secret(secret: str) -> str:
    """Parse a Windows domain account's stored JSON and return its realm.

    Never echoes the value: it is the credential."""
    try:
        parsed = json.loads(secret)
    except (json.JSONDecodeError, TypeError):
        raise InvalidWindowsDomainAccount(
            tr(
                "A Windows domain account is saved from the console's form (account, "
                "password, optional domain controller and CA certificate)."
            )
        ) from None
    if not isinstance(parsed, dict):
        raise InvalidWindowsDomainAccount(
            tr("A Windows domain account is saved from the console's form.")
        )
    username = parsed.get("username")
    password = parsed.get("secret")
    if not isinstance(username, str) or not username.strip():
        raise InvalidWindowsDomainAccount(tr_marked(UPN_REQUIRED))
    if not isinstance(password, str) or not password:
        raise InvalidWindowsDomainAccount(tr("A Windows domain account needs its password."))
    for key in ("kdc", "ca_bundle"):
        value = parsed.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidWindowsDomainAccount(
                tr("The domain controller and CA certificate, when given, are text.")
            )
    return realm_of_account(username)


def fqdn_in_realm(fqdn: str, realm: str) -> bool:
    """Is `fqdn` a host inside `realm`'s DNS domain (label boundary)?"""
    name = fqdn.strip().rstrip(".").lower()
    domain = realm.strip().rstrip(".").lower()
    return bool(domain) and name.endswith("." + domain)


async def record_realm(
    session: AsyncSession, *, org_id: UUID, credential_id: UUID, realm: str
) -> None:
    """Record (or replace, on rotation) the realm a credential names."""
    row = await session.get(PushCredentialRealm, credential_id)
    if row is None:
        session.add(PushCredentialRealm(org_id=org_id, credential_id=credential_id, realm=realm))
    else:
        row.realm = realm
        row.recorded_at = datetime.now(UTC)
    await session.flush()


async def realm_for(session: AsyncSession, org_id: UUID, credential_id: UUID) -> str | None:
    """The recorded realm, or None for a credential saved before realms were."""
    return (
        await session.execute(
            select(PushCredentialRealm.realm).where(
                PushCredentialRealm.org_id == org_id,
                PushCredentialRealm.credential_id == credential_id,
            )
        )
    ).scalar_one_or_none()

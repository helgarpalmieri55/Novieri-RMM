"""Which client a message belongs to (MSP clients spec §3.3, plan 2026-10-26
Task 4 and operator decision D1).

The email poller runs under scope all (`ALL_SCOPE_ALLOWLIST`), so RLS cannot
tell clients apart here: every rule below is an explicit predicate. A domain is
compared exactly, without case, against the ACTIVE clients of the organisation.
`its_client_domains_unique` (0232) keeps a domain in one client, but only for
equal text, so two clients can still hold one domain written in two cases; that
is ambiguous, and an ambiguous domain matches nothing -- mail is never routed
by guessing which of two customers it belongs to.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client


async def system_client_id(
    session: AsyncSession, org_id: UUID, kind: Literal["internal", "unassigned"]
) -> UUID:
    """The organisation's internal or Unassigned client (0230 creates both).

    The internal one's id IS the organisation's id (plan Global Constraints).
    "Sin asignar" is read from `clients`, and only a session with every client
    sees it, which is the scope the mailbox poller runs under."""
    if kind == "internal":
        return org_id
    return (
        await session.execute(
            select(Client.id).where(Client.org_id == org_id, Client.kind == "unassigned")
        )
    ).scalar_one()


async def client_for_sender(
    session: AsyncSession,
    org_id: UUID,
    *,
    sender_kind: str | None,
    sender_client_id: UUID | None,
    address: str,
) -> UUID:
    """Spec §3.3, in this order:

    1. An active contact: their client. (`resolve_sender` only returns active
       users, and archiving a client switches its contacts off.)
    2. A client whose `email_domains` holds the address's domain.
    3. Unassigned.

    Only the first applies to a contact; a technician (`staff`) belongs to no
    client, so the domain, and then Unassigned, place their mail. A sender who
    is no user at all is placed the same way: the message is quarantined (D1),
    but marked with the client its domain belongs to so the quarantine can be
    read per client."""
    if sender_kind == "contact" and sender_client_id is not None:
        return sender_client_id
    _, at, domain = address.strip().lower().rpartition("@")
    if at and domain:
        domains = func.unnest(Client.email_domains).column_valued("held")
        matches = (
            (
                await session.execute(
                    select(Client.id)
                    .where(
                        Client.org_id == org_id,
                        Client.status == "active",
                        exists(select(literal(1)).where(func.lower(domains) == domain)),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) == 1:
            return matches[0]
    return await system_client_id(session, org_id, "unassigned")
